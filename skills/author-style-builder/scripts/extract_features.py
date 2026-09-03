#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["sudachipy", "sudachidict-core"]
# ///
"""extract_features — 記事単位の定量特徴抽出と集計。

- 対象: status=eligible かつ authorship=subject-authored の記事のみ。
  転載クラスタは正準記事(dup_of が null)のみ(二重計上防止)
- features/<article_id>.json に FeatureRecord、features/_aggregate.json に
  記事単位分布(中央値・IQR・bootstrap 95% CI)を等記事重み・等文字重みの
  両方で出力する
- 形態素チャネル(lib/morph)は記事ごとの分布/スカラーを保存し、_aggregate に
  有界 centroid(上位 K + OTHER)・著者内 LOAO 距離分布・閾値を記録する。
  register / era / length の条件付き参照は十分な N があるときだけ構築し、
  それ以外は shrink / skip と理由を記録する
- `--split train+dev` が較正用の既定。`all` は holdout を含むため、
  compile_skill はそれを較正に使うことを拒否する
- fallback モードでは POS 依存特徴(func_word_rate 等)は null

終了コード: 0=成功 / 1=エラー
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import calibration as calib
from lib import features as feat
from lib import io_utils, morph, stats
from lib.tokenize import get_analyzer

SPLIT_CHOICES = ["train", "dev", "train+dev", "all"]
# 条件付き参照: n >= COND_FULL_N で独自較正、COND_SHRINK_N <= n < COND_FULL_N は
# centroid を全体へ縮約(閾値は全体のもの)、未満は skip
COND_FULL_N = morph.MIN_CALIBRATION_N
COND_SHRINK_N = 5
SHRINK_PSEUDO_N = 10
LENGTH_STRATA = {"short": [0, 800], "medium": [800, 3000], "long": [3000, None]}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument(
        "--split",
        choices=SPLIT_CHOICES,
        default="train+dev",
        help="対象 split(既定 train+dev。all は splits.json 不要だが較正には使えない)",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--bootstrap-n", type=int, default=1000)
    return p.parse_args(argv)


def _weighted_bootstrap(
    values: list[float], weights: list[float], n: int, seed: int
) -> dict:
    import random

    rng = random.Random(seed)
    k = len(values)
    medians = []
    for _ in range(n):
        idx = [rng.randrange(k) for _ in range(k)]
        medians.append(
            stats.weighted_median([values[i] for i in idx], [weights[i] for i in idx])
        )
    return {
        "median": stats.weighted_median(values, weights),
        "ci95": [stats.quantile(medians, 0.025), stats.quantile(medians, 0.975)],
    }


def register_of(record: dict) -> str:
    """文末分布からレジスターを決める(desu_masu / jotai / mixed)。"""
    f = record["sent_end_form"]
    polite = f.get("desu_masu", 0.0)
    plain = f.get("da_dearu", 0.0) + f.get("jotai_verb", 0.0) + f.get("jotai_adj", 0.0)
    if polite >= 0.5:
        return "desu_masu"
    if plain >= 0.5:
        return "jotai"
    return "mixed"


def length_stratum_of(n_chars: int) -> str:
    for name, (lo, hi) in LENGTH_STRATA.items():
        if n_chars >= (lo or 0) and (hi is None or n_chars < hi):
            return name
    return "long"


def era_of(published_at) -> str | None:
    if not published_at or len(published_at) < 4 or not published_at[:4].isdigit():
        return None
    return published_at[:4]


def calibrate_channels(records: list[tuple[dict, dict]]) -> dict:
    """記事レコード列 → チャネルごとの較正結果。"""
    out: dict = {}
    for name in morph.DIST_CHANNELS:
        per = [
            (m["article_id"], r["morph"]["dist"].get(name))
            for m, r in records
            if r["morph"]["dist"].get(name)
            and r["morph"]["sample"].get(name, 0) >= morph.CHANNELS[name]["min_sample"]
        ]
        out[name] = morph.calibrate_dist_channel(name, per)
    for name in morph.SCALAR_CHANNELS:
        per = [
            (m["article_id"], r["morph"]["scalar"].get(name))
            for m, r in records
            if r["morph"]["scalar"].get(name) is not None
            and r["morph"]["sample"].get(name, 0) >= morph.CHANNELS[name]["min_sample"]
        ]
        out[name] = morph.calibrate_scalar_channel(name, per)
    return out


def shrink_channels(group: list[tuple[dict, dict]], global_ch: dict) -> dict:
    """小 N グループ: centroid を全体へ縮約し、閾値は全体のものを使う。"""
    out: dict = {}
    n = len(group)
    for name, gref in global_ch.items():
        if gref.get("status") != "built":
            out[name] = {"status": "skipped", "reason": "global_not_built"}
            continue
        if gref["kind"] == "dist":
            dists = [
                morph.project(r["morph"]["dist"][name], gref["keys"])
                for _, r in group
                if r["morph"]["dist"].get(name)
            ]
            if not dists:
                out[name] = {"status": "skipped", "reason": "no_group_samples"}
                continue
            local = morph.mean_dist(dists)
            k = len(dists)
            centroid = {
                key: (local.get(key, 0.0) * k + gref["centroid"].get(key, 0.0) * SHRINK_PSEUDO_N)
                / (k + SHRINK_PSEUDO_N)
                for key in gref["centroid"]
            }
            ref = dict(gref)
            ref["centroid"] = {key: round(v, 6) for key, v in centroid.items()}
            ref["status"] = "shrunk"
            ref["reason"] = f"shrunk_to_global(n={n})"
            out[name] = ref
        else:
            out[name] = dict(gref, status="shrunk", reason=f"shrunk_to_global(n={n})")
    return out


def build_conditional(records: list[tuple[dict, dict]], global_ch: dict) -> dict:
    groups: dict[str, list] = {}
    for m, r in records:
        keys = [f"register:{register_of(r)}", f"length:{length_stratum_of(r['n_chars'])}"]
        era = era_of(m.get("published_at"))
        if era:
            keys.append(f"era:{era}")
        for k in keys:
            groups.setdefault(k, []).append((m, r))
    out: dict = {}
    for key in sorted(groups):
        group = groups[key]
        n = len(group)
        entry = {"n": n, "article_ids": sorted(m["article_id"] for m, _ in group)}
        if n >= COND_FULL_N:
            entry["status"] = "built"
            entry["channels"] = calibrate_channels(group)
        elif n >= COND_SHRINK_N:
            entry["status"] = "shrunk"
            entry["reason"] = f"insufficient_n_for_own_calibration({n}<{COND_FULL_N}); centroid shrunk to global, thresholds=global"
            entry["channels"] = shrink_channels(group, global_ch)
        else:
            entry["status"] = "skipped"
            entry["reason"] = f"insufficient_n({n}<{COND_SHRINK_N})"
        out[key] = entry
    return out


def build_aggregate(
    records: list[tuple[dict, dict]],
    *,
    split: str,
    analyzer_meta: dict,
    seed: int = 42,
    bootstrap_n: int = 1000,
) -> dict:
    """(meta, FeatureRecord) 列 → _aggregate.json の内容。

    meta は {"article_id", "strata", "published_at"} を持つ。テストから合成レコードで
    直接呼べるように、workspace I/O から分離している。
    """
    articles_info = [
        {
            "article_id": m["article_id"],
            "strata": m.get("strata"),
            "n_chars": r["n_chars"],
            "published_at": m.get("published_at"),
            "register": register_of(r),
            "length_stratum": length_stratum_of(r["n_chars"]),
        }
        for m, r in records
    ]
    features_agg: dict = {}
    for key in feat.SCALAR_KEYS + feat.MORPH_SCALAR_KEYS:
        pairs = [
            (m["article_id"], feat.scalar_value(r, key), r["n_chars"])
            for m, r in records
        ]
        pairs = [(aid, v, w) for aid, v, w in pairs if v is not None]
        if not pairs:
            continue  # 全記事 null(fallback の POS 依存特徴)は集計から除外
        values = [v for _, v, _ in pairs]
        weights = [float(w) for _, _, w in pairs]
        equal_article = stats.bootstrap_ci(values, n=bootstrap_n, seed=seed)
        equal_article["iqr"] = stats.iqr(values)
        equal_char = _weighted_bootstrap(values, weights, n=bootstrap_n, seed=seed)
        features_agg[key] = {
            "per_article": [[aid, v] for aid, v, _ in pairs],
            "equal_article": equal_article,
            "equal_char": equal_char,
            "min": min(values),
            "max": max(values),
        }

    global_channels = calibrate_channels(records)
    morphology = {
        "available": analyzer_meta["mode"] == "sudachi",
        "channel_registry_version": morph.CHANNEL_REGISTRY_VERSION,
        "distance": "jensen_shannon_distance(sqrt(JSD), log2)",
        "centroid": "equal-article mean, bounded to top_k + OTHER",
        "calibration_rule": {
            "dist": "warn=LOAO p90; fail=max(LOAO hard bound, p90); hard bound = LOAO max (n<large_n) or Bonferroni quantile (n>=large_n)",
            "scalar": "warn=two-sided p10/p90; fail=union(hard bound, Tukey fence); hard bound = min/max (n<large_n) or Bonferroni quantile per tail (n>=large_n)",
            "policy": calib.policy_description(),
        },
        "channels": global_channels,
        "conditional": build_conditional(records, global_channels),
        "length_strata": LENGTH_STRATA,
    }
    return {
        "feature_schema": feat.FEATURE_SCHEMA_VERSION,
        "channel_registry_version": morph.CHANNEL_REGISTRY_VERSION,
        "split": split,
        "calibration_split": split,
        "analyzer": analyzer_meta,
        "seed": seed,
        "bootstrap_n": bootstrap_n,
        "n_articles": len(records),
        "articles": articles_info,
        "features": features_agg,
        "morphology": morphology,
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    ws = args.workspace
    manifest = io_utils.load_manifest(ws)

    selected = [
        a
        for a in manifest["articles"]
        if a["status"] == "eligible"
        and a["authorship"] == "subject-authored"
        and not a.get("dup_of")
    ]
    if args.split != "all":
        splits = io_utils.load_splits(ws)
        allowed: set = set()
        for part in args.split.split("+"):
            allowed |= set(splits[part])
        selected = [a for a in selected if a["article_id"] in allowed]
    if not selected:
        print("error: 対象記事がありません", file=sys.stderr)
        return 1
    selected.sort(key=lambda a: a["article_id"])

    analyzer = get_analyzer()
    records = []
    for meta in selected:
        clean = io_utils.load_clean(ws, meta["article_id"])
        record = feat.extract_article_features(clean["blocks"], analyzer)
        record["article_id"] = meta["article_id"]
        io_utils.save_feature_record(ws, record)
        records.append((meta, record))

    aggregate = build_aggregate(
        records,
        split=args.split,
        analyzer_meta=analyzer.meta(),
        seed=args.seed,
        bootstrap_n=args.bootstrap_n,
    )
    io_utils.save_aggregate(ws, aggregate)

    global_channels = aggregate["morphology"]["channels"]
    n_built = sum(1 for c in global_channels.values() if c.get("status") == "built")
    print(
        f"extract: {len(records)} articles, {len(aggregate['features'])} features, "
        f"{n_built}/{len(global_channels)} morphology channels calibrated "
        f"(analyzer={analyzer.meta()['mode']}, split={args.split})"
    )
    if args.split == "all":
        print(
            "warning: split=all は holdout を含む。compile_skill はこの aggregate を"
            "較正に使わない(train / dev / train+dev で再実行すること)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
