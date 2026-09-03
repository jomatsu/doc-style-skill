"""intake → split → extract → stability の end-to-end スモークテスト。

fallback モード(sudachipy 不在)で必ず通ること。sudachipy が入っていれば
sudachi モードでも同じ契約で通る(POS 依存特徴が non-null になるだけ)。
"""

import json
import shutil
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR, run_script
from lib import morph as morph_lib

AUTHOR_ID = "synth-author"
CONSENT = "synthetic-consent-token"
DUP_PAIRS = [["a003", "a014"], ["a017", "a025"]]

FEATURE_RECORD_KEYS = {
    "article_id",
    "feature_schema",
    "analyzer",
    "n_sents",
    "n_chars",
    "sent_len",
    "para_len",
    "comma_per_sent",
    "sent_end_form",
    "max_consecutive_same_ending",
    "script_ratio",
    "func_word_rate",
    "particle_bigram",
    "pos_bigram",
    "aux_verb_dist",
    "ttr_window",
    "distinct_2",
    "prose",
    "morph",
}

SENT_END_FORMS = {
    "desu_masu",
    "da_dearu",
    "jotai_verb",
    "jotai_adj",
    "taigen",
    "question",
    "other",
}

CLAIM_KEYS = {
    "claim_id",
    "category",
    "scope_mode",
    "condition",
    "rule_text",
    "feature",
    "value",
    "evidence",
    "support",
    "control_result",
    "state",
    "status",
    "compilation_target",
    "rights_scope",
    "confidence",
    "version",
}


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> Path:
    """全段を実行済みの workspace(モジュール内で共有)。"""
    ws = tmp_path_factory.mktemp("ws") / AUTHOR_ID
    for name, args in [
        (
            "corpus_intake.py",
            ["--input", FIXTURES_DIR, "--author-id", AUTHOR_ID, "--consent", CONSENT],
        ),
        ("corpus_split.py", ["--ratio", "70,15,15"]),
        ("extract_features.py", ["--split", "train+dev", "--seed", "42"]),
        ("stability_test.py", ["--seed", "42"]),
    ]:
        result = run_script(name, "--workspace", ws, *args)
        assert result.returncode == 0, f"{name} failed: {result.stderr}"
    return ws


def _calibration_ids(ws: Path) -> list[str]:
    """train+dev の正準記事(転載クラスタの非正準は除く)。"""
    splits = _load(ws / "splits.json")
    manifest = _load(ws / "manifest.json")
    canon = {a["article_id"] for a in manifest["articles"] if not a.get("dup_of")}
    return sorted(i for i in splits["train"] + splits["dev"] if i in canon)


