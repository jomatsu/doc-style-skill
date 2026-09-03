"""ブロック分類と散文(prose)抽出の共有契約。

corpus_intake(クリーニング)・extract_features(特徴抽出)・style_lint(生成文評価)
・overlap_check(G6 正規化)はすべてこのモジュールを通る。同じ入力文字列からは
同じブロック・同じ散文セグメントが得られる(契約の一本化)。

契約:

1. `parse_frontmatter` + `classify_blocks` で raw テキストを
   body / quote / code / boilerplate / editorial ブロックへ分類する
   (frontmatter・見出しは boilerplate、フェンス/4 スペースコードは code)。
   オフセットは raw 基準。不正ネストフェンスの回復規則は corpus_intake 由来。
2. `prose_segments` が body ブロックから散文セグメントを作る。
   - Markdown テーブル行・単独行 URL・画像のみ行・HTML タグのみ行・
     Zenn の `:::` コンテナ行は除外
   - 箇条書きは**マーカーだけ落として本文を残す**(リスト散文は保持)
   - インラインコードと文中 URL は決定的に名詞プレースホルダ
     (`PLACEHOLDER`)へ置換し、識別子の中身が形態素統計に入らないようにする
   - リンク `[text](url)` は text のみ、強調記号(`**`/`__`/`~~`)は剥がす
   - 各セグメントは raw オフセットへの対応表(`raw_map`)と、置換区間
     (`masked`)を持つ。`to_raw_offset` で散文オフセット→raw オフセットを引ける
3. `prose_text` は G6(コピー検査)用の正規化散文(インラインコードは削除)。
"""

from __future__ import annotations

import re

PLACEHOLDER = "識別子"  # Sudachi: 名詞,普通名詞,一般。識別子の中身を統計に入れない

_META_KEYS = {"published_at", "strata", "canonical_url", "license", "authorship"}

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_INDENTED_LIST_RE = re.compile(r"^\s{4,}(?:[-*+] |\d+[.)] )")

# 散文抽出用パターン
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_STANDALONE_URL_RE = re.compile(r"^\s*<?(https?://\S+)>?\s*$")
_IMAGE_ONLY_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_HTML_ONLY_RE = re.compile(r"^\s*<[^>]+>\s*$")
_CONTAINER_RE = re.compile(r"^\s*:::.*$")
_FOOTNOTE_DEF_RE = re.compile(r"^\s*\[\^[^\]]+\]:\s*")
_LIST_MARKER_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")

# インライン置換(順序が重要: コード → 画像 → リンク → URL → 強調 → タグ)
_INLINE_RE = re.compile(
    r"(?P<code>`+)(?P<code_body>.+?)(?P=code)"
    r"|(?P<image>!\[[^\]]*\]\([^)]*\))"
    r"|\[(?P<link_text>[^\]]*)\]\((?P<link_url>[^)]*)\)"
    r"|(?P<url><?https?://[^\s>」』)】]+>?)"
    r"|(?P<emph>\*\*|__|~~)"
    r"|(?P<tag></?[A-Za-z][^<>\n]*>)"
    r"|(?P<footref>\[\^[^\]]+\])"
)


# ---------------- frontmatter / ブロック分類 ----------------

def parse_frontmatter(text: str) -> tuple[dict, int]:
    """先頭の `---` 区切り frontmatter を解析。(メタデータ, 本文開始オフセット)。"""
    meta: dict = {}
    if not text.startswith("---\n"):
        return meta, 0
    end = text.find("\n---", 4)
    if end < 0:
        return meta, 0
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in _META_KEYS:
            meta[key] = value.strip() or None
    body_start = text.find("\n", end + 1)
    return meta, (body_start + 1 if body_start >= 0 else len(text))


def is_indented_code_line(line: str) -> bool:
    """4スペース/tabコード。ただしネストされたMarkdown箇条書きは除く。"""
    return line.startswith(("    ", "\t")) and not _INDENTED_LIST_RE.match(line)


