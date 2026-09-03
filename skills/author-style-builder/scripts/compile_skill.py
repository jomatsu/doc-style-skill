#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""compile_skill — profile.json から author スキル一式を生成。

- compile-rules.md の写像規則に従い、claim を templates/author-skill/ に流し込む
- state=observed 以外(inferred)・status=quarantined はコンパイル禁止 → 除外して
  meta/profile-ref.json の excluded に理由つきで記録
- profile_class=exploratory のときに限り、observed / ambiguous / conditional_rule の
  承認済みclaimをSKILL.mdの「文体傾向」節へ描画する。成熟度はprovenance/lintへ分離する
- core + conditional_rule: condition が無ければ常時ルールへ、あれば SKILL.md の
  「条件付きルール(core)」節へ描画する(黙って落とさない)
- 完全性: observed・非 quarantined の全 claim が profile-ref の mappings か
  excluded のどちらかに載ること。描画・写像できない claim があれば exit 2
- 形態素チャネル(morph.*)を metric に持つ claim は validator / checklist / example
  のみ許可(ペルソナ・常時ルールへの直訳禁止)
- lint-config.json はレンジ型(G1/G2-form/G3/G4-func)を warn = median±IQR、
  hard = 著者実記事の極値(lib/calibration のポリシー。フル精度、丸めなし)、
  上限/下限型(G2-run/G4/G1-max)を warn = p95/p05 + hard = 極値で較正する。
  短すぎる入力は各ゲートの min_sents / min_chars / min_tokens で warn に格下げ(degrade)する。
  G7(形態素)は aggregate の LOAO 較正を lint-morphology.json へ書き出す
- 較正 split は train / dev / train+dev のみ。`all`(holdout 混入)は拒否
- aggregate の feature_schema / channel_registry_version が builder と違えば exit 1
  (extract_features の再実行を要求)
- profile claim の数値(value.median / range)が aggregate で再現できない(stale)場合は
  exit 2。`--allow-stale-claims` でのみ、該当 claim を excluded(理由つき)に回して
  **移行候補の生成専用**として続行する(provenance.migration に記録され、skill_lint が
  本番リリースを拒否する)
- validator claim は、その metric が style_lint で**実際に評価され、較正値が書き込まれる**
  場合に限り写像する(名目上の対応は exit 2)
- G5 の markers は validator claim(metric=caricature_markers, value.markers)からのみ取る。
  aggregate から発明しない。空なら G5 は skipped(pass ではない)
- 生成スキルに実行可能なリンター(scripts/lint.sh + 必要な Python モジュール)を同梱
- meta/profile-ref.json(ルール↔claim_id 対応)と meta/provenance.json を生成
- 決定的出力: 同じ入力からは同じスキルが生成される(生成日時は --now で固定可)

終了コード: 0=成功 / 1=エラー / 2=完全性・写像・stale claim の不合格
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import calibration as calib
from lib import claims as claims_lib
from lib import features as feat
from lib import io_utils
from lib import morph as morph_lib
from lib import stats

META_SKILL_VERSION = "0.4.0"
# builder の成熟度。独立コーパス群で外部妥当性を確認するまで experimental。
BUILDER_STATUS = "experimental"
ALLOWED_CALIBRATION_SPLITS = ("train", "dev", "train+dev")

# 短すぎる入力の信頼性ガード(未満は該当ゲートを warn に格下げ。style_lint が読む)
_MIN_SENTS = 10
_MIN_CHARS = 300
_MIN_TOKENS = 100

# 階層順(profile-schema.md の 7 層のうち claim 化される 2〜7 層)
_CATEGORY_ORDER = {"構造": 0, "談話": 1, "文": 2, "語彙": 3, "表記": 4, "文末": 5}

_STRATA_LABEL = {"tech": "技術記事", "essay": "エッセイ", "blog": "ブログ記事"}

# ゲートレンジの最小幅(IQR が退化した場合の下駄。算出方法は calibration に記録)
_WIDTH_FLOORS = {
    "sent_len_median": 8.0,
    "sent_len_max": 10.0,
    "para_len_median": 1.0,
    "comma_per_sent_median": 0.5,
    "max_consecutive_same_ending": 1.0,
    "ttr_window": 0.05,
    "distinct_2": 0.05,
    "func_word_rate": 0.05,
}
_RATIO_PREFIXES = ("sent_end_form.", "script_ratio.")
_RATIO_FLOOR = 0.08

# metric → lint-config ゲートの対応(lib/claims に一本化。実際に評価される metric のみ)
_GATE_OF_METRIC = claims_lib.GATE_OF_METRIC

# 形態素チャネル由来の claim に許す compilation_target(文面化禁止)
_MORPH_ALLOWED_TARGETS = {"validator", "checklist", "example"}

# レンジ補足のラベル(rule_text を主とし、補足だけに使う)
_METRIC_LABEL = {
    "sent_len_median": "記事ごとの文長中央値",
    "sent_len_max": "記事ごとの最長文",
    "para_len_median": "記事ごとの段落文数中央値",
    "comma_per_sent_median": "記事ごとの 1 文あたり読点数中央値",
    "max_consecutive_same_ending": "同一文末形式の最大連続数",
    "sent_end_form.desu_masu": "「です・ます」体の文末比",
    "sent_end_form.da_dearu": "「だ・である」体の文末比",
    "sent_end_form.jotai_verb": "常体動詞終止の文末比",
    "sent_end_form.jotai_adj": "常体形容詞終止の文末比",
    "sent_end_form.taigen": "体言止めの文末比",
    "sent_end_form.question": "疑問形の文末比",
    "script_ratio.kanji": "漢字の文字比",
    "script_ratio.hiragana": "ひらがなの文字比",
    "script_ratio.katakana": "カタカナの文字比",
    "script_ratio.latin": "英字の文字比",
    "script_ratio.digit": "数字の文字比",
    "func_word_rate": "機能語率",
    "ttr_window": "移動窓 TTR",
    "distinct_2": "distinct-2",
}
_RATIO_METRICS = ("sent_end_form.", "script_ratio.", "func_word_rate", "ttr_window", "distinct_2")

_EXAMPLE_SPAN_MAX = 50  # スキルに載せる引用 span の上限(長い生引用の禁止)

