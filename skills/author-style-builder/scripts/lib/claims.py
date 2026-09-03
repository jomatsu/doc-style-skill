"""claim ↔ aggregate の整合検査(compile_skill / skill_lint / feedback_intake が共有)。

1. `GATE_OF_METRIC`: **実際に style_lint が評価し、compile が閾値を書き込む** metric だけを
   ゲートへ写像する。ここに無い metric の validator claim は写像不能(exit 2)。
   名目上の対応(評価されない metric を近いゲートに割り当てる)は置かない
2. `check_claim_drift(claim, aggregate)`: claim の数値(`value.median` / `value.range`)が
   aggregate(同じ feature_schema / analyzer)で再現できるかを検査する。
   再現できない claim を黙って文面・ゲートに流さない
"""

from __future__ import annotations

from lib import features as feat
from lib import morph as morph_lib

# metric → lint-config ゲート。style_lint の具体的な検査に 1:1 で対応する
GATE_OF_METRIC: dict[str, str] = {
    "sent_len_median": "G1_distribution",
    "sent_len_max": "G1_distribution",
    "para_len_median": "G1_distribution",
    "comma_per_sent_median": "G1_distribution",
    "max_consecutive_same_ending": "G2_sentence_end",
    "sent_end_form.desu_masu": "G2_sentence_end",
    "sent_end_form.da_dearu": "G2_sentence_end",
    "sent_end_form.jotai_verb": "G2_sentence_end",
    "sent_end_form.jotai_adj": "G2_sentence_end",
    "sent_end_form.taigen": "G2_sentence_end",
    "sent_end_form.question": "G2_sentence_end",
    "script_ratio.kanji": "G3_orthography",
    "script_ratio.hiragana": "G3_orthography",
    "script_ratio.katakana": "G3_orthography",
    "script_ratio.latin": "G3_orthography",
    "ttr_window": "G4_vocabulary",
    "distinct_2": "G4_vocabulary",
    "func_word_rate": "G4_vocabulary",
    "caricature_markers": "G5_caricature",
}
for _ch in morph_lib.CHANNELS:
    GATE_OF_METRIC[f"morph.{_ch}"] = "G7_morphology"

# G3 の script 種別 → lint-config キー
SCRIPT_GATE_KEYS = {
    "script_ratio.kanji": "kanji_ratio",
    "script_ratio.hiragana": "hiragana_ratio",
    "script_ratio.katakana": "katakana_ratio",
    "script_ratio.latin": "latin_ratio",
}
G2_FORMS = ("desu_masu", "da_dearu", "jotai_verb", "jotai_adj", "taigen", "question")

# aggregate.features / morph チャネルに存在し得る定量 metric の全集合
KNOWN_METRICS = (
    set(feat.SCALAR_KEYS)
    | set(feat.MORPH_SCALAR_KEYS)
    | {f"morph.{d}" for d in morph_lib.DIST_CHANNELS}
    | {"caricature_markers"}
)
# 定量カタログの名前空間(これに該当するのに KNOWN_METRICS に無ければ「スキーマ外」)
_CATALOG_PREFIXES = (
    "sent_len",
    "para_len",
    "comma_per_sent",
    "max_consecutive",
    "sent_end_form",
    "script_ratio",
    "func_word",
    "ttr",
    "distinct_",
    "morph.",
)

# 数値を文面・ゲートへ流す compilation_target(drift 検査の対象)
NUMERIC_TARGETS = {"always_on_rule", "conditional_rule", "validator", "persona"}

# 退化した CI(幅 0)への絶対許容幅
_DRIFT_FLOORS = {
    "sent_len_median": 1.0,
    "sent_len_max": 2.0,
    "para_len_median": 0.5,
    "comma_per_sent_median": 0.5,
    "max_consecutive_same_ending": 1.0,
}
_DRIFT_FLOOR_DEFAULT = 0.01


def is_catalog_metric(metric: str | None) -> bool:
    return bool(metric) and metric.startswith(_CATALOG_PREFIXES)


def metric_evaluable(metric: str, aggregate: dict) -> tuple[bool, str | None]:
    """metric が aggregate の下で実際に評価・較正できるか。(可否, 理由)。"""
    if metric == "caricature_markers":
        return True, None
    if metric.startswith("morph."):
        ch = metric[len("morph.") :]
        entry = ((aggregate.get("morphology") or {}).get("channels") or {}).get(ch) or {}
        if entry.get("status") != "built":
            return False, f"morph channel {ch} は較正されていない(status={entry.get('status', 'absent')}: {entry.get('reason', '')})"
        return True, None
    if metric not in aggregate.get("features", {}):
        mode = (aggregate.get("analyzer") or {}).get("mode")
        return False, f"metric {metric} は aggregate に無い(analyzer={mode} では算出されない)"
    return True, None


def _floor(metric: str) -> float:
    return _DRIFT_FLOORS.get(metric, _DRIFT_FLOOR_DEFAULT)