def classify_blocks(text: str, body_start: int) -> list[dict]:
    """行走査でブロック分類。オフセットは raw テキスト基準。"""
    blocks: list[dict] = []
    if body_start > 0:
        blocks.append(
            {
                "type": "boilerplate",
                "text": text[:body_start].rstrip("\n"),
                "char_start": 0,
                "char_end": body_start,
            }
        )

    pos = body_start
    in_code = False
    fence_char: str | None = None
    fence_len = 0
    nested_fence_depth = 0
    cur_start: int | None = None
    cur_lines: list[str] = []
    cur_type: str | None = None

    def flush(end: int) -> None:
        nonlocal cur_start, cur_lines, cur_type
        if cur_start is not None and cur_lines:
            joined = "\n".join(cur_lines)
            if joined.strip():
                blocks.append(
                    {
                        "type": cur_type,
                        "text": joined,
                        "char_start": cur_start,
                        "char_end": end,
                    }
                )
        cur_start, cur_lines, cur_type = None, [], None

    source_lines = text[body_start:].splitlines(keepends=True)
    for line_idx, line in enumerate(source_lines):
        line_start = pos
        pos += len(line)
        stripped = line.rstrip("\n")
        s = stripped.strip()

        fence = _FENCE_RE.match(stripped)
        if in_code:
            if fence:
                delimiter, rest = fence.groups()
                same_fence = delimiter[0] == fence_char and len(delimiter) >= fence_len
                if same_fence and rest.strip():
                    # html/text変換で外側と同じ長さの ```json が入る不正ネストを
                    # 回復し、内側closeで外側を閉じない
                    nested_fence_depth += 1
                elif same_fence and not rest.strip():
                    has_following_close = False
                    if nested_fence_depth > 0:
                        # 次の非空行もcloseなら、現在は内側close。そうでなければ
                        # info付きフェンスは文字列扱いとし、現在で外側を閉じる
                        for future in source_lines[line_idx + 1 :]:
                            fs = future.rstrip("\n")
                            if not fs.strip():
                                continue
                            fm = _FENCE_RE.match(fs)
                            if fm:
                                fd, fr = fm.groups()
                                has_following_close = (
                                    fd[0] == fence_char
                                    and len(fd) >= fence_len
                                    and not fr.strip()
                                )
                            break
                    if nested_fence_depth > 0 and has_following_close:
                        nested_fence_depth -= 1
                    else:
                        cur_lines.append(stripped)
                        flush(pos)
                        in_code = False
                        fence_char, fence_len, nested_fence_depth = None, 0, 0
                        continue
            cur_lines.append(stripped)
            continue
        if fence:
            delimiter, info = fence.groups()
            # backtick fence の info string に backtick は置けない。変換で生じた
            # ````lang` のような不正表記を開きフェンスと誤認しない
            if delimiter[0] != "`" or "`" not in info:
                flush(line_start)
                in_code = True
                fence_char, fence_len, nested_fence_depth = delimiter[0], len(delimiter), 0
                cur_type = "code"
                cur_start = line_start
                cur_lines = [stripped]
                continue

        if not s:
            flush(line_start)
            continue

        if s.startswith("#"):
            flush(line_start)
            blocks.append(
                {
                    "type": "boilerplate",
                    "text": stripped,
                    "char_start": line_start,
                    "char_end": line_start + len(stripped),
                }
            )
            continue

        line_type = "body"
        if is_indented_code_line(stripped):
            # Markdown の indented code。ネスト箇条書きは body のまま保持する
            line_type = "code"
        elif s.startswith(">"):
            line_type = "quote"
        elif s.startswith(("(編集部", "(編集部", "【編集部")):
            line_type = "editorial"

        if cur_type != line_type:
            flush(line_start)
            cur_type = line_type
            cur_start = line_start
        cur_lines.append(stripped)

    flush(pos)
    return blocks


def classify_text(text: str) -> list[dict]:
    """frontmatter 解析 + ブロック分類の一括版(style_lint / G6 用)。"""
    _, body_start = parse_frontmatter(text)
    return classify_blocks(text, body_start)


# ---------------- ブロック健全性 ----------------

_CODELIKE_LINE_RE = re.compile(
    r"^\s*(?:[$>]\s+|(?:const|let|var|function|class|def|fn|pub|use|import|"
    r"package|SELECT|INSERT|UPDATE)\b|[{}\[\]();]{2,}|[\w.]+\s*[:=]\s*[^。]+[;,]?)"
)


