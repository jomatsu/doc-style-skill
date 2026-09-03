#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""overlap_check — 生成テキストとソースコーパスの重複検査。

- 比較は両者を lib/blocks の散文契約で正規化してから行う(コード・表・見出し・
  URL・インラインコードを除いた散文)。exact 一致の span は raw 座標へ写す
- exact: 25 字以上の連続一致(位置つき)。ストップリスト(既定: URL)を除いた
  実効文字数(effective_length)が min_chars 未満の一致は偽陽性として除外
- 文字 5-gram Jaccard / MinHash(全体)はストップリスト除去後のテキストで算出
- paragraph: 段落単位の局所重複。生成文の各段落の 5-gram がソース記事 1 本に
  含まれる割合(containment)の最大値と、近似重複段落(>= 0.8)の数を報告
- MinHash(128 permutation、seed 固定で決定的)
- 埋め込み類似は --embeddings 指定時のみ。未指定なら {"status": "skipped"}
- 全コーパスのハッシュ copy-index は未実装(deferred)。記事ごとの逐次比較で代替

コーパス本文はエラー・レポートに 50 字を超えて出力しない。

終了コード: 0=成功 / 1=エラー
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import blocks as blocks_lib
from lib import features as feat

EXACT_MIN_CHARS = 25
NGRAM_N = 5
PARAGRAPH_NEAR_DUP = 0.8  # corpus_intake.NEAR_DUP_JACCARD と同じ工学的既定値
PARAGRAPH_MIN_CHARS = 30
# G6 偽陽性除外の既定ストップリスト(lint-config の stoplist_patterns で上書き可)
DEFAULT_STOPLIST_PATTERNS = [r"https?://\S+"]
NUM_PERM = 128
_MERSENNE_P = (1 << 61) - 1
_EXCERPT_MAX = 50


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", type=Path, default=None, help="(任意・未使用)")
    p.add_argument("--text", required=True, type=Path)
    p.add_argument("--against", required=True, type=Path, help="ソースコーパスのディレクトリ")
    p.add_argument("--min-chars", type=int, default=EXACT_MIN_CHARS)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--stoplist-pattern",
        action="append",
        default=None,
        help="偽陽性除外の正規表現(繰り返し可。既定: URL)",
    )
    p.add_argument(
        "--embeddings",
        default=None,
        help="埋め込み類似の設定(未指定なら skipped)",
    )
    return p.parse_args(argv)


# ---------------- ストップリスト ----------------

def _stoplist_re(patterns: list[str] | None) -> re.Pattern | None:
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{p})" for p in patterns))


def strip_stoplist(text: str, patterns: list[str] | None) -> str:
    rx = _stoplist_re(patterns)
    return rx.sub(" ", text) if rx else text


def effective_length(text: str, patterns: list[str] | None) -> int:
    """ストップリスト一致部分と空白を除いた実効文字数。"""
    return len(re.sub(r"\s+", "", strip_stoplist(text, patterns)))


# ---------------- exact 連続一致 ----------------

def find_exact_matches(text: str, source: str, min_len: int) -> list[dict]:
    """text と source の min_len 字以上の連続一致(貪欲・非重複)。"""
    matches: list[dict] = []
    if len(text) < min_len or len(source) < min_len:
        return matches
    index: dict[str, list[int]] = {}
    for j in range(len(source) - min_len + 1):
        index.setdefault(source[j : j + min_len], []).append(j)
    i = 0
    while i <= len(text) - min_len:
        seed_gram = text[i : i + min_len]
        best: tuple[int, int] | None = None
        for j in index.get(seed_gram, []):
            length = min_len
            while (
                i + length < len(text)
                and j + length < len(source)
                and text[i + length] == source[j + length]
            ):
                length += 1
            if best is None or length > best[1]:
                best = (j, length)
        if best is not None:
            j, length = best
            excerpt = text[i : i + length]
            if len(excerpt) > _EXCERPT_MAX:
                excerpt = excerpt[:_EXCERPT_MAX] + "…"
            matches.append(
                {
                    "text_span": [i, i + length],
                    "source_span": [j, j + length],
                    "length": length,
                    "excerpt": excerpt,
                }
            )
            i += length
        else:
            i += 1
    return matches


# ---------------- MinHash ----------------

def _hash64(gram: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big"
    )


def minhash_params(num_perm: int = NUM_PERM, seed: int = 42) -> list[tuple[int, int]]:
    rng = random.Random(seed)
    return [
        (rng.randrange(1, _MERSENNE_P), rng.randrange(0, _MERSENNE_P))
        for _ in range(num_perm)
    ]


def minhash_signature(
    ngrams: set, params: list[tuple[int, int]]
) -> list[int] | None:
    if not ngrams:
        return None
    hashes = [_hash64(g) for g in sorted(ngrams)]
    return [min((a * h + b) % _MERSENNE_P for h in hashes) for a, b in params]


def minhash_similarity(sig_a: list[int] | None, sig_b: list[int] | None) -> float:
    if not sig_a or not sig_b:
        return 0.0
    return sum(1 for x, y in zip(sig_a, sig_b) if x == y) / len(sig_a)


# ---------------- corpus 比較 ----------------

