#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["sudachipy", "sudachidict-core"]
# ///
"""feedback_intake — 利用時フィードバック(Loop A)の採取と集計。

record: 生成文とユーザー最終稿の diff を特徴量レベルで記録し、
        lint-config のゲート・claim_id に帰責した候補を feedback/ に追記する。
report: 蓄積レコードを集計し、支持が min-support 以上の指摘だけを
        profile 更新候補レポート(feedback/report.json)に出力する。

このスクリプトは profile.json / 生成スキルを一切書き換えない(ACE 型:
蓄積と提案のみ。適用は maintainer が昇格フレームを通して行う)。

終了コード: 0=成功 / 1=エラー
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import claims as claims_lib
from lib import features as feat
from lib import io_utils
from lib.tokenize import get_analyzer

# metric → lint-config ゲート(lib/claims に一本化。compile_skill と同一)
GATE_OF_METRIC = claims_lib.GATE_OF_METRIC

_DELTA_EPS = 1e-6


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True, type=Path)
    sub = p.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="1 回の利用フィードバックを記録")
    rec.add_argument("--generated", required=True, type=Path, help="スキルが生成した文")
    rec.add_argument("--final", required=True, type=Path, help="ユーザーの最終稿")
    rec.add_argument("--skill", type=Path, default=None, help="生成スキル dir(claim 帰責に使用)")
    rec.add_argument("--task-note", default="", help="任意メモ(モード・依頼内容など)")
    rec.add_argument("--now", default=None, help="記録日時(ISO 8601)。決定的出力用")

    rep = sub.add_parser("report", help="蓄積レコードを集計して候補レポートを出力")
    rep.add_argument("--skill", type=Path, default=None)
    rep.add_argument("--min-support", type=int, default=3, help="採用候補に必要な独立レコード数")
    rep.add_argument("--direction-agreement", type=float, default=0.7, help="方向一致率の下限")
    return p.parse_args(argv)


def flatten_metrics(record: dict) -> dict:
    """FeatureRecord → metric キーのフラット辞書(lint-config と同じキー体系)。"""
    out = {
        "sent_len_median": record["sent_len"]["median"] if record["n_sents"] else None,
        "para_len_median": record["para_len"]["median"] if record["n_sents"] else None,
        "comma_per_sent_median": (
            record["comma_per_sent"]["median"] if record["n_sents"] else None
        ),
        "max_consecutive_same_ending": record["max_consecutive_same_ending"],
        "ttr_window": record.get("ttr_window"),
        "distinct_2": record.get("distinct_2"),
        "func_word_rate": record.get("func_word_rate"),
    }
    for form, v in record["sent_end_form"].items():
        out[f"sent_end_form.{form}"] = v
    for script, v in record["script_ratio"].items():
        out[f"script_ratio.{script}"] = v
    return out


def _extract_metrics(text: str, analyzer) -> dict:
    # style_lint / extract_features と同じ散文契約(lib/blocks)で計測する
    return flatten_metrics(feat.record_from_text(text, analyzer))


def _gate_ranges(lint_config: dict) -> dict:
    """lint-config.json から metric → [lo, hi] を抽出。"""
    gates = lint_config.get("gates", {})
    out = {}
    g1 = gates.get("G1_distribution", {})
    out["sent_len_median"] = (g1.get("sent_len_chars") or {}).get("median_range")
    out["para_len_median"] = (g1.get("para_len_sents") or {}).get("median_range")
    out["comma_per_sent_median"] = (g1.get("comma_per_sent") or {}).get("median_range")
    g2 = gates.get("G2_sentence_end", {})
    for form, rng in (g2.get("form_distribution") or {}).items():
        out[f"sent_end_form.{form}"] = rng
    g3 = gates.get("G3_orthography", {})
    for metric, key in claims_lib.SCRIPT_GATE_KEYS.items():
        out[metric] = g3.get(key)
    g4 = gates.get("G4_vocabulary", {})
    out["func_word_rate"] = g4.get("func_word_rate_range")
    return {k: v for k, v in out.items() if v and v[0] is not None}


def _claims_by_gate(skill_dir: Path | None) -> dict:
    """profile-ref.json から gate → claim_ids。"""
    if skill_dir is None:
        return {}
    ref_path = skill_dir / "meta" / "profile-ref.json"
    if not ref_path.exists():
        return {}
    ref = json.loads(ref_path.read_text(encoding="utf-8"))
    out = {}
    for m in ref.get("mappings", []):
        target = m.get("target", "")
        if target.startswith("lint-config.json#"):
            out[target.split("#", 1)[1]] = m.get("claim_ids", [])
    return out


def _in_range(value, rng) -> bool | None:
    if value is None or not rng:
        return None
    return rng[0] <= value <= rng[1]


def cmd_record(args) -> int:
    for p in (args.generated, args.final):
        if not p.exists():
            print(f"error: ファイルがありません: {p}", file=sys.stderr)
            return 1
    generated = args.generated.read_text(encoding="utf-8")
    final = args.final.read_text(encoding="utf-8")
    analyzer = get_analyzer()
    gen_m = _extract_metrics(generated, analyzer)
    fin_m = _extract_metrics(final, analyzer)

    lint_config = {}
    if args.skill is not None:
        cfg_path = args.skill / "lint-config.json"
        if cfg_path.exists():
            lint_config = json.loads(cfg_path.read_text(encoding="utf-8"))
    ranges = _gate_ranges(lint_config)
    claims = _claims_by_gate(args.skill)

    metrics = {}
    gate_shifts = []
    for key in sorted(set(gen_m) | set(fin_m)):
        g, f = gen_m.get(key), fin_m.get(key)
        if g is None or f is None:
            continue
        metrics[key] = {
            "generated": round(g, 4),
            "final": round(f, 4),
            "delta": round(f - g, 4),
        }
        rng = ranges.get(key)
        gin, fin_in = _in_range(g, rng), _in_range(f, rng)
        if rng and gin != fin_in:
            gate = GATE_OF_METRIC.get(key)
            gate_shifts.append(
                {
                    "metric": key,
                    "gate": gate,
                    "generated_in_range": gin,
                    "final_in_range": fin_in,
                    "claim_ids": claims.get(gate, []),
                    "note": (
                        "ユーザー編集がレンジ外へ移動 → レンジが実選好より狭い可能性"
                        if gin and not fin_in
                        else "ユーザー編集がレンジ内へ復帰 → ルールは有効だが生成が守れていない"
                    ),
                }
            )

    sim = difflib.SequenceMatcher(a=generated, b=final).ratio()
    now = args.now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = {
        "id": f"fb-{io_utils.content_hash(generated + final + now)[:8]}",
        "recorded_at": now,
        "task_note": args.task_note,
        "skill": str(args.skill) if args.skill else None,
        "diff": {
            "similarity": round(sim, 4),
            "edited_char_ratio": round(1 - sim, 4),
            "generated_chars": len(generated),
            "final_chars": len(final),
        },
        "metrics": metrics,
        "gate_shifts": gate_shifts,
    }
    fb_dir = args.workspace / "feedback"
    fb_dir.mkdir(parents=True, exist_ok=True)
    out_path = fb_dir / f"{record['id']}.json"
    out_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"feedback: {out_path.name} recorded "
        f"(similarity={record['diff']['similarity']}, shifts={len(gate_shifts)})"
    )
    return 0


def cmd_report(args) -> int:
    fb_dir = args.workspace / "feedback"
    records = []
    for p in sorted(fb_dir.glob("fb-*.json")) if fb_dir.exists() else []:
        records.append(json.loads(p.read_text(encoding="utf-8")))
    if not records:
        print("error: feedback レコードがありません", file=sys.stderr)
        return 1

    claims = _claims_by_gate(args.skill)
    by_metric: dict[str, list] = {}
    for r in records:
        for key, m in r.get("metrics", {}).items():
            if abs(m["delta"]) > _DELTA_EPS:
                by_metric.setdefault(key, []).append(m["delta"])

    candidates = []
    for key in sorted(by_metric):
        deltas = by_metric[key]
        n = len(deltas)
        if n < args.min_support:
            continue
        pos = sum(1 for d in deltas if d > 0)
        agreement = max(pos, n - pos) / n
        if agreement < args.direction_agreement:
            continue
        deltas_sorted = sorted(deltas)
        median = deltas_sorted[n // 2]
        gate = GATE_OF_METRIC.get(key)
        candidates.append(
            {
                "metric": key,
                "gate": gate,
                "claim_ids": claims.get(gate, []),
                "support": n,
                "direction_agreement": round(agreement, 3),
                "median_delta": round(median, 4),
                "suggestion": (
                    f"{key} をユーザー最終稿側へ再較正する候補"
                    f"(中央デルタ {median:+.4f})。profile の該当 claim の"
                    " range 見直しを maintainer がレビューすること"
                ),
            }
        )

    shift_counts: dict[str, int] = {}
    for r in records:
        for s in r.get("gate_shifts", []):
            k = f"{s['metric']}|{'widen' if s['generated_in_range'] else 'enforce'}"
            shift_counts[k] = shift_counts.get(k, 0) + 1

    report = {
        "n_records": len(records),
        "min_support": args.min_support,
        "candidates": candidates,
        "gate_shift_counts": dict(sorted(shift_counts.items())),
        "note": (
            "本レポートは提案のみ。profile.json への適用は昇格フレーム"
            "(dev 評価 + 人間承認)を通すこと。profile は自動更新されない"
        ),
    }
    out_path = fb_dir / "report.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"report: {len(records)} records -> {out_path} "
        f"(candidates={len(candidates)})"
    )
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.command == "record":
        return cmd_record(args)
    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
