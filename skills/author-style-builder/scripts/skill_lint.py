#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""skill_lint — 生成 author スキルの静的リント(eval-protocol ゲート 1)。

- SKILL.md frontmatter(name / description 必須)
- references/ 相対リンク切れ
- SKILL.md 行数 >500 / 概算トークン(文字数/3)>5000 で fail
- {{placeholder}} 残存
- シークレット様パターン(API キー・トークン等)
- meta/profile-ref.json の mappings に claim_ids が空の項目があれば warn
- --profile 指定時(または --workspace から <ws>/profile.json を解決できたとき):
  profile の observed・非 quarantined の全 claim が profile-ref の mappings か
  excluded のどちらかに載っていること(claim 完全性)。欠落は fail
- 同梱リンター(scripts/lint.sh + style_lint.py + lib/)の存在・provenance の
  sha256 一致・**任意の cwd からの実行**をスモーク検査する(未同梱は warn)
- --source-corpus 指定時: スキル本文(eval/ 以外の .md)と生コーパスの
  長い連続一致(既定 60 字、URL 除外後の実効長)を検出(eval-protocol ゲート 1)
- G5 の真実性: lint-config の G5 markers が空なら、production プロファイルは fail
  (G5 は無条件ハードゲートなのに評価できない)、exploratory は warn
- 移行マーカ: compile `--allow-stale-claims` で生成されたスキル(provenance.migration)は
  production で fail(リリース不可)、exploratory で warn
- --workspace 指定時(features/_aggregate.json があるとき): profile claim の数値が aggregate で
  再現できるか(stale claim)を再検査し、再現できない claim が mappings に載っていれば fail。
  aggregate の feature_schema がスキルのものと違えば fail
- スキーマ: lint-config.calibration.feature_schema / channel_registry_version が builder と違えば warn

終了コード: 0=成功(fail なし)/ 1=エラー / 2=fail あり
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import claims as claims_lib
from lib import features as feat
from lib import morph as morph_lib

MAX_LINES = 500
MAX_TOKENS = 5000
CHARS_PER_TOKEN = 3
RAW_QUOTE_MIN_CHARS = 60  # 例示の短い span(≤50字+…)は許容し、長い生引用のみ検出

_PLACEHOLDER_RE = re.compile(r"\{\{[^{}\n]+\}\}")
_REFERENCE_RE = re.compile(r"references/[\w.\-/]+")
_SECRET_PATTERNS = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "generic_credential",
        re.compile(
            r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*"
            r"[\"'][A-Za-z0-9_\-]{16,}[\"']"
        ),
    ),
]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="(任意)<ws>/profile.json を claim 完全性検査に使う",
    )
    p.add_argument("--skill", required=True, type=Path, help="生成スキルのディレクトリ")
    p.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="profile.json。指定時は claim 完全性を検査(既定: <workspace>/profile.json)",
    )
    p.add_argument(
        "--source-corpus",
        type=Path,
        default=None,
        help="生コーパス dir。指定時はスキル本文との長い連続一致を検査",
    )
    p.add_argument(
        "--no-runner-smoke",
        action="store_true",
        help="同梱リンターの実行スモーク(別 cwd から lint.sh を実行)を省略",
    )
    return p.parse_args(argv)


def _finding(file: str, message: str, **extra) -> dict:
    return {"file": file, "message": message, **extra}


def check_frontmatter(skill_dir: Path) -> dict:
    findings = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return {
            "status": "fail",
            "findings": [_finding("SKILL.md", "SKILL.md がありません")],
        }
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.S)
    if not m:
        findings.append(_finding("SKILL.md", "frontmatter(--- 区切り)がありません"))
    else:
        body = m.group(1)
        for key in ("name", "description"):
            if not re.search(rf"^{key}\s*:", body, flags=re.M):
                findings.append(
                    _finding("SKILL.md", f"frontmatter に {key} がありません")
                )
    return {"status": "fail" if findings else "pass", "findings": findings}


def check_reference_links(skill_dir: Path, md_files: list[Path]) -> dict:
    findings = []
    for path in md_files:
        text = path.read_text(encoding="utf-8")
        for m in _REFERENCE_RE.finditer(text):
            rel = m.group(0).split("#", 1)[0].rstrip(".、。)」")
            if not rel or rel == "references/":
                continue
            if not (skill_dir / rel).exists():
                findings.append(
                    _finding(
                        str(path.relative_to(skill_dir)),
                        f"参照リンク切れ: {rel}",
                    )
                )
    return {"status": "fail" if findings else "pass", "findings": findings}


