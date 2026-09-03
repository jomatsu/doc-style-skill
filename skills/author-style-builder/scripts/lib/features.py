"""記事単位の定量特徴抽出(extract_features と style_lint で共有)。

FeatureRecord のスキーマは scripts/ARCHITECTURE.md、特徴の定義は
references/feature-catalog.md に対応する。入力ブロックは lib/blocks の契約で
散文セグメントへ変換され(コード・表・見出し・単独行 URL を除外、インライン
コードはプレースホルダ)、その散文だけを統計対象にする。
sudachi モードでは POS 依存特徴と形態素チャネル(lib/morph)も算出し、
fallback では null / 表層チャネルのみにする。

FEATURE_SCHEMA_VERSION: FeatureRecord / aggregate / profile claim が共有する特徴スキーマ版。
値の意味(分母・マスキング・散文契約)が変わる変更で上げる。版が違う aggregate と
プロファイルを黙って突き合わせない(compile_skill / skill_lint が検出)。
- (未記載) = v1 パイプラインまたは v2 初期(マスキング前)の特徴。較正に使えない
- "2" = 散文契約 + プレースホルダトークンを全 POS 統計・形態素チャネルから除外、
  チャネルレジストリ 2
"""

from __future__ import annotations

import re
import unicodedata

from lib import blocks as blocks_lib
from lib import morph as morph_lib
from lib.tokenize import split_sentences

FEATURE_SCHEMA_VERSION = "2"

# ---------------- 文末形式分類 ----------------

_DESU_MASU_RE = re.compile(
    r"(です|ます|ました|でした|ません|でしたら|ましょう|でしょう|ください|ますし|ですし)"
    r"(ね|よ|が|し|けれど|けど)?$"
)
_DA_DEARU_RE = re.compile(
    r"(だ|である|だった|であった|だろう|であろう|ではない|ではなかった)(ね|よ|が)?$"
)
# fallback 用: 常体の動詞/助動詞終止(た・ない・ず・ている・れる・辞書形)
_JOTAI_VERB_RE = re.compile(r"(た|ない|ず|ている|てる|れる|られる|せる|させる|たい|[うくぐすつぬぶむる])$")
_JOTAI_ADJ_RE = re.compile(r"[^な]い$")

_KANJI_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf々]")
_KATAKANA_RE = re.compile(r"[\u30a0-\u30ff\u31f0-\u31ff]")
_HIRAGANA_RE = re.compile(r"[\u3040-\u309f]")
_LATIN_RE = re.compile(r"[A-Za-zＡ-Ｚａ-ｚ]")
_DIGIT_RE = re.compile(r"[0-9０-９]")

_TRAILING_PUNCT = "。！？!?、」』）)】…‥ 　"

SENT_END_FORMS = (
    "desu_masu",
    "da_dearu",
    "jotai_verb",
    "jotai_adj",
    "taigen",
    "question",
    "other",
)


def _sentence_core(text: str) -> str:
    """文末の句読点・閉じ括弧類を落とした本体。"""
    return text.rstrip(_TRAILING_PUNCT)


def classify_sentence_end(text: str, tokens: list[dict] | None = None) -> str:
    """文末形式: desu_masu / da_dearu / jotai_verb / jotai_adj / taigen / question / other。

    tokens(sudachi)があれば末尾トークンの POS を優先し、
    無ければ表層正規表現で近似する(fallback)。
    """
    stripped = text.rstrip("」』）)】 　")
    core = _sentence_core(text)
    if not core:
        return "other"
    # 疑問: 「?・？」終わり、または「か(。)」終わり
    if stripped.endswith(("？", "?")) or core.endswith("か"):
        return "question"
    if _DESU_MASU_RE.search(core):
        return "desu_masu"
    if _DA_DEARU_RE.search(core):
        return "da_dearu"
    if tokens:
        # 文末形式はプレースホルダを名詞として見る(識別子止め = 体言止め)
        fin = morph_lib.final_tokens(tokens, drop_masked=False)
        if not fin:
            return "other"
        last = fin[-1]
        pos = last.get("pos", "")
        base = last.get("base", "")
        if pos == "助動詞":
            if base in ("です", "ます"):
                return "desu_masu"
            if base == "だ":
                return "da_dearu"
            return "jotai_verb"  # た・ない・れる・たい 等(動詞句の常体終止)
        if pos == "動詞":
            return "jotai_verb"
        if pos == "形容詞":
            return "jotai_adj"
        if pos in ("名詞", "代名詞", "接尾辞", "形状詞"):
            return "taigen"
        if pos == "助詞":
            # 体言+助詞止め(「〜へ。」「〜まで。」等)も体言止め扱い
            return "taigen"
        return "other"
    # fallback: 表層近似
    if _JOTAI_VERB_RE.search(core):
        return "jotai_verb"
    if _JOTAI_ADJ_RE.search(core):
        return "jotai_adj"
    last_ch = core[-1]
    if (
        _KANJI_RE.match(last_ch)
        or _KATAKANA_RE.match(last_ch)
        or _LATIN_RE.match(last_ch)
        or _DIGIT_RE.match(last_ch)
    ):
        return "taigen"
    return "other"


