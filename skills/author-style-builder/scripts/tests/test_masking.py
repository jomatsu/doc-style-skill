"""形態素マスキング契約の metamorphic テスト。

- インラインコード / 文中 URL の密度を変えても形態素チャネル(morph.dist / morph.scalar)
  と POS 依存の legacy 特徴(func_word_rate / particle_bigram / pos_bigram / aux / TTR)は
  変わらない。変わるのは明示メタデータ(prose.n_masked_inline / morph.n_masked_tokens)だけ
- コード・表・見出し・単独行 URL の不変性は従来通り(test_lib.py::TestProseContract)
- raw span 写像と文レコードの同一性(masked 区間・トークン flag)を保つ
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import synth  # noqa: E402
from lib import blocks as blocks_lib  # noqa: E402
from lib import features as feat  # noqa: E402
from lib import morph as morph_lib  # noqa: E402
from lib.tokenize import get_analyzer  # noqa: E402

ANALYZER = get_analyzer()
MODE = ANALYZER.meta()["mode"]

_INSERTS = ["`git commit -m`", "`config.toml`", "`x`", "`a.b(c)`"]


def _with_inline(text: str, density: float, seed: int) -> str:
    """文中の「は」「を」の直後にインラインコード / URL を密度 density で挿入する。"""
    import random

    rng = random.Random(seed)
    out = []
    for ch in text:
        out.append(ch)
        if ch in "はを" and rng.random() < density:
            out.append(rng.choice(_INSERTS))
    return "".join(out)


def _morph_view(record: dict) -> dict:
    m = dict(record["morph"])
    m.pop("n_masked_tokens", None)
    return m


@pytest.mark.parametrize("register", ["desu_masu", "jotai", "mixed"])
@pytest.mark.parametrize("density", [0.15, 0.5, 1.0])
def test_inline_code_density_does_not_change_morphology(register, density):
    base = synth.article(11, register=register, n_paragraphs=5, with_frontmatter=False)
    noisy = _with_inline(base, density, seed=int(density * 100))
    assert noisy != base
    r0 = feat.record_from_text(base, ANALYZER)
    r1 = feat.record_from_text(noisy, ANALYZER)
    assert r1["prose"]["n_masked_inline"] > r0["prose"]["n_masked_inline"] == 0
    assert r0["n_sents"] == r1["n_sents"]
    assert _morph_view(r0) == _morph_view(r1)
    if MODE == "sudachi":
        # プレースホルダ 1 つが複数トークンに割れることがある(それらは全て masked)
        assert r1["morph"]["n_masked_tokens"] >= r1["prose"]["n_masked_inline"]
        for key in ("func_word_rate", "particle_bigram", "pos_bigram", "aux_verb_dist", "ttr_window", "distinct_2"):
            assert r0[key] == r1[key], key
    else:
        assert r1["morph"]["n_masked_tokens"] is None


@pytest.mark.parametrize("register", ["desu_masu", "jotai"])
def test_inline_url_does_not_change_morphology(register):
    """文中 URL(前後に空白)を同じ空白のみに置き換えたテキストと形態素チャネルが一致する。

    (URL は空白までを一塊として置換するので、直後に日本語を続ける書き方は対象外)"""
    base = synth.article(12, register=register, n_paragraphs=4, with_frontmatter=False)
    with_url = base.replace("を", "を https://example.com/path/to/doc ", 5)
    without = base.replace("を", "を  ", 5)
    r_url = feat.record_from_text(with_url, ANALYZER)
    r_no = feat.record_from_text(without, ANALYZER)
    assert r_url["prose"]["n_masked_inline"] == 5
    assert _morph_view(r_url) == _morph_view(r_no)


def test_masked_tokens_are_flagged_and_excluded():
    text = "私は `git commit` を小さく保ちます。理由は、https://example.com/x に書きました。\n"
    segs = blocks_lib.prose_segments(blocks_lib.classify_text(text))
    sents = feat.build_sentences(segs, ANALYZER)
    assert len(sents) == 2
    for s in sents:
        assert s["masked"], s["text"]
        for ms, me in s["masked"]:
            assert s["text"][ms:me] == blocks_lib.PLACEHOLDER
        # 置換区間を除いた文字列にプレースホルダは残らない
        assert blocks_lib.PLACEHOLDER not in morph_lib.unmasked_text(s["text"], s["masked"])
    if MODE == "sudachi":
        masked = [t for s in sents for t in s["tokens"] if t.get("masked")]
        assert len(masked) == 2
        assert all(t["surface"] == blocks_lib.PLACEHOLDER for t in masked)
        kept = [t for s in sents for t in morph_lib.content_tokens(s["tokens"])]
        assert not any(t.get("masked") for t in kept)


def test_comma_positions_ignore_masked_spans():
    """読点相対位置はプレースホルダ区間を分母から除く(表層チャネルの不変性)。"""
    plain = "まず、この設定を先に決めます。"
    masked_text = "まず、この設定を" + blocks_lib.PLACEHOLDER + "先に決めます。"
    i = masked_text.index(blocks_lib.PLACEHOLDER)
    spans = [[i, i + len(blocks_lib.PLACEHOLDER)]]
    assert morph_lib.comma_positions(plain) == morph_lib.comma_positions(masked_text, spans)
    assert morph_lib.comma_positions(plain) != morph_lib.comma_positions(masked_text)


def test_raw_span_mapping_preserved_with_masking():
    text = "前置き。\n\nここで `x` を使います。**強調**もある。\n"
    segs = blocks_lib.prose_segments(blocks_lib.classify_text(text))
    sents = feat.build_sentences(segs, ANALYZER)
    s = next(s for s in sents if "強調" in s["text"])
    rs, re_ = s["raw_span"]
    assert text[rs:re_].startswith("強調") or "強調" in text[rs:re_]
    s0 = next(s for s in sents if blocks_lib.PLACEHOLDER in s["text"])
    rs0, re0 = s0["raw_span"]
    assert "`x`" in text[rs0:re0]  # raw には置換前のコードがある


@pytest.mark.skipif(MODE != "sudachi", reason="POS チャネルは sudachi 専用")
def test_placeholder_never_reaches_pos_channels():
    text = "この関数は `parse` を呼びます。返り値は `Result` です。\n"
    r = feat.record_from_text(text, ANALYZER)
    m = r["morph"]
    assert m["n_masked_tokens"] == 2
    for name in ("final_suffix2", "final_suffix3", "content_masked_lemma_bigram", "pre_comma_lemma"):
        d = m["dist"].get(name) or {}
        assert not any(blocks_lib.PLACEHOLDER in k for k in d), (name, d)
    # 文末が識別子で終わる文でも、suffix はプレースホルダを跨いで直前の語から取る
    text2 = "設定は `config` です。設定は `config`。\n"
    r2 = feat.record_from_text(text2, ANALYZER)
    keys = "".join(r2["morph"]["dist"]["final_suffix2"] or {})
    assert blocks_lib.PLACEHOLDER not in keys


def test_formal_noun_and_first_person_registry_contract():
    assert "気" not in morph_lib._FORMAL_NOUN_LEMMAS
    assert {"こと", "もの", "ため", "わけ", "はず"} <= morph_lib._FORMAL_NOUN_LEMMAS
    assert morph_lib.CHANNELS["first_person_top_share"]["max_severity"] == "warn"
    assert morph_lib.CHANNELS["first_person_lemma"]["kind"] == "dist"
    assert morph_lib.CHANNELS["hedge_class"]["max_severity"] == "warn"
    assert morph_lib.CHANNELS["hedge_rate"]["max_severity"] == "warn"
    assert morph_lib.CHANNEL_REGISTRY_VERSION == "2"


@pytest.mark.skipif(MODE != "sudachi", reason="sudachi 専用")
def test_first_person_top_share_is_author_neutral():
    r_ji = feat.record_from_text("自分は書く。自分は読む。自分は考える。\n", ANALYZER)
    r_wa = feat.record_from_text("私は書く。私は読む。私は考える。\n", ANALYZER)
    assert r_ji["morph"]["scalar"]["first_person_top_share"] == 1.0
    assert r_wa["morph"]["scalar"]["first_person_top_share"] == 1.0
    assert r_ji["morph"]["dist"]["first_person_lemma"] == {"自分": 1.0}
    assert r_wa["morph"]["dist"]["first_person_lemma"] == {"私": 1.0}