def check_budget(skill_dir: Path) -> dict:
    findings = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return {"status": "skipped", "findings": []}
    text = skill_md.read_text(encoding="utf-8")
    n_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    n_tokens = len(text) // CHARS_PER_TOKEN
    if n_lines > MAX_LINES:
        findings.append(
            _finding(
                "SKILL.md",
                f"行数超過: {n_lines} > {MAX_LINES}",
                measured=n_lines,
                expected=MAX_LINES,
            )
        )
    if n_tokens > MAX_TOKENS:
        findings.append(
            _finding(
                "SKILL.md",
                f"概算トークン超過: {n_tokens} > {MAX_TOKENS}(文字数/{CHARS_PER_TOKEN})",
                measured=n_tokens,
                expected=MAX_TOKENS,
            )
        )
    return {
        "status": "fail" if findings else "pass",
        "findings": findings,
        "measured": {"lines": n_lines, "approx_tokens": n_tokens},
    }


def check_placeholders(skill_dir: Path, all_files: list[Path]) -> dict:
    findings = []
    for path in all_files:
        text = path.read_text(encoding="utf-8")
        for m in _PLACEHOLDER_RE.finditer(text):
            findings.append(
                _finding(
                    str(path.relative_to(skill_dir)),
                    f"プレースホルダ残存: {m.group(0)[:40]}",
                )
            )
    return {"status": "fail" if findings else "pass", "findings": findings}


def check_secrets(skill_dir: Path, all_files: list[Path]) -> dict:
    findings = []
    for path in all_files:
        text = path.read_text(encoding="utf-8")
        for name, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(
                    _finding(
                        str(path.relative_to(skill_dir)),
                        f"シークレット様パターン検出: {name}",
                    )
                )
    return {"status": "fail" if findings else "pass", "findings": findings}


def check_profile_ref(skill_dir: Path) -> dict:
    findings = []
    path = skill_dir / "meta" / "profile-ref.json"
    if not path.exists():
        return {
            "status": "warn",
            "findings": [_finding("meta/profile-ref.json", "profile-ref.json がありません")],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {
            "status": "fail",
            "findings": [_finding("meta/profile-ref.json", f"JSON 破損: {e}")],
        }
    for entry in data.get("mappings", []):
        if not entry.get("claim_ids"):
            findings.append(
                _finding(
                    "meta/profile-ref.json",
                    f"claim_ids が空: {entry.get('target', '?')}",
                )
            )
    return {"status": "warn" if findings else "pass", "findings": findings}


def check_profile_claims(skill_dir: Path, profile_path: Path | None) -> dict:
    """profile の採用 claim が profile-ref に漏れなく載っているか(完全性)。"""
    if profile_path is None:
        return {"status": "skipped", "findings": [], "note": "--profile / --workspace 未指定"}
    if not profile_path.exists():
        return {
            "status": "fail",
            "findings": [_finding("profile", f"profile がありません: {profile_path}")],
        }
    ref_path = skill_dir / "meta" / "profile-ref.json"
    if not ref_path.exists():
        return {
            "status": "fail",
            "findings": [_finding("meta/profile-ref.json", "profile-ref.json がありません(完全性を検証できない)")],
        }
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"status": "fail", "findings": [_finding("profile", f"JSON 破損: {e}")]}
    mapped = {i for m in ref.get("mappings", []) for i in m.get("claim_ids", [])}
    excluded = {e.get("claim_id") for e in ref.get("excluded", [])}
    findings = []
    adopted = []
    for c in profile.get("claims", []):
        cid = c.get("claim_id", "?")
        if c.get("state") != "observed" or c.get("status") == "quarantined":
            if cid in mapped:
                findings.append(
                    _finding("meta/profile-ref.json", f"コンパイル禁止 claim が写像されている: {cid}")
                )
            continue
        adopted.append(cid)
        if cid not in mapped and cid not in excluded:
            findings.append(
                _finding("meta/profile-ref.json", f"claim が mappings にも excluded にも無い: {cid}")
            )
    profile_ids = {c.get("claim_id") for c in profile.get("claims", [])}
    for cid in sorted(mapped - profile_ids):
        findings.append(
            _finding("meta/profile-ref.json", f"profile に存在しない claim_id が写像されている: {cid}")
        )
    return {
        "status": "fail" if findings else "pass",
        "findings": findings,
        "measured": {
            "adopted_claims": len(adopted),
            "mapped": len(mapped & set(adopted)),
            "excluded": len(excluded & set(adopted)),
        },
    }