class TestIntake:
    def test_manifest(self, workspace):
        manifest = _load(workspace / "manifest.json")
        assert manifest["author_id"] == AUTHOR_ID
        assert manifest["consent"]["record"] == CONSENT
        assert manifest["consent"]["evidence_level"] == "user_reported"
        assert manifest["consent"]["verification_status"] == "reported_unverified"
        assert len(manifest["articles"]) == 25
        for a in manifest["articles"]:
            assert a["status"] == "eligible"
            assert a["authorship"] == "subject-authored"
            assert a["strata"] in ("tech", "essay")
            assert a["published_at"].startswith(("2024", "2025"))
            assert 400 <= a["char_count"] <= 800
            assert len(a["content_hash"]) == 64
            assert a["block_health"]["status"] in ("pass", "warn")
            assert "code_like_line_ratio" in a["block_health"]["metrics"]

    def test_raw_and_clean(self, workspace):
        assert len(list((workspace / "raw").glob("*.txt"))) == 25
        assert len(list((workspace / "clean").glob("*.json"))) == 25
        clean = _load(workspace / "clean" / "a002.json")
        types = {b["type"] for b in clean["blocks"]}
        assert "body" in types
        assert "code" in types  # a002 はコードフェンスを含む
        raw = (workspace / "raw" / "a002.txt").read_text(encoding="utf-8")
        for b in clean["blocks"]:
            assert raw[b["char_start"] : b["char_end"]].strip() == b["text"].strip()

    def test_quote_block(self, workspace):
        clean = _load(workspace / "clean" / "a006.json")
        assert any(b["type"] == "quote" for b in clean["blocks"])

    def test_dup_clusters(self, workspace):
        manifest = _load(workspace / "manifest.json")
        assert manifest["dup_clusters"] == DUP_PAIRS
        by_id = {a["article_id"]: a for a in manifest["articles"]}
        assert by_id["a014"]["dup_of"] == "a003"
        assert by_id["a025"]["dup_of"] == "a017"
        assert by_id["a003"]["dup_of"] is None

    def test_indented_code_is_separated_and_health_checked(self, tmp_path):
        source = tmp_path / "indented.md"
        source.write_text(
            "---\npublished_at: 2025-01-01\nstrata: tech\n---\n"
            "本文の説明です。\n\n"
            "    const value = 1;\n"
            "    console.log(value);\n",
            encoding="utf-8",
        )
        ws = tmp_path / "indented-ws"
        result = run_script(
            "corpus_intake.py",
            "--workspace",
            ws,
            "--input",
            source,
            "--author-id",
            AUTHOR_ID,
            "--consent",
            CONSENT,
            "--consent-level",
            "direct_record",
        )
        assert result.returncode == 0, result.stderr
        clean = _load(ws / "clean" / "indented.json")
        assert any(b["type"] == "code" for b in clean["blocks"])
        body = "\n".join(b["text"] for b in clean["blocks"] if b["type"] == "body")
        assert "const value" not in body
        manifest = _load(ws / "manifest.json")
        assert manifest["consent"]["evidence_level"] == "direct_record"
        assert manifest["articles"][0]["block_health"]["status"] == "pass"

    def test_malformed_nested_fence_keeps_block_health(self, tmp_path):
        """html 変換由来の不正ネストフェンス(外側と同長の ```json)を回復し、
        body にコードが漏れず health=pass になる。"""
        source = tmp_path / "nested.md"
        source.write_text(
            "---\npublished_at: 2025-01-01\nstrata: tech\n---\n"
            "設定例を示します。\n\n"
            "```\n"
            "```json\n"
            '{"name": "demo", "value": 1}\n'
            "```\n"
            "```\n\n"
            "この設定で問題なく動きます。次に実行手順を見ます。\n",
            encoding="utf-8",
        )
        ws = tmp_path / "nested-ws"
        result = run_script(
            "corpus_intake.py", "--workspace", ws, "--input", source,
            "--author-id", AUTHOR_ID, "--consent", CONSENT,
        )
        assert result.returncode == 0, result.stderr
        clean = _load(ws / "clean" / "nested.json")
        body = "\n".join(b["text"] for b in clean["blocks"] if b["type"] == "body")
        assert "demo" not in body
        assert "```" not in body
        assert "問題なく動きます" in body
        manifest = _load(ws / "manifest.json")
        assert manifest["articles"][0]["block_health"]["status"] == "pass"
        assert manifest["articles"][0]["status"] == "eligible"

    def test_no_consent_quarantines(self, tmp_path):
        ws = tmp_path / "noconsent"
        result = run_script(
            "corpus_intake.py",
            "--workspace",
            ws,
            "--input",
            FIXTURES_DIR / "a001.md",
            "--author-id",
            AUTHOR_ID,
        )
        assert result.returncode == 0
        assert "quarantined" in result.stderr
        manifest = _load(ws / "manifest.json")
        assert manifest["articles"][0]["status"] == "quarantined"


class TestSplit:
    def test_partition(self, workspace):
        splits = _load(workspace / "splits.json")
        all_ids = splits["train"] + splits["dev"] + splits["holdout"]
        assert len(all_ids) == 25
        assert len(set(all_ids)) == 25
        assert splits["train"] and splits["dev"] and splits["holdout"]
        assert splits["leak_check"]["passed"] is True
        assert splits["leak_check"]["ngram"] == 8

    def test_dup_cluster_same_split(self, workspace):
        splits = _load(workspace / "splits.json")
        where = {
            aid: name
            for name in ("train", "dev", "holdout")
            for aid in splits[name]
        }
        for a, b in DUP_PAIRS:
            assert where[a] == where[b]

    def test_time_order(self, workspace):
        """時系列順: train(古)→ dev → holdout(新)。

        転載クラスタは最古メンバーの日付で配置されるため、
        正準記事(dup_of なし)のみで順序を検証する。
        """
        splits = _load(workspace / "splits.json")
        manifest = _load(workspace / "manifest.json")
        canon = {
            a["article_id"]: a["published_at"]
            for a in manifest["articles"]
            if not a.get("dup_of")
        }
        train = [canon[i] for i in splits["train"] if i in canon]
        dev = [canon[i] for i in splits["dev"] if i in canon]
        holdout = [canon[i] for i in splits["holdout"] if i in canon]
        assert max(train) <= min(dev)
        assert max(dev) <= min(holdout)


