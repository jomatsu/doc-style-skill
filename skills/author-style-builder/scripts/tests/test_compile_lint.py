"""compile → skill_lint → style_lint / overlap_check の end-to-end テスト。

profile.json は stability_test の profile-candidates.json から人間承認の流れを
シミュレートして構築する(core 昇格候補を採用 + mode/example/ambiguous を手動追加)。
fallback モード(sudachipy 不在)で必ず通ること。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR, run_script
from lib import morph as morph_lib
from lib.tokenize import get_analyzer

ANALYZER_MODE = get_analyzer().meta()["mode"]
AUTHOR_ID = "synth-author"
CONSENT = "synthetic-consent-token"
NOW = "2000-01-01T00:00:00+00:00"


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _manual_claim(claim_id, *, category, scope_mode, condition, rule_text, status,
                  target, evidence, metric=None, value=None):
    return {
        "claim_id": claim_id,
        "category": category,
        "scope_mode": scope_mode,
        "condition": condition,
        "rule_text": rule_text,
        "feature": {"metric": metric},
        "value": value or {},
        "evidence": evidence,
        "support": {"articles": 5, "strata": 1, "bootstrap_agreement": 0.8},
        "control_result": {"masking": "not_run", "cross_topic": "not_run", "loao": "pass"},
        "state": "observed",
        "status": status,
        "compilation_target": target,
        "rights_scope": CONSENT,
        "confidence": "medium",
        "version": "1.0.0",
    }


def _build_profile(ws: Path) -> dict:
    """候補から core claim を採用し、mode / example / ambiguous を手動追加。"""
    candidates = _load(ws / "profile-candidates.json")["candidates"]
    core = [c for c in candidates if c["status"] == "core"]
    assert len(core) >= 2, "合成コーパスから core 候補が 2 件未満"
    claims = []
    for c in core[:4]:
        c = dict(c)
        c["compilation_target"] = "always_on_rule"
        if not c["value"].get("range"):
            c["value"]["range"] = c["value"].get("ci95")
        claims.append(c)

    raw_a001 = (ws / "raw" / "a001.txt").read_text(encoding="utf-8")
    snippet = "合成標識語を一度だけ置き、直後に実務的な説明へ戻ります。"
    start = raw_a001.index(snippet)
    ev_a001 = [{"article_id": "a001", "char_start": start, "char_end": start + len(snippet)}]

    claims.append(
        _manual_claim(
            "mode-tech-001",
            category="談話",
            scope_mode="tech",
            condition="技術記事・解説記事を書くとき",
            rule_text="手順の説明では理由を先に短く述べてから操作を書くことが多い",
            status="mode_specific",
            target="conditional_rule",
            evidence=ev_a001,
        )
    )
    claims.append(
        _manual_claim(
            "example-001",
            category="文",
            scope_mode="core",
            condition=None,
            rule_text="比喩を一文で短く言い切り、直後に実務的な説明へ戻る",
            status="local",
            target="example",
            evidence=ev_a001,
        )
    )
    claims.append(
        _manual_claim(
            "ambiguous-001",
            category="談話",
            scope_mode="core",
            condition=None,
            rule_text="軽い自己言及ユーモアは文脈に合うときのみ使う(低一致特徴)",
            status="ambiguous",
            target="checklist",
            evidence=ev_a001,
        )
    )
    # G5 markers は人間承認の validator claim からのみ(aggregate から発明しない)
    claims.append(
        _manual_claim(
            "caricature-markers-001",
            category="語彙",
            scope_mode="core",
            condition=None,
            rule_text="合成標識語を短い範囲で反復しない",
            status="core",
            target="validator",
            evidence=ev_a001,
            metric="caricature_markers",
            value={"markers": ["合成標識語"]},
        )
    )
    return {
        "author_id": AUTHOR_ID,
        "author_name": "合成著者",
        "version": "1.0.0",
        "feature_schema": _load(ws / "features" / "_aggregate.json")["feature_schema"],
        "claims": claims,
    }


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


def _calibration_ids(ws: Path) -> list[str]:
    splits = _load(ws / "splits.json")
    manifest = _load(ws / "manifest.json")
    canon = {a["article_id"] for a in manifest["articles"] if not a.get("dup_of")}
    return sorted(i for i in splits["train"] + splits["dev"] if i in canon)


def _holdout_ids(ws: Path) -> list[str]:
    splits = _load(ws / "splits.json")
    manifest = _load(ws / "manifest.json")
    canon = {a["article_id"] for a in manifest["articles"] if not a.get("dup_of")}
    return sorted(i for i in splits["holdout"] if i in canon)


@pytest.fixture(scope="module")
def skill_dir(workspace, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("skill") / f"{AUTHOR_ID}-style"
    result = run_script(
        "compile_skill.py", "--workspace", workspace, "--out", out, "--now", NOW
    )
    assert result.returncode == 0, result.stderr
    return out


def _skill_files(root: Path) -> list:
    return sorted(
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )


def _body_text(ws: Path, article_id: str) -> str:
    clean = _load(ws / "clean" / f"{article_id}.json")
    return "\n\n".join(b["text"] for b in clean["blocks"] if b["type"] == "body")


def _closest_to_median_article(ws: Path) -> str:
    """aggregate の中央値に最も近い記事(pass テキスト用)。"""
    agg = _load(ws / "features" / "_aggregate.json")
    med_sent = agg["features"]["sent_len_median"]["equal_article"]["median"]
    med_form = agg["features"]["sent_end_form.desu_masu"]["equal_article"]["median"]
    best, best_d = None, None
    for p in sorted((ws / "features").glob("a*.json")):
        r = _load(p)
        d = abs(r["sent_len"]["median"] - med_sent) + 20 * abs(
            r["sent_end_form"]["desu_masu"] - med_form
        )
        if best_d is None or d < best_d:
            best, best_d = r["article_id"], d
    return best


def _agg_value(ws: Path, metric: str) -> dict:
    """aggregate から再現可能な claim value(median / range=IQR)を作る。"""
    agg = _load(ws / "features" / "_aggregate.json")
    ea = agg["features"][metric]["equal_article"]
    return {"median": ea["median"], "range": list(ea["iqr"]), "ci95": list(ea["ci95"])}


def _ambiguous_conditional_claims(ev, ws: Path) -> list:
    """人間承認済みのexploratory profileが持つ文体傾向claim。

    数値はaggregateから取り、現行schemaで再現可能にする。
    """
    return [
        _manual_claim(
            "explo-sent-len-001",
            category="文",
            scope_mode="core",
            condition="日本語の記事本文を書くとき",
            rule_text="1 文は短めが中心",
            status="ambiguous",
            target="conditional_rule",
            evidence=ev,
            metric="sent_len_median",
            value=_agg_value(ws, "sent_len_median"),
        ),
        _manual_claim(
            "explo-para-len-001",
            category="構造",
            scope_mode="core",
            condition="日本語の記事本文を書くとき",
            rule_text="段落は数文が中心",
            status="ambiguous",
            target="conditional_rule",
            evidence=ev,
            metric="para_len_median",
            value=_agg_value(ws, "para_len_median"),
        ),
    ]


def _write_variant_profile(
    workspace: Path, tmp_path: Path, name: str, *, exploratory: bool
) -> Path:
    profile = _load(workspace / "profile.json")
    ev = profile["claims"][-1]["evidence"]
    profile["claims"] = profile["claims"] + _ambiguous_conditional_claims(ev, workspace)
    if exploratory:
        profile["profile_class"] = "exploratory"
        profile["limitations"] = {"corpus": "synthetic fixture", "controls": "test-only"}
        profile["approval"] = {
            "decided_by": "synthetic-reviewer",
            "decided_at": "2000-01-01",
            "decisions": ["探索的スキルとして継続する"],
        }
    p = tmp_path / name
    p.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    return p


def _compile_variant(workspace: Path, tmp_path: Path, *, exploratory: bool) -> Path:
    tag = "exploratory" if exploratory else "production"
    profile_path = _write_variant_profile(
        workspace, tmp_path, f"profile-{tag}.json", exploratory=exploratory
    )
    out = tmp_path / f"skill-{tag}"
    result = run_script(
        "compile_skill.py", "--workspace", workspace,
        "--profile", profile_path, "--out", out, "--now", NOW,
    )
    assert result.returncode == 0, result.stderr
    return out


@pytest.fixture(scope="module")
def exploratory_skill_dir(workspace, tmp_path_factory) -> Path:
    return _compile_variant(
        workspace, tmp_path_factory.mktemp("explo"), exploratory=True
    )


@pytest.fixture(scope="module")
def production_skill_dir(workspace, tmp_path_factory) -> Path:
    return _compile_variant(
        workspace, tmp_path_factory.mktemp("prod"), exploratory=False
    )


class TestExploratoryCompile:
    """承認済みexploratory claimを通常の文体傾向として描画する。"""

    def test_section_and_rules_rendered(self, exploratory_skill_dir):
        text = (exploratory_skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "## 文体傾向" in text
        start = text.index("## 文体傾向")
        end = text.index("## ペルソナ")
        section = text[start:end]
        # 階層順(構造 → 文)で決定的に並ぶ
        assert section.index("段落は数文が中心") < section.index(
            "1 文は短めが中心"
        )

    def test_maturity_metadata_is_separate(self, exploratory_skill_dir):
        text = (exploratory_skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "本番リリース品質" not in text
        assert "守るべき制約" not in text
        provenance = _load(exploratory_skill_dir / "meta" / "provenance.json")
        assert provenance["maturity"]["profile_class"] == "exploratory"
        assert provenance["maturity"]["approval"]["decided_by"] == "synthetic-reviewer"
        assert provenance["maturity"]["limitations"]["corpus"] == "synthetic fixture"
        cfg = _load(exploratory_skill_dir / "lint-config.json")
        assert cfg["profile_class"] == "exploratory"

    def test_profile_ref_mapping(self, exploratory_skill_dir):
        ref = _load(exploratory_skill_dir / "meta" / "profile-ref.json")
        entry = next(
            m for m in ref["mappings"] if m["target"] == "SKILL.md#style_tendencies"
        )
        assert entry["claim_ids"] == ["explo-para-len-001", "explo-sent-len-001"]

    def test_no_empty_mappings(self, exploratory_skill_dir):
        ref = _load(exploratory_skill_dir / "meta" / "profile-ref.json")
        empty = [m["target"] for m in ref["mappings"] if not m["claim_ids"]]
        assert empty == [], f"claim_ids が空の mapping: {empty}"

    def test_skill_lint_no_warning(self, exploratory_skill_dir):
        result = run_script("skill_lint.py", "--skill", exploratory_skill_dir)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["checks"]["profile_ref"]["status"] == "pass"
        assert report["checks"]["placeholders"]["status"] == "pass"

    def test_no_placeholder_or_marker_residue(self, exploratory_skill_dir):
        for p in list(exploratory_skill_dir.rglob("*.md")) + list(
            exploratory_skill_dir.rglob("*.json")
        ):
            text = p.read_text(encoding="utf-8")
            assert "{{" not in text, f"placeholder 残存: {p}"
            assert "EXPLORATORY_SECTION" not in text, f"マーカー残存: {p}"

    def test_ambiguous_not_promoted_to_core(self, exploratory_skill_dir):
        """探索的 claim は常時ルール(core)にもペルソナにも入らない。"""
        text = (exploratory_skill_dir / "SKILL.md").read_text(encoding="utf-8")
        core = text[text.index("## 常時ルール") : text.index("## モード")]
        assert "探索的" not in core
        ref = _load(exploratory_skill_dir / "meta" / "profile-ref.json")
        for target in ("SKILL.md#persona", "SKILL.md#always_on_rules"):
            entry = next(
                (m for m in ref["mappings"] if m["target"] == target), None
            )
            if entry is not None:
                assert "explo-sent-len-001" not in entry["claim_ids"]
                assert "explo-para-len-001" not in entry["claim_ids"]

    def test_human_approval_metadata_required(self, workspace, tmp_path):
        profile = _load(workspace / "profile.json")
        ev = profile["claims"][-1]["evidence"]
        profile["claims"] += _ambiguous_conditional_claims(ev, workspace)
        profile["profile_class"] = "exploratory"
        profile.pop("approval", None)
        p = tmp_path / "profile-no-approval.json"
        p.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "skill-no-approval"
        result = run_script(
            "compile_skill.py", "--workspace", workspace,
            "--profile", p, "--out", out, "--now", NOW,
        )
        assert result.returncode == 1
        assert "approval.decided_by / decided_at / decisions" in result.stderr

    def test_deterministic(self, workspace, exploratory_skill_dir, tmp_path):
        again = _compile_variant(workspace, tmp_path, exploratory=True)
        files1 = _skill_files(exploratory_skill_dir)
        files2 = _skill_files(again)
        assert files1 == files2
        for rel in files1:
            assert (exploratory_skill_dir / rel).read_bytes() == (
                again / rel
            ).read_bytes(), rel


class TestNonExploratoryCompile:
    """非 exploratory profile では ambiguous/conditional_rule を出力しない。"""

    def test_section_absent(self, production_skill_dir):
        text = (production_skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "## 文体傾向" not in text
        assert "1 文は短めが中心" not in text
        assert "段落は数文が中心" not in text

    def test_no_placeholder_or_marker_residue(self, production_skill_dir):
        for p in list(production_skill_dir.rglob("*.md")) + list(
            production_skill_dir.rglob("*.json")
        ):
            text = p.read_text(encoding="utf-8")
            assert "{{" not in text, f"placeholder 残存: {p}"
            assert "EXPLORATORY_SECTION" not in text, f"マーカー残存: {p}"
            assert "<!--" not in text, f"コメント残存: {p}"

    def test_profile_ref_has_no_exploratory_target(self, production_skill_dir):
        ref = _load(production_skill_dir / "meta" / "profile-ref.json")
        targets = {m["target"] for m in ref["mappings"]}
        assert "SKILL.md#style_tendencies" not in targets
        all_ids = [i for m in ref["mappings"] for i in m["claim_ids"]]
        assert "explo-sent-len-001" not in all_ids
        assert "explo-para-len-001" not in all_ids

    def test_skill_lint_passes(self, production_skill_dir):
        result = run_script("skill_lint.py", "--skill", production_skill_dir)
        assert result.returncode == 0, result.stdout + result.stderr


class TestCompile:
    def test_outputs_exist(self, skill_dir):
        for rel in [
            "SKILL.md",
            "references/style-rules.md",
            "references/examples.md",
            "references/checklist.md",
            "lint-config.json",
            "meta/profile-ref.json",
            "meta/provenance.json",
        ]:
            assert (skill_dir / rel).exists(), f"missing: {rel}"

    def test_no_placeholders(self, skill_dir):
        for p in list(skill_dir.rglob("*.md")) + list(skill_dir.rglob("*.json")):
            text = p.read_text(encoding="utf-8")
            assert "{{" not in text, f"placeholder 残存: {p}"

    def test_lint_config_calibrated(self, skill_dir):
        cfg = _load(skill_dir / "lint-config.json")
        g1 = cfg["gates"]["G1_distribution"]
        lo, hi = g1["sent_len_chars"]["median_range"]
        assert lo is not None and hi is not None and lo < hi
        g2 = cfg["gates"]["G2_sentence_end"]
        assert g2["max_consecutive_same_ending"] >= 3
        assert g2["max_consecutive_hard_cap"] >= g2["max_consecutive_same_ending"] + 2
        assert cfg["gates"]["G6_copy"]["exact_match_max_chars"] == 25
        assert cfg["gates"]["G6_copy"]["stoplist_patterns"] == ["https?://\\S+"]
        assert cfg["author_id"] == AUTHOR_ID

    def test_range_expression_not_absolute(self, skill_dir):
        skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        start = skill_md.index("## 常時ルール")
        end = skill_md.index("## ", start + 1)  # 次のセクションまで
        rules = skill_md[start:end]  # コンパイルされたルール部分のみ検査
        assert "中心" in rules or "目安" in rules  # レンジ表現
        assert "常に" not in rules and "必ず" not in rules  # 絶対規則の禁止

    def test_profile_ref_mappings(self, skill_dir):
        ref = _load(skill_dir / "meta" / "profile-ref.json")
        targets = {m["target"] for m in ref["mappings"]}
        assert "SKILL.md#always_on_rules" in targets
        assert "references/style-rules.md#tech" in targets
        assert "references/checklist.md#ambiguous" in targets
        all_ids = [i for m in ref["mappings"] for i in m["claim_ids"]]
        assert "mode-tech-001" in all_ids
        assert "ambiguous-001" in all_ids

    def test_provenance(self, workspace, skill_dir):
        prov = _load(skill_dir / "meta" / "provenance.json")
        assert prov["rights_scope"] == CONSENT
        assert prov["generated_at"] == NOW
        assert prov["source"]["article_count"] == len(_calibration_ids(workspace))
        assert prov["source"]["calibration_split"] == "train+dev"
        assert len(prov["source"]["profile_hash"]) == 64
        runner = prov["runner"]
        assert runner["entry"] == "scripts/lint.sh"
        for rel, digest in runner["files"].items():
            assert (skill_dir / rel).exists(), rel
            assert len(digest) == 64

    def test_morphology_reference_bundled(self, workspace, skill_dir):
        cfg = _load(skill_dir / "lint-config.json")
        g7 = cfg["gates"]["G7_morphology"]
        assert g7["enabled"] is True
        assert g7["reference_file"] == "lint-morphology.json"
        assert g7["calibration"]["split"] == "train+dev"
        assert cfg["calibration"]["split"] == "train+dev"
        assert set(g7["channels"]) == set(morph_lib.CHANNELS)
        ref = _load(skill_dir / "lint-morphology.json")
        assert ref["calibration_split"] == "train+dev"
        assert ref["channel_registry_version"] == morph_lib.CHANNEL_REGISTRY_VERSION
        assert ref["channels"]["comma_rel_pos"]["status"] == "built"
        text = (skill_dir / "lint-morphology.json").read_text(encoding="utf-8")
        assert "article_ids" not in text  # 記事 ID を同梱しない

    def test_all_split_aggregate_rejected(self, workspace, tmp_path):
        """split=all(holdout 混入)の aggregate は較正に使えない → exit 1。"""
        ws = tmp_path / "all-ws"
        shutil.copytree(workspace, ws)
        result = run_script(
            "extract_features.py", "--workspace", ws, "--split", "all", "--seed", "42"
        )
        assert result.returncode == 0, result.stderr
        out = tmp_path / "skill-all"
        result = run_script(
            "compile_skill.py", "--workspace", ws, "--out", out, "--now", NOW
        )
        assert result.returncode == 1
        assert "split='all'" in result.stderr
        assert not (out / "SKILL.md").exists()
        # train+dev で再抽出すれば通る
        result = run_script(
            "extract_features.py", "--workspace", ws, "--split", "train+dev", "--seed", "42"
        )
        assert result.returncode == 0
        result = run_script(
            "compile_skill.py", "--workspace", ws, "--out", out, "--now", NOW
        )
        assert result.returncode == 0, result.stderr

    def test_deterministic(self, workspace, skill_dir, tmp_path):
        out2 = tmp_path / "again"
        result = run_script(
            "compile_skill.py", "--workspace", workspace, "--out", out2, "--now", NOW
        )
        assert result.returncode == 0, result.stderr
        files1 = _skill_files(skill_dir)
        files2 = _skill_files(out2)
        assert files1 == files2
        for rel in files1:
            assert (skill_dir / rel).read_bytes() == (out2 / rel).read_bytes(), rel

    def test_inferred_claim_excluded(self, workspace, tmp_path):
        profile = _load(workspace / "profile.json")
        bad = dict(profile["claims"][0])
        bad.update({"claim_id": "inferred-001", "state": "inferred"})
        profile["claims"].append(bad)
        p = tmp_path / "profile-with-inferred.json"
        p.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "skill"
        result = run_script(
            "compile_skill.py", "--workspace", workspace,
            "--profile", p, "--out", out, "--now", NOW,
        )
        assert result.returncode == 0
        assert "inferred-001" in result.stderr  # 除外警告
        ref = _load(out / "meta" / "profile-ref.json")
        all_ids = [i for m in ref["mappings"] for i in m["claim_ids"]]
        assert "inferred-001" not in all_ids


def _compile_with_claims(workspace: Path, tmp_path: Path, extra_claims: list, name: str,
                         *, drop_ids: tuple = ()) -> tuple:
    profile = _load(workspace / "profile.json")
    profile["claims"] = [c for c in profile["claims"] if c["claim_id"] not in drop_ids] + extra_claims
    p = tmp_path / f"profile-{name}.json"
    p.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / f"skill-{name}"
    result = run_script(
        "compile_skill.py", "--workspace", workspace,
        "--profile", p, "--out", out, "--now", NOW,
    )
    return result, out, p


class TestCompileRules:
    """compile-rules.md の写像規則と完全性。"""

    def _ev(self, workspace):
        return _load(workspace / "profile.json")["claims"][-1]["evidence"]

    def test_core_conditional_rendered(self, workspace, tmp_path):
        claim = _manual_claim(
            "core-cond-001", category="談話", scope_mode="core",
            condition="記事の結びを書くとき",
            rule_text="結びは読者への短い呼びかけで終えることが多い",
            status="core", target="conditional_rule", evidence=self._ev(workspace),
        )
        result, out, _ = _compile_with_claims(workspace, tmp_path, [claim], "corecond")
        assert result.returncode == 0, result.stderr
        text = (out / "SKILL.md").read_text(encoding="utf-8")
        assert "## 条件付きルール(core)" in text
        assert "条件「記事の結びを書くとき」のとき: 結びは読者への短い呼びかけ" in text
        always = text[text.index("## 常時ルール") : text.index("## 条件付きルール(core)")]
        assert "呼びかけ" not in always  # 条件付きは常時ルールに混ぜない
        ref = _load(out / "meta" / "profile-ref.json")
        entry = next(m for m in ref["mappings"] if m["target"] == "SKILL.md#core_conditional_rules")
        assert entry["claim_ids"] == ["core-cond-001"]
        assert "CORE_CONDITIONAL_SECTION" not in text

    def test_activation_cases_are_generated(self, skill_dir):
        """ゲート 2 の資材を compile が展開する(未生成だと regression_run が常に不合格)。"""
        cases = skill_dir / "eval" / "activation-cases.yaml"
        assert cases.exists(), "eval/activation-cases.yaml が生成されていない"
        text = cases.read_text(encoding="utf-8")
        assert "{{" not in text  # プレースホルダ残存なし
        assert "positive:" in text and "near_miss:" in text and "negative:" in text

    def test_activation_cases_are_not_overwritten(self, workspace, tmp_path):
        """人手で埋めた発火テスト資材を再コンパイルで壊さない。"""
        out = tmp_path / "activation-skill"
        first = run_script(
            "compile_skill.py", "--workspace", workspace, "--out", out, "--now", NOW
        )
        assert first.returncode == 0, first.stderr
        cases = out / "eval" / "activation-cases.yaml"
        edited = "positive:\n  - '人手で書いたケース'\n"
        cases.write_text(edited, encoding="utf-8")
        second = run_script(
            "compile_skill.py", "--workspace", workspace, "--out", out, "--now", NOW
        )
        assert second.returncode == 0, second.stderr
        assert cases.read_text(encoding="utf-8") == edited

    def test_core_conditional_section_absent_without_claims(self, skill_dir):
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "## 条件付きルール(core)" not in text
        assert "CORE_CONDITIONAL_SECTION" not in text

    def test_production_ambiguous_conditional_is_excluded_with_reason(self, production_skill_dir):
        ref = _load(production_skill_dir / "meta" / "profile-ref.json")
        excluded = {e["claim_id"]: e["reason"] for e in ref["excluded"]}
        assert "explo-sent-len-001" in excluded
        assert "explo-para-len-001" in excluded
        assert "本番プロファイル" in excluded["explo-sent-len-001"]

    def test_unmappable_claim_exits_2(self, workspace, tmp_path):
        """採用済み(observed・非 quarantined)なのに写像できない claim → exit 2、出力なし。"""
        claim = _manual_claim(
            "bad-validator-001", category="文", scope_mode="core", condition=None,
            rule_text="存在しない指標", status="core", target="validator",
            evidence=self._ev(workspace), metric="no_such_metric",
        )
        result, out, _ = _compile_with_claims(workspace, tmp_path, [claim], "unmapped")
        assert result.returncode == 2
        assert "bad-validator-001" in result.stderr
        assert not (out / "meta" / "profile-ref.json").exists()

    def test_unknown_target_exits_2(self, workspace, tmp_path):
        claim = _manual_claim(
            "bad-target-001", category="文", scope_mode="core", condition=None,
            rule_text="x", status="core", target="mystery", evidence=self._ev(workspace),
        )
        result, _, _ = _compile_with_claims(workspace, tmp_path, [claim], "unknowntarget")
        assert result.returncode == 2
        assert "bad-target-001" in result.stderr

    def test_no_synthetic_persona(self, skill_dir):
        """persona claim が無いとき、aggregate からペルソナ文を合成しない。"""
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        persona = text[text.index("## ペルソナ") : text.index("## 常時ルール")]
        assert "persona claim は未登録" in persona
        assert "中心" not in persona and "字" not in persona
        ref = _load(skill_dir / "meta" / "profile-ref.json")
        assert not any(m["target"] == "SKILL.md#persona" for m in ref["mappings"])

    def test_persona_from_claim_only(self, workspace, tmp_path):
        claim = _manual_claim(
            "persona-001", category="談話", scope_mode="core", condition=None,
            rule_text="結論を先に短く述べ、具体例で肉付けする", status="core",
            target="persona", evidence=self._ev(workspace),
        )
        result, out, _ = _compile_with_claims(workspace, tmp_path, [claim], "persona")
        assert result.returncode == 0, result.stderr
        text = (out / "SKILL.md").read_text(encoding="utf-8")
        persona = text[text.index("## ペルソナ") : text.index("## 常時ルール")]
        assert "結論を先に短く述べ、具体例で肉付けする。" in persona
        assert "未登録" not in persona

    def test_range_supplement_does_not_invent_hard_limits(self, workspace, tmp_path):
        """rule_text を主とし、レンジは補足のみ。hi*2 の上限等を発明しない。"""
        value = _agg_value(workspace, "sent_len_median")
        claim = _manual_claim(
            "len-001", category="文", scope_mode="core", condition=None,
            rule_text="短い文を重ねる。", status="core", target="always_on_rule",
            evidence=self._ev(workspace), metric="sent_len_median", value=value,
        )
        result, out, _ = _compile_with_claims(workspace, tmp_path, [claim], "range")
        assert result.returncode == 0, result.stderr
        text = (out / "SKILL.md").read_text(encoding="utf-8")
        lo, hi = value["range"]
        fmt = lambda v: str(int(round(v))) if abs(v - round(v)) < 1e-9 else f"{v:.2f}".rstrip("0").rstrip(".")
        assert f"短い文を重ねる(記事ごとの文長中央値は {fmt(lo)}〜{fmt(hi)}字 が中心)" in text
        rules = text.split("## 常時ルール")[1].split("## モード")[0]
        assert fmt(hi * 2) not in rules
        assert "超える文を連続" not in text

    def test_morph_validator_maps_to_g7_only(self, workspace, tmp_path):
        """表層チャネル(両モードで較正される)の validator claim は G7 へだけ写像される。"""
        claim = _manual_claim(
            "morph-val-001", category="文", scope_mode="core", condition=None,
            rule_text="読点位置が著者分布に近い", status="core", target="validator",
            evidence=self._ev(workspace), metric="morph.comma_rel_pos",
        )
        result, out, _ = _compile_with_claims(workspace, tmp_path, [claim], "morphval")
        assert result.returncode == 0, result.stderr
        ref = _load(out / "meta" / "profile-ref.json")
        entry = next(m for m in ref["mappings"] if m["target"] == "lint-config.json#G7_morphology")
        assert entry["claim_ids"] == ["morph-val-001"]
        text = (out / "SKILL.md").read_text(encoding="utf-8")
        assert "comma_rel_pos" not in text and "読点位置が著者分布" not in text

    def test_morph_validator_on_unbuilt_channel_is_unmappable(self, workspace, tmp_path):
        """較正されていないチャネル(fallback の POS チャネル / サンプル不足)の validator は写像不能。"""
        agg = _load(workspace / "features" / "_aggregate.json")
        unbuilt = next(
            n for n, ch in agg["morphology"]["channels"].items() if ch.get("status") != "built"
        )
        claim = _manual_claim(
            "morph-unbuilt-001", category="語彙", scope_mode="core", condition=None,
            rule_text="x", status="core", target="validator",
            evidence=self._ev(workspace), metric=f"morph.{unbuilt}",
        )
        result, out, _ = _compile_with_claims(workspace, tmp_path, [claim], "morphunbuilt")
        assert result.returncode == 2, result.stderr
        assert "metric_not_evaluable" in result.stderr or "評価できない" in result.stderr
        assert not (out / "meta" / "profile-ref.json").exists()

    def test_morph_claim_never_becomes_prose_command(self, workspace, tmp_path):
        for target in ("always_on_rule", "persona"):
            claim = _manual_claim(
                f"morph-{target}-001", category="語彙", scope_mode="core", condition=None,
                rule_text="品詞 bigram を著者に合わせる", status="core", target=target,
                evidence=self._ev(workspace), metric="morph.pos_bigram",
            )
            result, out, _ = _compile_with_claims(workspace, tmp_path, [claim], f"morph-{target}")
            assert result.returncode == 2, (target, result.stderr)
            assert "validator/checklist/example 専用" in result.stderr
            assert not (out / "SKILL.md").exists() or "品詞 bigram" not in (out / "SKILL.md").read_text(encoding="utf-8")

    def test_calibration_split_choices(self, workspace, tmp_path):
        """aggregate.calibration_split が train / dev / train+dev 以外なら拒否。"""
        ws = tmp_path / "badsplit-ws"
        shutil.copytree(workspace, ws)
        agg_path = ws / "features" / "_aggregate.json"
        agg = _load(agg_path)
        for bad in ("all", "holdout", None):
            agg["split"] = agg["calibration_split"] = bad
            agg_path.write_text(json.dumps(agg, ensure_ascii=False), encoding="utf-8")
            result = run_script("compile_skill.py", "--workspace", ws, "--out", tmp_path / "o", "--now", NOW)
            assert result.returncode == 1, bad
        for good in ("train", "dev", "train+dev"):
            agg["split"] = agg["calibration_split"] = good
            agg_path.write_text(json.dumps(agg, ensure_ascii=False), encoding="utf-8")
            result = run_script("compile_skill.py", "--workspace", ws, "--out", tmp_path / f"o-{good}", "--now", NOW)
            assert result.returncode == 0, (good, result.stderr)


class TestSkillLint:
    def test_generated_skill_passes(self, skill_dir):
        result = run_script("skill_lint.py", "--skill", skill_dir)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["checks"]["profile_claims"]["status"] == "skipped"
        assert report["checks"]["runner"]["status"] == "pass"
        assert report["checks"]["runner"]["smoke"]["exit"] in (0, 2)

    def test_profile_claim_completeness(self, workspace, skill_dir, tmp_path):
        result = run_script(
            "skill_lint.py", "--skill", skill_dir, "--profile", workspace / "profile.json",
            "--no-runner-smoke",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        pc = report["checks"]["profile_claims"]
        assert pc["status"] == "pass"
        n_claims = len(_load(workspace / "profile.json")["claims"])
        assert pc["measured"]["adopted_claims"] == n_claims
        assert pc["measured"]["mapped"] + pc["measured"]["excluded"] == n_claims
        # --workspace からも解決できる
        result = run_script("skill_lint.py", "--skill", skill_dir, "--workspace", workspace, "--no-runner-smoke")
        assert json.loads(result.stdout)["checks"]["profile_claims"]["status"] == "pass"
        # profile-ref から claim を落とすと fail
        broken = tmp_path / "broken-ref-skill"
        shutil.copytree(skill_dir, broken)
        ref_path = broken / "meta" / "profile-ref.json"
        ref = _load(ref_path)
        ref["mappings"] = [m for m in ref["mappings"] if m["target"] != "references/checklist.md#ambiguous"]
        ref_path.write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")
        result = run_script(
            "skill_lint.py", "--skill", broken, "--profile", workspace / "profile.json",
            "--no-runner-smoke",
        )
        assert result.returncode == 2
        pc = json.loads(result.stdout)["checks"]["profile_claims"]
        assert pc["status"] == "fail"
        assert any("ambiguous-001" in f["message"] for f in pc["findings"])

    def test_runner_tamper_detected(self, skill_dir, tmp_path):
        broken = tmp_path / "tampered-skill"
        shutil.copytree(skill_dir, broken)
        p = broken / "scripts" / "lib" / "morph.py"
        p.write_text(p.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        result = run_script("skill_lint.py", "--skill", broken, "--no-runner-smoke")
        assert result.returncode == 2
        runner = json.loads(result.stdout)["checks"]["runner"]
        assert any("sha256" in f["message"] for f in runner["findings"])

    def test_bundled_runner_works_from_arbitrary_cwd(self, skill_dir, tmp_path):
        """同梱 lint.sh を別の cwd から相対パスのテキストで実行し、G7 参照を解決できる。"""
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
        (cwd / "draft.txt").write_text(
            "小さな変更をこまめに残す習慣について書きます。理由は単純です。あとから履歴を読み返すとき、意図が分かりやすいからです。",
            encoding="utf-8",
        )
        env = dict(os.environ, STYLE_LINT_PYTHON=sys.executable)
        r = subprocess.run(
            ["bash", str(skill_dir / "scripts" / "lint.sh"), "--text", "draft.txt"],
            cwd=cwd, env=env, capture_output=True, text=True,
        )
        assert r.returncode in (0, 2), r.stderr
        report = json.loads(r.stdout)
        g7 = report["gates"]["G7"]
        assert g7["status"] != "skipped" or g7["reason"] == "no_channel_evaluated", g7
        assert not (g7.get("reason") or "").startswith("reference_missing")
        assert report["gates"]["G6"]["status"] == "skipped"
        assert report["gates"]["G6"]["reason"] == "source_corpus_not_given"

    def test_placeholder_fails(self, skill_dir, tmp_path):
        broken = tmp_path / "broken-skill"
        shutil.copytree(skill_dir, broken)
        md = broken / "SKILL.md"
        md.write_text(
            md.read_text(encoding="utf-8") + "\n{{oops}}\n", encoding="utf-8"
        )
        result = run_script("skill_lint.py", "--skill", broken)
        assert result.returncode == 2

    def test_broken_reference_fails(self, skill_dir, tmp_path):
        broken = tmp_path / "noref-skill"
        shutil.copytree(skill_dir, broken)
        (broken / "references" / "checklist.md").unlink()
        result = run_script("skill_lint.py", "--skill", broken)
        assert result.returncode == 2


class TestStyleLint:
    def test_author_like_text_no_fail(self, workspace, skill_dir, tmp_path):
        """著者コーパス由来のテキストは fail しない(warn は許容)。"""
        article_id = _closest_to_median_article(workspace)
        text_file = tmp_path / "pass.txt"
        text_file.write_text(_body_text(workspace, article_id), encoding="utf-8")
        result = run_script(
            "style_lint.py", "--config", skill_dir / "lint-config.json",
            "--text", text_file,
        )
        assert result.returncode == 0, result.stdout
        report = json.loads(result.stdout)
        assert report["gates"]["G6"]["status"] == "skipped"
        assert report["composite_score"] is None
        assert set(report["gates"]) == {"G1", "G2", "G3", "G4", "G5", "G6", "G7"}
        for name, gate in report["gates"].items():
            assert gate["status"] != "fail", (name, gate["findings"])

    def test_lint_uses_prose_contract(self, workspace, skill_dir, tmp_path):
        """コード・表・見出し・URL を挿入しても計測値は変わらない。"""
        body = _body_text(workspace, "a001")
        paras = body.split("\n\n")
        noisy = (
            "# 見出し\n\n" + paras[0] + "\n\n```js\nconst x = 1;\n```\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\nhttps://example.com/x\n\n"
            + "\n\n".join(paras[1:])
        )
        f1, f2 = tmp_path / "plain.txt", tmp_path / "noisy.txt"
        f1.write_text(body, encoding="utf-8")
        f2.write_text(noisy, encoding="utf-8")
        reports = []
        for f in (f1, f2):
            r = run_script("style_lint.py", "--config", skill_dir / "lint-config.json", "--text", f)
            reports.append(json.loads(r.stdout))
        a, b = reports
        assert a["n_sents"] == b["n_sents"]
        assert a["text_chars"] == b["text_chars"]
        for g in ("G1", "G2", "G3", "G4"):
            assert a["gates"][g]["status"] == b["gates"][g]["status"], g
        for name, ch in a["gates"]["G7"]["channels"].items():
            other = b["gates"]["G7"]["channels"][name]
            assert ch["status"] == other["status"], name
            assert ch.get("distance") == other.get("distance"), name
            assert ch.get("value") == other.get("value"), name

    def test_violating_text_fails(self, skill_dir, tmp_path):
        """長文連発 + 同一文末の連続(cap+2)→ G1/G2 fail、exit 2。"""
        cfg = _load(skill_dir / "lint-config.json")
        g2 = cfg["gates"]["G2_sentence_end"]
        cap = g2["max_consecutive_same_ending"]
        hard = g2.get("max_consecutive_hard_cap", cap + max(2, cap // 2))
        n = max(hard + 2, 6)
        long_clause = "この設定はとても重要でありなおかつ非常に複雑であって" * 6
        bad = "".join(f"{long_clause}という結論になるのです。" for _ in range(n))
        text_file = tmp_path / "fail.txt"
        text_file.write_text(bad, encoding="utf-8")
        result = run_script(
            "style_lint.py", "--config", skill_dir / "lint-config.json",
            "--text", text_file,
        )
        assert result.returncode == 2
        report = json.loads(result.stdout)
        assert report["gates"]["G1"]["status"] == "fail"
        assert report["gates"]["G2"]["status"] == "fail"
        spans = [f["span"] for f in report["gates"]["G2"]["findings"]]
        assert spans, "G2 fail に span 指摘がない"

    def test_copied_text_fails_g6(self, workspace, skill_dir, tmp_path):
        """ソースからの 100 字連続コピー → G6 fail(ハードゲート)。"""
        body = _body_text(workspace, "a001")
        copied = "以下に引用します。" + body[:100] + "ということでした。"
        text_file = tmp_path / "copy.txt"
        text_file.write_text(copied, encoding="utf-8")
        result = run_script(
            "style_lint.py", "--config", skill_dir / "lint-config.json",
            "--text", text_file, "--source-corpus", workspace / "raw",
        )
        assert result.returncode == 2
        report = json.loads(result.stdout)
        assert report["gates"]["G6"]["status"] == "fail"
        assert any(
            "連続一致" in f["message"] for f in report["gates"]["G6"]["findings"]
        )

    def test_half_verbatim_prose_fails_and_fresh_prose_passes_g6(self, workspace, skill_dir, tmp_path):
        """散文の 50% を逐語コピーした文は G6 fail、無関係の散文は pass。"""
        src = _body_text(workspace, "a006").split("\n\n")
        fresh_paras = [
            "昨日は山に登りました。朝は霧が濃く、山頂は見えませんでした。それでも歩き始めると、少しずつ空が明るくなりました。",
            "途中の小屋でお茶を飲みました。店番の方が天気の話をしてくれました。午後は晴れるという予想でした。実際、その通りになりました。",
            "山頂では風が強く、帽子を押さえながら写真を撮りました。遠くに海が見えました。下山は膝が笑いましたが、良い一日でした。",
            "帰りの電車ではすぐに眠ってしまいました。次は秋にまた来ようと思います。紅葉の季節はもっと混むらしいです。",
        ]
        half = "\n\n".join([src[0], fresh_paras[0], src[1], fresh_paras[1]])
        fresh = "\n\n".join(fresh_paras)
        f_half, f_fresh = tmp_path / "half.txt", tmp_path / "fresh.txt"
        f_half.write_text(half, encoding="utf-8")
        f_fresh.write_text(fresh, encoding="utf-8")
        r = run_script(
            "style_lint.py", "--config", skill_dir / "lint-config.json",
            "--text", f_half, "--source-corpus", workspace / "raw",
        )
        assert r.returncode == 2
        g6 = json.loads(r.stdout)["gates"]["G6"]
        assert g6["status"] == "fail"
        assert g6["measured"]["exact_matches"] >= 1
        assert g6["measured"]["paragraph_near_dup_count"] >= 1
        assert any("局所コピー" in f["message"] for f in g6["findings"])
        assert g6["copy_index"]["status"] == "deferred"
        r = run_script(
            "style_lint.py", "--config", skill_dir / "lint-config.json",
            "--text", f_fresh, "--source-corpus", workspace / "raw",
        )
        g6 = json.loads(r.stdout)["gates"]["G6"]
        assert g6["status"] == "pass", g6
        assert g6["measured"]["exact_matches"] == 0
        assert g6["measured"]["paragraph_near_dup_count"] == 0


def _lint(skill_dir: Path, path: Path, *extra) -> dict:
    r = run_script("style_lint.py", "--config", skill_dir / "lint-config.json", "--text", path, *extra)
    assert r.returncode in (0, 2), r.stderr
    return json.loads(r.stdout)


class TestMorphologyGate:
    """G7: 著者分布内の文は fail せず、機能語・文末・読点位置を崩した文は該当チャネルが反応する。"""

    def test_report_shape(self, workspace, skill_dir, tmp_path):
        f = tmp_path / "in.txt"
        f.write_text(_body_text(workspace, _closest_to_median_article(workspace)), encoding="utf-8")
        g7 = _lint(skill_dir, f)["gates"]["G7"]
        assert g7["status"] in ("pass", "warn")
        assert g7["composite_score"] is None
        assert g7["reference"]["calibration_split"] == "train+dev"
        assert set(g7["channels"]) == set(morph_lib.CHANNELS)
        assert set(g7["slices"]) >= {"register:desu_masu", "length:short"}
        for name, ch in g7["channels"].items():
            spec = morph_lib.CHANNELS[name]
            assert ch["kind"] == spec["kind"]
            if ch["status"] == "skipped":
                assert ch["reason"]
                if spec["requires"] == "sudachi" and ANALYZER_MODE == "fallback":
                    assert "fallback" in ch["reason"] or "reference_not_built" in ch["reason"] or ch["reason"].startswith("insufficient")
                continue
            assert ch["status"] in ("pass", "warn", "fail")
            assert 0.0 <= ch["percentile"] <= 1.0
            assert ch["worst_slice"] and ch["worst_slice"]["slice"]
            assert "thresholds" in ch
            if spec["kind"] == "dist":
                assert "distance" in ch and "top_deviations" in ch and "example_spans" in ch
            else:
                assert "value" in ch
            if spec["max_severity"] == "warn":
                assert ch["status"] != "fail"
        if ANALYZER_MODE == "fallback":
            assert g7["channels"]["pos_unigram"]["status"] == "skipped"
            assert g7["channels"]["comma_rel_pos"]["status"] != "skipped"

    def test_in_distribution_articles_never_fail(self, workspace, skill_dir, tmp_path):
        """較正記事と holdout 記事のいずれも G7 で fail しない(warn は許容)。"""
        ids = _calibration_ids(workspace) + _holdout_ids(workspace)
        for aid in ids:
            f = tmp_path / f"{aid}.txt"
            f.write_text(_body_text(workspace, aid), encoding="utf-8")
            g7 = _lint(skill_dir, f)["gates"]["G7"]
            failed = [n for n, c in g7["channels"].items() if c["status"] == "fail"]
            assert not failed, (aid, failed)

    def test_era_slice_reported(self, workspace, skill_dir, tmp_path):
        f = tmp_path / "era.txt"
        f.write_text(_body_text(workspace, "a001"), encoding="utf-8")
        g7 = _lint(skill_dir, f, "--era", "2024")["gates"]["G7"]
        assert "era:2024" in g7["slices"]
        assert g7["slices"]["era:2024"]["status"] in ("built", "shrunk", "skipped")

    def _channel(self, skill_dir, tmp_path, name, text, fname) -> dict:
        f = tmp_path / fname
        f.write_text(text, encoding="utf-8")
        return _lint(skill_dir, f)["gates"]["G7"]["channels"][name]

    def test_altered_comma_position(self, workspace, skill_dir, tmp_path):
        """読点を全て文頭寄りに移す → comma_rel_pos(表層チャネル。fallback でも動く)が反応。"""
        import re

        body = _body_text(workspace, "a001")
        parts = re.split(r"(?<=。)", body)
        moved = []
        for p in parts:
            n = p.count("、")
            p = p.replace("、", "")
            if n and len(p) > 4:
                p = p[:2] + "、" * n + p[2:]
            moved.append(p)
        base = self._channel(skill_dir, tmp_path, "comma_rel_pos", body, "c0.txt")
        alt = self._channel(skill_dir, tmp_path, "comma_rel_pos", "".join(moved), "c1.txt")
        assert base["status"] in ("pass", "warn")
        assert alt["status"] == "warn"  # max_severity=warn なので fail にはならない
        assert alt["distance"] > base["distance"]
        assert alt["distance"] > alt["thresholds"]["fail"]
        assert alt["top_deviations"][0]["key"] == "q1"
        assert alt["example_spans"], "過剰側の例 span が無い"
        assert all(len(e["excerpt"]) <= 51 for e in alt["example_spans"])

    @pytest.mark.skipif(ANALYZER_MODE != "sudachi", reason="POS チャネルは sudachi 専用")
    def test_altered_function_words(self, workspace, skill_dir, tmp_path):
        """助詞を組織的に入れ替える → particle_bigram / funcword_bigram が分布外。"""
        body = _body_text(workspace, "a001")
        altered = body.replace("は", "が").replace("を", "に")
        for name in ("particle_bigram", "funcword_bigram"):
            base = self._channel(skill_dir, tmp_path, name, body, "f0.txt")
            alt = self._channel(skill_dir, tmp_path, name, altered, "f1.txt")
            assert base["status"] in ("pass", "warn"), name
            assert alt["status"] == "fail", (name, alt)
            assert alt["distance"] > base["distance"]
            assert alt["example_spans"]

    @pytest.mark.skipif(ANALYZER_MODE != "sudachi", reason="POS チャネルは sudachi 専用")
    def test_altered_end_suffix(self, workspace, skill_dir, tmp_path):
        """文末を一律過去形に → final_suffix2 が分布外。"""
        body = _body_text(workspace, "a001")
        altered = body.replace("ます。", "ました。").replace("です。", "でした。")
        base = self._channel(skill_dir, tmp_path, "final_suffix2", body, "s0.txt")
        alt = self._channel(skill_dir, tmp_path, "final_suffix2", altered, "s1.txt")
        assert base["status"] in ("pass", "warn")
        assert alt["status"] == "fail", alt
        assert alt["distance"] > base["distance"]
        assert any("た" in d["key"] and d["delta"] > 0 for d in alt["top_deviations"])

    def test_missing_reference_is_explicit_skip(self, skill_dir, tmp_path):
        broken = tmp_path / "noref"
        shutil.copytree(skill_dir, broken)
        (broken / "lint-morphology.json").unlink()
        f = tmp_path / "x.txt"
        f.write_text("短い文です。もう一つ。", encoding="utf-8")
        g7 = _lint(broken, f)["gates"]["G7"]
        assert g7["status"] == "skipped"
        assert g7["reason"].startswith("reference_missing")


class TestOverlapCheck:
    def test_fresh_text_clean(self, workspace, tmp_path):
        text_file = tmp_path / "fresh.txt"
        text_file.write_text(
            "全く新しい話題について書きます。昨日は雨でした。"
            "傘を忘れて駅まで走ったので、靴の中まで濡れてしまいました。",
            encoding="utf-8",
        )
        result = run_script(
            "overlap_check.py", "--text", text_file, "--against", workspace / "raw"
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["exact"]["matches"] == []
        assert report["embedding"]["status"] == "skipped"