# 探索的プロファイル専用ブロックのマーカー(SKILL.md.template)
_EXPLORATORY_START = "<!-- EXPLORATORY_SECTION_START -->"
_EXPLORATORY_END = "<!-- EXPLORATORY_SECTION_END -->"
_CORE_COND_START = "<!-- CORE_CONDITIONAL_SECTION_START -->"
_CORE_COND_END = "<!-- CORE_CONDITIONAL_SECTION_END -->"

_EXPLORATORY_PROFILE_CLASS = "exploratory"

# 生成スキルへ同梱するリンター一式(builder の scripts/ からコピー)
RUNNER_FILES = [
    "style_lint.py",
    "overlap_check.py",
    "lib/__init__.py",
    "lib/blocks.py",
    "lib/calibration.py",
    "lib/features.py",
    "lib/morph.py",
    "lib/stats.py",
    "lib/tokenize.py",
]

LINT_SH = """#!/usr/bin/env bash
# 生成 author スキルに同梱されたリンター実行ラッパ。
# 使い方: scripts/lint.sh --text <file> [--source-corpus <dir>] [--era <YYYY>]
# - cwd に依存しない(このファイルの位置から lint-config.json を解決)
# - `uv` があれば PEP 723 で sudachipy を自動解決、無ければ python3 の
#   fallback モード(POS 依存ゲートは skipped と報告される)
# - STYLE_LINT_PYTHON=<python> で解釈系を強制できる
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1  # スキル dir に __pycache__ を書かない
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$HERE/../lint-config.json"
LINT="$HERE/style_lint.py"
if [ ! -f "$CONFIG" ] || [ ! -f "$LINT" ]; then
  echo "error: 同梱リンターが見つかりません ($CONFIG / $LINT)。meta/provenance.json の runner を確認してください" >&2
  exit 1
fi
if [ -n "${STYLE_LINT_PYTHON:-}" ]; then
  exec "$STYLE_LINT_PYTHON" "$LINT" --config "$CONFIG" "$@"
elif command -v uv >/dev/null 2>&1; then
  exec uv run --quiet "$LINT" --config "$CONFIG" "$@"
elif command -v python3 >/dev/null 2>&1; then
  echo "note: uv が無いため python3 で実行します(sudachipy 不在なら fallback モード)" >&2
  exec python3 "$LINT" --config "$CONFIG" "$@"
else
  echo "error: uv も python3 も見つかりません。リンターを実行できません" >&2
  exit 1
fi
"""


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument(
        "--profile", type=Path, default=None, help="既定: <workspace>/profile.json"
    )
    p.add_argument(
        "--templates",
        type=Path,
        default=_SCRIPTS_DIR.parent / "templates" / "author-skill",
    )
    p.add_argument("--out", required=True, type=Path)
    p.add_argument(
        "--now", default=None, help="生成日時(ISO 8601)。決定的出力用に固定可"
    )
    p.add_argument(
        "--allow-stale-claims",
        action="store_true",
        help="aggregate で再現できない claim を excluded に回して続行(移行候補生成専用。"
        "provenance.migration に記録され、skill_lint が本番リリースを拒否する)",
    )
    return p.parse_args(argv)


# ---------------- 数値・レンジ整形 ----------------

def _fmt(v) -> str:
    if v is None:
        return "?"
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _pct(v) -> str:
    return str(int(round(v * 100)))


def _is_ratio(key: str) -> bool:
    return key.startswith(_RATIO_PREFIXES)


def _per_article_values(aggregate: dict, key: str) -> list[float]:
    entry = aggregate["features"].get(key)
    if entry is None:
        return []
    return [float(v) for _, v in entry["per_article"] if v is not None]


def _gate_range(aggregate: dict, key: str) -> list:
    """_aggregate の記事単位分布から median±IQR の warn レンジを算出(フル精度)。"""
    entry = aggregate["features"].get(key)
    if entry is None:
        return [None, None]
    med = entry["equal_article"]["median"]
    q1, q3 = entry["equal_article"]["iqr"]
    floor = _RATIO_FLOOR if _is_ratio(key) else _WIDTH_FLOORS.get(key, 0.05)
    width = max(q3 - q1, floor)
    lo, hi = med - width, med + width
    lo = max(lo, 0.0)
    if _is_ratio(key):
        hi = min(hi, 1.0)
    return [lo, hi]


def _hard_range(aggregate: dict, key: str) -> list:
    """著者実記事の hard レンジ(lib/calibration のポリシー。フル精度)。

    値がこのレンジ内なら warn 帯の外でも fail にはならない(較正記事の不変条件)。
    """
    vals = _per_article_values(aggregate, key)
    if not vals:
        return [None, None]
    hard = calib.scalar_hard_range(vals)
    lo, hi = hard["lo"], hard["hi"]
    if _is_ratio(key):
        hi = min(hi, 1.0)
    return [lo, hi]


def _range_notes(key: str, warn: list, hard: list) -> list[str]:
    if warn[0] is None or hard[0] is None:
        return []
    notes = calib.band_notes(warn, {"lo": hard[0], "hi": hard[1], "sided": "two_sided"})
    if hard[0] == 0.0 and warn[0] == 0.0:
        notes.append("lower_bound_degenerate_zero(one_sided_upper)")
    if hard[1] - hard[0] <= (_RATIO_FLOOR if _is_ratio(key) else _WIDTH_FLOORS.get(key, 0.05)) / 2:
        notes.append("hard_band_narrow")
    return sorted(set(notes))


def _range_supplement(metric: str, lo, hi) -> str:
    label = _METRIC_LABEL.get(metric)
    if label is None:
        return f"({metric}: {_fmt(lo)}〜{_fmt(hi)} が中心)"
    if metric.startswith(_RATIO_METRICS):
        return f"({label}は記事単位で {_pct(lo)}〜{_pct(hi)}% が中心)"
    unit = "字" if metric.startswith("sent_len") else ("文" if metric == "para_len_median" else "")
    return f"({label}は {_fmt(lo)}〜{_fmt(hi)}{unit} が中心)"


def render_rule_text(claim: dict) -> str:
    """claim の rule_text を主とし、定量レンジは補足として添える。

    レンジは記事単位の中心レンジであり、rule_text の意味(era・記事内の一様性
    など)を上書きしない。数値からの文面の発明(例: 上限 = hi*2)はしない。
    """
    rule = " ".join((claim.get("rule_text") or "").split())
    metric = (claim.get("feature") or {}).get("metric") or ""
    rng = (claim.get("value") or {}).get("range")
    if not rng or rng[0] is None or rng[1] is None or not metric or metric.startswith("morph."):
        return rule
    supplement = _range_supplement(metric, rng[0], rng[1])
    if rule.endswith("。"):
        return rule[:-1] + supplement
    return rule + supplement