def compare_against_corpus(
    text: str,
    corpus_files: list[Path],
    *,
    min_chars: int,
    seed: int,
    stoplist_patterns: list[str] | None = None,
    paragraph_min_chars: int = PARAGRAPH_MIN_CHARS,
) -> dict:
    """style_lint G6 と共有する比較コア(散文正規化つき)。

    stoplist_patterns: 偽陽性除外パターン(None なら DEFAULT_STOPLIST_PATTERNS)。
    exact 一致は正規化散文同士で探し、実効文字数 < min_chars で除外。span は
    raw 座標(text_span)と正規化座標(prose_span)の両方を返す。
    n-gram/MinHash は除去後テキストで算出。paragraph は段落ごとの containment。
    """
    if stoplist_patterns is None:
        stoplist_patterns = DEFAULT_STOPLIST_PATTERNS
    params = minhash_params(NUM_PERM, seed)
    doc = blocks_lib.prose_document(text, mode="drop")
    prose = doc["text"]
    text_grams = feat.char_ngrams(strip_stoplist(prose, stoplist_patterns), NGRAM_N)
    text_sig = minhash_signature(text_grams, params)
    paragraphs = [
        (i, feat.char_ngrams(strip_stoplist(seg["text"], stoplist_patterns), NGRAM_N), seg)
        for i, seg in enumerate(doc["segments"])
        if len(re.sub(r"\s+", "", seg["text"])) >= paragraph_min_chars
    ]

    exact: list[dict] = []
    jaccard_per: dict[str, float] = {}
    minhash_per: dict[str, float] = {}
    para_max = [0.0] * len(paragraphs)
    para_src = [None] * len(paragraphs)
    for path in corpus_files:
        source_raw = path.read_text(encoding="utf-8")
        source = blocks_lib.prose_text(source_raw, mode="drop")
        for m in find_exact_matches(prose, source, min_chars):
            matched = prose[m["text_span"][0] : m["text_span"][1]]
            eff = effective_length(matched, stoplist_patterns)
            if eff < min_chars:
                continue  # URL 等ストップリスト主体の一致は偽陽性
            m["effective_length"] = eff
            m["source"] = path.name
            m["prose_span"] = m["text_span"]
            m["text_span"] = blocks_lib.document_raw_span(doc, *m["prose_span"])
            exact.append(m)
        grams = feat.char_ngrams(strip_stoplist(source, stoplist_patterns), NGRAM_N)
        jaccard_per[path.name] = round(feat.jaccard(text_grams, grams), 4)
        minhash_per[path.name] = round(
            minhash_similarity(text_sig, minhash_signature(grams, params)), 4
        )
        for k, (_, pg, _) in enumerate(paragraphs):
            c = feat.containment(pg, grams)
            if c > para_max[k]:
                para_max[k] = c
                para_src[k] = path.name
    para_report = [
        {
            "paragraph": i,
            "span": [seg["char_start"], seg["char_end"]],
            "containment_max": round(para_max[k], 4),
            "source": para_src[k],
        }
        for k, (i, _, seg) in enumerate(paragraphs)
    ]
    near_dup = [p for p in para_report if p["containment_max"] >= PARAGRAPH_NEAR_DUP]
    return {
        "normalization": "prose(lib/blocks; code/table/heading/url/inline-code removed)",
        "stoplist_patterns": stoplist_patterns,
        "exact": {"min_chars": min_chars, "matches": exact},
        "char_5gram_jaccard": {
            "per_source": jaccard_per,
            "max": max(jaccard_per.values(), default=0.0),
        },
        "minhash": {
            "num_perm": NUM_PERM,
            "seed": seed,
            "per_source": minhash_per,
            "max": max(minhash_per.values(), default=0.0),
        },
        "paragraph": {
            "min_chars": paragraph_min_chars,
            "near_dup_threshold": PARAGRAPH_NEAR_DUP,
            "n_paragraphs": len(para_report),
            "containment_max": max((p["containment_max"] for p in para_report), default=0.0),
            "near_dup_count": len(near_dup),
            "near_dup_ratio": (len(near_dup) / len(para_report)) if para_report else 0.0,
            "per_paragraph": para_report,
        },
        "copy_index": {"status": "deferred", "note": "全コーパスのハッシュ索引は未実装。記事ごとの逐次比較"},
    }


def corpus_text_files(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.rglob("*") if p.suffix in (".txt", ".md") and p.is_file()
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.text.exists():
        print(f"error: text がありません: {args.text}", file=sys.stderr)
        return 1
    if not args.against.is_dir():
        print(f"error: against がディレクトリではありません: {args.against}", file=sys.stderr)
        return 1
    corpus_files = corpus_text_files(args.against)
    if not corpus_files:
        print(f"error: {args.against} に txt/md がありません", file=sys.stderr)
        return 1

    text = args.text.read_text(encoding="utf-8")
    report = compare_against_corpus(
        text,
        corpus_files,
        min_chars=args.min_chars,
        seed=args.seed,
        stoplist_patterns=args.stoplist_pattern,
    )
    report["text"] = str(args.text)
    report["against"] = str(args.against)
    report["n_sources"] = len(corpus_files)
    if args.embeddings is None:
        report["embedding"] = {"status": "skipped"}
    else:
        report["embedding"] = {
            "status": "not_implemented",
            "note": "埋め込みバックエンドは未実装(オフライン制約)",
        }

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
