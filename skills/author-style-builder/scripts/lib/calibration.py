"""閾値較正の共通ポリシー(G1〜G4 の legacy ゲートと G7 形態素チャネルが共有)。

不変条件(compile-rules.md「リンター閾値の較正原則」):

- **n < LARGE_N(小 n)**: hard(fail)境界は較正記事の極値そのもの(min / max)を
  **丸めずに**採用する。較正記事を同じリンターに通したとき、丸め・境界一致だけで
  hard fail することは無い(値 == 境界は fail ではない)。スカラーはさらに Tukey
  フェンス(Q1-1.5IQR, Q3+1.5IQR)と合併し、未見の著者記事が極値を僅かに外れても
  fail しないマージンを持つ
- **n >= LARGE_N(大 n)**: 記事レベルの family-wise error を事前登録する。
  fail 境界は per-check の Bonferroni 分位点 q = 1 - ARTICLE_ALPHA / m(m = fail に
  なり得る検査の総数、両側スカラーは片側ずつ半分)を**外側に丸めた順序統計量**
  (stats.quantile_outward)で取る。線形補間だと境界が標本最大値の僅か下に落ち、
  最大値の記事が全検査で fail する(離散化の罠)。外側丸めなら、境界を厳密に超える
  較正観測は検査あたり高々 floor(alpha_check * (n-1)) 個、記事レベルでは合計
  ARTICLE_ALPHA * (n-1) 以下という厳密な上限が成り立つ。独立 p99 は使わない。
  n < 1/alpha_check(≈ 2,900)ではこの分位点は標本最大値に一致し、小 n と同じ「極値」
  規則に退化する。これは意図した保守的挙動であり、p99 の多重検定問題を隠すものではない
  (tests/test_calibration_policy.py が旧 p99 規則との差を明示的に検証する)

丸め: 較正値は JSON にフル精度で保存し、評価側も同じ値で比較する。表示用の
丸めは compile_skill の文面レンダリングだけで行う。
"""

from __future__ import annotations

from lib import stats

LARGE_N = 100
MIN_CALIBRATION_N = 10
TUKEY_K = 1.5
# 記事レベルの許容 family-wise error(較正記事のうち、いずれかの fail 可能検査で
# hard fail する記事の割合の上限。事前登録値。holdout を見て調整しない)
ARTICLE_ALPHA = 0.01

# fail になり得る legacy ゲート検査(G1〜G4)。G7 の fail 可能チャネルは
# lib/morph.CHANNELS から数え、合計 m を n_fail_capable_checks() が返す
LEGACY_FAIL_CAPABLE_CHECKS = (
    "G1.sent_len_median",
    "G1.sent_len_max",
    "G1.para_len_median",
    "G1.comma_per_sent_median",
    "G2.max_consecutive_same_ending",
    "G2.desu_masu",
    "G2.da_dearu",
    "G2.jotai_verb",
    "G2.jotai_adj",
    "G2.taigen",
    "G2.question",
    "G3.kanji",
    "G3.hiragana",
    "G3.katakana",
    "G3.latin",
    "G4.ttr_window",
    "G4.distinct_2",
    "G4.func_word_rate",
)


def n_fail_capable_checks() -> int:
    from lib import morph  # 循環 import 回避(関数呼び出し時)

    return len(LEGACY_FAIL_CAPABLE_CHECKS) + len(morph.FAIL_CAPABLE_CHANNELS)


def per_check_alpha(m: int | None = None) -> float:
    """Bonferroni: 1 検査あたりの許容 α。"""
    m = m or n_fail_capable_checks()
    return ARTICLE_ALPHA / m


