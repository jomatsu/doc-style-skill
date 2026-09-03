"""feedback_intake(Loop A)と regression_run(Loop C)のテスト。

ユーザー編集 diff の記録 → 集計レポート、凍結回帰スイートの検出力を検証。
fallback モード(sudachipy 不在)で必ず通ること。
"""

import json
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR, run_script
from test_compile_lint import (
    AUTHOR_ID,
    CONSENT,
    NOW,
    _body_text,
    _build_profile,
    _closest_to_median_article,
    _load,
)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> Path:
    ws = tmp_path_factory.mktemp("ws") / AUTHOR_ID
    for name, args in [
        ("corpus_intake.py",
         ["--input", FIXTURES_DIR, "--author-id", AUTHOR_ID, "--consent", CONSENT]),
        ("corpus_split.py", ["--ratio", "70,15,15"]),
        ("extract_features.py", ["--split", "train+dev", "--seed", "42"]),
        ("stability_test.py", ["--seed", "42"]),
    ]:
        result = run_script(name, "--workspace", ws, *args)
        assert result.returncode == 0, f"{name} failed: {result.stderr}"
    profile = _build_profile(ws)
    (ws / "profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ws


@pytest.fixture(scope="module")
def skill_dir(workspace, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("skill") / f"{AUTHOR_ID}-style"
    result = run_script(
        "compile_skill.py", "--workspace", workspace, "--out", out, "--now", NOW
    )
    assert result.returncode == 0, result.stderr
    return out


def _lengthen_sentences(text: str) -> str:
    """文を連結して「著者より長い文」の生成文をシミュレート(偶数番目の句点を読点化)。"""
    out, count = [], 0
    for ch in text:
        if ch == "。":
            count += 1
            out.append("、" if count % 2 == 0 else "。")
        else:
            out.append(ch)
    return "".join(out)


@pytest.fixture(scope="module")
def recorded(workspace, skill_dir, tmp_path_factory) -> Path:
    """3 記事分の「生成文(長文化)→ ユーザー最終稿(原文)」を記録済みの状態。"""
    tmp = tmp_path_factory.mktemp("fb")
    articles = ["a001", "a002", "a004"]
    for i, aid in enumerate(articles):
        final = _body_text(workspace, aid)
        generated = _lengthen_sentences(final)
        g, f = tmp / f"gen-{aid}.txt", tmp / f"fin-{aid}.txt"
        g.write_text(generated, encoding="utf-8")
        f.write_text(final, encoding="utf-8")
        result = run_script(
            "feedback_intake.py", "--workspace", workspace, "record",
            "--generated", g, "--final", f, "--skill", skill_dir,
            "--task-note", f"test-{aid}", "--now", f"2000-01-01T0{i}:00:00+00:00",
        )
        assert result.returncode == 0, result.stderr
    return workspace / "feedback"


class TestFeedbackRecord:
    def test_records_created(self, recorded):
        files = sorted(recorded.glob("fb-*.json"))
        assert len(files) == 3
        rec = _load(files[0])
        assert 0 < rec["diff"]["similarity"] < 1
        assert "sent_len_median" in rec["metrics"]
        # 生成文は長文化されているので final への delta は負
        assert rec["metrics"]["sent_len_median"]["delta"] < 0

    def test_gate_shift_attribution(self, recorded):
        """レンジ内外の移動があれば claim_id に帰責される。"""
        shifts = [s for f in recorded.glob("fb-*.json") for s in _load(f)["gate_shifts"]]
        assert shifts, "長文化でゲートシフトが 1 件も検出されていない"
        attributed = [s for s in shifts if s["claim_ids"]]
        assert attributed, "gate_shifts に claim_id が付与されていない"
        # 生成文がレンジ外 → 最終稿がレンジ内 = 「ルールは有効だが生成が守れていない」
        assert any(not s["generated_in_range"] and s["final_in_range"] for s in shifts)

    def test_profile_untouched(self, workspace, recorded):
        profile = _load(workspace / "profile.json")
        assert all(c["claim_id"] != "auto" for c in profile["claims"])
        assert not (workspace / "profile.json.new").exists()


class TestFeedbackReport:
    def test_report_candidates(self, workspace, skill_dir, recorded):
        result = run_script(
            "feedback_intake.py", "--workspace", workspace, "report",
            "--skill", skill_dir, "--min-support", "3",
        )
        assert result.returncode == 0, result.stderr
        report = _load(recorded / "report.json")
        assert report["n_records"] == 3
        by_metric = {c["metric"]: c for c in report["candidates"]}
        # 3 記録すべてで同方向(文が短くなる)→ 候補化される
        assert "sent_len_median" in by_metric
        cand = by_metric["sent_len_median"]
        assert cand["support"] == 3
        assert cand["direction_agreement"] >= 0.7
        assert cand["median_delta"] < 0
        assert "自動更新されない" in report["note"]

    def test_min_support_filters(self, workspace, skill_dir, recorded):
        result = run_script(
            "feedback_intake.py", "--workspace", workspace, "report",
            "--skill", skill_dir, "--min-support", "5",
        )
        assert result.returncode == 0
        report = _load(recorded / "report.json")
        assert report["candidates"] == []  # 支持 3 < 5 で全て除外


@pytest.fixture(scope="module")
def golden(workspace, skill_dir) -> Path:
    gdir = skill_dir / "eval" / "golden"
    return _make_golden(gdir, workspace, skill_dir)


def _make_golden(gdir: Path, workspace: Path, skill_dir: Path) -> Path:
    (gdir / "pass").mkdir(parents=True, exist_ok=True)
    (gdir / "fail").mkdir(parents=True, exist_ok=True)
    aid = _closest_to_median_article(workspace)
    (gdir / "pass" / f"{aid}.txt").write_text(
        _body_text(workspace, aid), encoding="utf-8"
    )
    cfg = _load(skill_dir / "lint-config.json")
    g2 = cfg["gates"]["G2_sentence_end"]
    cap = g2["max_consecutive_same_ending"]
    hard = g2.get("max_consecutive_hard_cap", cap + max(2, cap // 2))
    long_clause = "この設定はとても重要でありなおかつ非常に複雑であって" * 6
    bad = "".join(
        f"{long_clause}という結論になるのです。" for _ in range(max(hard + 2, 6))
    )
    (gdir / "fail" / "monotone.txt").write_text(bad, encoding="utf-8")
    # 発火テスト資材(テンプレートを埋めた体)
    cases = skill_dir / "eval" / "activation-cases.yaml"
    cases.write_text(
        "positive:\n  - '合成著者さん風に書き直して'\n"
        "near_miss:\n  - '読みやすく校正して'\n",
        encoding="utf-8",
    )
    return gdir


class TestRegressionRun:
    def test_all_pass(self, skill_dir, golden):
        result = run_script("regression_run.py", "--skill", skill_dir)
        assert result.returncode == 0, result.stdout
        report = json.loads(result.stdout)
        assert report["status"] == "pass"
        assert report["regressions"] == []
        assert all(g["ok"] for g in report["results"]["golden_pass"])
        assert all(g["ok"] for g in report["results"]["golden_fail"])
        assert report["results"]["activation_cases"]["ok"] is True

    def test_workspace_golden_is_preferred(
        self, tmp_path, workspace, skill_dir, golden
    ):
        ws_for_run = tmp_path / "run-ws"
        workspace_golden = ws_for_run / "eval" / "golden"
        _make_golden(workspace_golden, workspace, skill_dir)
        result = run_script(
            "regression_run.py",
            "--skill",
            skill_dir,
            "--workspace",
            ws_for_run,
        )
        assert result.returncode == 0, result.stdout
        report = json.loads(result.stdout)
        assert report["results"]["golden_source"] == str(workspace_golden)

    def test_copy_check_failure_is_attributed_to_g6(
        self, tmp_path, workspace, skill_dir
    ):
        """--with-copy-check で pass golden が落ちたとき、原因ゲートを報告する。

        pass golden が著者実記事(生コーパス由来)の場合、G6 は定義上 fail する。
        これを「閾値の劣化」と区別できないと誤診断につながるため、
        failed_gates に G6 だけが入ることを固定する。
        """
        ws_for_run = tmp_path / "copycheck-ws"
        (ws_for_run / "eval" / "golden").mkdir(parents=True)
        _make_golden(ws_for_run / "eval" / "golden", workspace, skill_dir)
        # 生コーパスを --source-corpus として見せる
        (ws_for_run / "raw").mkdir(parents=True, exist_ok=True)
        for src in (workspace / "raw").glob("*.txt"):
            (ws_for_run / "raw" / src.name).write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )
        result = run_script(
            "regression_run.py", "--skill", skill_dir,
            "--workspace", ws_for_run, "--with-copy-check",
        )
        assert result.returncode == 2, result.stdout
        report = json.loads(result.stdout)
        failing = [g for g in report["results"]["golden_pass"] if not g["ok"]]
        assert failing, report["results"]["golden_pass"]
        for entry in failing:
            assert entry["failed_gates"] == ["G6"], entry
        assert any("G6" in r for r in report["regressions"]), report["regressions"]

    def test_detects_gate_degradation(self, workspace, skill_dir, golden):
        """fail golden が fail しなくなったら回帰として検出される。"""
        good = _body_text(workspace, _closest_to_median_article(workspace))
        target = golden / "fail" / "monotone.txt"
        original = target.read_text(encoding="utf-8")
        try:
            target.write_text(good, encoding="utf-8")
            result = run_script("regression_run.py", "--skill", skill_dir)
            assert result.returncode == 2
            report = json.loads(result.stdout)
            assert any("ゲート劣化" in r for r in report["regressions"])
            assert all(
                "failed_gates" in g for g in report["results"]["golden_fail"]
            )
        finally:
            target.write_text(original, encoding="utf-8")