class TestExtract:
    def test_feature_records(self, workspace):
        # train+dev の正準記事のみ(holdout と転載の非正準は除外)+ _aggregate.json
        expected = _calibration_ids(workspace)
        feature_files = sorted((workspace / "features").glob("*.json"))
        names = [p.stem for p in feature_files]
        assert "_aggregate" in names
        assert sorted(n for n in names if n != "_aggregate") == expected
        assert "a014" not in names and "a025" not in names
        record = _load(workspace / "features" / f"{expected[0]}.json")
        assert set(record) == FEATURE_RECORD_KEYS
        assert record["analyzer"]["mode"] in ("sudachi", "fallback")
        assert record["n_sents"] > 0
        assert 400 <= record["n_chars"] <= 800
        forms = record["sent_end_form"]
        assert set(forms) == SENT_END_FORMS
        assert forms["desu_masu"] > 0.3  # 合成コーパスはです・ます中心
        assert record["prose"]["n_segments"] > 0
        assert record["feature_schema"] == "2"
        morph = record["morph"]
        assert set(morph) == {"available", "n_tokens", "n_masked_tokens", "n_sents", "dist", "scalar", "sample"}
        if record["analyzer"]["mode"] == "fallback":
            assert record["func_word_rate"] is None
            assert record["pos_bigram"] is None
            assert morph["available"] is False
            assert morph["dist"]["pos_unigram"] is None
            assert morph["scalar"]["hedge_rate"] is not None  # 表層チャネルは計算される
        else:
            assert record["func_word_rate"] is not None
            assert morph["available"] is True
            assert morph["dist"]["pos_unigram"]
            assert morph["dist"]["final_suffix2"]

    def test_aggregate(self, workspace):
        agg = _load(workspace / "features" / "_aggregate.json")
        expected = _calibration_ids(workspace)
        assert agg["n_articles"] == len(expected)
        assert agg["split"] == "train+dev"
        assert agg["calibration_split"] == "train+dev"
        assert agg["seed"] == 42
        key = "sent_len_median"
        entry = agg["features"][key]
        assert len(entry["per_article"]) == len(expected)
        for weighting in ("equal_article", "equal_char"):
            block = entry[weighting]
            assert block["ci95"][0] <= block["median"] <= block["ci95"][1]
        assert "iqr" in entry["equal_article"]
        strata = {a["strata"] for a in agg["articles"]}
        assert strata == {"tech", "essay"}

    def test_aggregate_morphology_calibration(self, workspace):
        agg = _load(workspace / "features" / "_aggregate.json")
        assert agg["feature_schema"] == "2"
        assert agg["channel_registry_version"] == "2"
        m = agg["morphology"]
        assert m["channel_registry_version"] == "2"
        assert m["calibration_rule"]["policy"]["article_alpha"] == 0.01
        channels = m["channels"]
        # 表層チャネルは解析器に依らず較正される(合成コーパスは読点を十分に含む)
        assert channels["comma_rel_pos"]["status"] == "built"
        ref = channels["comma_rel_pos"]
        assert ref["thresholds"]["warn"] <= ref["thresholds"]["fail"]
        assert abs(sum(ref["centroid"].values()) - 1.0) < 1e-6
        assert "OTHER" in ref["centroid"]
        assert ref["loao"]["distances"] == sorted(ref["loao"]["distances"])
        # min_sample を満たす記事だけが較正に入る
        assert morph_lib.MIN_CALIBRATION_N <= ref["n_articles"] <= agg["n_articles"]
        assert "article_ids" not in ref
        if agg["analyzer"]["mode"] == "sudachi":
            assert channels["pos_unigram"]["status"] == "built"
            assert channels["final_suffix2"]["status"] == "built"
            scalar = channels["formal_noun_rate"]
            assert scalar["status"] == "built"
            lo, hi = scalar["thresholds"]["fail"]
            assert lo <= scalar["min"] and hi >= scalar["max"]
            assert "first_person_top_share" in channels and "first_person_lemma" in channels
        else:
            assert channels["pos_unigram"]["status"] == "skipped"
            assert m["available"] is False
        # 条件付き参照は N に応じて built / shrunk / skipped のいずれか
        for key, entry in m["conditional"].items():
            assert entry["status"] in ("built", "shrunk", "skipped"), key
            if entry["status"] != "skipped":
                assert "channels" in entry

    def test_extract_all_split_is_warned(self, workspace, tmp_path):
        """--split all は動くが、holdout 混入を警告し aggregate に split=all を残す。"""
        ws = tmp_path / "all-ws"
        shutil.copytree(workspace, ws)
        result = run_script(
            "extract_features.py", "--workspace", ws, "--split", "all", "--seed", "42"
        )
        assert result.returncode == 0, result.stderr
        assert "holdout" in result.stderr
        agg = _load(ws / "features" / "_aggregate.json")
        assert agg["split"] == "all"
        assert agg["n_articles"] == 23


