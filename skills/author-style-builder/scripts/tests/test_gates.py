"""合成コーパス(register / 長さ / リスト密度 / n>=100)での end-to-end ゲート検証。

- 較正記事はどのゲートでも hard fail しない(丸め・境界一致・短文で fail しない)
- G5 の真実性(markers 空は skipped、skill_lint が production で fail)
- 解析器不一致(Sudachi 較正のスキルを fallback で実行)は G2 degraded / POS 系 skipped
- stale claim(aggregate で再現できない数値)は compile exit 2、escape hatch は migration 印
- 名目上の validator 写像(評価されない metric)は exit 2
- n >= 100 の Bonferroni ポリシーが記事レベルで守られる
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import synth  # noqa: E402
from conftest import run_script  # noqa: E402
from lib import calibration as calib  # noqa: E402
from lib import morph as morph_lib  # noqa: E402
from lib.tokenize import get_analyzer  # noqa: E402
from test_compile_lint import NOW, _load, _manual_claim  # noqa: E402

MODE = get_analyzer().meta()["mode"]
CONSENT = "synthetic-consent-token"

CORPORA = {
    "polite_short": dict(n=24, register="desu_masu", n_paragraphs=(2, 3)),
    "jotai_mid_long": dict(n=24, register="jotai", n_paragraphs=(30, 60)),
    "mixed_list": dict(n=24, register="mixed", n_paragraphs=(4, 8), list_heavy=True, inline_code=0.25),
    "big": dict(n=120, register="desu_masu", n_paragraphs=(3, 6), seed=99),
}


def _write_corpus(root: Path, name: str) -> Path:
    spec = dict(CORPORA[name])
    n = spec.pop("n")
    seed = spec.pop("seed", 7)
    d = root / f"input-{name}"
    d.mkdir(parents=True, exist_ok=True)
    for aid, md in synth.corpus(n, seed=seed, **spec):
        (d / f"{aid}.md").write_text(md, encoding="utf-8")
    return d


def _first_span(ws: Path) -> list:
    manifest = _load(ws / "manifest.json")
    aid = manifest["articles"][0]["article_id"]
    clean = _load(ws / "clean" / f"{aid}.json")
    b = next(b for b in clean["blocks"] if b["type"] == "body")
    return [{"article_id": aid, "char_start": b["char_start"], "char_end": min(b["char_end"], b["char_start"] + 30)}]


def _profile(ws: Path, *, markers: bool = True, exploratory: bool = False) -> dict:
    cands = _load(ws / "profile-candidates.json")["candidates"]
    agg = _load(ws / "features" / "_aggregate.json")
    claims = []
    for c in [c for c in cands if c["status"] == "core"][:3]:
        c = dict(c)
        c["compilation_target"] = "always_on_rule"
        claims.append(c)
    ev = _first_span(ws)
    if markers:
        claims.append(
            _manual_claim(
                "caricature-markers-001", category="語彙", scope_mode="core", condition=None,
                rule_text="口癖を短い範囲で反復しない", status="core", target="validator",
                evidence=ev, metric="caricature_markers", value={"markers": ["合成標識語"]},
            )
        )
    profile = {
        "author_id": "synth-author",
        "author_name": "合成著者",
        "version": "1.0.0",
        "feature_schema": agg["feature_schema"],
        "claims": claims,
    }
    if exploratory:
        profile["profile_class"] = "exploratory"
        profile["approval"] = {"decided_by": "synthetic-reviewer", "decided_at": "2000-01-01", "decisions": ["x"]}
    return profile


def _build_workspace(tmp: Path, name: str) -> Path:
    inp = _write_corpus(tmp, name)
    ws = tmp / f"ws-{name}"
    for script, args in [
        ("corpus_intake.py", ["--input", inp, "--author-id", "synth-author", "--consent", CONSENT]),
        ("corpus_split.py", ["--ratio", "70,15,15"]),
        ("extract_features.py", ["--split", "train+dev", "--seed", "42", "--bootstrap-n", "200"]),
        ("stability_test.py", ["--seed", "42", "--bootstrap-n", "200"]),
    ]:
        r = run_script(script, "--workspace", ws, *args)
        assert r.returncode == 0, f"{script}: {r.stderr}"
    (ws / "profile.json").write_text(json.dumps(_profile(ws), ensure_ascii=False, indent=2), encoding="utf-8")
    return ws


def _compile(ws: Path, out: Path, *extra) -> "subprocess.CompletedProcess":
    return run_script("compile_skill.py", "--workspace", ws, "--out", out, "--now", NOW, *extra)


def _calibration_ids(ws: Path) -> list[str]:
    splits = _load(ws / "splits.json")
    return sorted(splits["train"] + splits["dev"])


def _raw(ws: Path, aid: str) -> str:
    return (ws / "raw" / f"{aid}.txt").read_text(encoding="utf-8")


def _lint(skill: Path, text_path: Path, *extra, env=None) -> dict:
    import subprocess

    cmd = [sys.executable, str(Path(__file__).resolve().parent.parent / "style_lint.py"),
           "--config", str(skill / "lint-config.json"), "--text", str(text_path), *map(str, extra)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert r.returncode in (0, 2), r.stderr
    return json.loads(r.stdout)


@pytest.fixture(scope="module", params=list(CORPORA))
def built(request, tmp_path_factory):
    name = request.param
    tmp = tmp_path_factory.mktemp(name)
    ws = _build_workspace(tmp, name)
    out = tmp / "skill"
    r = _compile(ws, out)
    assert r.returncode == 0, r.stderr
    return name, ws, out


class TestCalibrationArticlesNeverHardFail:
    def test_config_shape(self, built):
        name, ws, skill = built
        cfg = _load(skill / "lint-config.json")
        g1 = cfg["gates"]["G1_distribution"]
        assert g1["sent_len_chars"]["median_hard_range"][0] is not None
        assert g1["sent_len_chars"]["max_hard"] is not None
        assert g1["min_sents"] == 10
        g2 = cfg["gates"]["G2_sentence_end"]
        assert set(g2["form_distribution"]) == {"desu_masu", "da_dearu", "jotai_verb", "jotai_adj", "taigen", "question"}
        assert set(g2["form_hard_range"]) == set(g2["form_distribution"])
        g3 = cfg["gates"]["G3_orthography"]
        assert g3["latin_hard_range"][0] is not None and g3["hiragana_ratio"][0] is not None
        assert cfg["calibration"]["analyzer_meta"]["mode"] == MODE
        assert cfg["calibration"]["feature_schema"] == "2"
        assert cfg["calibration"]["policy"]["article_alpha"] == 0.01
        assert cfg["builder_status"] == "experimental"
        assert isinstance(cfg["calibration"]["warnings"], list)
        assert cfg["gates"]["G5_caricature"]["markers"] == ["合成標識語"]
        agg = _load(ws / "features" / "_aggregate.json")
        n = agg["n_articles"]
        if n >= calib.LARGE_N:
            assert "bonferroni" in g1["sent_len_chars"]["max_rule"]
        else:
            assert "author_max" in g1["sent_len_chars"]["max_rule"]

    def test_all_calibration_articles_pass_every_gate(self, built, tmp_path):
        name, ws, skill = built
        ids = _calibration_ids(ws)
        per_channel_fails: dict[str, int] = {}
        article_fail = 0
        for aid in ids:
            f = tmp_path / f"{aid}.txt"
            f.write_text(_raw(ws, aid), encoding="utf-8")
            rep = _lint(skill, f)
            # G5 は人間設定の cap(較正ゲートではない)、G6 は skipped。不変条件は較正ゲートに限る
            failed = {g: v for g, v in rep["gates"].items() if v["status"] == "fail" and g not in ("G5", "G6")}
            for ch, res in rep["gates"]["G7"].get("channels", {}).items():
                if res["status"] == "fail":
                    per_channel_fails[ch] = per_channel_fails.get(ch, 0) + 1
            if failed:
                article_fail += 1
            assert rep["gates"]["G6"]["status"] == "skipped"
            assert rep["gates"]["G5"]["status"] in ("pass", "fail")
            for g in ("G1", "G2", "G3", "G4"):
                assert rep["gates"][g]["status"] != "fail", (aid, g, rep["gates"][g])
        n = len(ids)
        if n < calib.LARGE_N:
            assert article_fail == 0
            assert per_channel_fails == {}
        else:
            alpha = calib.per_check_alpha()
            for ch, k in per_channel_fails.items():
                two_sided = morph_lib.CHANNELS[ch]["kind"] == "scalar"
                allowed = (2 if two_sided else 1) * calib.max_exceed_count(n, alpha / (2 if two_sided else 1))
                assert k <= allowed, (ch, k, allowed)
            bound = sum(
                (2 if morph_lib.CHANNELS[c]["kind"] == "scalar" else 1)
                * calib.max_exceed_count(n, alpha / (2 if morph_lib.CHANNELS[c]["kind"] == "scalar" else 1))
                for c in morph_lib.FAIL_CAPABLE_CHANNELS
            ) + len(calib.LEGACY_FAIL_CAPABLE_CHECKS) * 2 * calib.max_exceed_count(n, alpha / 2)
            assert article_fail <= bound

    def test_register_and_length_reported(self, built, tmp_path):
        name, ws, skill = built
        agg = _load(ws / "features" / "_aggregate.json")
        regs = {a["register"] for a in agg["articles"]}
        lens = {a["length_stratum"] for a in agg["articles"]}
        if name == "jotai_mid_long":
            assert "jotai" in regs
            assert {"medium", "long"} <= lens
            cond = agg["morphology"]["conditional"]
            assert cond["register:jotai"]["status"] in ("built", "shrunk")
        if name == "polite_short":
            assert regs == {"desu_masu"} or "desu_masu" in regs
            assert "short" in lens
        if name == "mixed_list":
            rec = _load(ws / "features" / f"{_calibration_ids(ws)[0]}.json")
            assert rec["prose"]["n_list_segments"] > 0
            assert rec["prose"]["n_masked_inline"] > 0

    def test_skill_lint_production_passes(self, built):
        name, ws, skill = built
        r = run_script("skill_lint.py", "--skill", skill, "--workspace", ws, "--no-runner-smoke")
        rep = json.loads(r.stdout)
        assert r.returncode == 0, json.dumps(rep["checks"], ensure_ascii=False)[:2000]
        assert rep["checks"]["g5_markers"]["status"] == "pass"
        assert rep["checks"]["migration"]["status"] == "pass"
        assert rep["checks"]["claim_drift"]["status"] in ("pass", "warn")
        assert rep["checks"]["schema"]["status"] == "pass"


@pytest.fixture(scope="module")
def polite(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("polite")
    ws = _build_workspace(tmp, "polite_short")
    out = tmp / "skill"
    assert _compile(ws, out).returncode == 0
    return tmp, ws, out


class TestG5Truthfulness:
    def test_no_markers_is_skipped_and_lint_flags(self, polite):
        tmp, ws, _ = polite
        for exploratory in (False, True):
            p = tmp / f"profile-nomark-{exploratory}.json"
            p.write_text(json.dumps(_profile(ws, markers=False, exploratory=exploratory), ensure_ascii=False), encoding="utf-8")
            out = tmp / f"skill-nomark-{exploratory}"
            r = run_script("compile_skill.py", "--workspace", ws, "--profile", p, "--out", out, "--now", NOW)
            assert r.returncode == 0, r.stderr
            assert "markers が空" in r.stderr
            cfg = _load(out / "lint-config.json")
            assert cfg["gates"]["G5_caricature"]["markers"] == []
            assert cfg["gates"]["G5_caricature"]["configured"] is False
            f = tmp / "t.txt"
            f.write_text("合成標識語を書きます。合成標識語を確認します。合成標識語を残します。", encoding="utf-8")
            rep = _lint(out, f)
            assert rep["gates"]["G5"]["status"] == "skipped"
            assert rep["gates"]["G5"]["reason"] == "no_markers"
            r = run_script("skill_lint.py", "--skill", out, "--profile", p, "--no-runner-smoke")
            rep = json.loads(r.stdout)
            assert rep["checks"]["g5_markers"]["status"] == ("warn" if exploratory else "fail")
            assert r.returncode == (0 if exploratory else 2)

    def test_marker_repetition_fails(self, polite, tmp_path):
        _, ws, skill = polite
        f = tmp_path / "rep.txt"
        f.write_text("合成標識語を書きます。合成標識語を確認します。合成標識語を残します。", encoding="utf-8")
        rep = _lint(skill, f)
        assert rep["gates"]["G5"]["status"] == "fail"
        f2 = tmp_path / "ok.txt"
        f2.write_text("合成標識語を書きます。確認します。残します。", encoding="utf-8")
        assert _lint(skill, f2)["gates"]["G5"]["status"] == "pass"


class TestShortInputDegrades:
    def test_four_sentence_article_does_not_hard_fail_distribution_gates(self, polite, tmp_path):
        _, ws, skill = polite
        f = tmp_path / "four.txt"
        # 4 文・常体のみ(です・ます著者の分布から大きく外れる)
        f.write_text("設定を決めた。理由を書いた。範囲は変えない。結果を見直す。", encoding="utf-8")
        rep = _lint(skill, f)
        for g in ("G1", "G2", "G3"):
            assert rep["gates"][g]["status"] != "fail", (g, rep["gates"][g])
        assert any(d.startswith("insufficient_sents") for d in rep["gates"]["G2"].get("degraded", []))
        assert any(d.startswith("insufficient_chars") for d in rep["gates"]["G3"].get("degraded", []))

    def test_long_violating_text_still_fails(self, polite, tmp_path):
        _, ws, skill = polite
        cfg = _load(skill / "lint-config.json")
        hard = cfg["gates"]["G2_sentence_end"]["max_consecutive_hard_cap"]
        clause = "この設定はとても重要でありなおかつ非常に複雑であって" * 6
        f = tmp_path / "bad.txt"
        f.write_text("".join(f"{clause}という結論になるのです。" for _ in range(max(hard + 2, 12))), encoding="utf-8")
        rep = _lint(skill, f)
        assert rep["gates"]["G1"]["status"] == "fail"
        assert rep["gates"]["G2"]["status"] == "fail"


class TestAnalyzerCompat:
    def test_mode_mismatch_degrades_g2_and_skips_pos(self, polite, tmp_path):
        tmp, ws, skill = polite
        f = tmp_path / "t.txt"
        # 表層チャネルも互換性テストできるよう、min_sample を十分満たす読点を含める。
        f.write_text(
            "".join("まず、この設定を確認し、結果を記録します。" for _ in range(20)),
            encoding="utf-8",
        )
        if MODE == "sudachi":
            env = dict(os.environ, DOC_STYLE_ANALYZER="fallback")
            rep = _lint(skill, f, env=env)
            assert rep["analyzer_compat"]["status"] == "mode_mismatch"
            assert rep["analyzer_compat"]["calibrated_mode"] == "sudachi"
            assert rep["analyzer_compat"]["runtime_mode"] == "fallback"
        else:
            # fallback 環境: Sudachi 較正の config を模す
            broken = tmp / "skill-sudachi-cal"
            if broken.exists():
                shutil.rmtree(broken)
            shutil.copytree(skill, broken)
            cfg_path = broken / "lint-config.json"
            cfg = _load(cfg_path)
            cfg["calibration"]["analyzer_meta"] = {"mode": "sudachi", "version": "synthetic", "dict": "synthetic", "split_mode": "C"}
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            rep = _lint(broken, f)
            assert rep["analyzer_compat"]["status"] == "mode_mismatch"
        g2 = rep["gates"]["G2"]
        assert g2["status"] != "fail"
        assert g2.get("reason") == "analyzer_mode_mismatch"
        assert any(d.startswith("analyzer_mode_mismatch") for d in g2.get("degraded", []))
        assert rep["gates"]["G4"]["status"] == "skipped"
        g7 = rep["gates"]["G7"]
        for name, ch in g7["channels"].items():
            if morph_lib.CHANNELS[name]["requires"] == "sudachi":
                assert ch["status"] == "skipped"
                assert ch["reason"].startswith("analyzer_mode_mismatch") or ch["reason"].startswith("analyzer_fallback") or "reference" in ch["reason"]
        # 較正参照が built の表層チャネルは analyzer mismatch でも維持する。
        # comma_rel_pos は短文コーパスでは参照自体が疎になるため hedge_rate を使う。
        assert g7["channels"]["hedge_rate"]["status"] != "skipped"

    def test_match_when_same_mode(self, polite, tmp_path):
        _, ws, skill = polite
        f = tmp_path / "t.txt"
        f.write_text(_raw(ws, _calibration_ids(ws)[1]), encoding="utf-8")
        rep = _lint(skill, f)
        assert rep["analyzer_compat"]["status"] == "match"
        assert "degraded" not in rep["gates"]["G2"] or not any(
            d.startswith("analyzer") for d in rep["gates"]["G2"]["degraded"]
        )


class TestStaleProfile:
    def _stale_profile(self, ws: Path) -> dict:
        profile = _profile(ws)
        ev = _first_span(ws)
        profile["claims"].append(
            _manual_claim(
                "stale-sent-len-001", category="文", scope_mode="core", condition=None,
                rule_text="1 文は 41 字が中心", status="core", target="always_on_rule",
                evidence=ev, metric="sent_len_median",
                value={"median": 410.0, "range": [400.0, 420.0]},
            )
        )
        profile["claims"].append(
            _manual_claim(
                "stale-mean-001", category="文", scope_mode="core", condition=None,
                rule_text="読点は平均 999", status="ambiguous", target="validator",
                evidence=ev, metric="comma_per_sent_mean",
                value={"median": 999.0, "range": [900.0, 1100.0]},
            )
        )
        return profile

    def test_stale_claims_rejected_by_default(self, polite):
        tmp, ws, _ = polite
        p = tmp / "profile-stale.json"
        p.write_text(json.dumps(self._stale_profile(ws), ensure_ascii=False), encoding="utf-8")
        out = tmp / "skill-stale"
        r = run_script("compile_skill.py", "--workspace", ws, "--profile", p, "--out", out, "--now", NOW)
        assert r.returncode == 2, r.stderr
        assert "stale-sent-len-001" in r.stderr and "value_drift" in r.stderr
        assert "stale-mean-001" in r.stderr and "metric_not_in_schema" in r.stderr
        assert "--allow-stale-claims" in r.stderr
        assert not (out / "SKILL.md").exists()

    def test_escape_hatch_marks_migration_and_lint_rejects_release(self, polite):
        tmp, ws, _ = polite
        p = tmp / "profile-stale2.json"
        p.write_text(json.dumps(self._stale_profile(ws), ensure_ascii=False), encoding="utf-8")
        out = tmp / "skill-stale-allowed"
        r = run_script("compile_skill.py", "--workspace", ws, "--profile", p, "--out", out, "--now", NOW, "--allow-stale-claims")
        assert r.returncode == 0, r.stderr
        prov = _load(out / "meta" / "provenance.json")
        assert prov["migration"]["not_for_release"] is True
        assert {d["claim_id"] for d in prov["migration"]["stale_claims"]} == {"stale-sent-len-001", "stale-mean-001"}
        ref = _load(out / "meta" / "profile-ref.json")
        excluded = {e["claim_id"]: e["reason"] for e in ref["excluded"]}
        assert "stale-sent-len-001" in excluded and "stale_claim" in excluded["stale-sent-len-001"]
        text = (out / "SKILL.md").read_text(encoding="utf-8")
        assert "41 字" not in text and "410" not in text
        r = run_script("skill_lint.py", "--skill", out, "--profile", p, "--workspace", ws, "--no-runner-smoke")
        rep = json.loads(r.stdout)
        assert r.returncode == 2
        assert rep["checks"]["migration"]["status"] == "fail"
        assert rep["checks"]["profile_claims"]["status"] == "pass"  # excluded に載っている
        assert set(rep["checks"]["claim_drift"]["measured"]["stale_claims"]) == {"stale-sent-len-001", "stale-mean-001"}
        # exploratory なら warn
        prof = self._stale_profile(ws)
        prof["profile_class"] = "exploratory"
        prof["approval"] = {"decided_by": "synthetic-reviewer", "decided_at": "2000-01-01", "decisions": ["stale migration test"]}
        p2 = tmp / "profile-stale3.json"
        p2.write_text(json.dumps(prof, ensure_ascii=False), encoding="utf-8")
        out2 = tmp / "skill-stale-explo"
        r = run_script("compile_skill.py", "--workspace", ws, "--profile", p2, "--out", out2, "--now", NOW, "--allow-stale-claims")
        assert r.returncode == 0, r.stderr
        r = run_script("skill_lint.py", "--skill", out2, "--profile", p2, "--no-runner-smoke")
        assert json.loads(r.stdout)["checks"]["migration"]["status"] == "warn"

    def test_skill_lint_detects_stale_mapped_claim(self, polite):
        """profile-ref が stale claim を mappings に載せていれば --workspace で fail。"""
        tmp, ws, skill = polite
        broken = tmp / "skill-tampered-map"
        if broken.exists():
            shutil.rmtree(broken)
        shutil.copytree(skill, broken)
        p = tmp / "profile-stale4.json"
        p.write_text(json.dumps(self._stale_profile(ws), ensure_ascii=False), encoding="utf-8")
        ref_path = broken / "meta" / "profile-ref.json"
        ref = _load(ref_path)
        ref["mappings"].append({"target": "SKILL.md#always_on_rules", "claim_ids": ["stale-sent-len-001"]})
        ref["mappings"].append({"target": "lint-config.json#G1_distribution", "claim_ids": ["stale-mean-001"]})
        ref_path.write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")
        r = run_script("skill_lint.py", "--skill", broken, "--profile", p, "--workspace", ws, "--no-runner-smoke")
        rep = json.loads(r.stdout)
        assert r.returncode == 2
        assert rep["checks"]["claim_drift"]["status"] == "fail"
        assert set(rep["checks"]["claim_drift"]["measured"]["stale_mapped"]) == {"stale-sent-len-001", "stale-mean-001"}

    def test_aggregate_schema_mismatch_rejected(self, polite):
        tmp, ws, _ = polite
        ws2 = tmp / "ws-oldagg"
        if ws2.exists():
            shutil.rmtree(ws2)
        shutil.copytree(ws, ws2)
        agg_path = ws2 / "features" / "_aggregate.json"
        agg = _load(agg_path)
        agg["feature_schema"] = "1"
        agg_path.write_text(json.dumps(agg, ensure_ascii=False), encoding="utf-8")
        r = _compile(ws2, tmp / "skill-oldagg")
        assert r.returncode == 1
        assert "feature_schema" in r.stderr and "extract_features" in r.stderr


class TestSemanticMapping:
    def _claim(self, ws, cid, metric, value=None, target="validator"):
        return _manual_claim(
            cid, category="文", scope_mode="core", condition=None, rule_text="x",
            status="core", target=target, evidence=_first_span(ws), metric=metric, value=value,
        )

    def _compile_with(self, polite, cid, claim):
        tmp, ws, _ = polite
        profile = _profile(ws)
        profile["claims"].append(claim)
        p = tmp / f"profile-{cid}.json"
        p.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        out = tmp / f"skill-{cid}"
        r = run_script("compile_skill.py", "--workspace", ws, "--profile", p, "--out", out, "--now", NOW)
        return r, out

    def _agg_value(self, ws, metric):
        agg = _load(ws / "features" / "_aggregate.json")
        e = agg["features"].get(metric)
        if not e:
            return None
        ea = e["equal_article"]
        return {"median": ea["median"], "range": list(ea["iqr"])}

    def test_unevaluated_metrics_are_rejected(self, polite):
        _, ws, _ = polite
        for cid, metric in (("digit-001", "script_ratio.digit"), ("mean-001", "comma_per_sent_mean"), ("other-001", "sent_end_form.other")):
            r, out = self._compile_with(polite, cid, self._claim(ws, cid, metric))
            assert r.returncode == 2, (metric, r.stderr)
            assert not (out / "meta" / "profile-ref.json").exists()

    def test_sent_len_max_maps_to_concrete_g1_check(self, polite, tmp_path):
        _, ws, _ = polite
        r, out = self._compile_with(polite, "slmax-001", self._claim(ws, "slmax-001", "sent_len_max", self._agg_value(ws, "sent_len_max")))
        assert r.returncode == 0, r.stderr
        cfg = _load(out / "lint-config.json")
        assert cfg["gates"]["G1_distribution"]["sent_len_chars"]["max_hard"] is not None
        ref = _load(out / "meta" / "profile-ref.json")
        g1 = next(m for m in ref["mappings"] if m["target"] == "lint-config.json#G1_distribution")
        assert "slmax-001" in g1["claim_ids"]
        f = tmp_path / "longsent.txt"
        f.write_text("短い文です。" * 12 + "とても長い文を一つだけ入れます、" * 40 + "終わります。", encoding="utf-8")
        rep = _lint(out, f)
        assert any("最長文" in x["message"] for x in rep["gates"]["G1"]["findings"])

    def test_latin_ratio_maps_to_g3(self, polite):
        _, ws, _ = polite
        r, out = self._compile_with(polite, "latin-001", self._claim(ws, "latin-001", "script_ratio.latin", self._agg_value(ws, "script_ratio.latin")))
        assert r.returncode == 0, r.stderr
        cfg = _load(out / "lint-config.json")
        assert cfg["gates"]["G3_orthography"]["latin_ratio"][0] is not None

    def test_func_word_rate_only_maps_when_evaluable(self, polite, tmp_path):
        _, ws, _ = polite
        r, out = self._compile_with(polite, "fwr-001", self._claim(ws, "fwr-001", "func_word_rate", self._agg_value(ws, "func_word_rate")))
        if MODE == "sudachi":
            assert r.returncode == 0, r.stderr
            cfg = _load(out / "lint-config.json")
            assert cfg["gates"]["G4_vocabulary"]["func_word_rate_range"][0] is not None
            f = tmp_path / "fw.txt"
            f.write_text(_raw(ws, _calibration_ids(ws)[0]), encoding="utf-8")
            rep = _lint(out, f)
            assert rep["gates"]["G4"]["status"] in ("pass", "warn")
        else:
            assert r.returncode == 2
            assert "func_word_rate" in r.stderr


class TestCompileWarnings:
    def test_degenerate_bands_are_reported(self, polite):
        _, ws, skill = polite
        cfg = _load(skill / "lint-config.json")
        warns = cfg["calibration"]["warnings"]
        # 合成コーパスは jotai_adj / da_dearu がほぼ 0 → 退化帯域の注記が出る
        assert any("degenerate" in w or "zero_width" in w or "near_min" in w or "outlier" in w for w in warns), warns
        prov = _load(skill / "meta" / "provenance.json")
        assert prov["generator"]["builder_status"] == "experimental"
        assert prov["calibration_policy"]["article_alpha"] == 0.01
        assert prov["source"]["feature_schema"] == "2"
