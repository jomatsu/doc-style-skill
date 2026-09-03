"""lib モジュールの単体テスト(fallback モードで動くこと)。"""

import pytest

from lib import blocks as blocks_lib
from lib import morph as morph_lib
from lib import stats
from lib.features import (
    char_ngrams,
    classify_sentence_end,
    extract_article_features,
    feature_distance,
    jaccard,
    record_from_text,
    scalar_value,
)
from lib.tokenize import FallbackAnalyzer, get_analyzer, split_sentences

PROSE = (
    "小さな変更をこまめに残す習慣について書きます。私はコミットをできるだけ小さく保つようにしています。"
    "理由は単純です。あとから履歴を読み返すとき、変更の意図が分かりやすいからです。\n\n"
    "コミットメッセージは短い要約から始めます。本文には、なぜその変更が必要だったのかを書きます。"
    "何を変えたかはコードを見れば分かります。合成標識語を一度だけ置き、直後に実務的な説明へ戻ります。\n"
)

NOISE_TOP = "---\npublished_at: 2025-01-01\nstrata: tech\n---\n# 見出しは統計に入らない\n\n"
NOISE_MID = (
    "\n```python\nprint('これはコードです。文末も数えない。')\n```\n\n"
    "| 列 | 値 |\n|---|---|\n| あ | いです。 |\n\n"
    "https://example.com/standalone/url\n\n"
    "## 中見出しです。\n\n"
)


class TestProseContract:
    def _morph(self, text: str, analyzer) -> dict:
        record = record_from_text(text, analyzer)
        return record["morph"], record

    def test_noise_blocks_do_not_alter_morphology(self):
        """コード・表・見出し・単独行 URL を挿入しても形態素ブロック・文数は変わらない。"""
        analyzer = get_analyzer()
        para1, para2 = PROSE.split("\n\n")
        noisy = NOISE_TOP + para1 + "\n" + NOISE_MID + para2
        m_clean, r_clean = self._morph(PROSE, analyzer)
        m_noisy, r_noisy = self._morph(noisy, analyzer)
        assert r_clean["n_sents"] == r_noisy["n_sents"] == 8
        assert r_clean["n_chars"] == r_noisy["n_chars"]
        assert r_clean["sent_end_form"] == r_noisy["sent_end_form"]
        assert r_clean["script_ratio"] == r_noisy["script_ratio"]
        assert m_clean == m_noisy
        assert r_clean["prose"]["n_segments"] == r_noisy["prose"]["n_segments"] == 2

    def test_inline_code_is_deterministic_noun_placeholder(self):
        analyzer = get_analyzer()
        with_code = PROSE.replace("コミットメッセージ", "`git commit -m`", 1)
        blocks = blocks_lib.classify_text(with_code)
        segs = blocks_lib.prose_segments(blocks)
        joined = "\n".join(s["text"] for s in segs)
        assert "git commit" not in joined
        assert blocks_lib.PLACEHOLDER in joined
        assert sum(s["n_masked"] for s in segs) == 1
        record = record_from_text(with_code, analyzer)
        assert record["prose"]["n_masked_inline"] == 1
        # 置換は決定的: 同じ入力→同じ出力
        assert record == record_from_text(with_code, analyzer)
        # プレースホルダ区間は散文文字数に入らない
        base = record_from_text(PROSE, analyzer)
        assert record["n_chars"] < base["n_chars"]
        if analyzer.meta()["mode"] == "sudachi":
            toks = analyzer.tokenize(blocks_lib.PLACEHOLDER)
            assert len(toks) == 1 and toks[0]["pos"] == "名詞"

    def test_list_prose_kept_and_table_dropped(self):
        text = "- 一つ目の理由です。\n- 二つ目の理由です。\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        segs = blocks_lib.prose_segments(blocks_lib.classify_text(text))
        assert len(segs) == 1
        assert segs[0]["kind"] == "list"
        assert segs[0]["text"] == "一つ目の理由です。\n二つ目の理由です。"

    def test_raw_span_maps_back(self):
        text = "前置き。\n\nここで `x` を使います。**強調**もある。\n"
        segs = blocks_lib.prose_segments(blocks_lib.classify_text(text))
        seg = segs[1]
        i = seg["text"].index("強調")
        s, e = blocks_lib.raw_span(seg, i, i + 2)
        assert text[s:e] == "強調"

    def test_prose_text_for_copy_check_drops_inline_code(self):
        text = "```\ncode\n```\n\n本文 `secret_token` を含む。\n"
        prose = blocks_lib.prose_text(text)
        assert "secret_token" not in prose
        assert "code" not in prose
        assert blocks_lib.PLACEHOLDER not in prose