def check_runner(skill_dir: Path, *, smoke: bool) -> dict:
    """同梱リンターの整合性と、任意の cwd からの実行可否。"""
    prov_path = skill_dir / "meta" / "provenance.json"
    lint_sh = skill_dir / "scripts" / "lint.sh"
    if not lint_sh.exists():
        return {
            "status": "warn",
            "findings": [_finding("scripts/lint.sh", "同梱リンターが無い(旧 builder で生成されたスキル)")],
        }
    findings = []
    if not os.access(lint_sh, os.X_OK):
        findings.append(_finding("scripts/lint.sh", "実行権限がない"))
    runner = {}
    if prov_path.exists():
        try:
            runner = json.loads(prov_path.read_text(encoding="utf-8")).get("runner") or {}
        except json.JSONDecodeError:
            runner = {}
    for rel, digest in (runner.get("files") or {}).items():
        path = skill_dir / rel
        if not path.exists():
            findings.append(_finding(rel, "provenance.runner に記載のファイルが無い"))
            continue
        actual = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        if actual != digest:
            findings.append(_finding(rel, "provenance.runner の sha256 と不一致(同梱リンターが改変されている)"))
    if not (skill_dir / "lint-config.json").exists():
        findings.append(_finding("lint-config.json", "lint-config.json がありません"))
    g7_ref = None
    cfg_path = skill_dir / "lint-config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            g7 = (cfg.get("gates") or {}).get("G7_morphology") or {}
            if g7.get("enabled"):
                g7_ref = g7.get("reference_file") or "lint-morphology.json"
                if not (skill_dir / g7_ref).exists():
                    findings.append(_finding(g7_ref, "G7 が有効だが参照ファイルが無い"))
        except json.JSONDecodeError:
            findings.append(_finding("lint-config.json", "JSON 破損"))
    smoke_result = None
    if smoke and not findings:
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.txt"
            sample.write_text("これはスモーク用の短い文です。同梱リンターが別の場所から動くかを見ます。", encoding="utf-8")
            env = dict(os.environ, STYLE_LINT_PYTHON=sys.executable)
            try:
                r = subprocess.run(
                    ["bash", str(lint_sh.resolve()), "--text", "sample.txt"],
                    cwd=tmp,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                ok = r.returncode in (0, 2)
                parsed = None
                if ok:
                    try:
                        parsed = json.loads(r.stdout)
                    except json.JSONDecodeError:
                        ok = False
                if ok and parsed is not None:
                    g7_status = ((parsed.get("gates") or {}).get("G7") or {}).get("status")
                    smoke_result = {"exit": r.returncode, "cwd": "tempdir", "g7_status": g7_status}
                    if g7_ref and g7_status == "skipped":
                        reason = ((parsed.get("gates") or {}).get("G7") or {}).get("reason") or ""
                        if reason.startswith(("reference_missing", "reference_invalid", "registry_version")):
                            findings.append(_finding("scripts/lint.sh", f"別 cwd からの実行で G7 参照を解決できない: {reason}"))
                else:
                    findings.append(
                        _finding(
                            "scripts/lint.sh",
                            f"別 cwd からの実行に失敗(exit={r.returncode}): {r.stderr.strip()[:200]}",
                        )
                    )
            except (OSError, subprocess.TimeoutExpired) as e:
                findings.append(_finding("scripts/lint.sh", f"実行できない: {e}"))
    return {
        "status": "fail" if findings else "pass",
        "findings": findings,
        "smoke": smoke_result,
    }


def check_raw_quotes(
    skill_dir: Path, md_files: list[Path], corpus_dir: Path | None
) -> dict:
    """生コーパスからの長い引用検出(eval-protocol ゲート 1)。"""
    if corpus_dir is None:
        return {"status": "skipped", "findings": [], "note": "--source-corpus 未指定"}
    from overlap_check import (
        DEFAULT_STOPLIST_PATTERNS,
        corpus_text_files,
        effective_length,
        find_exact_matches,
    )

    corpus_files = corpus_text_files(corpus_dir)
    if not corpus_files:
        return {
            "status": "skipped",
            "findings": [],
            "note": "source-corpus に txt/md がない",
        }
    findings = []
    targets = [p for p in md_files if "eval" not in p.relative_to(skill_dir).parts]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for src in corpus_files:
            source = src.read_text(encoding="utf-8")
            for m in find_exact_matches(text, source, RAW_QUOTE_MIN_CHARS):
                matched = text[m["text_span"][0] : m["text_span"][1]]
                if effective_length(matched, DEFAULT_STOPLIST_PATTERNS) < RAW_QUOTE_MIN_CHARS:
                    continue
                findings.append(
                    _finding(
                        str(path.relative_to(skill_dir)),
                        f"生コーパス {src.name} と {m['length']} 字の連続一致(長い引用)",
                        span=m["text_span"],
                    )
                )
    return {"status": "fail" if findings else "pass", "findings": findings}


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _profile_class(skill_dir: Path, profile_path: Path | None) -> str:
    """profile(あれば)→ lint-config の順で profile_class を決める(既定 production)。"""
    if profile_path is not None and profile_path.exists():
        p = _load_json(profile_path) or {}
        if p.get("profile_class"):
            return p["profile_class"]
    cfg = _load_json(skill_dir / "lint-config.json") or {}
    return cfg.get("profile_class") or "production"


def check_g5_markers(skill_dir: Path, profile_class: str) -> dict:
    """G5 は無条件ハードゲート(eval-protocol)。markers が空なら評価できないことを明示する。"""
    cfg = _load_json(skill_dir / "lint-config.json")
    if cfg is None:
        return {"status": "skipped", "findings": [], "note": "lint-config.json が無い"}
    g5 = (cfg.get("gates") or {}).get("G5_caricature") or {}
    markers = g5.get("markers") or []
    if markers:
        return {"status": "pass", "findings": [], "measured": {"markers": len(markers)}}
    severity = "warn" if profile_class == "exploratory" else "fail"
    return {
        "status": severity,
        "findings": [
            _finding(
                "lint-config.json",
                "G5_caricature.markers が空: カリカチュア検査は skipped になり評価できない"
                + ("(production は G5 が無条件ハードゲートのためリリース不可)" if severity == "fail" else "(exploratory: 警告)")
                + "。人間承認の validator claim(metric=caricature_markers, value.markers)を profile に追加すること",
            )
        ],
        "measured": {"markers": 0},
    }


def check_migration(skill_dir: Path, profile_class: str) -> dict:
    """compile --allow-stale-claims で生成された移行候補は本番リリースできない。"""
    prov = _load_json(skill_dir / "meta" / "provenance.json") or {}
    cfg = _load_json(skill_dir / "lint-config.json") or {}
    mig = prov.get("migration") or cfg.get("migration")
    if not mig:
        return {"status": "pass", "findings": []}
    n = len(mig.get("stale_claims") or [])
    severity = "warn" if profile_class == "exploratory" else "fail"
    return {
        "status": severity,
        "findings": [
            _finding(
                "meta/provenance.json",
                f"migration マーカあり(--allow-stale-claims、stale claim {n} 件)。移行候補の確認専用でリリース不可",
            )
        ],
        "measured": {"stale_claims": n},
    }


def check_schema(skill_dir: Path) -> dict:
    """スキルの特徴スキーマ / チャネルレジストリが builder と一致するか(旧スキルは warn)。"""
    cfg = _load_json(skill_dir / "lint-config.json")
    if cfg is None:
        return {"status": "skipped", "findings": [], "note": "lint-config.json が無い"}
    cal = cfg.get("calibration") or {}
    findings = []
    fs, cr = cal.get("feature_schema"), cal.get("channel_registry_version")
    if fs != feat.FEATURE_SCHEMA_VERSION:
        findings.append(_finding("lint-config.json", f"calibration.feature_schema={fs!r} が builder の {feat.FEATURE_SCHEMA_VERSION!r} と違う(旧 builder 生成物。再ビルド推奨)"))
    if cr != morph_lib.CHANNEL_REGISTRY_VERSION:
        findings.append(_finding("lint-config.json", f"calibration.channel_registry_version={cr!r} が builder の {morph_lib.CHANNEL_REGISTRY_VERSION!r} と違う"))
    if not cal.get("analyzer_meta"):
        findings.append(_finding("lint-config.json", "calibration.analyzer_meta が無い(実行時の解析器互換を検証できない)"))
    return {
        "status": "warn" if findings else "pass",
        "findings": findings,
        "measured": {"feature_schema": fs, "channel_registry_version": cr, "builder_status": cfg.get("builder_status")},
    }


def check_claim_drift(skill_dir: Path, profile_path: Path | None, workspace: Path | None) -> dict:
    """profile claim の数値が workspace の aggregate で再現できるか(--workspace 時のみ)。"""
    if workspace is None or profile_path is None or not profile_path.exists():
        return {"status": "skipped", "findings": [], "note": "--workspace(aggregate)と profile の両方が必要"}
    agg_path = workspace / "features" / "_aggregate.json"
    if not agg_path.exists():
        return {"status": "skipped", "findings": [], "note": "features/_aggregate.json が無い"}
    aggregate = _load_json(agg_path)
    profile = _load_json(profile_path)
    if aggregate is None or profile is None:
        return {"status": "fail", "findings": [_finding("profile", "aggregate / profile の JSON が読めない")]}
    findings = []
    prov = _load_json(skill_dir / "meta" / "provenance.json") or {}
    skill_schema = (prov.get("source") or {}).get("feature_schema")
    agg_schema = aggregate.get("feature_schema")
    if skill_schema != agg_schema:
        findings.append(
            _finding(
                "meta/provenance.json",
                f"スキルの feature_schema={skill_schema!r} と workspace aggregate の {agg_schema!r} が違う(スキルと aggregate が別世代)",
            )
        )
    if agg_schema != feat.FEATURE_SCHEMA_VERSION:
        findings.append(
            _finding(
                "features/_aggregate.json",
                f"aggregate の feature_schema={agg_schema!r} が builder の {feat.FEATURE_SCHEMA_VERSION!r} と違う。extract_features.py を再実行すること",
            )
        )
    ref = _load_json(skill_dir / "meta" / "profile-ref.json") or {}
    mapped = {i for m in ref.get("mappings", []) for i in m.get("claim_ids", [])}
    drifts, warnings = claims_lib.check_profile_drift(profile, aggregate)
    mapped_stale = []
    for d in drifts:
        if d["claim_id"] in mapped:
            mapped_stale.append(d["claim_id"])
            findings.append(_finding("profile", "stale claim が写像されている: " + claims_lib.format_drift(d)))
    return {
        "status": "fail" if findings else ("warn" if drifts or warnings else "pass"),
        "findings": findings,
        "measured": {
            "stale_claims": [d["claim_id"] for d in drifts],
            "stale_mapped": mapped_stale,
            "schema_warnings": len(warnings),
        },
        "drift": drifts,
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    skill_dir = args.skill
    if not skill_dir.is_dir():
        print(f"error: skill ディレクトリがありません: {skill_dir}", file=sys.stderr)
        return 1

    all_files = sorted(
        p
        for p in skill_dir.rglob("*")
        if p.is_file() and p.suffix in (".md", ".json")
    )
    md_files = [p for p in all_files if p.suffix == ".md"]
    profile_path = args.profile
    if profile_path is None and args.workspace is not None:
        candidate = args.workspace / "profile.json"
        if candidate.exists():
            profile_path = candidate
    profile_class = _profile_class(skill_dir, profile_path)

    checks = {
        "frontmatter": check_frontmatter(skill_dir),
        "reference_links": check_reference_links(skill_dir, md_files),
        "budget": check_budget(skill_dir),
        "placeholders": check_placeholders(skill_dir, all_files),
        "secrets": check_secrets(skill_dir, all_files),
        "profile_ref": check_profile_ref(skill_dir),
        "profile_claims": check_profile_claims(skill_dir, profile_path),
        "claim_drift": check_claim_drift(skill_dir, profile_path, args.workspace),
        "schema": check_schema(skill_dir),
        "g5_markers": check_g5_markers(skill_dir, profile_class),
        "migration": check_migration(skill_dir, profile_class),
        "runner": check_runner(skill_dir, smoke=not args.no_runner_smoke),
        "raw_quotes": check_raw_quotes(skill_dir, md_files, args.source_corpus),
    }
    rank = {"pass": 0, "skipped": 0, "warn": 1, "fail": 2}
    worst = max((c["status"] for c in checks.values()), key=lambda s: rank[s])
    out = {"skill": str(skill_dir), "status": worst, "profile_class": profile_class, "checks": checks}
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if worst == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
