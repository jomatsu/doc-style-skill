#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["sudachipy", "sudachidict-core"]
# ///
"""regression_run — 凍結回帰スイートを 1 コマンドで実行(Loop C)。

実行内容:
1. skill_lint(静的リント)
2. golden/pass/*.txt → style_lint で fail が出ないこと
3. golden/fail/*.txt → style_lint が fail(exit 2)になること
   (fail 側 golden は「ゲートが壊れていないこと」の検出器。
    fail しなくなったら閾値の劣化を疑う)
4. eval/activation-cases.yaml の存在・プレースホルダ残存チェック
   (発火テスト自体は LLM/人間が実施。ここでは資材の健全性のみ)

モデル更新時・スキル改訂時・定期(cron)に実行する。
golden の既定位置: --workspace 指定時は <workspace>/eval/golden を優先。
無い場合のみ後方互換で <skill>/eval/golden/{pass,fail}/*.txt

終了コード: 0=全合格 / 1=エラー / 2=回帰あり
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", type=Path, default=None, help="(任意)raw と golden の場所")
    p.add_argument("--skill", required=True, type=Path, help="生成スキル dir")
    p.add_argument(
        "--golden",
        type=Path,
        default=None,
        help="既定: <workspace>/eval/golden(存在時)、次に <skill>/eval/golden",
    )
    p.add_argument("--with-copy-check", action="store_true",
                   help="pass golden にも G6(要 --workspace)を適用。"
                        "pass golden が著者実記事(生コーパス由来)の場合は"
                        "定義上 G6 が fail するので合成 golden にのみ使う")
    return p.parse_args(argv)


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / script), *[str(a) for a in args]],
        capture_output=True,
        text=True,
    )


def _failed_gates(proc: subprocess.CompletedProcess) -> list[str]:
    """style_lint の JSON から fail したゲート名を取り出す(診断用)。

    どのゲートで落ちたかが分からないと、閾値の劣化なのか G6(コピー)なのかを
    切り分けられない。パースできない場合は空リストを返す(判定は exit code が正)。
    """
    try:
        report = json.loads(proc.stdout)
        gates = report["gates"]
    except (ValueError, KeyError, TypeError):
        return []
    return sorted(
        name for name, gate in gates.items()
        if isinstance(gate, dict) and gate.get("status") == "fail"
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    skill = args.skill
    if not (skill / "SKILL.md").exists():
        print(f"error: スキルがありません: {skill}", file=sys.stderr)
        return 1
    config = skill / "lint-config.json"
    workspace_golden = (
        args.workspace / "eval" / "golden" if args.workspace is not None else None
    )
    golden = args.golden or (
        workspace_golden
        if workspace_golden is not None and workspace_golden.is_dir()
        else skill / "eval" / "golden"
    )
    results = {"skill_lint": None, "golden_source": str(golden),
               "golden_pass": [], "golden_fail": [], "activation_cases": None}
    regressions = []

    # 1. 静的リント(--workspace 指定時は生コーパス引用検査も有効化)
    lint_args = ["--skill", skill]
    if args.workspace is not None and (args.workspace / "raw").is_dir():
        lint_args += ["--source-corpus", args.workspace / "raw"]
    r = _run("skill_lint.py", *lint_args)
    results["skill_lint"] = {"exit": r.returncode}
    if r.returncode != 0:
        regressions.append("skill_lint が不合格")

    # 2/3. golden
    def lint(path: Path) -> subprocess.CompletedProcess:
        extra = []
        if args.with_copy_check and args.workspace is not None:
            extra = ["--source-corpus", str(args.workspace / "raw")]
        return _run("style_lint.py", "--config", config, "--text", path, *extra)

    pass_dir, fail_dir = golden / "pass", golden / "fail"
    if not pass_dir.is_dir() or not list(pass_dir.glob("*.txt")):
        print(f"warning: pass golden がありません: {pass_dir}", file=sys.stderr)
    for p in sorted(pass_dir.glob("*.txt")) if pass_dir.is_dir() else []:
        r = lint(p)
        ok = r.returncode == 0
        gates = [] if ok else _failed_gates(r)
        results["golden_pass"].append(
            {"file": p.name, "exit": r.returncode, "ok": ok, "failed_gates": gates}
        )
        if not ok:
            detail = f"({'/'.join(gates)})" if gates else ""
            regressions.append(f"pass golden が fail{detail}: {p.name}")
    for p in sorted(fail_dir.glob("*.txt")) if fail_dir.is_dir() else []:
        r = lint(p)
        ok = r.returncode == 2  # fail することが期待値
        results["golden_fail"].append(
            {"file": p.name, "exit": r.returncode, "ok": ok,
             "failed_gates": _failed_gates(r)}
        )
        if not ok:
            regressions.append(
                f"fail golden が fail しない(ゲート劣化の疑い): {p.name}"
            )

    # 4. 発火テスト資材
    cases = skill / "eval" / "activation-cases.yaml"
    if cases.exists():
        text = cases.read_text(encoding="utf-8")
        ok = "{{" not in text and "positive:" in text
        results["activation_cases"] = {"present": True, "ok": ok}
        if not ok:
            regressions.append("activation-cases.yaml にプレースホルダ残存または positive 欠落")
    else:
        results["activation_cases"] = {"present": False, "ok": False}
        print(f"warning: 発火テスト資材がありません: {cases}", file=sys.stderr)

    out = {"results": results, "regressions": regressions,
           "status": "fail" if regressions else "pass"}
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
