#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["sudachipy", "sudachidict-core"]
# ///
"""corpus_split — 記事(転載クラスタ)単位・時系列の train/dev/holdout 分割。

- published_at(なければ retrieval_timestamp)昇順に並べ、古い順に
  train / dev / holdout(最新)へ割当てる。転載クラスタは同一 split に置く
- 分割後に文字 8-gram の跨割リークチェックを行い、splits.json に記録する

終了コード: 0=成功 / 1=エラー / 2=リークチェック不合格
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import features as feat
from lib import io_utils

LEAK_NGRAM = 8
LEAK_JACCARD_FAIL = 0.3


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--ratio", default="70,15,15", help="train,dev,holdout(合計 100)")
    return p.parse_args(argv)


def parse_ratio(text: str) -> list[int]:
    parts = [int(x) for x in text.split(",")]
    if len(parts) != 3 or sum(parts) != 100 or any(x < 0 for x in parts):
        raise ValueError(f"invalid --ratio: {text}(例: 70,15,15)")
    return parts


def sort_key(article: dict) -> tuple:
    date = article["published_at"] or article["retrieval_timestamp"][:10]
    return (date, article["article_id"])


def allocate(n_units: int, ratio: list[int]) -> list[int]:
    """ユニット数を比率で 3 分割(各非ゼロ比率に最低 1 ユニット)。"""
    counts = [round(n_units * r / 100) for r in ratio]
    for i, r in enumerate(ratio):
        if r > 0 and counts[i] == 0 and n_units >= 3:
            counts[i] = 1
    # 合計調整は train で吸収
    counts[0] = n_units - counts[1] - counts[2]
    if counts[0] < 0:
        raise ValueError(f"too few units ({n_units}) for ratio {ratio}")
    return counts


def main(argv=None) -> int:
    args = parse_args(argv)
    ws = args.workspace
    try:
        ratio = parse_ratio(args.ratio)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    manifest = io_utils.load_manifest(ws)
    eligible = [a for a in manifest["articles"] if a["status"] == "eligible"]
    if not eligible:
        print("error: eligible な記事がありません", file=sys.stderr)
        return 1
    by_id = {a["article_id"]: a for a in eligible}

    # 転載クラスタを 1 ユニットに(クラスタ日付は最古メンバー)
    cluster_of: dict = {}
    for cluster in manifest.get("dup_clusters", []):
        members = [m for m in cluster if m in by_id]
        if len(members) < 2:
            continue
        for m in members:
            cluster_of[m] = tuple(sorted(members))

    units: dict = {}
    for a in eligible:
        key = cluster_of.get(a["article_id"], (a["article_id"],))
        units.setdefault(key, []).append(a)
    unit_list = sorted(
        units.values(), key=lambda arts: min(sort_key(a) for a in arts)
    )

    n_train, n_dev, n_holdout = allocate(len(unit_list), ratio)
    split_names = (
        ["train"] * n_train + ["dev"] * n_dev + ["holdout"] * n_holdout
    )
    splits: dict = {"train": [], "dev": [], "holdout": []}
    for unit, name in zip(unit_list, split_names):
        for a in sorted(unit, key=sort_key):
            splits[name].append(a["article_id"])

    # 8-gram 跨割リークチェック(記事ペア単位、重複クラスタ内は同 split 前提)
    grams = {
        aid: feat.char_ngrams(
            io_utils.body_text(io_utils.load_clean(ws, aid)), LEAK_NGRAM
        )
        for aid in by_id
    }
    leaks = []
    max_jaccard = 0.0
    names = ["train", "dev", "holdout"]
    for i, sa in enumerate(names):
        for sb in names[i + 1 :]:
            for ida in splits[sa]:
                for idb in splits[sb]:
                    j = feat.jaccard(grams[ida], grams[idb])
                    max_jaccard = max(max_jaccard, j)
                    if j > LEAK_JACCARD_FAIL:
                        leaks.append(
                            {
                                "pair": [ida, idb],
                                "splits": [sa, sb],
                                "jaccard": round(j, 4),
                            }
                        )

    leak_check = {
        "ngram": LEAK_NGRAM,
        "fail_threshold": LEAK_JACCARD_FAIL,
        "max_cross_split_jaccard": round(max_jaccard, 4),
        "leaks": leaks,
        "passed": not leaks,
    }
    result = {
        "train": splits["train"],
        "dev": splits["dev"],
        "holdout": splits["holdout"],
        "ratio": ratio,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "leak_check": leak_check,
    }
    io_utils.save_splits(ws, result)

    print(
        f"split: train={len(splits['train'])} dev={len(splits['dev'])} "
        f"holdout={len(splits['holdout'])} leak_check="
        f"{'pass' if leak_check['passed'] else 'FAIL'}"
    )
    if not leak_check["passed"]:
        for leak in leaks:
            print(f"leak: {leak}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