def sentence_endings(sentences: list[dict], analyzer) -> list[str]:
    """文リストの文末形式列(tokens が無ければ解析器で付与)。"""
    use_tokens = analyzer.meta()["mode"] == "sudachi"
    out = []
    for s in sentences:
        toks = s.get("tokens")
        if toks is None:
            toks = analyzer.tokenize(_sentence_core(s["text"])) if use_tokens else None
        out.append(classify_sentence_end(s["text"], toks))
    return out


# ---------------- 記事特徴 ----------------

_FUNC_POS = {"助詞", "助動詞", "接続詞"}
_TTR_WINDOW = 100
_TTR_STEP = 50


def _dist(counter: dict, total: int) -> dict | None:
    if total <= 0:
        return None
    return {k: v / total for k, v in sorted(counter.items())}


def _sentence_masked(seg: dict, start: int, end: int) -> list[list[int]]:
    """セグメントの置換区間のうち文 [start, end) に重なるものを文内オフセットで返す。"""
    out: list[list[int]] = []
    for ms, me in seg.get("masked") or []:
        lo, hi = max(ms, start), min(me, end)
        if hi > lo:
            out.append([lo - start, hi - start])
    return out


def _mark_masked_tokens(tokens: list[dict], masked: list[list[int]]) -> None:
    """置換区間に重なるトークンに masked=True を付ける(オフセットは文本体基準)。"""
    if not masked:
        return
    for t in tokens:
        ts, te = t.get("start", 0), t.get("end", 0)
        if any(ts < me and te > ms for ms, me in masked):
            t["masked"] = True


def build_sentences(segments: list[dict], analyzer) -> list[dict]:
    """散文セグメント → 文レコード列(散文契約の共通入口)。

    文レコード: {"text", "prose_span"(セグメント内), "raw_span", "seg", "para",
                 "masked"(文内の置換区間), "tokens"(sudachi のみ。置換区間に重なる
                 トークンは masked=True)}
    raw_span は置換前の raw 座標への写像を保つ(マスキングは統計から除くだけで、
    span の同一性は変えない)。
    """
    use_tokens = analyzer.meta()["mode"] == "sudachi"
    sentences: list[dict] = []
    for si, seg in enumerate(segments):
        for s in split_sentences(seg["text"]):
            core = _sentence_core(s["text"])
            masked = _sentence_masked(seg, s["char_start"], s["char_end"])
            tokens = analyzer.tokenize(core) if use_tokens and core else []
            _mark_masked_tokens(tokens, masked)
            sentences.append(
                {
                    "text": s["text"],
                    "prose_span": [s["char_start"], s["char_end"]],
                    "raw_span": blocks_lib.raw_span(seg, s["char_start"], s["char_end"]),
                    "seg": si,
                    "para": si,
                    "masked": masked,
                    "tokens": tokens,
                }
            )
    return sentences


def empty_record(meta: dict) -> dict:
    return {
        "article_id": None,
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "analyzer": meta,
        "n_sents": 0,
        "n_chars": 0,
        "sent_len": {"median": 0, "iqr": [0, 0], "max": 0},
        "para_len": {"median": 0, "iqr": [0, 0]},
        "comma_per_sent": {"median": 0, "iqr": [0, 0]},
        "sent_end_form": {k: 0.0 for k in SENT_END_FORMS},
        "max_consecutive_same_ending": 0,
        "script_ratio": {
            "kanji": 0.0,
            "hiragana": 0.0,
            "katakana": 0.0,
            "latin": 0.0,
            "digit": 0.0,
            "other": 0.0,
        },
        "func_word_rate": None,
        "particle_bigram": None,
        "pos_bigram": None,
        "aux_verb_dist": None,
        "ttr_window": None,
        "distinct_2": None,
        "prose": {
            "n_segments": 0,
            "n_list_segments": 0,
            "n_masked_inline": 0,
            "max_consecutive_span": [0, 0],
        },
        "morph": morph_lib.extract_morphology([], 0, meta["mode"]),
    }


