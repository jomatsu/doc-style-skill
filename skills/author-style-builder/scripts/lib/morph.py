"""形態素チャネル(morphology channels)の抽出・較正・評価。

extract_features / style_lint が共有する。入力は lib/features が作る文レコード
(散文契約を通った文 + Sudachi トークン列)。fallback モードでは POS 依存チャネルは
None(skipped)になり、表層のみで計算できるチャネル(読点位置・ヘッジ等)だけ残る。

すべてのチャネルは **validator 専用**(feature-catalog.md)。SKILL.md の文面
(ペルソナ・常時ルール)へは描画しない。スカラーの一部は checklist 観察候補に
なり得るが、自動採用しない。

分布チャネルの距離は Jensen-Shannon 距離(sqrt(JSD), 底 2, 0〜1)。
参照(centroid)は記事等重みの平均分布を上位 K + OTHER に有界化したもの。
閾値は著者内 leave-one-article-out(LOAO)距離分布から取る(compile_skill)。

マスキング契約: 散文契約がインラインコード / 文中 URL を置換したプレースホルダ
トークン(token["masked"]=True。lib/features.build_sentences が付与)は、**全ての**
形態素チャネルの分子・分母から除外する(品詞 n-gram・機能語・形式名詞・動詞名詞比・
修飾語密度・文末 suffix・段落頭・読点直前)。表層チャネルの読点相対位置も置換区間を
除いた文字列で計算する。したがって、インラインコードの密度を変えても形態素チャネル
は変化しない(tests/test_masking.py の metamorphic テスト)。n-gram は置換トークンを
跨いで隣接させる(挿入不変性を優先する設計上の選択。feature-catalog.md 参照)。

レジストリ版 2 の一人称チャネル:
- `first_person_lemma`(一人称 lemma 分布)と `first_person_top_share`(最頻一人称
  lemma の集中度)を著者非依存の定義で計算
- 形式名詞レジスタから「気」を除外(「気がする」等の慣用表現と「気」の名詞用法を
  文脈なしで区別できないため。こと・もの・ため・わけ・はず・準体助詞「の」は維持)
- 閾値はフル精度で保存し、hard 境界は lib/calibration のポリシーに従う
"""

from __future__ import annotations

import math
import re

from lib import calibration as calib
from lib import stats

CHANNEL_REGISTRY_VERSION = "2"

