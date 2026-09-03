"""workspace ファイル I/O(スキーマ軽検証つき)。

原則: manifest / profile は破壊的変更禁止 → 上書き前に .bak を作る。
出力 JSON は sort_keys=True / ensure_ascii=False で決定的に書く。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data, *, backup: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


# ---- manifest ----

_ARTICLE_META_KEYS = {
    "article_id",
    "canonical_url",
    "retrieval_timestamp",
    "license",
    "consent_record",
    "content_hash",
    "status",
    "authorship",
    "strata",
    "published_at",
    "char_count",
}

_STATUSES = {"eligible", "quarantined", "excluded", "withdrawn"}


def validate_manifest(manifest: dict) -> None:
    for key in ("author_id", "consent", "articles"):
        if key not in manifest:
            raise ValueError(f"manifest missing key: {key}")
    for art in manifest["articles"]:
        missing = _ARTICLE_META_KEYS - set(art)
        if missing:
            raise ValueError(
                f"article {art.get('article_id', '?')} missing: {sorted(missing)}"
            )
        if art["status"] not in _STATUSES:
            raise ValueError(f"invalid status: {art['status']}")


def load_manifest(workspace: Path) -> dict:
    manifest = load_json(Path(workspace) / "manifest.json")
    validate_manifest(manifest)
    return manifest


def save_manifest(workspace: Path, manifest: dict) -> None:
    validate_manifest(manifest)
    save_json(Path(workspace) / "manifest.json", manifest, backup=True)


# ---- clean blocks ----

_BLOCK_TYPES = {"body", "quote", "code", "boilerplate", "editorial"}


def validate_clean(clean: dict) -> None:
    for key in ("article_id", "blocks"):
        if key not in clean:
            raise ValueError(f"clean record missing key: {key}")
    for b in clean["blocks"]:
        for key in ("type", "text", "char_start", "char_end"):
            if key not in b:
                raise ValueError(f"block missing key: {key}")
        if b["type"] not in _BLOCK_TYPES:
            raise ValueError(f"invalid block type: {b['type']}")


def load_clean(workspace: Path, article_id: str) -> dict:
    clean = load_json(Path(workspace) / "clean" / f"{article_id}.json")
    validate_clean(clean)
    return clean


def save_clean(workspace: Path, clean: dict) -> None:
    validate_clean(clean)
    save_json(Path(workspace) / "clean" / f"{clean['article_id']}.json", clean)


def body_text(clean: dict) -> str:
    """body ブロックのみを連結したテキスト。"""
    return "\n".join(b["text"] for b in clean["blocks"] if b["type"] == "body")


# ---- splits ----

def validate_splits(splits: dict) -> None:
    for key in ("train", "dev", "holdout", "created_at", "leak_check"):
        if key not in splits:
            raise ValueError(f"splits missing key: {key}")


def load_splits(workspace: Path) -> dict:
    splits = load_json(Path(workspace) / "splits.json")
    validate_splits(splits)
    return splits


def save_splits(workspace: Path, splits: dict) -> None:
    validate_splits(splits)
    save_json(Path(workspace) / "splits.json", splits)


# ---- features ----

def save_feature_record(workspace: Path, record: dict) -> None:
    if "article_id" not in record:
        raise ValueError("feature record missing article_id")
    save_json(
        Path(workspace) / "features" / f"{record['article_id']}.json", record
    )


def load_feature_record(workspace: Path, article_id: str) -> dict:
    return load_json(Path(workspace) / "features" / f"{article_id}.json")


def load_aggregate(workspace: Path) -> dict:
    return load_json(Path(workspace) / "features" / "_aggregate.json")


def save_aggregate(workspace: Path, aggregate: dict) -> None:
    save_json(Path(workspace) / "features" / "_aggregate.json", aggregate)


# ---- profile ----

def load_profile(workspace: Path) -> dict:
    return load_json(Path(workspace) / "profile.json")


def save_profile(workspace: Path, profile: dict) -> None:
    save_json(Path(workspace) / "profile.json", profile, backup=True)