def check_claim_drift(claim: dict, aggregate: dict) -> dict | None:
    """claim の数値が aggregate で再現できるか。問題なければ None。

    返り値(問題あり): {"claim_id", "metric", "kind", "detail", "claim", "aggregate"}
    kind: metric_not_in_schema | metric_not_evaluable | value_drift
    検査対象: NUMERIC_TARGETS の claim で、metric が定量カタログの名前空間にあるもの。
    mode_specific(部分集合の観測)は global aggregate と比べない(metric 存在のみ)。
    """
    cid = claim.get("claim_id", "?")
    target = claim.get("compilation_target")
    metric = (claim.get("feature") or {}).get("metric") or ""
    if target not in NUMERIC_TARGETS or not is_catalog_metric(metric):
        return None
    if metric.startswith("morph.") and target != "validator":
        return None  # 文面化禁止は compile_skill が別途 exit 2 で拒否する
    if metric not in KNOWN_METRICS:
        return {
            "claim_id": cid,
            "metric": metric,
            "kind": "metric_not_in_schema",
            "detail": f"metric {metric!r} は feature_schema {feat.FEATURE_SCHEMA_VERSION} / "
            f"channel_registry {morph_lib.CHANNEL_REGISTRY_VERSION} に存在しない",
        }
    ok, reason = metric_evaluable(metric, aggregate)
    if not ok:
        return {"claim_id": cid, "metric": metric, "kind": "metric_not_evaluable", "detail": reason}
    if metric.startswith("morph.") and metric[len("morph.") :] in morph_lib.DIST_CHANNELS:
        return None  # 分布チャネルの claim は数値レンジを持たない
    if claim.get("status") == "mode_specific" or (claim.get("scope_mode") or "core") != "core":
        return None
    value = claim.get("value") or {}
    cmed = value.get("median")
    crng = value.get("range")
    if cmed is None and not crng:
        return None
    if metric == "caricature_markers":
        return None
    entry = aggregate["features"][metric]
    ea = entry["equal_article"]
    amed = ea["median"]
    lo, hi = ea.get("ci95") or [amed, amed]
    q1, q3 = ea.get("iqr") or [amed, amed]
    tol = _floor(metric)
    problems = []
    if cmed is not None and not (lo - tol <= cmed <= hi + tol):
        problems.append(
            f"claim median {cmed:.4g} が aggregate median の 95% CI [{lo:.4g}, {hi:.4g}] (±{tol:g}) の外"
        )
    if crng and crng[0] is not None and crng[1] is not None:
        rlo, rhi = crng
        if rhi + tol < q1 or rlo - tol > q3:
            problems.append(
                f"claim range [{rlo:.4g}, {rhi:.4g}] が aggregate IQR [{q1:.4g}, {q3:.4g}] と重ならない"
            )
    if not problems:
        return None
    return {
        "claim_id": cid,
        "metric": metric,
        "kind": "value_drift",
        "detail": "; ".join(problems),
        "claim": {"median": cmed, "range": crng},
        "aggregate": {"median": amed, "ci95": [lo, hi], "iqr": [q1, q3]},
    }


def schema_of_claim(claim: dict, profile: dict) -> str | None:
    """claim(または profile ルート)に記録された feature_schema。無ければ None。"""
    f = claim.get("feature") or {}
    return f.get("schema") or profile.get("feature_schema")


def check_profile_drift(profile: dict, aggregate: dict) -> tuple[list[dict], list[str]]:
    """profile 全体の drift 検査。(drift レコード列, 警告文字列列)。

    observed・非 quarantined の claim だけを対象にする(コンパイル対象と同じ集合)。
    """
    drifts: list[dict] = []
    warnings: list[str] = []
    agg_schema = aggregate.get("feature_schema")
    agg_mode = (aggregate.get("analyzer") or {}).get("mode")
    for c in profile.get("claims", []):
        if c.get("state") != "observed" or c.get("status") == "quarantined":
            continue
        metric = (c.get("feature") or {}).get("metric") or ""
        if is_catalog_metric(metric):
            sch = schema_of_claim(c, profile)
            if sch != agg_schema:
                warnings.append(
                    f"claim {c.get('claim_id')}: feature_schema={sch!r} が aggregate の {agg_schema!r} と異なる"
                    "(数値が再現できるかを検査する)"
                )
            an = (c.get("feature") or {}).get("analyzer") or ""
            if agg_mode == "sudachi" and an and "fallback" in an:
                warnings.append(f"claim {c.get('claim_id')}: analyzer={an!r} は aggregate(sudachi)と異なる")
            if agg_mode == "fallback" and an and "sudachi" in an:
                warnings.append(f"claim {c.get('claim_id')}: analyzer={an!r} は aggregate(fallback)と異なる")
        d = check_claim_drift(c, aggregate)
        if d is not None:
            drifts.append(d)
    return drifts, warnings


def format_drift(d: dict) -> str:
    base = f"claim {d['claim_id']} (metric={d['metric']}, {d['kind']}): {d['detail']}"
    return base