# ---------------- テンプレート処理 ----------------

def _strip_comments(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _render(template: str, mapping: dict) -> str:
    def sub(m):
        key = m.group(1)
        if key not in mapping:
            raise KeyError(f"unfilled placeholder: {key}")
        return str(mapping[key])

    return re.sub(r"\{\{(\w+)\}\}", sub, template)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sorted_claims(claims: list) -> list:
    return sorted(
        claims,
        key=lambda c: (_CATEGORY_ORDER.get(c.get("category"), 9), c["claim_id"]),
    )


def _evidence_span_text(workspace: Path, claim: dict) -> str | None:
    """evidence の先頭 span を raw から読み、短い引用として返す(50 字上限)。"""
    ev = claim.get("evidence") or []
    if not ev:
        return None
    span = ev[0]
    raw_path = workspace / "raw" / f"{span['article_id']}.txt"
    if not raw_path.exists():
        return None
    raw = raw_path.read_text(encoding="utf-8")
    text = raw[span["char_start"] : span["char_end"]]
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > _EXAMPLE_SPAN_MAX:
        text = text[: _EXAMPLE_SPAN_MAX] + "…"
    return text


# ---------------- 各成果物の生成 ----------------

def build_persona(persona_claims: list) -> str:
    """persona claim からのみ生成。無ければ未登録と明記し、aggregate から合成しない。"""
    if persona_claims:
        parts = []
        for c in _sorted_claims(persona_claims):
            t = c["rule_text"].strip()
            parts.append(t if t.endswith("。") else t + "。")
        return "".join(parts)
    return (
        "(persona claim は未登録。profile に compilation_target=persona の core claim が"
        "無いため、文体の要約は提示しない。常時ルールと lint-config.json のみを根拠にする)"
    )


def build_always_on(rule_claims: list) -> str:
    if not rule_claims:
        return "- コアルール claim は未登録。lint-config.json のゲート値を目安にする"
    return "\n".join(f"- {render_rule_text(c)}" for c in _sorted_claims(rule_claims))


def build_core_conditional(claims: list) -> str:
    lines = []
    for c in _sorted_claims(claims):
        cond = " ".join((c.get("condition") or "").split())
        lines.append(f"- 条件「{cond}」のとき: {render_rule_text(c)}")
    return "\n".join(lines)


def collect_exploratory_claims(profile: dict, compilable: list) -> list:
    """profile_class=exploratory のときだけ ambiguous/conditional_rule を集める。

    本番(非 exploratory)プロファイルでは常に空を返す。ambiguous claim を
    core / always_on_rule へ昇格させることは無い(compile-rules.md の例外規則)。
    """
    if profile.get("profile_class") != _EXPLORATORY_PROFILE_CLASS:
        return []
    return [
        c
        for c in compilable
        if c.get("compilation_target") == "conditional_rule"
        and c.get("status") == "ambiguous"
    ]


def build_exploratory_rules(exploratory_claims: list) -> str:
    """探索的 claim を常時ルールと同じ決定的レンダリング・階層順で箇条書きに。"""
    return "\n".join(
        f"- {render_rule_text(c)}" for c in _sorted_claims(exploratory_claims)
    )


def _apply_marked_block(template: str, start_marker: str, end_marker: str, mapping: dict | None) -> str:
    """マーカーで囲まれた節を描画(mapping あり)または丸ごと削除(None)。"""
    start = template.find(start_marker)
    end = template.find(end_marker)
    if start == -1 or end == -1 or end < start:
        return template
    head = template[:start]
    block = template[start + len(start_marker) : end]
    tail = template[end + len(end_marker) :]
    if mapping is None:
        return head.rstrip("\n") + "\n\n" + tail.lstrip("\n")
    rendered = _render(block.strip("\n"), mapping)
    return head.rstrip("\n") + "\n\n" + rendered + "\n\n" + tail.lstrip("\n")


def apply_exploratory_block(template: str, exploratory_claims: list) -> str:
    """探索profileの承認済みclaimを通常の文体傾向セクションへ描画する。"""
    if not exploratory_claims:
        return _apply_marked_block(template, _EXPLORATORY_START, _EXPLORATORY_END, None)
    return _apply_marked_block(
        template,
        _EXPLORATORY_START,
        _EXPLORATORY_END,
        {"style_tendencies": build_exploratory_rules(exploratory_claims)},
    )


def apply_core_conditional_block(template: str, claims: list) -> str:
    if not claims:
        return _apply_marked_block(template, _CORE_COND_START, _CORE_COND_END, None)
    return _apply_marked_block(
        template,
        _CORE_COND_START,
        _CORE_COND_END,
        {"core_conditional_rules": build_core_conditional(claims)},
    )


def build_mode_table(mode_claims: dict) -> str:
    if not mode_claims:
        return "| core | 既定(モード観測なし) | 常時ルールのみを適用 |"
    rows = []
    for m in sorted(mode_claims):
        condition = next(
            (c["condition"] for c in mode_claims[m] if c.get("condition")),
            "(条件記載なし)",
        )
        rows.append(f"| {m} | {condition} | references/style-rules.md#{m} |")
    return "\n".join(rows)


def build_style_rules(
    template: str, author_name: str, mode_claims: dict, unobserved_modes: list
) -> str:
    # テンプレートの記入例ガイド「（例: …）」は生成物に残さない
    template = re.sub(r"（例: [^）]*）", "", template)
    idx_mode = template.index("## {{mode_id}}")
    idx_unobs = template.index("## unobserved モード")
    head = template[:idx_mode]
    block = template[idx_mode:idx_unobs]
    tail = template[idx_unobs:]

    sections = []
    for mode_id in sorted(mode_claims):
        claims = _sorted_claims(mode_claims[mode_id])
        by_layer = {"arch": [], "disc": [], "surface": []}
        for c in claims:
            cat = c.get("category")
            key = "arch" if cat == "構造" else ("disc" if cat == "談話" else "surface")
            by_layer[key].append(f"- {render_rule_text(c)}")
        empty = "- (このモードでの観測なし。core のルールに従う)"
        condition = next(
            (c["condition"] for c in claims if c.get("condition")), "(条件記載なし)"
        )
        articles = max(
            (c.get("support", {}).get("articles", 0) for c in claims), default=0
        )
        sections.append(
            _render(
                block,
                {
                    "mode_id": mode_id,
                    "mode_condition": condition,
                    "mode_support": f"{articles} 記事",
                    "mode_architecture_rules": "\n".join(by_layer["arch"]) or empty,
                    "mode_discourse_rules": "\n".join(by_layer["disc"]) or empty,
                    "mode_surface_rules": "\n".join(by_layer["surface"]) or empty,
                },
            )
        )
    if not sections:
        sections.append(
            "## (モード別 claim なし)\n\n"
            "mode_specific claim が profile に無いため、常時ルール(SKILL.md)のみを適用する。\n\n"
        )
    unobs = "、".join(sorted(unobserved_modes)) if unobserved_modes else "(なし)"
    text = (
        _render(head, {"author_name": author_name})
        + "".join(sections)
        + _render(tail, {"unobserved_modes": unobs})
    )
    return _strip_comments(text)


def build_examples(
    template: str,
    author_name: str,
    example_claims: list,
    negative_claims: list,
    workspace: Path,
    unmapped: list,
) -> tuple[str | None, list]:
    """(examples.md テキスト, mappings)。例が 1 つも作れなければ None。

    evidence span を読めない example claim は描画不能 → unmapped に積む。
    """
    header = template[: template.index("## 正例")]
    header = _render(header, {"author_name": author_name})

    def label_of(cat):
        if cat == "構造":
            return "構造"
        if cat == "談話":
            return "談話・レジスター"
        return "局所形式"

    mappings = []
    pos_sections = []
    for c in _sorted_claims(example_claims):
        text = _evidence_span_text(workspace, c)
        if text is None:
            unmapped.append((c["claim_id"], "example claim の evidence span を読めない(raw 不在または空)"))
            continue
        i = len(pos_sections) + 1
        summary = " ".join(c["rule_text"].split())[:30]
        pos_sections.append(
            f"### 例 {i}: {label_of(c.get('category'))}({summary})\n\n"
            f"> {text}\n\n"
            f"ポイント: {c['rule_text']}\n\n"
        )
        mappings.append(
            {"target": f"references/examples.md#example-{i}", "claim_ids": [c["claim_id"]]}
        )

    neg_sections = []
    for c in _sorted_claims(negative_claims):
        text = _evidence_span_text(workspace, c)
        if text is None:
            unmapped.append((c["claim_id"], "negative_example claim の例文を読めない"))
            continue
        i = len(neg_sections) + 1
        neg_sections.append(
            f"### ✗ 負例 {i}: {c.get('category', '失敗次元')}\n\n"
            f"> {text}\n\n"
            f"違反理由: {c['rule_text']}\n\n"
        )
        mappings.append(
            {
                "target": f"references/examples.md#negative-{i}",
                "claim_ids": [c["claim_id"]],
            }
        )

    if not pos_sections and not neg_sections:
        return None, []
    text = header + "## 正例\n\n" + ("".join(pos_sections) or "(正例 claim なし)\n\n")
    if neg_sections:
        text += "## 負例(やってはいけない)\n\n" + "".join(neg_sections)
    return _strip_comments(text), mappings


def build_checklist(
    template: str, author_name: str, checklist_claims: list
) -> str:
    if checklist_claims:
        items = "\n".join(
            f"- [ ] {c['rule_text']}(観察項目。低一致または文脈判断が必要)"
            for c in _sorted_claims(checklist_claims)
        )
    else:
        items = "(checklist claim 由来の追加項目なし)"
    mapping = {
        "author_name": author_name,
        "opening_pattern": "特記なし。core ルールに従う",
        "closing_pattern": "特記なし。core ルールに従う",
        "example_density_hint": "特記なし",
        "reader_address_hint": "特記なし",
        "modality_hint": "特記なし",
        "ambiguous_items": items,
    }
    return _strip_comments(_render(template, mapping))


def _strip_article_ids(channels: dict) -> dict:
    """スキル同梱用: 記事 ID を含まない形にする(距離・値の列は保持)。"""
    out = {}
    for name, ref in channels.items():
        out[name] = {k: v for k, v in ref.items() if k != "article_ids"}
    return out


def build_morphology_reference(aggregate: dict) -> dict:
    m = aggregate.get("morphology") or {}
    conditional = {}
    for key, entry in (m.get("conditional") or {}).items():
        e = {k: v for k, v in entry.items() if k != "article_ids"}
        if "channels" in e:
            e["channels"] = _strip_article_ids(e["channels"])
        conditional[key] = e
    return {
        "$comment": (
            "G7 形態素チャネルの参照(validator 専用)。centroid は上位 K + OTHER に"
            "有界化した記事等重み平均、thresholds は著者内 LOAO 距離分布から較正(フル精度)。"
            "conditional は register / era / length 別の参照(十分な N のみ built)"
        ),
        "channel_registry_version": m.get("channel_registry_version"),
        "feature_schema": aggregate.get("feature_schema"),
        "analyzer": aggregate.get("analyzer"),
        "distance": m.get("distance"),
        "calibration_rule": m.get("calibration_rule"),
        "calibration_split": aggregate.get("calibration_split", aggregate.get("split")),
        "n_articles": aggregate.get("n_articles"),
        "available": m.get("available", False),
        "length_strata": m.get("length_strata"),
        "channels": _strip_article_ids(m.get("channels") or {}),
        "conditional": conditional,
    }


def _upper_gate(aggregate: dict, key: str) -> tuple[float | None, float | None, str | None]:
    """上限型: (warn=p95, hard=著者極値/Bonferroni, rule)。"""
    vals = _per_article_values(aggregate, key)
    if not vals:
        return None, None, None
    hard, rule = calib.upper_hard_bound(vals)
    return stats.quantile(vals, 0.95), hard, rule


def _lower_gate(aggregate: dict, key: str) -> tuple[float | None, float | None, str | None]:
    """下限型: (warn=p05, hard=著者極値/Bonferroni, rule)。"""
    vals = _per_article_values(aggregate, key)
    if not vals:
        return None, None, None
    hard, rule = calib.lower_hard_bound(vals)
    return stats.quantile(vals, 0.05), hard, rule


def build_lint_config(
    template: str,
    aggregate: dict,
    author_id: str,
    profile_version: str,
    now: str,
    manifest_hash: str,
    *,
    markers: list[str] | None = None,
    profile_class: str = "production",
    migration: dict | None = None,
) -> tuple[str, list[str]]:
    """lint-config.json の本文と、較正時の警告(退化帯域等)を返す。"""
    cfg = json.loads(template)
    gates = cfg["gates"]
    cal_warnings: list[str] = []

    def _range_gate(section: dict, warn_key: str, hard_key: str, metric: str) -> None:
        warn = _gate_range(aggregate, metric)
        hard = _hard_range(aggregate, metric)
        section[warn_key] = warn
        section[hard_key] = hard
        for note in _range_notes(metric, warn, hard):
            cal_warnings.append(f"{metric}: {note}")

    g1 = gates["G1_distribution"]
    _range_gate(g1["sent_len_chars"], "median_range", "median_hard_range", "sent_len_median")
    _range_gate(g1["para_len_sents"], "median_range", "median_hard_range", "para_len_median")
    _range_gate(g1["comma_per_sent"], "median_range", "median_hard_range", "comma_per_sent_median")
    mx_warn, mx_hard, mx_rule = _upper_gate(aggregate, "sent_len_max")
    g1["sent_len_chars"]["max_warn"] = mx_warn
    g1["sent_len_chars"]["max_hard"] = mx_hard
    g1["sent_len_chars"]["max_rule"] = mx_rule
    g1["min_sents"] = _MIN_SENTS

    g2 = gates["G2_sentence_end"]
    g2["form_distribution"] = {}
    g2["form_hard_range"] = {}
    for form in claims_lib.G2_FORMS:
        _range_gate_key = f"sent_end_form.{form}"
        warn = _gate_range(aggregate, _range_gate_key)
        hard = _hard_range(aggregate, _range_gate_key)
        g2["form_distribution"][form] = warn
        g2["form_hard_range"][form] = hard
        for note in _range_notes(_range_gate_key, warn, hard):
            cal_warnings.append(f"{_range_gate_key}: {note}")
    g2["min_sents"] = _MIN_SENTS
    run_vals = _per_article_values(aggregate, "max_consecutive_same_ending")
    if run_vals:
        # cap = 記事単位分布の p95(線形補間。超過は warn)、hard cap = 著者極値ポリシー(超過で fail)
        p95 = stats.quantile(run_vals, 0.95)
        cap = max(3, math.ceil(p95 - 1e-9))
        hard, rule = calib.upper_hard_bound(run_vals)
        g2["max_consecutive_same_ending"] = cap
        g2["max_consecutive_hard_cap"] = max(cap + 2, math.ceil(hard - 1e-9))
        g2["max_consecutive_rule"] = f"warn=p95(interpolated), hard={rule}"

    g3 = gates["G3_orthography"]
    for metric, cfg_key in claims_lib.SCRIPT_GATE_KEYS.items():
        _range_gate(g3, cfg_key, cfg_key.replace("_ratio", "_hard_range"), metric)
    g3["min_chars"] = _MIN_CHARS

    g4 = gates["G4_vocabulary"]
    for cfg_key, agg_key in (("ttr_window", "ttr_window"), ("distinct_2", "distinct_2")):
        warn, hard, rule = _lower_gate(aggregate, agg_key)
        g4[f"{cfg_key}_min"] = warn
        g4[f"{cfg_key}_hard_min"] = hard
        g4[f"{cfg_key}_rule"] = rule
    if aggregate["features"].get("func_word_rate"):
        _range_gate(g4, "func_word_rate_range", "func_word_rate_hard_range", "func_word_rate")
    else:
        g4["func_word_rate_range"] = [None, None]
        g4["func_word_rate_hard_range"] = [None, None]
    g4["min_tokens"] = _MIN_TOKENS

    g5 = gates["G5_caricature"]
    g5["markers"] = sorted(set(markers or []))
    g5["configured"] = bool(g5["markers"])
    g5["markers_source"] = "profile validator claims (metric=caricature_markers)" if g5["markers"] else None
    if not g5["markers"]:
        cal_warnings.append("G5_caricature: markers が空(G5 は skipped と報告され、pass ではない)")

    g6 = gates["G6_copy"]
    # 工学的初期値(コーパス間較正は Phase 6)。算出根拠は $comment 参照
    g6["char_ngram_overlap_max"] = 0.35
    g6["minhash_similarity_max"] = 0.7
    g6["paragraph_containment_max"] = 0.8

    g7 = gates["G7_morphology"]
    m = aggregate.get("morphology") or {}
    channels = m.get("channels") or {}
    g7["enabled"] = True
    g7["reference_file"] = "lint-morphology.json"
    g7["calibration"] = {
        "split": aggregate.get("calibration_split", aggregate.get("split")),
        "n_articles": aggregate.get("n_articles"),
        "rule": m.get("calibration_rule"),
        "analyzer_available": m.get("available", False),
        "channel_registry_version": m.get("channel_registry_version"),
    }
    g7["channels"] = {}
    for name, spec in morph_lib.CHANNELS.items():
        ch = channels.get(name) or {}
        g7["channels"][name] = {
            "kind": spec["kind"],
            "requires": spec["requires"],
            "max_severity": spec["max_severity"],
            "status": ch.get("status", "skipped"),
        }
        for note in ch.get("notes") or []:
            cal_warnings.append(f"morph.{name}: {note}")

    analyzer = aggregate["analyzer"]
    analyzer_str = (
        f"sudachipy=={analyzer['version']} / {analyzer['dict']}"
        if analyzer["mode"] == "sudachi"
        else "fallback(正規表現ベース)"
    )
    cfg["calibration"]["split"] = aggregate.get("calibration_split", aggregate.get("split"))
    cfg["calibration"]["analyzer_meta"] = analyzer
    cfg["calibration"]["feature_schema"] = aggregate.get("feature_schema")
    cfg["calibration"]["channel_registry_version"] = aggregate.get("channel_registry_version")
    cfg["calibration"]["policy"] = calib.policy_description()
    cfg["calibration"]["warnings"] = sorted(set(cal_warnings))
    cfg["profile_class"] = profile_class
    cfg["builder_status"] = BUILDER_STATUS
    cfg["migration"] = migration
    text = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    rendered = _render(
        text,
        {
            "author_id": author_id,
            "profile_version": profile_version,
            "dev_article_count": aggregate["n_articles"],
            "analyzer_version": analyzer_str,
            "calibration_date": now,
            "corpus_hash": manifest_hash,
        },
    )
    return rendered, sorted(set(cal_warnings))


def bundle_runner(out: Path) -> dict:
    """builder の style_lint 一式を生成スキルへコピーし、ハッシュを返す。"""
    files: dict[str, str] = {}
    for rel in RUNNER_FILES:
        src = _SCRIPTS_DIR / rel
        dst = out / "scripts" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        files[f"scripts/{rel}"] = io_utils.content_hash(src.read_text(encoding="utf-8"))
    sh = out / "scripts" / "lint.sh"
    sh.write_text(LINT_SH, encoding="utf-8")
    sh.chmod(0o755)
    files["scripts/lint.sh"] = io_utils.content_hash(LINT_SH)
    return files


# ---------------- main ----------------

def main(argv=None) -> int:
    args = parse_args(argv)
    ws = args.workspace
    profile_path = args.profile or (ws / "profile.json")

    if not profile_path.exists():
        print(f"error: profile がありません: {profile_path}", file=sys.stderr)
        return 1
    try:
        aggregate = io_utils.load_aggregate(ws)
    except FileNotFoundError:
        print(
            "error: features/_aggregate.json がありません。extract_features.py を先に実行してください",
            file=sys.stderr,
        )
        return 1
    calibration_split = aggregate.get("calibration_split") or aggregate.get("split")
    if calibration_split not in ALLOWED_CALIBRATION_SPLITS:
        print(
            f"error: aggregate の split={calibration_split!r} は較正に使えない"
            f"(holdout 混入)。extract_features.py --split train+dev で再実行すること",
            file=sys.stderr,
        )
        return 1
    agg_schema = aggregate.get("feature_schema")
    agg_registry = aggregate.get("channel_registry_version") or (aggregate.get("morphology") or {}).get("channel_registry_version")
    if agg_schema != feat.FEATURE_SCHEMA_VERSION or agg_registry != morph_lib.CHANNEL_REGISTRY_VERSION:
        print(
            f"error: aggregate の feature_schema={agg_schema!r} / channel_registry_version={agg_registry!r} が"
            f" builder(feature_schema={feat.FEATURE_SCHEMA_VERSION}, registry={morph_lib.CHANNEL_REGISTRY_VERSION})"
            "と違う。extract_features.py --split train+dev を再実行すること。"
            "古い aggregate と新しい profile(またはその逆)を黙って突き合わせない",
            file=sys.stderr,
        )
        return 1
    manifest = io_utils.load_manifest(ws)
    profile_text = profile_path.read_text(encoding="utf-8")
    profile = json.loads(profile_text)

    now = args.now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    author_id = profile.get("author_id") or manifest["author_id"]
    author_name = profile.get("author_name") or author_id
    profile_version = profile.get("version", "0.0.0")
    profile_hash = io_utils.content_hash(profile_text)
    manifest_hash = io_utils.content_hash(
        (ws / "manifest.json").read_text(encoding="utf-8")
    )
    rights_scope = (
        manifest.get("consent", {}).get("record") or "unspecified"
    )

    warnings: list[str] = []
    excluded: list[dict] = []  # 方針による除外(理由つき)
    unmapped: list[tuple[str, str]] = []  # 描画・写像できない claim → exit 2

    # ---- profile ↔ aggregate の整合(stale claim 検出) ----
    drifts, drift_warnings = claims_lib.check_profile_drift(profile, aggregate)
    for w in drift_warnings:
        warnings.append("warning: " + w)
    stale_ids = {d["claim_id"] for d in drifts}
    migration: dict | None = None
    if drifts and not args.allow_stale_claims:
        for w in warnings:
            print(w, file=sys.stderr)
        for d in drifts:
            print("error: stale claim: " + claims_lib.format_drift(d), file=sys.stderr)
        print(
            f"error: {len(drifts)} claim の数値が aggregate(feature_schema={agg_schema}, "
            f"analyzer={aggregate['analyzer'].get('mode')})で再現できないためコンパイル不合格(exit 2)。"
            "対処: (a) `stability_test.py --workspace <ws> --out profile-candidates-migration.json` で"
            "現行スキーマの候補を生成し、人間レビューで profile を更新する / "
            "(b) 移行候補の確認専用に `--allow-stale-claims` で該当 claim を excluded に回して続行する"
            "(本番リリースは skill_lint が拒否する)",
            file=sys.stderr,
        )
        return 2
    if drifts:
        migration = {
            "allow_stale_claims": True,
            "not_for_release": True,
            "stale_claims": [
                {k: v for k, v in d.items()} for d in sorted(drifts, key=lambda d: d["claim_id"])
            ],
        }
        warnings.append(
            f"warning: --allow-stale-claims: {len(drifts)} claim を excluded(stale)に回して続行。"
            "この出力は移行候補の確認専用で、本番リリースに使えない"
        )

    # ---- claim の選別(inferred / quarantined はコンパイル禁止) ----
    compilable: list[dict] = []
    unobserved_modes: set[str] = set()
    for c in profile.get("claims", []):
        cid = c.get("claim_id", "?")
        state = c.get("state")
        status = c.get("status")
        if state == "unobserved":
            if c.get("scope_mode") and c["scope_mode"] != "core":
                unobserved_modes.add(c["scope_mode"])
            excluded.append({"claim_id": cid, "reason": "state=unobserved(abstain 条件。描画対象外)"})
            continue
        if state != "observed" or status == "quarantined":
            warnings.append(
                f"warning: claim {cid} を除外"
                f"(state={state}, status={status})— inferred/quarantined はコンパイル禁止"
            )
            excluded.append({"claim_id": cid, "reason": f"state={state}, status={status}(コンパイル禁止)"})
            continue
        if cid in stale_ids:
            d = next(x for x in drifts if x["claim_id"] == cid)
            excluded.append({"claim_id": cid, "reason": f"stale_claim({d['kind']}): {d['detail']}"})
            continue
        compilable.append(c)

    if not compilable:
        for w in warnings:
            print(w, file=sys.stderr)
        print("error: コンパイル可能な claim がありません", file=sys.stderr)
        return 1

    is_exploratory = profile.get("profile_class") == _EXPLORATORY_PROFILE_CLASS
    if is_exploratory:
        approval = profile.get("approval") or {}
        approval_ok = bool(
            str(approval.get("decided_by") or "").strip()
            and str(approval.get("decided_at") or "").strip()
            and approval.get("decisions")
        )
        if not approval_ok:
            print(
                "error: exploratory profile には人間承認メタデータ"
                "(approval.decided_by / decided_at / decisions)が必要",
                file=sys.stderr,
            )
            return 1
        warnings.append(
            "profile: exploratory。成熟度と制約はmeta/provenance.jsonとlint-config.jsonに記録"
        )
    exploratory_claims = collect_exploratory_claims(profile, compilable)
    exploratory_ids = {c["claim_id"] for c in exploratory_claims}

    persona_claims: list = []
    always_on_claims: list = []
    core_conditional_claims: list = []
    example_claims: list = []
    negative_claims: list = []
    validator_claims: list = []
    checklist_claims: list = []
    mode_claims: dict[str, list] = {}
    g5_markers: list[str] = []

    for c in compilable:
        cid = c["claim_id"]
        target = c.get("compilation_target")
        status = c.get("status")
        metric = (c.get("feature") or {}).get("metric") or ""
        if metric.startswith("morph.") and target not in _MORPH_ALLOWED_TARGETS:
            unmapped.append(
                (cid, f"形態素チャネル {metric} は validator/checklist/example 専用。{target} へは描画しない")
            )
            continue
        if target == "persona":
            if status == "core":
                persona_claims.append(c)
            else:
                unmapped.append((cid, f"persona は core のみ(status={status})"))
        elif target == "always_on_rule":
            if status == "core":
                always_on_claims.append(c)
            else:
                unmapped.append((cid, f"always_on_rule は core のみ(status={status})"))
        elif target == "conditional_rule":
            cond = (c.get("condition") or "").strip()
            scope = c.get("scope_mode") or "core"
            if status == "mode_specific":
                mode_claims.setdefault(scope, []).append(c)
            elif status == "core":
                if not cond:
                    always_on_claims.append(c)  # core・無条件 → 常時ルールへ
                else:
                    core_conditional_claims.append(c)
            elif status == "ambiguous":
                if cid in exploratory_ids:
                    pass  # 文体傾向セクションへ(下で mapping)
                else:
                    excluded.append(
                        {
                            "claim_id": cid,
                            "reason": "ambiguous/conditional_rule は本番プロファイルの描画対象外(compile-rules.md)",
                        }
                    )
            else:
                unmapped.append((cid, f"conditional_rule に status={status} は写像できない"))
        elif target == "example":
            example_claims.append(c)
        elif target == "negative_example":
            negative_claims.append(c)
        elif target == "validator":
            gate = _GATE_OF_METRIC.get(metric)
            if not gate:
                unmapped.append((cid, f"validator の metric={metric!r} を評価するゲートが無い(名目上の対応は作らない)"))
                continue
            ok, reason = claims_lib.metric_evaluable(metric, aggregate)
            if not ok:
                unmapped.append((cid, f"validator の metric={metric!r} はこの aggregate で評価できない: {reason}"))
                continue
            if metric == "caricature_markers":
                mk = (c.get("value") or {}).get("markers") or []
                mk = [m for m in mk if isinstance(m, str) and m.strip()]
                if not mk:
                    unmapped.append((cid, "caricature_markers claim に value.markers(非空の文字列列)が無い"))
                    continue
                g5_markers.extend(mk)
            validator_claims.append(c)
        elif target == "checklist":
            checklist_claims.append(c)
        else:
            unmapped.append((cid, f"未知の compilation_target={target!r}"))

    # ---- テンプレート読込 ----
    tdir = args.templates
    templates = {
        "skill": (tdir / "SKILL.md.template").read_text(encoding="utf-8"),
        "lint": (tdir / "lint-config.json.template").read_text(encoding="utf-8"),
        "style": (tdir / "references" / "style-rules.md.template").read_text(
            encoding="utf-8"
        ),
        "examples": (tdir / "references" / "examples.md.template").read_text(
            encoding="utf-8"
        ),
        "checklist": (tdir / "references" / "checklist.md.template").read_text(
            encoding="utf-8"
        ),
        "profile_ref": (tdir / "meta" / "profile-ref.json.template").read_text(
            encoding="utf-8"
        ),
        "provenance": (tdir / "meta" / "provenance.json.template").read_text(
            encoding="utf-8"
        ),
    }
    activation_template_path = tdir / "eval" / "activation-cases.yaml.template"
    templates["activation"] = (
        activation_template_path.read_text(encoding="utf-8")
        if activation_template_path.exists()
        else None
    )

    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    strata = sorted({a["strata"] for a in aggregate["articles"]})
    genres = "・".join(_STRATA_LABEL.get(s, s) for s in strata) or "記事"

    # ---- SKILL.md ----
    skill_template = apply_exploratory_block(
        templates["skill"], exploratory_claims
    )
    skill_template = apply_core_conditional_block(skill_template, core_conditional_claims)
    skill_md = _render(
        skill_template,
        {
            "author_id": author_id,
            "author_name": author_name,
            "genres": genres,
            "rights_scope": rights_scope,
            "persona": build_persona(persona_claims),
            "always_on_rules": build_always_on(always_on_claims),
            "mode_table": build_mode_table(mode_claims),
        },
    )
    _write_text(out / "SKILL.md", _strip_comments(skill_md))

    # ---- references/ ----
    _write_text(
        out / "references" / "style-rules.md",
        build_style_rules(
            templates["style"], author_name, mode_claims, sorted(unobserved_modes)
        ),
    )
    examples_md, example_mappings = build_examples(
        templates["examples"],
        author_name,
        example_claims,
        negative_claims,
        ws,
        unmapped,
    )
    if examples_md is not None:
        _write_text(out / "references" / "examples.md", examples_md)
    _write_text(
        out / "references" / "checklist.md",
        build_checklist(templates["checklist"], author_name, checklist_claims),
    )

    # ---- lint-config.json / lint-morphology.json ----
    profile_class = profile.get("profile_class") or "production"
    lint_text, cal_warnings = build_lint_config(
        templates["lint"],
        aggregate,
        author_id,
        profile_version,
        now,
        manifest_hash,
        markers=g5_markers,
        profile_class=profile_class,
        migration=migration,
    )
    _write_text(out / "lint-config.json", lint_text)
    for w in cal_warnings:
        warnings.append("calibration: " + w)
    _write_text(
        out / "lint-morphology.json",
        json.dumps(build_morphology_reference(aggregate), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    # ---- eval/activation-cases.yaml(ゲート 2 の資材) ----
    # 人手で埋めるファイルなので、既存のものは上書きしない。
    # 未生成だと regression_run の activation_cases が常に不合格になるため、
    # 初回コンパイル時にテンプレートを展開しておく。
    activation_path = out / "eval" / "activation-cases.yaml"
    if templates["activation"] is not None and not activation_path.exists():
        _write_text(
            activation_path,
            _render(templates["activation"], {"author_name": author_name}),
        )

    # ---- scripts/(同梱リンター) ----
    runner_files = bundle_runner(out)

    # ---- meta/profile-ref.json ----
    mappings = []
    if persona_claims:
        mappings.append(
            {
                "target": "SKILL.md#persona",
                "claim_ids": sorted(c["claim_id"] for c in persona_claims),
            }
        )
    if always_on_claims:
        mappings.append(
            {
                "target": "SKILL.md#always_on_rules",
                "claim_ids": sorted(c["claim_id"] for c in always_on_claims),
            }
        )
    if core_conditional_claims:
        mappings.append(
            {
                "target": "SKILL.md#core_conditional_rules",
                "claim_ids": sorted(c["claim_id"] for c in core_conditional_claims),
            }
        )
    if exploratory_claims:
        mappings.append(
            {
                "target": "SKILL.md#style_tendencies",
                "claim_ids": sorted(c["claim_id"] for c in exploratory_claims),
            }
        )
    for mode_id in sorted(mode_claims):
        mappings.append(
            {
                "target": f"references/style-rules.md#{mode_id}",
                "claim_ids": sorted(c["claim_id"] for c in mode_claims[mode_id]),
            }
        )
    mappings.extend(example_mappings)
    if checklist_claims:
        mappings.append(
            {
                "target": "references/checklist.md#ambiguous",
                "claim_ids": sorted(c["claim_id"] for c in checklist_claims),
            }
        )
    gate_claims: dict[str, list] = {}
    for c in always_on_claims + validator_claims:
        metric = (c.get("feature") or {}).get("metric")
        gate = _GATE_OF_METRIC.get(metric)
        if gate:
            gate_claims.setdefault(gate, []).append(c["claim_id"])
    for gate in sorted(gate_claims):
        mappings.append(
            {"target": f"lint-config.json#{gate}", "claim_ids": sorted(gate_claims[gate])}
        )

    # ---- 完全性検査 ----
    mapped_ids = {i for m in mappings for i in m["claim_ids"]}
    excluded_ids = {e["claim_id"] for e in excluded}
    for c in compilable:
        cid = c["claim_id"]
        if cid not in mapped_ids and cid not in excluded_ids and not any(u[0] == cid for u in unmapped):
            unmapped.append((cid, "描画されたが profile-ref に写像がない(内部不整合)"))
    if unmapped:
        for w in warnings:
            print(w, file=sys.stderr)
        for cid, reason in unmapped:
            print(f"error: claim {cid} を描画・写像できない: {reason}", file=sys.stderr)
        print(
            f"error: {len(unmapped)} claim が profile-ref に写像できないためコンパイル不合格(exit 2)。"
            "claim の status / compilation_target / metric / evidence を修正すること",
            file=sys.stderr,
        )
        return 2

    profile_ref = json.loads(templates["profile_ref"])
    profile_ref["mappings"] = mappings
    profile_ref["excluded"] = sorted(excluded, key=lambda e: e["claim_id"])
    profile_ref_text = _render(
        json.dumps(profile_ref, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        {"author_id": author_id, "profile_version": profile_version},
    )
    _write_text(out / "meta" / "profile-ref.json", profile_ref_text)

    # ---- meta/provenance.json ----
    provenance = json.loads(templates["provenance"])
    provenance["source"]["article_count"] = aggregate["n_articles"]
    provenance["source"]["total_chars"] = sum(
        a["n_chars"] for a in aggregate["articles"]
    )
    provenance["source"]["strata"] = strata
    provenance["source"]["calibration_split"] = calibration_split
    provenance["source"]["analyzer"] = aggregate["analyzer"]
    provenance["source"]["feature_schema"] = aggregate.get("feature_schema")
    provenance["source"]["channel_registry_version"] = aggregate.get("channel_registry_version")
    provenance["source"]["profile_class"] = profile_class
    provenance["source"]["profile_feature_schema"] = profile.get("feature_schema")
    provenance["maturity"] = {
        "profile_class": profile_class,
        "approval": profile.get("approval") if is_exploratory else None,
        "limitations": profile.get("limitations"),
    }
    provenance["generator"]["builder_status"] = BUILDER_STATUS
    provenance["generator"]["feature_schema"] = feat.FEATURE_SCHEMA_VERSION
    provenance["generator"]["channel_registry_version"] = morph_lib.CHANNEL_REGISTRY_VERSION
    provenance["calibration_policy"] = calib.policy_description()
    provenance["migration"] = migration
    provenance["runner"] = {
        "$comment": "同梱リンター。builder の scripts/ から compile 時にコピー(sha256)",
        "entry": "scripts/lint.sh",
        "builder_version": META_SKILL_VERSION,
        "files": runner_files,
    }
    provenance_text = _render(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        {
            "author_id": author_id,
            "rights_scope": rights_scope,
            "consent_record_ref": rights_scope,
            "generated_at": now,
            "meta_skill_version": META_SKILL_VERSION,
            "profile_version": profile_version,
            "profile_hash": profile_hash,
            "manifest_hash": manifest_hash,
        },
    )
    _write_text(out / "meta" / "provenance.json", provenance_text)

    for w in warnings:
        print(w, file=sys.stderr)
    n_files = len(list(out.rglob("*.md"))) + len(list(out.rglob("*.json")))
    print(
        f"compile: {len(compilable)} claims -> {out}({n_files} files, "
        f"persona={len(persona_claims)}, rules={len(always_on_claims)}, "
        f"core_conditional={len(core_conditional_claims)}, "
        f"exploratory={len(exploratory_claims)}, "
        f"modes={len(mode_claims)}, examples={len(example_claims)}, "
        f"validators={len(validator_claims)}, excluded={len(excluded)}, "
        f"stale={len(stale_ids)}, g5_markers={len(set(g5_markers))}, "
        f"calibration_split={calibration_split}, feature_schema={agg_schema}, "
        f"builder_status={BUILDER_STATUS})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