def extract_article_features(blocks: list[dict], analyzer) -> dict:
    """ブロック列から FeatureRecord(article_id なし)を算出する。

    呼び出し側で record["article_id"] を設定すること。
    """
    meta = analyzer.meta()
    segments = blocks_lib.prose_segments(blocks)
    record = empty_record(meta)
    record["prose"] = {
        "n_segments": len(segments),
        "n_list_segments": sum(1 for s in segments if s["kind"] == "list"),
        "n_masked_inline": sum(s["n_masked"] for s in segments),
        "max_consecutive_span": [0, 0],
    }
    sentences = build_sentences(segments, analyzer)
    n_sents = len(sentences)
    record["n_sents"] = n_sents

    # 散文文字数(プレースホルダ区間は除く)
    n_chars = 0
    for seg in segments:
        masked_len = sum(e - s for s, e in seg["masked"])
        n_chars += len(seg["text"].replace("\n", "")) - masked_len
    record["n_chars"] = n_chars
    if n_sents == 0:
        return record

    from lib import stats

    # 文長(散文文字数)
    sent_lens = [len(s["text"]) for s in sentences]
    record["sent_len"] = {
        "median": stats.median(sent_lens),
        "iqr": stats.iqr(sent_lens),
        "max": max(sent_lens),
    }
    para_lens = [0] * len(segments)
    for s in sentences:
        para_lens[s["para"]] += 1
    para_lens = [n for n in para_lens if n > 0]
    if para_lens:
        record["para_len"] = {
            "median": stats.median(para_lens),
            "iqr": stats.iqr(para_lens),
        }
    commas = [s["text"].count("、") + s["text"].count("，") for s in sentences]
    record["comma_per_sent"] = {
        "median": stats.median(commas),
        "iqr": stats.iqr(commas),
    }

    # 文末形式
    endings = sentence_endings(sentences, analyzer)
    form_counts = {k: 0 for k in SENT_END_FORMS}
    for e in endings:
        form_counts[e] += 1
    record["sent_end_form"] = {k: v / n_sents for k, v in form_counts.items()}

    best_len, best_start = 1, 0
    run_len, run_start = 1, 0
    for i in range(1, len(endings)):
        if endings[i] == endings[i - 1]:
            run_len += 1
        else:
            run_len, run_start = 1, i
        if run_len > best_len:
            best_len, best_start = run_len, run_start
    record["max_consecutive_same_ending"] = best_len
    record["prose"]["max_consecutive_span"] = [
        sentences[best_start]["raw_span"][0],
        sentences[best_start + best_len - 1]["raw_span"][1],
    ]

    # 文字種比率(プレースホルダ区間・空白を除く散文文字)
    script_counts = {k: 0 for k in record["script_ratio"]}
    total_chars = 0
    for seg in segments:
        masked_ranges = seg["masked"]
        for idx, ch in enumerate(seg["text"]):
            if ch.isspace():
                continue
            if any(s <= idx < e for s, e in masked_ranges):
                continue
            total_chars += 1
            if _KANJI_RE.match(ch):
                script_counts["kanji"] += 1
            elif _HIRAGANA_RE.match(ch):
                script_counts["hiragana"] += 1
            elif _KATAKANA_RE.match(ch) or ch == "ー":
                script_counts["katakana"] += 1
            elif _LATIN_RE.match(ch):
                script_counts["latin"] += 1
            elif _DIGIT_RE.match(ch):
                script_counts["digit"] += 1
            else:
                script_counts["other"] += 1
    if total_chars:
        record["script_ratio"] = {k: v / total_chars for k, v in script_counts.items()}

    # ---- POS 依存特徴(sudachi モードのみ) ----
    if meta["mode"] == "sudachi":
        # プレースホルダトークンは全 POS 統計の分子・分母から除く(feature_schema 2)
        tokens = [t for s in sentences for t in morph_lib.content_tokens(s["tokens"])]
        if tokens:
            n_tokens = len(tokens)
            func = sum(1 for t in tokens if t["pos"] in _FUNC_POS)
            record["func_word_rate"] = func / n_tokens

            particles = [t["base"] for t in tokens if t["pos"] == "助詞"]
            pb: dict = {}
            for a, b in zip(particles, particles[1:]):
                pb[f"{a}|{b}"] = pb.get(f"{a}|{b}", 0) + 1
            record["particle_bigram"] = _dist(pb, max(len(particles) - 1, 0))

            posb: dict = {}
            for a, b in zip(tokens, tokens[1:]):
                key = f"{a['pos']}|{b['pos']}"
                posb[key] = posb.get(key, 0) + 1
            record["pos_bigram"] = _dist(posb, max(n_tokens - 1, 0))

            aux: dict = {}
            n_aux = 0
            for t in tokens:
                if t["pos"] == "助動詞":
                    aux[t["base"]] = aux.get(t["base"], 0) + 1
                    n_aux += 1
            record["aux_verb_dist"] = _dist(aux, n_aux)

            # 語彙多様性: プレースホルダは既に tokens から除かれている
            surfaces = [t["surface"] for t in tokens]
            record["ttr_window"] = _mean_window_ttr(surfaces)
            bigrams = list(zip(surfaces, surfaces[1:]))
            if bigrams:
                record["distinct_2"] = len(set(bigrams)) / len(bigrams)

    record["morph"] = morph_lib.extract_morphology(sentences, len(segments), meta["mode"])
    return record