# name -> {kind, requires, max_severity, top_k(dist), min_sample, label}
# max_severity="warn" のチャネルは単一記事で不安定なため hard fail させない。
CHANNELS: dict[str, dict] = {
    # ---- 分布(dist) ----
    "pos_unigram": dict(kind="dist", requires="sudachi", max_severity="fail", top_k=20, min_sample=50, label="品詞 unigram"),
    "pos_bigram": dict(kind="dist", requires="sudachi", max_severity="fail", top_k=40, min_sample=50, label="品詞 bigram"),
    "pos_trigram": dict(kind="dist", requires="sudachi", max_severity="fail", top_k=60, min_sample=80, label="品詞 trigram"),
    "particle_bigram": dict(kind="dist", requires="sudachi", max_severity="fail", top_k=40, min_sample=30, label="助詞 bigram"),
    "funcword_bigram": dict(kind="dist", requires="sudachi", max_severity="fail", top_k=60, min_sample=40, label="機能語 bigram"),
    "aux_lemma": dict(kind="dist", requires="sudachi", max_severity="fail", top_k=20, min_sample=15, label="助動詞分布"),
    "final_suffix2": dict(kind="dist", requires="sudachi", max_severity="fail", top_k=40, min_sample=8, label="文末 suffix2"),
    "final_suffix3": dict(kind="dist", requires="sudachi", max_severity="warn", top_k=60, min_sample=10, label="文末 suffix3"),
    "final_pos_cform": dict(kind="dist", requires="sudachi", max_severity="fail", top_k=20, min_sample=8, label="文末 品詞×活用形"),
    "content_masked_lemma_bigram": dict(kind="dist", requires="sudachi", max_severity="warn", top_k=80, min_sample=60, label="内容語マスク lemma bigram"),
    "formal_noun": dict(kind="dist", requires="sudachi", max_severity="warn", top_k=7, min_sample=5, label="形式名詞分布"),
    "conj_lemma": dict(kind="dist", requires="sudachi", max_severity="warn", top_k=20, min_sample=5, label="接続詞 lemma"),
    "para_initial_pos": dict(kind="dist", requires="sudachi", max_severity="warn", top_k=10, min_sample=5, label="段落頭 品詞"),
    "para_initial_conj": dict(kind="dist", requires="sudachi", max_severity="warn", top_k=15, min_sample=3, label="段落頭 接続詞"),
    "pre_comma_pos": dict(kind="dist", requires="sudachi", max_severity="warn", top_k=15, min_sample=8, label="読点直前 品詞"),
    "pre_comma_lemma": dict(kind="dist", requires="sudachi", max_severity="warn", top_k=30, min_sample=8, label="読点直前 lemma"),
    "comma_rel_pos": dict(kind="dist", requires="surface", max_severity="warn", top_k=4, min_sample=8, label="読点相対位置"),
    "hedge_class": dict(kind="dist", requires="surface", max_severity="warn", top_k=8, min_sample=3, label="ヘッジ種別"),
    "first_person_lemma": dict(kind="dist", requires="sudachi", max_severity="warn", top_k=6, min_sample=3, label="一人称 lemma 分布"),
    # ---- スカラー(scalar) ----
    "formal_noun_rate": dict(kind="scalar", requires="sudachi", max_severity="fail", min_sample=50, label="形式名詞率"),
    "demonstrative_rate": dict(kind="scalar", requires="sudachi", max_severity="warn", min_sample=50, label="指示語率"),
    "first_person_rate": dict(kind="scalar", requires="sudachi", max_severity="warn", min_sample=50, label="一人称率"),
    "first_person_top_share": dict(kind="scalar", requires="sudachi", max_severity="warn", min_sample=3, label="最頻一人称 lemma の集中度"),
    "quote_sentence_rate": dict(kind="scalar", requires="surface", max_severity="warn", min_sample=5, label="引用文率"),
    "conj_rate": dict(kind="scalar", requires="sudachi", max_severity="warn", min_sample=5, label="接続詞率(文あたり)"),
    "para_initial_conj_rate": dict(kind="scalar", requires="sudachi", max_severity="warn", min_sample=3, label="段落頭接続詞率"),
    "verb_noun_ratio": dict(kind="scalar", requires="sudachi", max_severity="fail", min_sample=50, label="動詞/名詞比"),
    "modifier_density": dict(kind="scalar", requires="sudachi", max_severity="fail", min_sample=50, label="修飾語密度"),
    "comma_first_quarter_ratio": dict(kind="scalar", requires="surface", max_severity="warn", min_sample=8, label="読点の前 1/4 比"),
    "comma_rel_pos_median": dict(kind="scalar", requires="surface", max_severity="warn", min_sample=8, label="読点相対位置中央値"),
    "negative_rate": dict(kind="scalar", requires="sudachi", max_severity="warn", min_sample=5, label="否定文率"),
    "past_rate": dict(kind="scalar", requires="sudachi", max_severity="warn", min_sample=5, label="過去文率"),
    "speculative_rate": dict(kind="scalar", requires="surface", max_severity="warn", min_sample=5, label="推量文率"),
    "hedge_rate": dict(kind="scalar", requires="surface", max_severity="warn", min_sample=5, label="ヘッジ文率"),
    "max_consecutive_same_suffix2": dict(kind="scalar", requires="sudachi", max_severity="warn", min_sample=5, label="同一文末 suffix2 の最大連続"),
}

DIST_CHANNELS = [k for k, v in CHANNELS.items() if v["kind"] == "dist"]
SCALAR_CHANNELS = [k for k, v in CHANNELS.items() if v["kind"] == "scalar"]
FAIL_CAPABLE_CHANNELS = [k for k, v in CHANNELS.items() if v["max_severity"] == "fail"]

OTHER = "OTHER"