class TestMorph:
    def test_js_distance_bounds(self):
        assert morph_lib.js_distance({"a": 1.0}, {"a": 1.0}) == 0.0
        assert abs(morph_lib.js_distance({"a": 1.0}, {"b": 1.0}) - 1.0) < 1e-9

    def test_project_preserves_mass(self):
        p = morph_lib.project({"a": 0.5, "b": 0.3, "c": 0.2}, ["a"])
        assert p == {"a": 0.5, "OTHER": 0.5}

    def test_calibration_small_n_uses_max_and_margin(self):
        per = [(f"a{i}", {"x": 0.5 + 0.01 * i, "y": 0.5 - 0.01 * i}) for i in range(12)]
        ref = morph_lib.calibrate_dist_channel("comma_rel_pos", per)
        assert ref["status"] == "built"
        assert "author_max(small_n)" in ref["thresholds"]["fail_rule"]
        assert ref["thresholds"]["warn"] <= ref["thresholds"]["fail"]
        assert ref["thresholds"]["fail"] == max(ref["loao"]["distances"])  # 丸めなし
        vals = [(f"a{i}", 0.1 + 0.01 * i) for i in range(12)]
        sref = morph_lib.calibrate_scalar_channel("hedge_rate", vals)
        lo, hi = sref["thresholds"]["fail"]
        assert lo < sref["min"] and hi > sref["max"]
        assert "author_min(small_n)" in sref["thresholds"]["fail_rule"]
        assert "tukey_fence" in sref["thresholds"]["fail_rule"]

    def test_calibration_insufficient_n_skipped(self):
        ref = morph_lib.calibrate_dist_channel("comma_rel_pos", [("a", {"q1": 1.0})] * 3)
        assert ref["status"] == "skipped"

    def test_evaluate_caps_warn_channels(self):
        per = [(f"a{i}", {"q1": 0.5, "q2": 0.5}) for i in range(12)]
        ref = morph_lib.calibrate_dist_channel("comma_rel_pos", per)
        far = morph_lib.evaluate_dist("comma_rel_pos", {"q4": 1.0}, ref, 100)
        assert far["status"] == "warn"  # max_severity=warn は fail にならない
        assert far["distance"] > ref["thresholds"]["fail"]
        near = morph_lib.evaluate_dist("comma_rel_pos", {"q1": 0.5, "q2": 0.5}, ref, 100)
        assert near["status"] == "pass"
        low = morph_lib.evaluate_dist("comma_rel_pos", {"q4": 1.0}, ref, 1)
        assert low["status"] == "skipped" and low["reason"].startswith("insufficient_sample")

    def test_fallback_pos_channels_unavailable(self):
        record = record_from_text(PROSE, FallbackAnalyzer())
        m = record["morph"]
        assert m["available"] is False
        assert all(m["dist"][k] is None for k in ("pos_unigram", "final_suffix2"))
        assert m["dist"]["comma_rel_pos"] is not None
        assert m["scalar"]["hedge_rate"] is not None
        assert m["scalar"]["formal_noun_rate"] is None

    @pytest.mark.skipif(get_analyzer().meta()["mode"] != "sudachi", reason="sudachi 専用")
    def test_sudachi_channels_present(self):
        record = record_from_text(PROSE, get_analyzer())
        m = record["morph"]
        assert m["available"] is True
        assert m["n_tokens"] > 30
        for k in ("pos_unigram", "pos_bigram", "funcword_bigram", "final_suffix2", "aux_lemma"):
            assert m["dist"][k], k
            assert abs(sum(m["dist"][k].values()) - 1.0) < 1e-9
        assert m["scalar"]["formal_noun_rate"] is not None
        assert m["scalar"]["first_person_rate"] > 0  # 「私」「自分」を含む
        assert "です" in "".join(m["dist"]["final_suffix2"])


class TestSplitSentences:
    def test_basic(self):
        sents = split_sentences("今日は晴れです。明日は雨でしょうか？")
        assert [s["text"] for s in sents] == [
            "今日は晴れです。",
            "明日は雨でしょうか？",
        ]

    def test_offsets(self):
        text = "一文目。二文目です。"
        sents = split_sentences(text)
        for s in sents:
            assert text[s["char_start"] : s["char_end"]] == s["text"]

    def test_newline_split_and_closer(self):
        sents = split_sentences("見出しの行\n「そうです。」と言いました。")
        assert sents[0]["text"] == "見出しの行"
        assert sents[1]["text"] == "「そうです。」"


