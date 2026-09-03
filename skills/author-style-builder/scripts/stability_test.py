#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["sudachipy", "sudachidict-core"]
# ///
"""stability_test — 候補 claim の生成と安定性検定。

features/_aggregate.json の各スカラー特徴について、工学的基準値
(--baseline で差し替え可能)に対する方向を仮 claim とし、

1. 3 記事以上・2 層以上の支持
2. 記事単位 bootstrap の 70% 以上で方向一致
3. leave-one-article-out(LOAO)で方向が反転しない

を判定する。masking・対照著者(cross_topic)検定は未実装のため
control_result に not_run を記録する。結果は profile-candidates.json に
出力し、profile.json は書き換えない。

終了コード: 0=成功 / 1=エラー
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import features as feat
from lib import io_utils, morph, stats

# 工学的初期値(要・自コーパス検証)。日本語 Web 記事の一般的な目安。
DEFAULT_BASELINE = {
    "sent_len_median": 45.0,
    "sent_len_max": 120.0,
    "para_len_median": 3.0,
    "comma_per_sent_median": 1.2,
    "max_consecutive_same_ending": 3.0,
    "sent_end_form.desu_masu": 0.4,
    "sent_end_form.da_dearu": 0.3,
    "sent_end_form.taigen": 0.1,
    "sent_end_form.question": 0.05,
    "sent_end_form.other": 0.15,
    "script_ratio.kanji": 0.32,
    "script_ratio.hiragana": 0.48,
    "script_ratio.katakana": 0.08,
    "script_ratio.latin": 0.04,
    "script_ratio.digit": 0.02,
    "script_ratio.other": 0.06,
    "func_word_rate": 0.5,
    "ttr_window": 0.7,
    "distinct_2": 0.85,
}

_CATEGORY_BY_PREFIX = [
    ("sent_end_form.", "文末"),
    ("max_consecutive_same_ending", "文末"),
    ("script_ratio.", "表記"),
    ("func_word_rate", "語彙"),
    ("ttr_window", "語彙"),
    ("distinct_2", "語彙"),
    ("para_len", "構造"),
]

AGREEMENT_THRESHOLD = 0.7
MIN_ARTICLES = 3
MIN_STRATA = 2


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument(
        "--feature",
        nargs="*",
        default=None,
        help="検定する特徴キー(既定: aggregate 中の基準値のある全特徴)",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--bootstrap-n", type=int, default=1000)
    p.add_argument(
        "--baseline", type=Path, default=None, help="基準値 JSON(キー→数値)"
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="出力先(既定 <workspace>/profile-candidates.json)。既存候補を保って移行候補を別名で出すときに使う",
    )
    return p.parse_args(argv)


def category_of(key: str) -> str:
    for prefix, cat in _CATEGORY_BY_PREFIX:
        if key.startswith(prefix):
            return cat
    return "文"


def first_body_span(workspace: Path, article_id: str) -> dict | None:
    clean = io_utils.load_clean(workspace, article_id)
    for block in clean["blocks"]:
        if block["type"] == "body":
            return {
                "article_id": article_id,
                "char_start": block["char_start"],
                "char_end": block["char_end"],
            }
    return None


def build_claim(
    key: str,
    per_article: list,
    baseline: float,
    strata_by_id: dict,
    analyzer: dict,
    consent_record,
    workspace: Path,
    seed: int,
    bootstrap_n: int,
) -> dict | None:
    ids = [aid for aid, _ in per_article]
    values = [v for _, v in per_article]
    med = stats.median(values)
    if med == baseline:
        return None  # 方向なし
    higher = med > baseline

    supporting = [
        (aid, v) for aid, v in per_article if (v > baseline) == higher and v != baseline
    ]
    support_articles = len(supporting)
    support_strata = len({strata_by_id.get(aid, "unknown") for aid, _ in supporting})

    agreement = stats.bootstrap_direction_agreement(
        values, [baseline], n=bootstrap_n, seed=seed
    )
    loao = stats.loao_stable(values, lambda vs: stats.median(vs) > baseline)
    ci = stats.bootstrap_ci(values, n=bootstrap_n, seed=seed)
    effect = stats.cliffs_delta(values, [baseline])

    gates = {
        "support": support_articles >= MIN_ARTICLES and support_strata >= MIN_STRATA,
        "bootstrap": agreement >= AGREEMENT_THRESHOLD,
        "loao": loao,
    }
    if all(gates.values()):
        status = "core"
        compilation_target = "always_on_rule"
    elif not gates["support"]:
        status = "local"
        compilation_target = "example"
    else:
        status = "ambiguous"
        compilation_target = "checklist"

    direction_text = "高い" if higher else "低い"
    evidence = []
    for aid, _ in sorted(supporting, key=lambda t: abs(t[1] - baseline), reverse=True)[:3]:
        span = first_body_span(workspace, aid)
        if span:
            evidence.append(span)
    if not evidence:
        return None  # evidence 最低 1 span 必須(profile-schema.md)

    slug = key.replace(".", "-").replace("_", "-")
    return {
        "claim_id": f"{slug}-001",
        "category": category_of(key),
        "scope_mode": "core",
        "condition": None,
        "rule_text": (
            f"{key} が基準値 {baseline} より{direction_text}"
            f"(中央値 {round(med, 4)})"
        ),
        "feature": {
            "analyzer": (
                f"sudachipy=={analyzer['version']}"
                if analyzer["mode"] == "sudachi"
                else "fallback"
            ),
            "dictionary": analyzer.get("dict"),
            "split_mode": analyzer.get("split_mode"),
            "schema": feat.FEATURE_SCHEMA_VERSION,
            "channel_registry_version": morph.CHANNEL_REGISTRY_VERSION,
            "metric": key,
            "denominator": "記事単位スカラー(feature-catalog.md 参照)",
        },
        "value": {
            "median": med,
            "range": stats.iqr(values),
            "effect_size": effect,
            "ci95": ci["ci95"],
        },
        "evidence": evidence,
        "support": {
            "articles": support_articles,
            "strata": support_strata,
            "bootstrap_agreement": agreement,
        },
        "control_result": {
            "masking": "not_run",
            "cross_topic": "not_run",
            "loao": "pass" if loao else "fail",
        },
        "state": "observed",
        "status": status,
        "compilation_target": compilation_target,
        "rights_scope": consent_record,
        "confidence": "high" if (all(gates.values()) and agreement >= 0.9) else "medium",
        "version": "1.0.0",
        "history": [],
        "gates": gates,
        "baseline": baseline,
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    ws = args.workspace
    try:
        aggregate = io_utils.load_aggregate(ws)
    except FileNotFoundError:
        print(
            "error: features/_aggregate.json がありません。extract_features.py を先に実行してください",
            file=sys.stderr,
        )
        return 1
    manifest = io_utils.load_manifest(ws)

    baseline = dict(DEFAULT_BASELINE)
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as f:
            baseline.update(json.load(f))

    available = aggregate["features"]
    keys = args.feature if args.feature else sorted(available)
    unknown = [k for k in keys if k not in available]
    if unknown:
        print(f"error: aggregate に無い特徴: {unknown}", file=sys.stderr)
        return 1

    strata_by_id = {a["article_id"]: a["strata"] for a in aggregate["articles"]}
    consent_record = manifest.get("consent", {}).get("record")

    candidates = []
    skipped = []
    for key in keys:
        if key not in baseline:
            skipped.append(key)
            continue
        claim = build_claim(
            key,
            available[key]["per_article"],
            float(baseline[key]),
            strata_by_id,
            aggregate["analyzer"],
            consent_record,
            ws,
            args.seed,
            args.bootstrap_n,
        )
        if claim is not None:
            candidates.append(claim)

    agg_schema = aggregate.get("feature_schema")
    if agg_schema != feat.FEATURE_SCHEMA_VERSION:
        print(
            f"warning: aggregate の feature_schema={agg_schema!r} が builder の"
            f" {feat.FEATURE_SCHEMA_VERSION!r} と違う。extract_features.py を再実行すること"
            "(候補は生成するが compile_skill はこの aggregate を拒否する)",
            file=sys.stderr,
        )

    out = {
        "author_id": manifest["author_id"],
        "feature_schema": agg_schema,
        "channel_registry_version": aggregate.get("channel_registry_version"),
        "source_split": aggregate["split"],
        "analyzer": aggregate["analyzer"],
        "seed": args.seed,
        "bootstrap_n": args.bootstrap_n,
        "baseline_source": (
            str(args.baseline) if args.baseline else "built_in_engineering_defaults"
        ),
        "note": (
            "masking / cross_topic 検定は未実装(not_run)。"
            "profile.json への反映は人間レビュー後に別途行うこと"
        ),
        "skipped_features": skipped,
        "candidates": candidates,
    }
    out_path = args.out or (ws / "profile-candidates.json")
    io_utils.save_json(out_path, out)

    n_core = sum(1 for c in candidates if c["status"] == "core")
    print(
        f"stability: {len(candidates)} candidates "
        f"({n_core} core, {len(candidates) - n_core} demoted, "
        f"{len(skipped)} skipped) -> {out_path.name} (feature_schema={agg_schema})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