_FUNC_POS = {"助詞", "助動詞", "接続詞"}
_CLOSED_POS = {"助詞", "助動詞", "接続詞", "代名詞", "連体詞", "感動詞"}
_SKIP_POS = {"空白", "補助記号", "記号"}
_CONTENT_MASK_POS = {"名詞", "動詞", "形容詞", "形状詞", "副詞", "接頭辞", "接尾辞"}
# 形式名詞(一般的な日本語文法記述に基づく閉じたリスト。「気」は v2 で除外)
_FORMAL_NOUN_LEMMAS = {"こと", "もの", "ため", "わけ", "はず"}
# 一人称代名詞(一般的な日本語一人称の閉じたリスト。特定著者の分布に基づかない)
_FIRST_PERSON = {"自分", "私", "僕", "俺", "わたし", "ぼく", "おれ", "我々", "私たち", "僕ら", "自分たち", "われわれ", "当方", "筆者"}
_DEMONSTRATIVE = {
    "これ", "それ", "あれ", "ここ", "そこ", "あそこ", "こちら", "そちら", "あちら",
    "この", "その", "あの", "こう", "そう", "ああ", "こんな", "そんな", "あんな",
    "こういう", "そういう", "ああいう",
}
_MODIFIER_POS = {"副詞", "形容詞", "形状詞", "連体詞"}

# ヘッジ種別(warn 専用)。出所: 日本語のモダリティ表現の一般的な文法記述
# (推量・可能性・当然・様態・推量副詞・思考動詞・感覚表現)を種別化した事前リスト。
# 特定著者のコーパス頻度から導いたものではなく、並び順は文法カテゴリ順で、
# どの種別も優先されない(分布キーはソートされる)。
_HEDGE_PATTERNS = [
    # 推量の助動詞
    ("darou", re.compile(r"だろう|でしょう")),
    # 可能性(かもしれない)
    ("kamo", re.compile(r"かもしれ|かも[。、!！?？]|かも$|かもな")),
    # 当然・予定(はず)
    ("hazu", re.compile(r"はず")),
    # 様態・伝聞(ようだ・らしい・みたい)
    ("youda", re.compile(r"ようだ|ようです|らしい|みたい|ように思|ように見")),
    # 推量副詞
    ("tabun", re.compile(r"たぶん|多分|おそらく|恐らく|もしかし")),
    # 思考動詞による緩和
    ("toomou", re.compile(r"と思[うっいわえ]|と考え|と感じ")),
    # 感覚表現による緩和
    ("kigasuru", re.compile(r"気がす|気がし|気もす")),
]
_SPECULATIVE_CLASSES = {"kamo", "hazu", "darou", "youda", "tabun"}
_QUOTE_RE = re.compile(r"[「『]")
_COMMA_CHARS = "、，"


# ---------------- 基本ユーティリティ ----------------

def normalize(counter: dict) -> dict | None:
    total = sum(counter.values())
    if total <= 0:
        return None
    return {k: v / total for k, v in sorted(counter.items())}


def js_distance(p: dict, q: dict) -> float:
    """Jensen-Shannon 距離(sqrt(JSD)、底 2、0〜1)。

    キーをソートして加算順を固定する(フル精度で保存するため、set の反復順による
    最下位ビットの揺れも許さない)。
    """
    keys = sorted(set(p) | set(q))
    js = 0.0
    for k in keys:
        pk = p.get(k, 0.0)
        qk = q.get(k, 0.0)
        mk = (pk + qk) / 2
        if pk > 0:
            js += 0.5 * pk * math.log2(pk / mk)
        if qk > 0:
            js += 0.5 * qk * math.log2(qk / mk)
    return math.sqrt(max(js, 0.0))


def project(dist: dict, keys: list[str]) -> dict:
    """分布を keys + OTHER に有界化(質量保存)。"""
    keyset = set(keys)
    out = {k: 0.0 for k in keys}
    other = 0.0
    for k, v in dist.items():
        if k in keyset:
            out[k] += v
        else:
            other += v
    out[OTHER] = other
    return out


def mean_dist(dists: list[dict]) -> dict:
    acc: dict = {}
    for d in dists:
        for k, v in d.items():
            acc[k] = acc.get(k, 0.0) + v
    n = len(dists)
    return {k: v / n for k, v in acc.items()} if n else {}


def bounded_keys(centroid: dict, top_k: int) -> list[str]:
    ranked = sorted(centroid.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[:top_k] if k != OTHER]


def is_function_token(t: dict) -> bool:
    pos = t["pos"]
    if pos in _CLOSED_POS:
        return True
    if pos == "名詞" and t["base"] in _FORMAL_NOUN_LEMMAS:
        return True
    if pos in ("動詞", "形容詞") and t.get("pos_detail") == "非自立可能":
        return True
    return False


def masked_lemma(t: dict) -> str:
    """機能語は lemma、内容語は品詞へマスク。"""
    if is_function_token(t):
        return t["base"]
    return t["pos"]