def record_from_text(text: str, analyzer) -> dict:
    """raw テキスト(Markdown 可)→ FeatureRecord。style_lint / feedback_intake 用。

    extract_features は clean ブロック(同じ classify_blocks の出力)から同じ
    関数を呼ぶため、同一散文に対して同一の FeatureRecord になる。
    """
    return extract_article_features(blocks_lib.classify_text(text), analyzer)


def _mean_window_ttr(tokens: list[str]) -> float | None:
    if not tokens:
        return None
    if len(tokens) <= _TTR_WINDOW:
        return len(set(tokens)) / len(tokens)
    ttrs = []
    for start in range(0, len(tokens) - _TTR_WINDOW + 1, _TTR_STEP):
        window = tokens[start : start + _TTR_WINDOW]
        ttrs.append(len(set(window)) / len(window))
    return sum(ttrs) / len(ttrs)


# ---------------- 分布間距離(lint 用) ----------------

def _js_divergence(p: dict, q: dict) -> float:
    """Jensen-Shannon divergence(底 2、0〜1)。"""
    return morph_lib.js_distance(p, q) ** 2


def feature_distance(a: dict, b: dict, keys: list[str]) -> dict:
    """FeatureRecord a, b の分布特徴間の Jensen-Shannon divergence。"""
    out: dict = {}
    for key in keys:
        pa, pb = a.get(key), b.get(key)
        if isinstance(pa, dict) and isinstance(pb, dict):
            out[key] = _js_divergence(pa, pb)
        else:
            out[key] = None
    return out


# ---------------- 集計用スカラー抽出 ----------------

SCALAR_KEYS = [
    "sent_len_median",
    "sent_len_max",
    "para_len_median",
    "comma_per_sent_median",
    "max_consecutive_same_ending",
    "sent_end_form.desu_masu",
    "sent_end_form.da_dearu",
    "sent_end_form.jotai_verb",
    "sent_end_form.jotai_adj",
    "sent_end_form.taigen",
    "sent_end_form.question",
    "sent_end_form.other",
    "script_ratio.kanji",
    "script_ratio.hiragana",
    "script_ratio.katakana",
    "script_ratio.latin",
    "script_ratio.digit",
    "script_ratio.other",
    "func_word_rate",
    "ttr_window",
    "distinct_2",
]

MORPH_SCALAR_KEYS = [f"morph.{k}" for k in morph_lib.SCALAR_CHANNELS]


def scalar_value(record: dict, key: str) -> float | None:
    """FeatureRecord から集計用スカラー値を取り出す。"""
    if key == "sent_len_median":
        return record["sent_len"]["median"]
    if key == "sent_len_max":
        return record["sent_len"]["max"]
    if key == "para_len_median":
        return record["para_len"]["median"]
    if key == "comma_per_sent_median":
        return record["comma_per_sent"]["median"]
    if key == "max_consecutive_same_ending":
        return record["max_consecutive_same_ending"]
    if key.startswith("morph."):
        return (record.get("morph") or {}).get("scalar", {}).get(key[len("morph.") :])
    if "." in key:
        top, sub = key.split(".", 1)
        val = record.get(top)
        if isinstance(val, dict):
            return val.get(sub)
        return None
    return record.get(key)


def normalize_text(text: str) -> str:
    """NFKC 正規化(並行ビュー用)。"""
    return unicodedata.normalize("NFKC", text)


def char_ngrams(text: str, n: int) -> set:
    """重複検出・リーク検査用の文字 n-gram 集合(空白除去後)。"""
    compact = re.sub(r"\s+", "", text)
    if len(compact) < n:
        return {compact} if compact else set()
    return {compact[i : i + n] for i in range(len(compact) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: set, b: set) -> float:
    """|a ∩ b| / |a|(a が b にどれだけ含まれるか)。"""
    if not a:
        return 0.0
    return len(a & b) / len(a)