def check_block_health(blocks: list[dict]) -> dict:
    """クリーニング後の body 健全性を保守的に検査する。"""
    body_blocks = [b for b in blocks if b["type"] == "body"]
    body = "\n".join(b["text"] for b in body_blocks)
    lines = [line for line in body.splitlines() if line.strip()]
    nonspace = [ch for ch in body if not ch.isspace()]
    latin = sum(ch.isascii() and ch.isalpha() for ch in nonspace)
    latin_ratio = latin / len(nonspace) if nonspace else 0.0
    code_like = sum(bool(_CODELIKE_LINE_RE.match(line)) for line in lines)
    code_like_ratio = code_like / len(lines) if lines else 0.0

    errors = []
    warnings = []
    if not nonspace:
        errors.append("body が空")
    residual_fence = False
    for line in lines:
        fence = _FENCE_RE.match(line)
        if fence:
            delimiter, info = fence.groups()
            if delimiter[0] != "`" or "`" not in info:
                residual_fence = True
                break
    if residual_fence:
        errors.append("body にコードフェンスが残存")
    if any(is_indented_code_line(line) for line in lines):
        errors.append("body にインデントコードが残存")
    if len(lines) >= 10 and code_like_ratio >= 0.35:
        errors.append("body のコードらしい行が35%以上")
    if len(nonspace) >= 500 and latin_ratio >= 0.45:
        warnings.append("body のラテン文字比率が45%以上(変換結果を要確認)")

    return {
        "status": "fail" if errors else ("warn" if warnings else "pass"),
        "metrics": {
            "body_chars": len(nonspace),
            "body_lines": len(lines),
            "code_like_lines": code_like,
            "code_like_line_ratio": round(code_like_ratio, 4),
            "latin_ratio": round(latin_ratio, 4),
        },
        "errors": errors,
        "warnings": warnings,
    }


# ---------------- 散文セグメント ----------------

def _clean_inline(line: str, raw_base: int, out_base: int, mode: str):
    """1 行のインライン置換。(出力文字列, raw_map 断片, masked 断片, 置換数)。

    raw_map: [[out_off, raw_off, length], ...](コピーされた連続区間)
    masked:  [[out_start, out_end], ...](プレースホルダ区間、out 基準)
    mode: "placeholder"(形態素統計用)| "drop"(G6 正規化用)
    """
    out: list[str] = []
    raw_map: list[list[int]] = []
    masked: list[list[int]] = []
    n_masked = 0
    out_len = 0
    last = 0

    def copy(seg_start: int, seg_end: int) -> None:
        nonlocal out_len
        if seg_end <= seg_start:
            return
        raw_map.append([out_base + out_len, raw_base + seg_start, seg_end - seg_start])
        out.append(line[seg_start:seg_end])
        out_len += seg_end - seg_start

    def insert(token: str, raw_at: int) -> None:
        nonlocal out_len, n_masked
        if not token:
            return
        masked.append([out_base + out_len, out_base + out_len + len(token)])
        raw_map.append([out_base + out_len, raw_base + raw_at, 0])
        out.append(token)
        out_len += len(token)
        n_masked += 1

    for m in _INLINE_RE.finditer(line):
        copy(last, m.start())
        kind = m.lastgroup
        if kind in ("code", "code_body"):
            if mode == "placeholder":
                insert(PLACEHOLDER, m.start())
        elif kind == "image":
            pass
        elif kind in ("link_text", "link_url"):
            text = m.group("link_text")
            # リンクテキストは通常散文なので保持(URL は落とす)
            sub_start = m.start("link_text")
            raw_map.append([out_base + out_len, raw_base + sub_start, len(text)])
            out.append(text)
            out_len += len(text)
        elif kind == "url":
            if mode == "placeholder":
                insert(PLACEHOLDER, m.start())
        elif kind in ("emph", "tag", "footref"):
            pass
        last = m.end()
    copy(last, len(line))
    return "".join(out), raw_map, masked, n_masked