class TestClassifySentenceEnd:
    def test_desu_masu(self):
        assert classify_sentence_end("今日は晴れです。") == "desu_masu"
        assert classify_sentence_end("明日行きます。") == "desu_masu"
        assert classify_sentence_end("見つかりません。") == "desu_masu"

    def test_question(self):
        assert classify_sentence_end("行きますか。") == "question"
        assert classify_sentence_end("なぜでしょうか？") == "question"

    def test_da_dearu(self):
        assert classify_sentence_end("それが答えだ。") == "da_dearu"
        assert classify_sentence_end("重要である。") == "da_dearu"

    def test_taigen_fallback(self):
        assert classify_sentence_end("静けさは雨の日の特典。") == "taigen"


class TestStats:
    def test_bootstrap_ci_deterministic(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        a = stats.bootstrap_ci(vals, n=200, seed=42)
        b = stats.bootstrap_ci(vals, n=200, seed=42)
        assert a == b
        assert a["median"] == 3.0
        assert a["ci95"][0] <= a["median"] <= a["ci95"][1]

    def test_direction_agreement(self):
        high = [10.0, 11.0, 12.0, 13.0]
        assert stats.bootstrap_direction_agreement(high, [1.0], n=200, seed=1) == 1.0

    def test_cliffs_delta(self):
        assert stats.cliffs_delta([2, 3, 4], [1]) == 1.0
        assert stats.cliffs_delta([0], [1, 2]) == -1.0

    def test_loao_stable(self):
        assert stats.loao_stable(
            [10.0, 11.0, 12.0], lambda vs: stats.median(vs) > 5
        )
        assert not stats.loao_stable(
            [1.0, 100.0], lambda vs: stats.median(vs) > 50
        )

    def test_weighted_median(self):
        assert stats.weighted_median([1.0, 10.0], [1.0, 100.0]) == 10.0


class TestFeatures:
    def _blocks(self):
        return [
            {
                "type": "body",
                "text": "今日は晴れです。散歩に行きます。気分は上々。",
                "char_start": 0,
                "char_end": 22,
            },
            {"type": "code", "text": "print('x')", "char_start": 23, "char_end": 33},
            {
                "type": "body",
                "text": "明日はどうでしょうか？雨かもしれません。",
                "char_start": 34,
                "char_end": 54,
            },
        ]

    def test_fallback_record(self):
        record = extract_article_features(self._blocks(), FallbackAnalyzer())
        assert record["analyzer"]["mode"] == "fallback"
        assert record["n_sents"] == 5
        assert record["prose"]["n_segments"] == 2
        assert record["func_word_rate"] is None
        assert record["particle_bigram"] is None
        forms = record["sent_end_form"]
        assert abs(sum(forms.values()) - 1.0) < 1e-9
        assert forms["desu_masu"] > 0
        assert forms["question"] > 0
        ratios = record["script_ratio"]
        assert abs(sum(ratios.values()) - 1.0) < 1e-9
        # code ブロックは body 特徴に入らない → latin は句読点等以外ほぼ 0
        assert ratios["latin"] == 0.0

    def test_scalar_value(self):
        record = extract_article_features(self._blocks(), FallbackAnalyzer())
        assert scalar_value(record, "sent_len_median") == record["sent_len"]["median"]
        assert (
            scalar_value(record, "sent_end_form.desu_masu")
            == record["sent_end_form"]["desu_masu"]
        )
        assert scalar_value(record, "func_word_rate") is None

    def test_feature_distance(self):
        a = {"sent_end_form": {"desu_masu": 1.0}}
        b = {"sent_end_form": {"desu_masu": 1.0}}
        c = {"sent_end_form": {"da_dearu": 1.0}}
        assert feature_distance(a, b, ["sent_end_form"])["sent_end_form"] == 0.0
        assert feature_distance(a, c, ["sent_end_form"])["sent_end_form"] == 1.0
        assert feature_distance(a, {}, ["sent_end_form"])["sent_end_form"] is None

    def test_ngram_jaccard(self):
        a = char_ngrams("これはテストの文章です", 5)
        assert jaccard(a, a) == 1.0
        b = char_ngrams("まったく違う内容のもの", 5)
        assert jaccard(a, b) < 0.1


def test_get_analyzer_meta():
    analyzer = get_analyzer()
    meta = analyzer.meta()
    assert meta["mode"] in ("sudachi", "fallback")
    if meta["mode"] == "sudachi":
        tokens = analyzer.tokenize("今日は晴れです。")
        assert tokens and {"surface", "pos", "pos_detail", "base"} <= set(tokens[0])
    else:
        assert analyzer.tokenize("今日は晴れです。") == []
