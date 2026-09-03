"""合成コーパス生成(テスト用。著作権フリーの自作句をシード付き乱数で組み合わせる)。

- register: "desu_masu" / "jotai" / "mixed"
- list_heavy: 箇条書き段落を混ぜる
- inline_code: インラインコード / URL を挿入する密度(0..1)
- n_paragraphs / sents_per_para で長さを制御

生成文は自然文としての品質より、形態素統計の多様性(文末・助詞・読点位置)を
再現可能に揺らすことを目的にする。
"""

from __future__ import annotations

import random

_SUBJECTS = ["この設定", "新しい手順", "小さな変更", "チームの慣習", "設計の判断", "テストの書き方", "履歴の読み方", "命名の規則", "依存の整理", "レビューの流れ"]
_TOPICS = ["コミット", "レビュー", "ビルド", "リリース", "設定", "検証", "移行", "監視", "記録", "共有"]
_OBJECTS = ["理由", "意図", "手順", "範囲", "前提", "結果", "例外", "影響", "順序", "境界"]
_ADVERBS = ["まず", "ただし", "つまり", "たとえば", "一方で", "それでも", "加えて", "実際には", "むしろ", "少なくとも"]
_MODIFIERS = ["短い", "小さな", "明確な", "単純な", "具体的な", "静かな", "素直な", "丁寧な"]
_CONJ = ["しかし", "そして", "また", "ただ", "なお", "つまり"]

_POLITE_ENDS = [
    "を先に決めます。",
    "が分かりやすいです。",
    "を残しておきます。",
    "を書きます。",
    "は変えません。",
    "を確認しました。",
    "が必要でした。",
    "を見直します。",
    "を短く保ちます。",
    "も同じです。",
]
_PLAIN_ENDS = [
    "を先に決める。",
    "が分かりやすい。",
    "を残しておく。",
    "を書いた。",
    "は変えない。",
    "を確認した。",
    "が必要だった。",
    "を見直す。",
    "を短く保つ。",
    "も同じだ。",
    "が要点である。",
]
_TAIGEN_ENDS = ["が要点。", "の話。", "が前提。", "だけの違い。"]
_QUESTION_ENDS = ["は本当に必要でしょうか？", "はどこで決まるのか。"]
_HEDGES = ["と思います。", "かもしれません。", "はずです。", "ようです。", "気がします。", "だろう。", "かもしれない。", "と思う。"]


def _clause(rng: random.Random, *, comma: bool) -> str:
    parts = []
    if rng.random() < 0.4:
        parts.append(rng.choice(_ADVERBS))
        if comma:
            parts.append("、")
    parts.append(rng.choice(_SUBJECTS))
    if rng.random() < 0.5:
        parts.append("は")
        parts.append(rng.choice(_MODIFIERS))
        parts.append(rng.choice(_OBJECTS))
    else:
        parts.append("の")
        parts.append(rng.choice(_OBJECTS))
    if comma and rng.random() < 0.3:
        parts.append("、")
        parts.append(rng.choice(_TOPICS))
        parts.append("の")
        parts.append(rng.choice(_OBJECTS))
    return "".join(parts)


def sentence(rng: random.Random, register: str) -> str:
    body = _clause(rng, comma=rng.random() < 0.6)
    r = rng.random()
    if register == "desu_masu":
        polite = 0.85
    elif register == "jotai":
        polite = 0.05
    else:
        polite = 0.5
    if r < 0.06:
        end = rng.choice(_TAIGEN_ENDS)
    elif r < 0.09:
        end = rng.choice(_QUESTION_ENDS)
    elif r < 0.2:
        end = rng.choice(_HEDGES[:5] if rng.random() < polite else _HEDGES[5:])
        body = body + "は大事だ" if rng.random() < 0.5 else body + "が要る"
        return body + end
    elif rng.random() < polite:
        end = rng.choice(_POLITE_ENDS)
    else:
        end = rng.choice(_PLAIN_ENDS)
    if rng.random() < 0.15:
        body = "私は" + body
    if rng.random() < 0.1:
        body = rng.choice(_CONJ) + "、" + body
    return body + end


def article(
    seed: int,
    *,
    register: str = "desu_masu",
    n_paragraphs: int = 4,
    sents_per_para: tuple[int, int] = (2, 5),
    list_heavy: bool = False,
    inline_code: float = 0.0,
    published_at: str = "2025-01-01",
    strata: str = "tech",
    with_frontmatter: bool = True,
) -> str:
    rng = random.Random(seed)
    paras = []
    for pi in range(n_paragraphs):
        n = rng.randint(*sents_per_para)
        sents = [sentence(rng, register) for _ in range(n)]
        if inline_code > 0:
            out = []
            for s in sents:
                if rng.random() < inline_code:
                    code = rng.choice(["`git commit`", "`npm run build`", "https://example.com/doc", "`config.toml`"])
                    idx = s.find("は")
                    s = s[: idx + 1] + code + s[idx + 1 :] if idx > 0 else code + s
                out.append(s)
            sents = out
        if list_heavy and pi % 2 == 1:
            paras.append("\n".join("- " + s for s in sents))
        else:
            paras.append("".join(sents))
    body = "\n\n".join(paras) + "\n"
    if not with_frontmatter:
        return body
    return f"---\npublished_at: {published_at}\nstrata: {strata}\n---\n# 見出し\n\n" + body


def corpus(
    n: int,
    *,
    seed: int = 7,
    register: str = "desu_masu",
    n_paragraphs: tuple[int, int] = (3, 6),
    list_heavy: bool = False,
    inline_code: float = 0.0,
) -> list[tuple[str, str]]:
    """[(article_id, markdown)] を返す。published_at と strata を交互に振る。"""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        year = 2023 + (i % 3)
        month = 1 + (i % 12)
        out.append(
            (
                f"s{i:04d}",
                article(
                    seed * 1000 + i,
                    register=register,
                    n_paragraphs=rng.randint(*n_paragraphs),
                    list_heavy=list_heavy,
                    inline_code=inline_code,
                    published_at=f"{year}-{month:02d}-{1 + (i % 27):02d}",
                    strata="tech" if i % 2 == 0 else "essay",
                ),
            )
        )
    return out