def prose_segments(blocks: list[dict], *, mode: str = "placeholder") -> list[dict]:
    """body ブロック → 散文セグメント列。

    セグメント: {"text", "char_start", "char_end", "block_index", "kind",
                 "raw_map", "masked", "n_masked"}
    kind: "paragraph" | "list"(箇条書きのみで構成されるブロック)
    char_start/char_end は raw 基準(ブロックの範囲)。
    """
    segments: list[dict] = []
    for bi, block in enumerate(blocks):
        if block["type"] != "body":
            continue
        lines = block["text"].split("\n")
        raw_pos = block["char_start"]
        out_parts: list[str] = []
        raw_map: list[list[int]] = []
        masked: list[list[int]] = []
        n_masked = 0
        n_list = 0
        n_kept = 0
        out_len = 0
        for line in lines:
            line_raw_start = raw_pos
            raw_pos += len(line) + 1
            if not line.strip():
                continue
            if (
                _TABLE_SEP_RE.match(line)
                or _TABLE_ROW_RE.match(line)
                or _STANDALONE_URL_RE.match(line)
                or _IMAGE_ONLY_RE.match(line)
                or _HTML_ONLY_RE.match(line)
                or _CONTAINER_RE.match(line)
            ):
                continue
            body_line = line
            offset = 0
            lm = _LIST_MARKER_RE.match(line)
            if lm:
                n_list += 1
                offset = lm.end()
                body_line = line[offset:]
            else:
                fm = _FOOTNOTE_DEF_RE.match(line)
                if fm:
                    offset = fm.end()
                    body_line = line[offset:]
            if not body_line.strip():
                continue
            if n_kept > 0:
                raw_map.append([out_len, line_raw_start - 1, 1])
                out_parts.append("\n")
                out_len += 1
            text, rmap, msk, nm = _clean_inline(
                body_line, line_raw_start + offset, out_len, mode
            )
            out_parts.append(text)
            raw_map.extend(rmap)
            masked.extend(msk)
            n_masked += nm
            out_len += len(text)
            n_kept += 1
        text = "".join(out_parts)
        if not text.strip():
            continue
        segments.append(
            {
                "text": text,
                "char_start": block["char_start"],
                "char_end": block["char_end"],
                "block_index": bi,
                "kind": "list" if n_list and n_list == n_kept else "paragraph",
                "raw_map": raw_map,
                "masked": masked,
                "n_masked": n_masked,
            }
        )
    return segments


def to_raw_offset(segment: dict, prose_offset: int) -> int:
    """散文オフセット(セグメント内)→ raw オフセット。置換区間は置換元の先頭へ。"""
    raw_map = segment["raw_map"]
    if not raw_map:
        return segment["char_start"]
    best = raw_map[0]
    for entry in raw_map:
        if entry[0] <= prose_offset:
            best = entry
        else:
            break
    out_off, raw_off, length = best
    delta = prose_offset - out_off
    if length > 0:
        delta = min(delta, length)
    else:
        delta = 0
    return raw_off + delta


def raw_span(segment: dict, start: int, end: int) -> list[int]:
    """散文区間 [start, end) → raw 区間 [s, e)。"""
    s = to_raw_offset(segment, start)
    e = to_raw_offset(segment, max(end - 1, start))
    # end は排他的: 最後の文字の raw 位置 + 1(置換区間なら置換元の先頭 + 1)
    return [s, max(e + 1, s)]


def prose_document(text: str, *, mode: str = "drop") -> dict:
    """raw テキスト → 正規化散文ドキュメント(G6 用)。

    返り値: {"text": セグメントを空行で連結した散文, "segments": [...],
             "offsets": [各セグメントの連結文字列内開始位置]}
    """
    blocks = classify_text(text)
    segments = prose_segments(blocks, mode=mode)
    parts: list[str] = []
    offsets: list[int] = []
    pos = 0
    for seg in segments:
        if parts:
            pos += 2
        offsets.append(pos)
        parts.append(seg["text"])
        pos += len(seg["text"])
    return {"text": "\n\n".join(parts), "segments": segments, "offsets": offsets}


def document_raw_span(doc: dict, start: int, end: int) -> list[int]:
    """prose_document の連結文字列区間 → raw 区間(先頭セグメント基準)。"""
    segments, offsets = doc["segments"], doc["offsets"]
    if not segments:
        return [start, end]
    idx = 0
    for i, off in enumerate(offsets):
        if off <= start:
            idx = i
    seg = segments[idx]
    local_start = max(start - offsets[idx], 0)
    local_end = min(max(end - offsets[idx], local_start + 1), len(seg["text"]))
    if local_end <= local_start:
        local_end = local_start + 1
    return raw_span(seg, local_start, local_end)


def prose_text(text: str, *, mode: str = "drop") -> str:
    """raw テキスト → 正規化散文(G6 用)。セグメントを空行で連結。"""
    return prose_document(text, mode=mode)["text"]


def prose_paragraphs(text: str, *, mode: str = "drop") -> list[dict]:
    """raw テキスト → 段落単位の正規化散文(G6 局所重複用)。"""
    blocks = classify_text(text)
    return prose_segments(blocks, mode=mode)