class TestStability:
    def test_candidates(self, workspace):
        out = _load(workspace / "profile-candidates.json")
        assert out["author_id"] == AUTHOR_ID
        assert out["seed"] == 42
        assert out["candidates"], "候補 claim が 1 件も生成されていない"
        for claim in out["candidates"]:
            assert CLAIM_KEYS <= set(claim)
            assert claim["state"] == "observed"
            assert claim["status"] in ("core", "mode_specific", "local", "ambiguous")
            assert claim["control_result"]["masking"] == "not_run"
            assert claim["control_result"]["cross_topic"] == "not_run"
            assert claim["control_result"]["loao"] in ("pass", "fail")
            assert claim["evidence"], "evidence span は最低 1 つ必須"
            span = claim["evidence"][0]
            assert {"article_id", "char_start", "char_end"} <= set(span)
            assert claim["rights_scope"] == CONSENT

    def test_desu_masu_is_core(self, workspace):
        # です・ます中心・文短めのコーパスなので、この 2 つは core になるはず
        out = _load(workspace / "profile-candidates.json")
        by_metric = {c["feature"]["metric"]: c for c in out["candidates"]}
        assert by_metric["sent_end_form.desu_masu"]["status"] == "core"
        assert by_metric["sent_end_form.desu_masu"]["value"]["median"] > 0.4
        assert by_metric["sent_len_median"]["value"]["median"] < 45

    def test_profile_json_not_written(self, workspace):
        assert not (workspace / "profile.json").exists()


class TestDeterminism:
    def test_same_seed_same_output(self, workspace):
        """extract → stability を同 seed で再実行しても出力が一致する。"""
        agg_path = workspace / "features" / "_aggregate.json"
        cand_path = workspace / "profile-candidates.json"
        before = (agg_path.read_bytes(), cand_path.read_bytes())
        for name, args in [
            ("extract_features.py", ["--split", "train+dev", "--seed", "42"]),
            ("stability_test.py", ["--seed", "42"]),
        ]:
            result = run_script(name, "--workspace", workspace, *args)
            assert result.returncode == 0, result.stderr
        after = (agg_path.read_bytes(), cand_path.read_bytes())
        assert before == after


class TestProseContract:
    """extract_features(clean blocks)と style_lint(raw テキスト)が同じ FeatureRecord を作る。"""

    def test_same_prose_same_record(self, workspace):
        from lib import features as feat
        from lib.tokenize import get_analyzer

        analyzer = get_analyzer()
        for aid in ("a001", "a002", "a006"):
            raw = (workspace / "raw" / f"{aid}.txt").read_text(encoding="utf-8")
            clean = _load(workspace / "clean" / f"{aid}.json")
            from_clean = feat.extract_article_features(clean["blocks"], analyzer)
            from_raw = feat.record_from_text(raw, analyzer)
            assert from_clean == from_raw, aid
            if aid in _calibration_ids(workspace):
                saved = _load(workspace / "features" / f"{aid}.json")
                saved.pop("article_id")
                from_clean["article_id"] = None
                saved["article_id"] = None
                assert saved == json.loads(json.dumps(from_clean)), aid
