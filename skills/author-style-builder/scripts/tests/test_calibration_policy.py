"""較正ポリシー(lib/calibration)の性質テスト。

- 較正 → 較正観測を評価、で文書化された per-check / 記事レベルの誤り方針に従う
  (n < 100: 較正観測は hard fail しない。n >= 100: Bonferroni 分位点の離散上限)
- 丸め由来の自己 fail(境界 = round(極値))の回帰
- 独立 p99 の多重検定問題を「隠さず」検証する(旧規則なら記事レベルの fail 率が
  ARTICLE_ALPHA を超えることを明示し、新規則で上限内に収まることを確認する)
"""

import math
import random

import pytest

from lib import calibration as calib
from lib import morph as morph_lib
from lib import stats

# policy境界と丸い合成sample sizeのみを使う。実コーパス件数はfixtureへ持ち込まない。
NS = [10, 20, 32, 50, 64, 99, 100, 128, 500]
DIST_CHANNEL = "pos_unigram"  # fail 可能な分布チャネル
SCALAR_CHANNEL = "modifier_density"  # fail 可能なスカラーチャネル
BIG = 10**6  # min_sample を満たす sample_n


def _rand_dist(rng: random.Random, n_keys: int = 12) -> dict:
    w = [rng.gammavariate(0.7, 1.0) for _ in range(n_keys)]
    s = sum(w)
    return {f"k{i}": v / s for i, v in enumerate(w)}


def _rand_scalar(rng: random.Random) -> float:
    return rng.lognormvariate(-3.0, 0.4)


@pytest.mark.parametrize("n", NS)
def test_dist_calibration_observations_obey_policy(n):
    rng = random.Random(100 + n)
    per = [(f"a{i}", _rand_dist(rng)) for i in range(n)]
    ref = morph_lib.calibrate_dist_channel(DIST_CHANNEL, per)
    assert ref["status"] == "built"
    statuses = [morph_lib.evaluate_dist(DIST_CHANNEL, d, ref, BIG)["status"] for _, d in per]
    fails = statuses.count("fail")
    alpha = calib.per_check_alpha()
    allowed = calib.max_exceed_count(n, alpha)
    assert fails <= allowed, (n, fails, allowed)
    if n < calib.LARGE_N:
        assert fails == 0
        assert "author_max(small_n)" in ref["thresholds"]["fail_rule"]
        assert ref["thresholds"]["fail"] >= max(ref["loao"]["distances"])
    else:
        assert "bonferroni" in ref["thresholds"]["fail_rule"]
    assert ref["thresholds"]["fail"] >= ref["thresholds"]["warn"]


@pytest.mark.parametrize("n", NS)
def test_scalar_calibration_observations_obey_policy(n):
    rng = random.Random(200 + n)
    per = [(f"a{i}", _rand_scalar(rng)) for i in range(n)]
    ref = morph_lib.calibrate_scalar_channel(SCALAR_CHANNEL, per)
    assert ref["status"] == "built"
    statuses = [morph_lib.evaluate_scalar(SCALAR_CHANNEL, v, ref, BIG)["status"] for _, v in per]
    fails = statuses.count("fail")
    alpha_tail = calib.per_check_alpha() / 2
    allowed = 2 * calib.max_exceed_count(n, alpha_tail)
    assert fails <= allowed, (n, fails, allowed)
    lo, hi = ref["thresholds"]["fail"]
    if n < calib.LARGE_N:
        assert fails == 0
        assert lo <= ref["min"] and hi >= ref["max"]  # 極値そのものは fail しない
        assert "author_min(small_n)" in ref["thresholds"]["fail_rule"]
    # 丸めなし: 境界は JSON 往復しても同じ float
    import json

    rt = json.loads(json.dumps(ref))
    assert rt["thresholds"]["fail"] == [lo, hi]


def test_rounding_self_fail_regression():
    """合成境界0.1000004を6桁丸めしても自己failさせない。"""
    boundary = 0.1000004
    vals = [0.02 + 0.005 * i for i in range(15)] + [boundary]
    per = [(f"a{i}", v) for i, v in enumerate(vals)]
    ref = morph_lib.calibrate_scalar_channel(SCALAR_CHANNEL, per)
    assert ref["thresholds"]["fail"][1] >= boundary
    res = morph_lib.evaluate_scalar(SCALAR_CHANNEL, boundary, ref, BIG)
    assert res["status"] != "fail", res
    assert res["value"] == boundary  # 評価値も丸めない
    # 旧実装(round 6 桁)なら自己failするよう、意図的に構成した合成値。
    assert round(boundary, 6) < boundary


