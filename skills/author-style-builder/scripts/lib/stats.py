"""純 Python の統計ユーティリティ(numpy 不使用)。"""

from __future__ import annotations

import math
import random
import statistics


def median(values: list[float]) -> float:
    return float(statistics.median(values))


def quantile(values: list[float], q: float) -> float:
    """線形補間による分位点(values はソート不要)。"""
    if not values:
        raise ValueError("quantile of empty list")
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return float(s[lo] * (1 - frac) + s[hi] * frac)


def quantile_outward(values: list[float], q: float, side: str) -> float:
    """外側に丸める順序統計量の分位点(線形補間しない)。

    side="upper": 位置 ceil(q(n-1)) の値(境界を上に寄せる)。この値を厳密に超える標本は
    高々 floor((1-q)(n-1)) 個。side="lower": 位置 floor(q(n-1)) の値(境界を下に寄せる)。
    hard 境界の許容誤りを標本数で厳密に押さえるために使う(lib/calibration)。
    """
    if not values:
        raise ValueError("quantile of empty list")
    s = sorted(values)
    n = len(s)
    if n == 1:
        return float(s[0])
    pos = q * (n - 1)
    if side == "upper":
        idx = min(int(math.ceil(pos - 1e-12)), n - 1)
    elif side == "lower":
        idx = max(int(math.floor(pos + 1e-12)), 0)
    else:
        raise ValueError(f"side must be upper|lower: {side}")
    return float(s[idx])


def iqr(values: list[float]) -> list[float]:
    """[Q1, Q3]"""
    return [quantile(values, 0.25), quantile(values, 0.75)]


def weighted_median(values: list[float], weights: list[float]) -> float:
    """重み付き中央値(重みの累積 50% 点)。"""
    if not values:
        raise ValueError("weighted_median of empty list")
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    if total <= 0:
        return median(values)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return float(v)
    return float(pairs[-1][0])


def bootstrap_ci(
    values: list[float], n: int = 1000, seed: int = 42
) -> dict:
    """記事単位 bootstrap による中央値と 95% CI。

    返り値: {"median": float, "ci95": [lo, hi]}
    """
    if not values:
        raise ValueError("bootstrap_ci of empty list")
    rng = random.Random(seed)
    k = len(values)
    medians = []
    for _ in range(n):
        sample = [values[rng.randrange(k)] for _ in range(k)]
        medians.append(statistics.median(sample))
    return {
        "median": median(values),
        "ci95": [quantile(medians, 0.025), quantile(medians, 0.975)],
    }


def bootstrap_direction_agreement(
    author_vals: list[float],
    ref_vals: list[float],
    n: int = 1000,
    seed: int = 42,
) -> float:
    """bootstrap リサンプルで median(author) - median(ref) の符号が
    元の符号と一致する割合を返す。元の差が 0 なら 0.0。"""
    if not author_vals or not ref_vals:
        raise ValueError("empty input")
    base = median(author_vals) - median(ref_vals)
    if base == 0:
        return 0.0
    base_sign = 1 if base > 0 else -1
    rng = random.Random(seed)
    ka, kr = len(author_vals), len(ref_vals)
    agree = 0
    for _ in range(n):
        a = [author_vals[rng.randrange(ka)] for _ in range(ka)]
        r = [ref_vals[rng.randrange(kr)] for _ in range(kr)]
        diff = statistics.median(a) - statistics.median(r)
        sign = 1 if diff > 0 else (-1 if diff < 0 else 0)
        if sign == base_sign:
            agree += 1
    return agree / n


def cliffs_delta(a: list[float], b: list[float]) -> float:
    """Cliff's delta 効果量。a が b より大きい傾向なら正。"""
    if not a or not b:
        raise ValueError("empty input")
    gt = lt = 0
    for x in a:
        for y in b:
            if x > y:
                gt += 1
            elif x < y:
                lt += 1
    return (gt - lt) / (len(a) * len(b))


def loao_stable(values: list[float], direction_fn) -> bool:
    """leave-one-article-out で direction_fn(残り) が全一致するか。

    direction_fn: list[float] -> hashable(方向を表す値。bool 等)
    """
    if len(values) < 2:
        return False
    base = direction_fn(values)
    for i in range(len(values)):
        rest = values[:i] + values[i + 1 :]
        if direction_fn(rest) != base:
            return False
    return True