def is_formal_noun(t: dict) -> bool:
    if t["pos"] == "名詞" and t["base"] in _FORMAL_NOUN_LEMMAS:
        return True
    return t["pos"] == "助詞" and t.get("pos_detail") == "準体助詞" and t["base"] == "の"


def is_masked(t: dict) -> bool:
    """散文契約のプレースホルダ(インラインコード / URL 置換)由来のトークンか。"""
    return bool(t.get("masked"))


def content_tokens(tokens: list[dict]) -> list[dict]:
    """統計対象トークン(補助記号・空白・プレースホルダを除く)。"""
    return [t for t in tokens if t["pos"] not in _SKIP_POS and not is_masked(t)]


def unmasked_text(text: str, masked: list | None) -> str:
    """置換区間(文内オフセット)を取り除いた文字列。"""
    if not masked:
        return text
    out = []
    pos = 0
    for s, e in sorted(masked):
        s = max(s, pos)
        if s > pos:
            out.append(text[pos:s])
        pos = max(pos, e)
    out.append(text[pos:])
    return "".join(out)


# ---------------- 抽出 ----------------

def _inc(counter: dict, key: str, n: int = 1) -> None:
    counter[key] = counter.get(key, 0) + n


def hedge_classes(text: str) -> list[str]:
    return [name for name, rx in _HEDGE_PATTERNS if rx.search(text)]


def comma_positions(text: str, masked: list | None = None) -> list[float]:
    """文中の読点の相対位置(0〜1)。文末句読点と置換区間は分母から除く。"""
    core = unmasked_text(text, masked).rstrip("。！？!?、」』)）】 　")
    n = len(core)
    if n <= 1:
        return []
    return [i / n for i, ch in enumerate(core) if ch in _COMMA_CHARS]


def final_tokens(tokens: list[dict], *, drop_masked: bool = True) -> list[dict]:
    """文末の補助記号・空白(と既定でプレースホルダ)を除いたトークン列。"""
    out = [t for t in tokens if not (drop_masked and is_masked(t))]
    while out and out[-1]["pos"] in _SKIP_POS:
        out.pop()
    return out


def suffix_key(tokens: list[dict], n: int) -> str | None:
    fin = content_tokens(tokens)
    if not fin:
        return None
    tail = fin[-n:]
    return "|".join(masked_lemma(t) for t in tail)