def test_g4_style_lower_bound_equal_is_not_fail():
    """hard 下限 == 観測最小値のとき、値 == 下限は fail ではない(G4 と同じ比較規則)。"""
    lower = 0.5
    vals = [lower + 0.02 * i for i in range(12)]
    hard, rule = calib.lower_hard_bound(vals)
    assert hard == lower and "author_min" in rule
    assert not (lower < hard)


def test_p95_cap_is_interpolated_not_index_max():
    """G2 連続 cap: 旧 index 版は n=14 で max に一致した。線形補間なら max より小さく、hard cap は max。"""
    vals = [3.0] * 13 + [11.0]
    p95 = stats.quantile(vals, 0.95)
    assert 3.0 < p95 < 11.0
    cap = max(3, math.ceil(p95 - 1e-9))
    hard, _ = calib.upper_hard_bound(vals)
    assert cap < hard == 11.0


def test_max_exceed_count_discreteness():
    assert calib.max_exceed_count(50, 0.001) == 0  # 小 n は極値規則(0)
    # 外側丸め: n < 1/alpha では境界 = max → 超える標本は 0
    assert calib.max_exceed_count(128, calib.per_check_alpha()) == 0
    assert calib.max_exceed_count(500, calib.per_check_alpha()) == 0
    assert calib.max_exceed_count(500, 0.01) == 4  # ceil(0.99*499=494.01)=495 → index 496..499 の 4 個
    for n in (100, 128, 500, 3000, 10000):
        for alpha in (calib.per_check_alpha(), 0.001, 0.01):
            assert calib.max_exceed_count(n, alpha) <= int(alpha * (n - 1)) + 0


@pytest.mark.parametrize("n", [100, 128, 500, 3000])
def test_outward_quantile_exact_exceed_bound(n):
    rng = random.Random(n)
    vals = [_rand_scalar(rng) for _ in range(n)]
    for alpha in (calib.per_check_alpha(), 0.001, 0.01):
        hi = stats.quantile_outward(vals, 1 - alpha, "upper")
        lo = stats.quantile_outward(vals, alpha, "lower")
        assert sum(1 for v in vals if v > hi) <= int(alpha * (n - 1))
        assert sum(1 for v in vals if v < lo) <= int(alpha * (n - 1))
        assert sum(1 for v in vals if v > hi) == calib.max_exceed_count(n, alpha) or n < calib.LARGE_N


@pytest.mark.parametrize("n", [100, 128, 500])
def test_independent_p99_multi_test_issue_is_real_and_policy_bounds_it(n):
    """m 個の fail 可能検査で独立 p01/p99 を使うと、較正記事の記事レベル fail 率が
    ARTICLE_ALPHA を超える(隠さない)。Bonferroni ポリシーでは離散上限内に収まる。"""
    m = len(morph_lib.FAIL_CAPABLE_CHANNELS)
    rng = random.Random(300 + n)
    values = [[_rand_scalar(rng) for _ in range(n)] for _ in range(m)]
    # 旧規則: 各検査で p01 / p99(独立)
    old_fail_articles = set()
    for ch in range(m):
        lo, hi = stats.quantile(values[ch], 0.01), stats.quantile(values[ch], 0.99)
        for i, v in enumerate(values[ch]):
            if v < lo or v > hi:
                old_fail_articles.add(i)
    old_rate = len(old_fail_articles) / n
    assert old_rate > calib.ARTICLE_ALPHA, (n, old_rate)
    # 新規則: Bonferroni 分位点 ∪ Tukey
    new_fail_articles = set()
    for ch in range(m):
        hard = calib.scalar_hard_range(values[ch])
        for i, v in enumerate(values[ch]):
            if v < hard["lo"] or v > hard["hi"]:
                new_fail_articles.add(i)
    bound = m * 2 * calib.max_exceed_count(n, calib.per_check_alpha() / 2)
    assert len(new_fail_articles) <= bound, (n, len(new_fail_articles), bound)
    # 記事レベルの保証: 較正観測の fail 割合 <= ARTICLE_ALPHA
    assert len(new_fail_articles) / n <= calib.ARTICLE_ALPHA
    assert len(new_fail_articles) / n < old_rate


def test_policy_description_is_declared():
    p = calib.policy_description()
    assert p["article_alpha"] == 0.01
    assert p["n_fail_capable_checks"] == len(calib.LEGACY_FAIL_CAPABLE_CHECKS) + len(morph_lib.FAIL_CAPABLE_CHANNELS)
    assert p["per_check_alpha"] == pytest.approx(0.01 / p["n_fail_capable_checks"])


def test_band_notes_flag_degenerate():
    notes = calib.band_notes([0.0, 0.0], {"lo": 0.0, "hi": 0.0, "sided": "upper_only"})
    assert "warn_band_zero_width" in notes
    assert "hard_band_zero_width" in notes
    assert any("lower_bound_degenerate" in n for n in notes)