def policy_description() -> dict:
    m = n_fail_capable_checks()
    return {
        "article_alpha": ARTICLE_ALPHA,
        "n_fail_capable_checks": m,
        "per_check_alpha": ARTICLE_ALPHA / m,
        "large_n": LARGE_N,
        "min_calibration_n": MIN_CALIBRATION_N,
        "tukey_k": TUKEY_K,
        "small_n": "hard = author extreme (min/max, unrounded); scalar ∪ Tukey fence",
        "large_n": "hard = outward order-statistic Bonferroni quantile q=1-article_alpha/m per check (two-sided: alpha/2 per tail); scalar ∪ Tukey fence; dist lower-bounded by warn(p90)",
        "guarantee": "calibration observations failing per check <= floor(alpha_check*(n-1)); article-level <= article_alpha*(n-1)",
        "note": "for n < 1/alpha_check (~2,900) the outward quantile equals the sample max; this is intended",
    }


def upper_hard_bound(values: list[float], *, m: int | None = None, two_sided: bool = False) -> tuple[float, str]:
    """上限型検査の hard 境界(値 > 境界で fail)。(境界, 規則名)。"""
    n = len(values)
    if n == 0:
        raise ValueError("empty")
    if n < LARGE_N:
        return float(max(values)), "author_max(small_n)"
    alpha = per_check_alpha(m) / (2 if two_sided else 1)
    q = 1.0 - alpha
    return stats.quantile_outward(values, q, "upper"), f"bonferroni_q_outward(1-{alpha:.3g})"


def lower_hard_bound(values: list[float], *, m: int | None = None, two_sided: bool = False) -> tuple[float, str]:
    """下限型検査の hard 境界(値 < 境界で fail)。"""
    n = len(values)
    if n == 0:
        raise ValueError("empty")
    if n < LARGE_N:
        return float(min(values)), "author_min(small_n)"
    alpha = per_check_alpha(m) / (2 if two_sided else 1)
    return stats.quantile_outward(values, alpha, "lower"), f"bonferroni_q_outward({alpha:.3g})"


def scalar_hard_range(values: list[float], *, m: int | None = None) -> dict:
    """両側スカラーの hard 境界。極値(または Bonferroni 分位点)と Tukey フェンスの合併。

    返り値: {"lo", "hi", "rule", "sided"}。全値が 0 以上なら lo は 0 で打ち切る。
    """
    vals = [float(v) for v in values]
    q1, q3 = stats.iqr(vals)
    iqr_w = q3 - q1
    lo_b, lo_rule = lower_hard_bound(vals, m=m, two_sided=True)
    hi_b, hi_rule = upper_hard_bound(vals, m=m, two_sided=True)
    lo = min(lo_b, q1 - TUKEY_K * iqr_w)
    hi = max(hi_b, q3 + TUKEY_K * iqr_w)
    if min(vals) >= 0:
        lo = max(lo, 0.0)
    sided = "two_sided"
    if lo <= 0.0 and min(vals) >= 0:
        sided = "upper_only"
    return {
        "lo": lo,
        "hi": hi,
        "rule": f"union({lo_rule}/{hi_rule}, tukey_fence_k={TUKEY_K})",
        "sided": sided,
    }


def max_exceed_count(n: int, alpha: float) -> int:
    """外側丸め分位点 q=1-alpha を**厳密に超え得る**標本数の上限 = n-1-ceil(q(n-1))
    (= floor(alpha(n-1)) を超えない)。テストが「較正記事のうち fail するものの数」を
    照合するときに使う。小 n は極値規則なので 0。
    """
    if n < LARGE_N:
        return 0
    import math

    pos = (1.0 - alpha) * (n - 1)
    hi = min(int(math.ceil(pos - 1e-12)), n - 1)
    return max(0, n - 1 - hi)


def band_notes(warn: list, hard: dict, *, kind: str = "scalar") -> list[str]:
    """退化・空虚な帯域を検出して注記を返す(compile 時警告用)。"""
    notes: list[str] = []
    wlo, whi = warn
    if wlo is None or whi is None:
        return ["warn_band_missing"]
    if whi - wlo <= 0:
        notes.append("warn_band_zero_width")
    if hard.get("sided") == "upper_only":
        notes.append("lower_bound_degenerate_zero(one_sided_upper)")
    if hard["hi"] - hard["lo"] <= 0:
        notes.append("hard_band_zero_width")
    return notes