def extract_morphology(sentences: list[dict], n_paragraphs: int, mode: str) -> dict:
    """文レコード列 → morph ブロック。

    sentences: [{"text", "tokens", "para"}, ...]。tokens は fallback では []。
    返り値: {"available", "n_tokens", "n_sents", "dist": {...}, "scalar": {...},
             "sample": {channel: n}}
    """
    n_sents = len(sentences)
    pos_available = mode == "sudachi" and any(s["tokens"] for s in sentences)

    dist: dict[str, dict | None] = {k: None for k in DIST_CHANNELS}
    scalar: dict[str, float | None] = {k: None for k in SCALAR_CHANNELS}
    sample: dict[str, int] = {}

    # ---- 表層チャネル(fallback でも計算) ----
    comma_bins: dict = {}
    comma_rel: list[float] = []
    hedge_counter: dict = {}
    n_hedge_sents = 0
    n_spec_sents = 0
    n_quote_sents = 0
    for s in sentences:
        rel = comma_positions(s["text"], s.get("masked"))
        comma_rel.extend(rel)
        for r in rel:
            q = min(int(r * 4), 3)
            _inc(comma_bins, f"q{q + 1}")
        classes = hedge_classes(s["text"])
        if classes:
            n_hedge_sents += 1
            for c in classes:
                _inc(hedge_counter, c)
        if any(c in _SPECULATIVE_CLASSES for c in classes):
            n_spec_sents += 1
        if _QUOTE_RE.search(s["text"]):
            n_quote_sents += 1

    sample["comma_rel_pos"] = len(comma_rel)
    sample["comma_first_quarter_ratio"] = len(comma_rel)
    sample["comma_rel_pos_median"] = len(comma_rel)
    dist["comma_rel_pos"] = normalize(comma_bins)
    if comma_rel:
        scalar["comma_first_quarter_ratio"] = sum(1 for r in comma_rel if r < 0.25) / len(comma_rel)
        srt = sorted(comma_rel)
        scalar["comma_rel_pos_median"] = srt[len(srt) // 2]
    n_hedges = sum(hedge_counter.values())
    sample["hedge_class"] = n_hedges
    dist["hedge_class"] = normalize(hedge_counter)
    for key in ("hedge_rate", "speculative_rate", "quote_sentence_rate"):
        sample[key] = n_sents
    if n_sents:
        scalar["hedge_rate"] = n_hedge_sents / n_sents
        scalar["speculative_rate"] = n_spec_sents / n_sents
        scalar["quote_sentence_rate"] = n_quote_sents / n_sents

    if not pos_available:
        return {
            "available": False,
            "n_tokens": None,
            "n_masked_tokens": None,
            "n_sents": n_sents,
            "dist": dist,
            "scalar": scalar,
            "sample": sample,
        }

    # ---- POS 依存チャネル ----
    pos_uni: dict = {}
    pos_bi: dict = {}
    pos_tri: dict = {}
    particle_bi: dict = {}
    func_bi: dict = {}
    aux: dict = {}
    suf2: dict = {}
    suf3: dict = {}
    final_pc: dict = {}
    cm_bi: dict = {}
    formal: dict = {}
    conj: dict = {}
    para_pos: dict = {}
    para_conj: dict = {}
    pre_pos: dict = {}
    pre_lem: dict = {}
    first_lemma: dict = {}

    n_tokens = 0
    n_particles = 0
    n_func_seq = 0
    n_formal = 0
    n_demo = 0
    n_first = 0
    n_masked_tokens = 0
    n_conj_tokens = 0
    n_verb = 0
    n_noun = 0
    n_modifier = 0
    n_neg_sents = 0
    n_past_sents = 0
    n_commas = 0
    n_para_starts = 0
    n_para_conj = 0
    seen_para: set = set()
    suffix_seq: list[str | None] = []

    for s in sentences:
        n_masked_tokens += sum(1 for t in s["tokens"] if is_masked(t))
        toks = content_tokens(s["tokens"])
        if not toks:
            suffix_seq.append(None)
            continue
        n_tokens += len(toks)
        neg = past = False
        for t in toks:
            pos = t["pos"]
            _inc(pos_uni, pos)
            if pos == "動詞":
                n_verb += 1
            elif pos == "名詞":
                n_noun += 1
            if pos in _MODIFIER_POS:
                n_modifier += 1
            if pos == "助動詞":
                _inc(aux, t["base"])
                if t["base"] in ("ない", "ぬ", "ん"):
                    neg = True
                if t["base"] == "た":
                    past = True
            if pos == "形容詞" and t["base"] == "ない":
                neg = True
            if is_formal_noun(t):
                n_formal += 1
                _inc(formal, t["base"])
            if t["base"] in _DEMONSTRATIVE and pos in ("代名詞", "連体詞", "副詞", "形状詞", "名詞"):
                n_demo += 1
            if t["base"] in _FIRST_PERSON and pos in ("代名詞", "名詞"):
                n_first += 1
                _inc(first_lemma, t["base"])
            if pos == "接続詞":
                n_conj_tokens += 1
                _inc(conj, t["base"])
        if neg:
            n_neg_sents += 1
        if past:
            n_past_sents += 1

        for a, b in zip(toks, toks[1:]):
            _inc(pos_bi, f"{a['pos']}|{b['pos']}")
            _inc(cm_bi, f"{masked_lemma(a)}|{masked_lemma(b)}")
        for a, b, c in zip(toks, toks[1:], toks[2:]):
            _inc(pos_tri, f"{a['pos']}|{b['pos']}|{c['pos']}")

        particles = [t["base"] for t in toks if t["pos"] == "助詞"]
        n_particles += len(particles)
        for a, b in zip(particles, particles[1:]):
            _inc(particle_bi, f"{a}|{b}")
        funcs = [t["base"] for t in toks if t["pos"] in _FUNC_POS]
        for a, b in zip(funcs, funcs[1:]):
            _inc(func_bi, f"{a}|{b}")
            n_func_seq += 1

        k2 = suffix_key(s["tokens"], 2)
        k3 = suffix_key(s["tokens"], 3)
        suffix_seq.append(k2)
        if k2:
            _inc(suf2, k2)
        if k3:
            _inc(suf3, k3)
        fin = final_tokens(s["tokens"])
        if fin:
            last = fin[-1]
            _inc(final_pc, f"{last['pos']}|{last.get('cform', '*')}")

        # 読点直前(プレースホルダは読点直前の語として数えない)
        raw_toks = [t for t in s["tokens"] if not is_masked(t)]
        for i, t in enumerate(raw_toks):
            if t["pos"] == "補助記号" and t.get("pos_detail") == "読点":
                j = i - 1
                while j >= 0 and raw_toks[j]["pos"] in _SKIP_POS:
                    j -= 1
                if j >= 0:
                    n_commas += 1
                    _inc(pre_pos, raw_toks[j]["pos"])
                    _inc(pre_lem, masked_lemma(raw_toks[j]))

        # 段落頭
        para = s.get("para")
        if para is not None and para not in seen_para:
            seen_para.add(para)
            n_para_starts += 1
            first = toks[0]
            _inc(para_pos, first["pos"])
            if first["pos"] == "接続詞":
                n_para_conj += 1
                _inc(para_conj, first["base"])

    dist["pos_unigram"] = normalize(pos_uni)
    dist["pos_bigram"] = normalize(pos_bi)
    dist["pos_trigram"] = normalize(pos_tri)
    dist["particle_bigram"] = normalize(particle_bi)
    dist["funcword_bigram"] = normalize(func_bi)
    dist["aux_lemma"] = normalize(aux)
    dist["final_suffix2"] = normalize(suf2)
    dist["final_suffix3"] = normalize(suf3)
    dist["final_pos_cform"] = normalize(final_pc)
    dist["content_masked_lemma_bigram"] = normalize(cm_bi)
    dist["formal_noun"] = normalize(formal)
    dist["conj_lemma"] = normalize(conj)
    dist["para_initial_pos"] = normalize(para_pos)
    dist["para_initial_conj"] = normalize(para_conj)
    dist["pre_comma_pos"] = normalize(pre_pos)
    dist["pre_comma_lemma"] = normalize(pre_lem)
    dist["first_person_lemma"] = normalize(first_lemma)

    sample.update(
        {
            "pos_unigram": n_tokens,
            "pos_bigram": max(n_tokens - n_sents, 0),
            "pos_trigram": max(n_tokens - 2 * n_sents, 0),
            "particle_bigram": max(n_particles - n_sents, 0),
            "funcword_bigram": n_func_seq,
            "aux_lemma": sum(aux.values()),
            "final_suffix2": sum(suf2.values()),
            "final_suffix3": sum(suf3.values()),
            "final_pos_cform": sum(final_pc.values()),
            "content_masked_lemma_bigram": sum(cm_bi.values()),
            "formal_noun": n_formal,
            "conj_lemma": n_conj_tokens,
            "para_initial_pos": n_para_starts,
            "para_initial_conj": n_para_conj,
            "pre_comma_pos": n_commas,
            "pre_comma_lemma": n_commas,
            "first_person_lemma": n_first,
            "formal_noun_rate": n_tokens,
            "demonstrative_rate": n_tokens,
            "first_person_rate": n_tokens,
            "first_person_top_share": n_first,
            "conj_rate": n_sents,
            "para_initial_conj_rate": n_para_starts,
            "verb_noun_ratio": n_tokens,
            "modifier_density": n_tokens,
            "negative_rate": n_sents,
            "past_rate": n_sents,
            "max_consecutive_same_suffix2": n_sents,
        }
    )

    if n_tokens:
        scalar["formal_noun_rate"] = n_formal / n_tokens
        scalar["demonstrative_rate"] = n_demo / n_tokens
        scalar["first_person_rate"] = n_first / n_tokens
        scalar["modifier_density"] = n_modifier / n_tokens
        scalar["verb_noun_ratio"] = n_verb / n_noun if n_noun else None
    if n_first:
        scalar["first_person_top_share"] = max(first_lemma.values()) / n_first
    if n_sents:
        scalar["conj_rate"] = n_conj_tokens / n_sents
        scalar["negative_rate"] = n_neg_sents / n_sents
        scalar["past_rate"] = n_past_sents / n_sents
    if n_para_starts:
        scalar["para_initial_conj_rate"] = n_para_conj / n_para_starts

    # 同一 suffix2 の最大連続(意味のある文末分類での連続数)
    best = run = 0
    prev = None
    for k in suffix_seq:
        if k is not None and k == prev:
            run += 1
        else:
            run = 1 if k is not None else 0
        prev = k
        best = max(best, run)
    scalar["max_consecutive_same_suffix2"] = float(best) if n_sents else None

    return {
        "available": True,
        "n_tokens": n_tokens,
        "n_masked_tokens": n_masked_tokens,
        "n_sents": n_sents,
        "dist": dist,
        "scalar": scalar,
        "sample": sample,
    }


# ---------------- 較正(compile 側) ----------------

# ポリシー定数は lib/calibration に一本化(後方互換のため再 export)
LARGE_N = calib.LARGE_N
MIN_CALIBRATION_N = calib.MIN_CALIBRATION_N
TUKEY_K = calib.TUKEY_K

# 較正時の安定性注記(compile 警告用)
_OUTLIER_FAIL_TO_P50 = 2.0  # fail が p50 の 2 倍超 → 極値記事が境界を支配
_OTHER_MASS_MAX = 0.5  # centroid の OTHER 質量が半分超 → top_k 不足
_NEAR_MIN_N_MARGIN = 5  # n < MIN_CALIBRATION_N + 5 → min_sample の出入りで境界が大きく動く


def calibrate_dist_channel(name: str, per_article: list[tuple[str, dict]]) -> dict:
    """記事ごとの分布 → 有界 centroid + LOAO 距離分布 + 閾値(フル精度)。

    hard(fail)境界: n < LARGE_N では LOAO max(較正記事の評価距離は JSD の凸性から
    LOAO 距離以下なので fail しない)、n >= LARGE_N では Bonferroni 分位点(lib/calibration)。
    いずれも warn(p90)を下回らない。
    """
    spec = CHANNELS[name]
    dists = [d for _, d in per_article]
    n = len(dists)
    if n < MIN_CALIBRATION_N:
        return {"status": "skipped", "reason": f"insufficient_n({n}<{MIN_CALIBRATION_N})", "n_articles": n}
    full = mean_dist(dists)
    keys = bounded_keys(full, spec["top_k"])
    projected = [project(d, keys) for d in dists]
    centroid = project(full, keys)
    total = mean_dist(projected)
    loao = []
    for i, p in enumerate(projected):
        rest = {k: (total[k] * n - p.get(k, 0.0)) / (n - 1) for k in total}
        loao.append((per_article[i][0], js_distance(p, rest)))
    d_only = [d for _, d in loao]
    p50 = stats.quantile(d_only, 0.5)
    p90 = stats.quantile(d_only, 0.90)
    hard, hard_rule = calib.upper_hard_bound(d_only)
    fail = max(hard, p90)
    notes: list[str] = []
    if p50 > 0 and fail / p50 > _OUTLIER_FAIL_TO_P50:
        notes.append(f"fail_threshold_outlier_dominated(fail/p50={fail / p50:.2f})")
    if centroid.get(OTHER, 0.0) > _OTHER_MASS_MAX:
        notes.append(f"top_k_too_small(other_mass={centroid[OTHER]:.2f})")
    if len(keys) < spec["top_k"]:
        notes.append(f"top_k_vacuous(observed_keys={len(keys)}<top_k={spec['top_k']})")
    if n < MIN_CALIBRATION_N + _NEAR_MIN_N_MARGIN:
        notes.append(f"near_min_calibration_n({n})")
    return {
        "status": "built",
        "kind": "dist",
        "n_articles": n,
        "top_k": spec["top_k"],
        "keys": keys,
        "centroid": centroid,
        "loao": {
            "distances": sorted(d_only),
            "p50": p50,
            "p90": p90,
            "p99": stats.quantile(d_only, 0.99),
            "max": max(d_only),
        },
        "thresholds": {
            "warn": p90,
            "fail": fail,
            "warn_rule": "loao_p90",
            "fail_rule": f"max(loao_{hard_rule}, loao_p90)",
        },
        "max_severity": spec["max_severity"],
        "min_sample": spec["min_sample"],
        "notes": notes,
    }


def calibrate_scalar_channel(name: str, per_article: list[tuple[str, float]]) -> dict:
    """記事ごとのスカラー → 分位点 + 閾値(フル精度)。hard 境界は lib/calibration。"""
    spec = CHANNELS[name]
    vals = [float(v) for _, v in per_article if v is not None]
    n = len(vals)
    if n < MIN_CALIBRATION_N:
        return {"status": "skipped", "reason": f"insufficient_n({n}<{MIN_CALIBRATION_N})", "n_articles": n}
    q = {f"p{int(p * 100):02d}": stats.quantile(vals, p) for p in (0.01, 0.05, 0.10, 0.25, 0.5, 0.75, 0.90, 0.95, 0.99)}
    lo, hi = min(vals), max(vals)
    hard = calib.scalar_hard_range(vals)
    warn = [q["p10"], q["p90"]]
    notes = calib.band_notes(warn, hard)
    if n < MIN_CALIBRATION_N + _NEAR_MIN_N_MARGIN:
        notes.append(f"near_min_calibration_n({n})")
    return {
        "status": "built",
        "kind": "scalar",
        "n_articles": n,
        "values": sorted(vals),
        "quantiles": q,
        "min": lo,
        "max": hi,
        "thresholds": {
            "warn": warn,
            "fail": [hard["lo"], hard["hi"]],
            "warn_rule": "two_sided_p10_p90",
            "fail_rule": hard["rule"],
            "sided": hard["sided"],
        },
        "max_severity": spec["max_severity"],
        "min_sample": spec["min_sample"],
        "notes": notes,
    }


# ---------------- 評価(style_lint 側) ----------------

_RANK = {"pass": 0, "warn": 1, "fail": 2}


def _cap(status: str, max_severity: str) -> str:
    if _RANK[status] > _RANK[max_severity]:
        return max_severity
    return status


def percentile_of(sorted_values: list[float], x: float) -> float:
    if not sorted_values:
        return 0.0
    below = sum(1 for v in sorted_values if v <= x)
    return below / len(sorted_values)


def evaluate_dist(name: str, text_dist: dict | None, ref: dict, sample_n: int) -> dict:
    spec = CHANNELS[name]
    if ref.get("status") != "built":
        return {"status": "skipped", "reason": ref.get("reason", "reference_not_built")}
    if text_dist is None:
        return {"status": "skipped", "reason": "channel_unavailable"}
    if sample_n < ref.get("min_sample", spec["min_sample"]):
        return {"status": "skipped", "reason": f"insufficient_sample({sample_n}<{ref.get('min_sample', spec['min_sample'])})"}
    p = project(text_dist, ref["keys"])
    c = ref["centroid"]
    d = js_distance(p, c)
    th = ref["thresholds"]
    status = "pass" if d <= th["warn"] else ("warn" if d <= th["fail"] else "fail")
    status = _cap(status, ref.get("max_severity", spec["max_severity"]))
    devs = sorted(
        ((k, p.get(k, 0.0) - c.get(k, 0.0)) for k in c),
        key=lambda kv: (-abs(kv[1]), kv[0]),
    )[:5]
    return {
        "status": status,
        "distance": d,
        "percentile": round(percentile_of(ref["loao"]["distances"], d), 4),
        "thresholds": {"warn": th["warn"], "fail": th["fail"]},
        "top_deviations": [
            {"key": k, "text": round(p.get(k, 0.0), 4), "reference": round(c.get(k, 0.0), 4), "delta": round(v, 4)}
            for k, v in devs
        ],
        "max_severity": ref.get("max_severity", spec["max_severity"]),
    }


def evaluate_scalar(name: str, value: float | None, ref: dict, sample_n: int) -> dict:
    spec = CHANNELS[name]
    if ref.get("status") != "built":
        return {"status": "skipped", "reason": ref.get("reason", "reference_not_built")}
    if value is None:
        return {"status": "skipped", "reason": "channel_unavailable"}
    if sample_n < ref.get("min_sample", spec["min_sample"]):
        return {"status": "skipped", "reason": f"insufficient_sample({sample_n}<{ref.get('min_sample', spec['min_sample'])})"}
    th = ref["thresholds"]
    wlo, whi = th["warn"]
    flo, fhi = th["fail"]
    if wlo <= value <= whi:
        status = "pass"
    elif flo <= value <= fhi:
        status = "warn"
    else:
        status = "fail"
    status = _cap(status, ref.get("max_severity", spec["max_severity"]))
    return {
        "status": status,
        "value": value,
        "percentile": round(percentile_of(ref["values"], value), 4),
        "thresholds": {"warn": th["warn"], "fail": th["fail"]},
        "max_severity": ref.get("max_severity", spec["max_severity"]),
    }
