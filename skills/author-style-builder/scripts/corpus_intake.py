#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["sudachipy", "sudachidict-core"]
# ///
"""corpus_intake — txt/md コーパスの取込・ブロック分類・重複検出・manifest 更新。

- raw/<article_id>.txt に不変スナップショット保存(既存と異なる内容なら拒否)
- 規則ベースのブロック分類(lib/blocks.py の共有契約): frontmatter/見出し →
  boilerplate、コードフェンス・Markdown の4スペースインデント → code、
  引用行 → quote、編集部注 → editorial、残り(空行区切りの段落)→ body
- ブロック健全性検査: body 空、コード記法残存、コードらしい行の過剰混入を検出。
  fail は quarantined、警告・測定値は manifest の block_health に保存
- 重複検出: 完全一致(body hash)+ 近似(文字 5-gram Jaccard >= 0.8)→ クラスタ化
- consent 未記録なら警告し status=quarantined

終了コード: 0=成功 / 1=エラー
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import blocks as blocks_lib
from lib import features as feat
from lib import io_utils

NEAR_DUP_JACCARD = 0.8
NEAR_DUP_NGRAM = 5
_CONSENT_LEVELS = (
    "self_attested",
    "direct_record",
    "authorized_delegate",
    "user_reported",
)

# ブロック分類・健全性検査は lib/blocks.py の共有契約に委譲する
# (extract_features / style_lint / overlap_check と同一の散文抽出を保証)。
parse_frontmatter = blocks_lib.parse_frontmatter
classify_blocks = blocks_lib.classify_blocks
check_block_health = blocks_lib.check_block_health
_is_indented_code_line = blocks_lib.is_indented_code_line


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument(
        "--input", required=True, nargs="+", type=Path, help="入力ディレクトリ or ファイル"
    )
    p.add_argument("--author-id", required=True)
    p.add_argument(
        "--consent",
        default=None,
        help="同意記録の参照(本文でなく保存先・チケット等)。未指定なら quarantined",
    )
    p.add_argument(
        "--consent-level",
        choices=_CONSENT_LEVELS,
        default=None,
        help="証拠水準。--consent 指定時の既定は user_reported",
    )
    return p.parse_args(argv)


def collect_files(inputs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in inputs:
        if path.is_dir():
            files.extend(
                p for p in sorted(path.rglob("*")) if p.suffix in (".txt", ".md")
            )
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(f"input not found: {path}")
    # 決定的順序
    return sorted(set(files), key=lambda p: p.name)


def cluster_duplicates(bodies: dict) -> list[list[str]]:
    """完全一致 + 5-gram Jaccard による重複クラスタ(サイズ 2 以上のみ)。"""
    ids = sorted(bodies)
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    hashes = {i: io_utils.content_hash(bodies[i]) for i in ids}
    grams = {i: feat.char_ngrams(bodies[i], NEAR_DUP_NGRAM) for i in ids}
    for idx, a in enumerate(ids):
        for b in ids[idx + 1 :]:
            if hashes[a] == hashes[b] or feat.jaccard(grams[a], grams[b]) >= NEAR_DUP_JACCARD:
                union(a, b)

    clusters: dict = {}
    for i in ids:
        clusters.setdefault(find(i), []).append(i)
    return sorted([sorted(c) for c in clusters.values() if len(c) > 1])


def main(argv=None) -> int:
    args = parse_args(argv)
    ws = args.workspace
    (ws / "raw").mkdir(parents=True, exist_ok=True)
    (ws / "clean").mkdir(parents=True, exist_ok=True)

    try:
        files = collect_files(args.input)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not files:
        print("error: no input .txt/.md files found", file=sys.stderr)
        return 1

    if args.consent is None:
        print(
            "warning: consent 未記録。全記事を status=quarantined で取り込みます",
            file=sys.stderr,
        )

    manifest_path = ws / "manifest.json"
    if manifest_path.exists():
        manifest = io_utils.load_manifest(ws)
        if manifest["author_id"] != args.author_id:
            print(
                f"error: workspace author_id mismatch: {manifest['author_id']}",
                file=sys.stderr,
            )
            return 1
    else:
        manifest = {"author_id": args.author_id, "consent": {}, "articles": []}
    if args.consent is None and args.consent_level is not None:
        print("error: --consent-level には --consent が必要です", file=sys.stderr)
        return 1
    consent_level = (
        args.consent_level
        if args.consent is not None
        else None
    ) or ("user_reported" if args.consent is not None else "none")
    manifest["consent"] = {
        "record": args.consent,
        "granted": args.consent is not None,
        "evidence_level": consent_level,
        "verification_status": (
            "verified"
            if consent_level in ("self_attested", "direct_record", "authorized_delegate")
            else ("reported_unverified" if args.consent is not None else "missing")
        ),
    }

    by_id = {a["article_id"]: a for a in manifest["articles"]}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    bodies: dict = {}

    for path in files:
        article_id = re.sub(r"[^A-Za-z0-9_-]", "-", path.stem)
        text = path.read_text(encoding="utf-8")

        raw_path = ws / "raw" / f"{article_id}.txt"
        if raw_path.exists():
            if raw_path.read_text(encoding="utf-8") != text:
                print(
                    f"error: raw/{article_id}.txt は不変スナップショットです。"
                    "内容の異なる再取込は拒否します",
                    file=sys.stderr,
                )
                return 1
        else:
            raw_path.write_text(text, encoding="utf-8")

        meta, body_start = parse_frontmatter(text)
        blocks = classify_blocks(text, body_start)
        clean = {"article_id": article_id, "blocks": blocks}
        io_utils.save_clean(ws, clean)

        body = io_utils.body_text(clean)
        bodies[article_id] = body
        health = check_block_health(blocks)

        status = (
            "eligible"
            if args.consent and health["status"] != "fail"
            else "quarantined"
        )
        by_id[article_id] = {
            "article_id": article_id,
            "canonical_url": meta.get("canonical_url"),
            "retrieval_timestamp": by_id.get(article_id, {}).get(
                "retrieval_timestamp", now
            ),
            "license": meta.get("license"),
            "consent_record": args.consent,
            "content_hash": io_utils.content_hash(text),
            "status": status,
            "authorship": meta.get("authorship") or "subject-authored",
            "strata": meta.get("strata") or "unknown",
            "published_at": meta.get("published_at"),
            "char_count": len(body.replace("\n", "")),
            "block_health": health,
        }

    clusters = cluster_duplicates(bodies)
    dup_of: dict = {}
    for cluster in clusters:
        members = sorted(
            cluster,
            key=lambda i: (by_id[i]["published_at"] or "9999-99-99", i),
        )
        canonical = members[0]
        for m in members[1:]:
            dup_of[m] = canonical
    for aid, meta_rec in by_id.items():
        meta_rec["dup_of"] = dup_of.get(aid)

    manifest["articles"] = [by_id[k] for k in sorted(by_id)]
    manifest["dup_clusters"] = clusters
    io_utils.save_manifest(ws, manifest)

    n_q = sum(1 for a in manifest["articles"] if a["status"] == "quarantined")
    n_health = sum(
        1 for a in manifest["articles"] if a.get("block_health", {}).get("status") != "pass"
    )
    print(
        f"intake: {len(files)} files -> {len(by_id)} articles "
        f"({n_q} quarantined, {n_health} health warnings/failures, "
        f"{len(clusters)} dup clusters)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
