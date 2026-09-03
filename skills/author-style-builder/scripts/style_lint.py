#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["sudachipy", "sudachidict-core"]
# ///
"""style_lint — 生成テキストを lint-config.json のゲート G1〜G7 で評価。

- 入力テキストは lib/blocks の散文契約(extract_features と同一)で正規化する。
  コード・表・見出し・単独行 URL は統計に入らず、インラインコードは名詞
  プレースホルダに置換される。同じ散文からは extract_features と同じ
  FeatureRecord が得られる
- ゲート別に pass / warn / fail(+span 指摘、raw 座標)を JSON で報告。合成スコアなし
- レンジ型検査は warn 帯(median±IQR)と hard 帯(著者実記事の極値ポリシー)を別々に持ち、
  hard 帯の内側は fail しない(較正記事が丸めや境界一致だけで fail しない不変条件)
- 短すぎる入力(min_sents / min_chars / min_tokens 未満)は該当ゲートを warn に格下げ
  (degraded)し、理由を報告する。四文の記事を分布ゲートで hard fail させない
- 解析器互換: config の calibration.analyzer_meta と実行時の解析器を比較し、モードが
  違えば(例: Sudachi 較正のスキルを fallback で実行)G2 は degraded(analyzer_mode_mismatch)、
  G4 と G7 の POS チャネルは skipped。互換性の無い分布を黙って比べない。表層チャネルは維持
- 長さ層別(short / medium / long)を length_stratum に報告(散文文字数)
- G4 は POS 依存のため fallback モードでは skipped
- G5 は config の markers のローカルウィンドウ内 cap。markers が空なら skipped(pass ではない)
- G6(コピー)は --source-corpus 指定時のみ実行(未指定なら明示的に skipped)。
  exact 一致・文字 5-gram Jaccard・MinHash に加え、段落単位の局所重複
  (containment)を評価する
- G7(形態素)は config の G7_morphology.reference_file(config からの相対パス)
  を読み、チャネルごとに status / distance / percentile / top_deviations /
  example_spans / worst_slice を報告する。合成スコアは作らない。
  max_severity=warn のチャネルは fail にならない。fallback では POS 依存
  チャネルは skipped。register / length / era の条件付き参照(slices)は
  情報として併記し、ゲート判定は global 参照で行う
- コーパス・生成本文はレポートに 50 字を超えて引用しない

終了コード: 0=成功(fail なし)/ 1=エラー / 2=fail あり
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import blocks as blocks_lib
from lib import features as feat
from lib import morph as morph_lib
from lib.tokenize import get_analyzer
from overlap_check import (
    DEFAULT_STOPLIST_PATTERNS,
    compare_against_corpus,
    corpus_text_files,
)

# 範囲外でも幅の 50% 以内なら warn、それを超えたら fail
_WARN_FACTOR = 0.5
_EXAMPLE_SPANS_MAX = 2
_EXCERPT_MAX = 50
_STATUS_RANK = {"pass": 0, "warn": 1, "fail": 2, "skipped": -1}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", type=Path, default=None, help="(任意・未使用)")
    p.add_argument("--config", required=True, type=Path, help="lint-config.json")
    p.add_argument("--text", required=True, type=Path, help="評価対象テキスト")
    p.add_argument(
        "--source-corpus",
        type=Path,
        default=None,
        help="G6(コピー検査)のソースコーパス。未指定なら G6 は skipped",
    )
    p.add_argument(
        "--era",
        default=None,
        help="G7 の era スライス(YYYY)。未指定なら era スライスは評価しない",
    )
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def _check_range(measured, rng, hard=None) -> tuple[str | None, list | None]:
    """(status, expected)。評価不能なら (None, None)。

    warn 帯 rng の内側 = pass。外側は、hard 帯(著者極値)と warn 帯±幅の 50% の合併の
    内側なら warn、それも外れたら fail。hard が無ければ従来通り 50% ルールのみ。
    """
    if measured is None or not rng or rng[0] is None or rng[1] is None:
        return None, None
    lo, hi = rng
    width = max(hi - lo, 1e-9)
    if lo <= measured <= hi:
        return "pass", [lo, hi]
    fail_lo = lo - _WARN_FACTOR * width
    fail_hi = hi + _WARN_FACTOR * width
    if hard and hard[0] is not None and hard[1] is not None:
        fail_lo = min(fail_lo, hard[0])
        fail_hi = max(fail_hi, hard[1])
    if fail_lo <= measured <= fail_hi:
        return "warn", [lo, hi]
    return "fail", [lo, hi]


class GateResult:
    def __init__(self, span_all: list):
        self.findings: list[dict] = []
        self.statuses: list[str] = []
        self._span_all = span_all
        self.degraded: list[str] = []

    def degrade(self, reason: str) -> None:
        """以降の判定を warn 上限に格下げする(理由を報告)。"""
        if reason not in self.degraded:
            self.degraded.append(reason)

    def _cap(self, status: str) -> str:
        if self.degraded and status == "fail":
            return "warn"
        return status

    def add_range_check(self, name: str, measured, rng, span=None, *, hard=None) -> None:
        status, expected = _check_range(measured, rng, hard)
        if status is None:
            return
        raw_status = status
        status = self._cap(status)
        self.statuses.append(status)
        if status != "pass":
            finding = {
                "span": span or self._span_all,
                "message": f"{name} が範囲外",
                "measured": measured,
                "expected": expected,
            }
            if hard and hard[0] is not None:
                finding["hard_range"] = hard
            if raw_status != status:
                finding["degraded"] = self.degraded[0]
            self.findings.append(finding)

    def add(self, status: str, finding: dict | None = None) -> None:
        raw_status = status
        status = self._cap(status)
        self.statuses.append(status)
        if finding is not None:
            if raw_status != status:
                finding = dict(finding, degraded=self.degraded[0])
            self.findings.append(finding)

    def result(self) -> dict:
        if not self.statuses:
            out = {"status": "skipped", "findings": []}
        else:
            worst = max(self.statuses, key=lambda s: _STATUS_RANK[s])
            out = {"status": worst, "findings": self.findings}
        if self.degraded:
            out["degraded"] = list(self.degraded)
        return out


def _length_stratum(n_chars: int, strata_cfg: dict) -> str:
    for name in ("short", "medium", "long"):
        bounds = strata_cfg.get(name)
        if not bounds:
            continue
        lo = bounds[0] or 0
        hi = bounds[1]
        if n_chars >= lo and (hi is None or n_chars < hi):
            return name
    return "long"


def _register_of(record: dict) -> str:
    f = record["sent_end_form"]
    polite = f.get("desu_masu", 0.0)
    plain = f.get("da_dearu", 0.0) + f.get("jotai_verb", 0.0) + f.get("jotai_adj", 0.0)
    if polite >= 0.5:
        return "desu_masu"
    if plain >= 0.5:
        return "jotai"
    return "mixed"


def _excerpt(text: str) -> str:
    t = " ".join(text.split())
    return t if len(t) <= _EXCERPT_MAX else t[:_EXCERPT_MAX] + "…"


# ---------------- 解析器互換 ----------------

def _analyzer_compat(calibrated: dict | None, runtime: dict) -> dict:
    """較正時と実行時の解析器を比較。status: match | version_mismatch | mode_mismatch | unknown。"""
    if not calibrated or not calibrated.get("mode"):
        return {
            "status": "unknown",
            "calibrated_mode": None,
            "runtime_mode": runtime.get("mode"),
            "note": "config に calibration.analyzer_meta が無い(旧 builder 生成物)。互換性を検証できない",
        }
    cm, rm = calibrated.get("mode"), runtime.get("mode")
    if cm != rm:
        status = "mode_mismatch"
    elif cm == "sudachi" and (
        calibrated.get("version") != runtime.get("version")
        or calibrated.get("dict") != runtime.get("dict")
        or calibrated.get("split_mode") != runtime.get("split_mode")
    ):
        status = "version_mismatch"
    else:
        status = "match"
    return {
        "status": status,
        "calibrated_mode": cm,
        "runtime_mode": rm,
        "calibrated": calibrated,
        "runtime": runtime,
        "note": (
            "解析器モードが較正と違う: G2 は degraded、G4 / G7 の POS チャネルは skipped。表層ゲートのみ有効"
            if status == "mode_mismatch"
            else ("辞書/版が較正と違う。POS 依存の値は小さくずれ得る(報告のみ)" if status == "version_mismatch" else None)
        ),
    }


# ---------------- G5 ----------------

def _gate5(text: str, cfg: dict, span_all: list) -> dict:
    g = GateResult(span_all)
    markers = cfg.get("markers") or []
    if not markers:
        return {
            "status": "skipped",
            "findings": [],
            "reason": "no_markers",
            "note": "G5 markers が未設定。カリカチュア検査は評価していない(pass ではない)",
        }
    window = cfg.get("window_chars", 1000)
    cap = cfg.get("max_per_window", 2)
    for marker in markers:
        positions = [m.start() for m in re.finditer(re.escape(marker), text)]
        ok = True
        for i in range(len(positions) - cap):
            # positions[i] .. positions[i+cap] の cap+1 個が同一ウィンドウ内
            if positions[i + cap] - positions[i] < window:
                ok = False
                g.add(
                    "fail",
                    {
                        "span": [positions[i], positions[i + cap] + len(marker)],
                        "message": f"マーカー「{marker[:20]}」が {window} 字以内に "
                        f"{cap + 1} 回以上(カリカチュア)",
                        "measured": cap + 1,
                        "expected": cap,
                    },
                )
                break
        if ok:
            g.add("pass")
    return g.result()


# ---------------- G6 ----------------

def _gate6(text: str, cfg: dict, source_dir: Path, seed: int) -> dict:
    corpus_files = corpus_text_files(source_dir)
    if not corpus_files:
        return {
            "status": "skipped",
            "findings": [],
            "reason": "no_corpus_files",
            "note": "source-corpus に txt/md がない",
        }
    max_chars = cfg.get("exact_match_max_chars") or 25
    stoplist = cfg.get("stoplist_patterns") or DEFAULT_STOPLIST_PATTERNS
    report = compare_against_corpus(
        text,
        corpus_files,
        min_chars=max_chars + 1,
        seed=seed,
        stoplist_patterns=stoplist,
        paragraph_min_chars=cfg.get("paragraph_min_chars") or 30,
    )
    g = GateResult([0, len(text)])
    matches = report["exact"]["matches"]
    if matches:
        for m in matches:
            g.add(
                "fail",
                {
                    "span": m["text_span"],
                    "message": f"ソース {m['source']} と {m['length']} 字の連続一致(コピー)",
                    "measured": m["length"],
                    "expected": max_chars,
                },
            )
    else:
        g.add("pass")
    jac_max = report["char_5gram_jaccard"]["max"]
    jac_cap = cfg.get("char_ngram_overlap_max")
    if jac_cap is not None:
        if jac_max > jac_cap:
            g.add(
                "fail",
                {
                    "span": [0, len(text)],
                    "message": "文字 5-gram Jaccard がソースと過大",
                    "measured": jac_max,
                    "expected": jac_cap,
                },
            )
        else:
            g.add("pass")
    mh_max = report["minhash"]["max"]
    mh_cap = cfg.get("minhash_similarity_max")
    if mh_cap is not None:
        if mh_max > mh_cap:
            g.add(
                "fail",
                {
                    "span": [0, len(text)],
                    "message": "MinHash 類似度がソースと過大",
                    "measured": mh_max,
                    "expected": mh_cap,
                },
            )
        else:
            g.add("pass")
    para = report["paragraph"]
    para_cap = cfg.get("paragraph_containment_max")
    if para_cap is not None and para["n_paragraphs"]:
        offenders = [p for p in para["per_paragraph"] if p["containment_max"] > para_cap]
        if offenders:
            for p in offenders:
                g.add(
                    "fail",
                    {
                        "span": p["span"],
                        "message": f"段落がソース {p['source']} とほぼ重複(局所コピー)",
                        "measured": p["containment_max"],
                        "expected": para_cap,
                    },
                )
        else:
            g.add("pass")
    out = g.result()
    out["normalization"] = report["normalization"]
    out["measured"] = {
        "char_5gram_jaccard_max": jac_max,
        "minhash_similarity_max": mh_max,
        "exact_matches": len(matches),
        "paragraph_containment_max": para["containment_max"],
        "paragraph_near_dup_count": para["near_dup_count"],
        "paragraph_near_dup_ratio": round(para["near_dup_ratio"], 4),
        "n_paragraphs": para["n_paragraphs"],
    }
    out["copy_index"] = report["copy_index"]
    return out


# ---------------- G7 ----------------

def _load_morph_reference(config_path: Path, g7_cfg: dict) -> tuple[dict | None, str | None]:
    """lint-morphology.json を config からの相対パスで解決。(ref, reason)。"""
    if not g7_cfg or not g7_cfg.get("enabled", False):
        return None, "disabled"
    rel = g7_cfg.get("reference_file") or "lint-morphology.json"
    path = Path(rel)
    if not path.is_absolute():
        path = config_path.resolve().parent / rel
    if not path.exists():
        return None, f"reference_missing({rel})"
    try:
        with open(path, encoding="utf-8") as f:
            ref = json.load(f)
    except json.JSONDecodeError as e:
        return None, f"reference_invalid({e.msg})"
    if ref.get("channel_registry_version") != morph_lib.CHANNEL_REGISTRY_VERSION:
        return None, (
            f"registry_version_mismatch(reference={ref.get('channel_registry_version')}, "
            f"runner={morph_lib.CHANNEL_REGISTRY_VERSION})"
        )
    return ref, None


def _sentence_channel_keys(sentence: dict, channel: str, mode: str) -> set:
    """1 文がチャネルに寄与するキー集合(example span 用)。"""
    s = dict(sentence)
    if not channel.startswith("para_initial"):
        s["para"] = None
    m = morph_lib.extract_morphology([s], 1, mode)
    d = m["dist"].get(channel)
    return set(d) if d else set()


def _example_spans(
    channel: str,
    deviations: list[dict],
    sentences: list[dict],
    mode: str,
) -> list[dict]:
    """過剰側(delta>0)の最大偏差キーを含む文の raw span(最大 2)。"""
    over = [d for d in deviations if d["delta"] > 0 and d["key"] != morph_lib.OTHER]
    if not over or not sentences:
        return []
    target = over[0]["key"]
    spans: list[dict] = []
    for s in sentences:
        if target in _sentence_channel_keys(s, channel, mode):
            spans.append(
                {
                    "span": s["raw_span"],
                    "key": target,
                    "excerpt": _excerpt(s["text"]),
                }
            )
            if len(spans) >= _EXAMPLE_SPANS_MAX:
                break
    return spans


def _evaluate_channels(morph_block: dict, channels_ref: dict, sentences: list[dict], mode: str, *, with_examples: bool) -> dict:
    out: dict = {}
    for name, spec in morph_lib.CHANNELS.items():
        ref = channels_ref.get(name) or {"status": "skipped", "reason": "reference_absent"}
        sample_n = morph_block["sample"].get(name, 0)
        if spec["kind"] == "dist":
            res = morph_lib.evaluate_dist(name, morph_block["dist"].get(name), ref, sample_n)
        else:
            res = morph_lib.evaluate_scalar(name, morph_block["scalar"].get(name), ref, sample_n)
        if res["status"] == "skipped" and res.get("reason") == "channel_unavailable" and spec["requires"] == "sudachi" and mode != "sudachi":
            res["reason"] = "analyzer_fallback(pos_channel_unavailable)"
        res["kind"] = spec["kind"]
        res["requires"] = spec["requires"]
        res["label"] = spec["label"]
        res["sample"] = sample_n
        if with_examples and spec["kind"] == "dist" and res["status"] in ("warn", "fail"):
            res["example_spans"] = _example_spans(name, res.get("top_deviations", []), sentences, mode)
        elif with_examples and spec["kind"] == "dist":
            res["example_spans"] = []
        out[name] = res
    return out


def _gate7(record: dict, sentences: list[dict], ref: dict | None, reason: str | None, mode: str, slice_keys: list[str], span_all: list, *, mode_mismatch: bool = False) -> dict:
    if ref is None:
        return {"status": "skipped", "findings": [], "reason": reason, "channels": {}}
    morph_block = record["morph"]
    channels_ref = ref.get("channels") or {}
    global_eval = _evaluate_channels(morph_block, channels_ref, sentences, mode, with_examples=True)
    if mode_mismatch:
        # 較正と実行の解析器モードが違う: POS 依存チャネルは互換性の無い分布を比べない
        for name, res in global_eval.items():
            if morph_lib.CHANNELS[name]["requires"] == "sudachi":
                res["status"] = "skipped"
                res["reason"] = f"analyzer_mode_mismatch({res.get('reason') or 'pos_channel'})"
                for k in ("distance", "value", "percentile", "top_deviations", "example_spans"):
                    res.pop(k, None)

    # 条件付き参照(register / length / era)は情報として併記。判定は global
    conditional = ref.get("conditional") or {}
    slice_evals: dict[str, dict] = {}
    for key in slice_keys:
        entry = conditional.get(key)
        if not entry or entry.get("status") not in ("built", "shrunk"):
            slice_evals[key] = {"status": "skipped", "reason": (entry or {}).get("reason", "slice_absent")}
            continue
        slice_evals[key] = {
            "status": entry["status"],
            "n": entry.get("n"),
            "channels": _evaluate_channels(morph_block, entry.get("channels") or {}, sentences, mode, with_examples=False),
        }

    findings: list[dict] = []
    statuses: list[str] = []
    for name, res in global_eval.items():
        # worst_slice: global + 各スライスのうち最も悪い(rank, percentile)
        candidates = [("global", res)]
        for key, sev in slice_evals.items():
            ch = (sev.get("channels") or {}).get(name)
            if ch and ch.get("status") not in (None, "skipped"):
                candidates.append((key, ch))
        scored = [
            (k, r) for k, r in candidates if r.get("status") not in (None, "skipped")
        ]
        if scored:
            worst_key, worst = max(
                scored, key=lambda kr: (_STATUS_RANK[kr[1]["status"]], kr[1].get("percentile", 0.0))
            )
            res["worst_slice"] = {
                "slice": worst_key,
                "status": worst["status"],
                "percentile": worst.get("percentile"),
                "distance": worst.get("distance", worst.get("value")),
            }
        else:
            res["worst_slice"] = None
        res["slices"] = {
            k: (sev.get("channels") or {}).get(name, {"status": sev.get("status"), "reason": sev.get("reason")})
            for k, sev in slice_evals.items()
        }
        if res["status"] == "skipped":
            continue
        statuses.append(res["status"])
        if res["status"] != "pass":
            spans = res.get("example_spans") or []
            metric = res.get("distance", res.get("value"))
            findings.append(
                {
                    "span": spans[0]["span"] if spans else span_all,
                    "message": f"morph.{name}({res['label']})が著者分布から乖離"
                    + ("(warn 上限のチャネル)" if res["max_severity"] == "warn" else ""),
                    "measured": metric,
                    "expected": res["thresholds"],
                    "channel": name,
                    "percentile": res.get("percentile"),
                }
            )
    n_eval = len(statuses)
    if n_eval == 0:
        status = "skipped"
    else:
        status = max(statuses, key=lambda s: _STATUS_RANK[s])
    return {
        "status": status,
        "findings": findings,
        "reason": None if n_eval else "no_channel_evaluated",
        "reference": {
            "calibration_split": ref.get("calibration_split"),
            "n_articles": ref.get("n_articles"),
            "analyzer_available": ref.get("available"),
            "distance": ref.get("distance"),
        },
        "analyzer_mode": mode,
        "summary": {
            "evaluated": n_eval,
            "pass": statuses.count("pass"),
            "warn": statuses.count("warn"),
            "fail": statuses.count("fail"),
            "skipped": len(global_eval) - n_eval,
        },
        "slices": {k: {"status": v.get("status"), "n": v.get("n"), "reason": v.get("reason")} for k, v in slice_evals.items()},
        "composite_score": None,
        "channels": global_eval,
    }


# ---------------- main ----------------

def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.config.exists():
        print(f"error: config がありません: {args.config}", file=sys.stderr)
        return 1
    if not args.text.exists():
        print(f"error: text がありません: {args.text}", file=sys.stderr)
        return 1
    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    text = args.text.read_text(encoding="utf-8")

    analyzer = get_analyzer()
    runtime_meta = analyzer.meta()
    mode = runtime_meta["mode"]
    blocks = blocks_lib.classify_text(text)
    segments = blocks_lib.prose_segments(blocks)
    record = feat.extract_article_features(blocks, analyzer)
    sentences = feat.build_sentences(segments, analyzer)
    span_all = [0, len(text)]
    gates_cfg = config.get("gates", {})
    gates: dict[str, dict] = {}

    # ---- 解析器互換(較正時 vs 実行時) ----
    cal_meta = (config.get("calibration") or {}).get("analyzer_meta") or None
    compat = _analyzer_compat(cal_meta, runtime_meta)
    mode_mismatch = compat["status"] == "mode_mismatch"
    n_sents = record["n_sents"]
    n_tokens = (record.get("morph") or {}).get("n_tokens") or 0

    # ---- G1 分布 ----
    g1_cfg = gates_cfg.get("G1_distribution", {})
    g1 = GateResult(span_all)
    min_sents = g1_cfg.get("min_sents")
    if min_sents and n_sents < min_sents:
        g1.degrade(f"insufficient_sents({n_sents}<{min_sents})")
    sl = g1_cfg.get("sent_len_chars") or {}
    g1.add_range_check(
        "sent_len_median",
        record["sent_len"]["median"] if n_sents else None,
        sl.get("median_range"),
        hard=sl.get("median_hard_range"),
    )
    mx_warn, mx_hard = sl.get("max_warn"), sl.get("max_hard")
    if mx_warn is not None and n_sents:
        max_len = record["sent_len"]["max"]
        if max_len <= mx_warn:
            g1.add("pass")
        else:
            over_hard = mx_hard is not None and max_len > mx_hard
            g1.add(
                "fail" if over_hard else "warn",
                {
                    "span": span_all,
                    "message": f"最長文が著者分布を超過(目安 {mx_warn:.4g} 字、上限 {mx_hard})",
                    "measured": max_len,
                    "expected": mx_warn,
                    "hard_max": mx_hard,
                },
            )
    pl = g1_cfg.get("para_len_sents") or {}
    g1.add_range_check(
        "para_len_median",
        record["para_len"]["median"] if n_sents else None,
        pl.get("median_range"),
        hard=pl.get("median_hard_range"),
    )
    cm = g1_cfg.get("comma_per_sent") or {}
    g1.add_range_check(
        "comma_per_sent_median",
        record["comma_per_sent"]["median"] if n_sents else None,
        cm.get("median_range"),
        hard=cm.get("median_hard_range"),
    )
    gates["G1"] = g1.result()

    # ---- G2 文末 ----
    g2_cfg = gates_cfg.get("G2_sentence_end", {})
    g2 = GateResult(span_all)
    if mode_mismatch:
        # 文末形式分類は解析器モードに依存する(fallback は表層近似)。較正と違うモードで
        # 得た分布を hard fail の根拠にしない
        g2.degrade(f"analyzer_mode_mismatch(calibrated={compat['calibrated_mode']}, runtime={mode})")
    min_sents2 = g2_cfg.get("min_sents")
    if min_sents2 and n_sents < min_sents2:
        g2.degrade(f"insufficient_sents({n_sents}<{min_sents2})")
    hard_forms = g2_cfg.get("form_hard_range") or {}
    for form, rng in (g2_cfg.get("form_distribution") or {}).items():
        g2.add_range_check(
            f"sent_end_form.{form}",
            record["sent_end_form"].get(form) if n_sents else None,
            rng,
            hard=hard_forms.get(form),
        )
    cap = g2_cfg.get("max_consecutive_same_ending")
    if cap is not None and n_sents:
        # cap(p95 較正)超過は warn、hard cap(著者極値ポリシー)超過で fail。
        # 連続数と span は FeatureRecord(散文契約)のものをそのまま使う
        hard = g2_cfg.get("max_consecutive_hard_cap") or (cap + max(2, cap // 2))
        max_run = record["max_consecutive_same_ending"]
        run_span = record["prose"]["max_consecutive_span"]
        if max_run > cap:
            status = "fail" if max_run > hard else "warn"
            g2.add(
                status,
                {
                    "span": run_span,
                    "message": (
                        f"同一文末形式が {max_run} 連続"
                        f"(目安 {cap}、上限 {hard})"
                    ),
                    "measured": max_run,
                    "expected": cap,
                },
            )
        else:
            g2.add("pass")
    gates["G2"] = g2.result()
    if mode_mismatch:
        gates["G2"]["reason"] = "analyzer_mode_mismatch"

    # ---- G3 表記 ----
    g3_cfg = gates_cfg.get("G3_orthography", {})
    g3 = GateResult(span_all)
    min_chars = g3_cfg.get("min_chars")
    if min_chars and record["n_chars"] < min_chars:
        g3.degrade(f"insufficient_chars({record['n_chars']}<{min_chars})")
    for script in ("kanji", "hiragana", "katakana", "latin"):
        g3.add_range_check(
            f"script_ratio.{script}",
            record["script_ratio"][script] if record["n_chars"] else None,
            g3_cfg.get(f"{script}_ratio"),
            hard=g3_cfg.get(f"{script}_hard_range"),
        )
    gates["G3"] = g3.result()

    # ---- G4 語彙(POS 依存。fallback では skipped) ----
    g4_cfg = gates_cfg.get("G4_vocabulary", {})
    g4 = GateResult(span_all)
    min_tokens = g4_cfg.get("min_tokens")
    if min_tokens and mode == "sudachi" and n_tokens < min_tokens:
        g4.degrade(f"insufficient_tokens({n_tokens}<{min_tokens})")
    if mode == "sudachi" and not mode_mismatch:
        for cfg_key, rec_key in (
            ("ttr_window_min", "ttr_window"),
            ("distinct_2_min", "distinct_2"),
        ):
            floor = g4_cfg.get(cfg_key)
            measured = record.get(rec_key)
            if floor is None or measured is None:
                continue
            # floor(p05 較正)未満は warn、hard 下限(著者極値ポリシー)未満で fail。値 == hard は fail ではない
            hard = g4_cfg.get(cfg_key.replace("_min", "_hard_min"))
            if hard is None:
                hard = floor * 0.9
            if measured >= floor:
                g4.add("pass")
            else:
                g4.add(
                    "fail" if measured < hard else "warn",
                    {
                        "span": span_all,
                        "message": f"{rec_key} が下限未満(語彙の多様性不足)",
                        "measured": measured,
                        "expected": floor,
                        "hard_min": hard,
                    },
                )
        g4.add_range_check(
            "func_word_rate",
            record.get("func_word_rate"),
            g4_cfg.get("func_word_rate_range"),
            hard=g4_cfg.get("func_word_rate_hard_range"),
        )
    gates["G4"] = g4.result()
    if gates["G4"]["status"] == "skipped":
        gates["G4"]["reason"] = (
            "analyzer_fallback(pos_unavailable)" if mode != "sudachi"
            else ("analyzer_mode_mismatch" if mode_mismatch else "no_threshold")
        )

    # ---- G5 カリカチュア ----
    gates["G5"] = _gate5(text, gates_cfg.get("G5_caricature", {}), span_all)

    # ---- G6 コピー(--source-corpus 指定時のみ) ----
    if args.source_corpus is None:
        gates["G6"] = {
            "status": "skipped",
            "findings": [],
            "reason": "source_corpus_not_given",
            "note": "--source-corpus 未指定。コピー検査は評価していない(pass ではない)",
        }
    elif not args.source_corpus.is_dir():
        print(
            f"error: source-corpus がディレクトリではありません: {args.source_corpus}",
            file=sys.stderr,
        )
        return 1
    else:
        gates["G6"] = _gate6(
            text, gates_cfg.get("G6_copy", {}), args.source_corpus, args.seed
        )

    # ---- G7 形態素チャネル ----
    g7_cfg = gates_cfg.get("G7_morphology", {})
    ref, reason = _load_morph_reference(args.config, g7_cfg)
    length_stratum = _length_stratum(record["n_chars"], config.get("length_strata", {}))
    slice_keys = [f"register:{_register_of(record)}", f"length:{length_stratum}"]
    if args.era:
        slice_keys.append(f"era:{args.era}")
    gates["G7"] = _gate7(record, sentences, ref, reason, mode, slice_keys, span_all, mode_mismatch=mode_mismatch)

    gates["G7"]["analyzer_compat"] = compat

    out = {
        "gates": gates,
        "length_stratum": length_stratum,
        "register": _register_of(record),
        "analyzer": record["analyzer"],
        "analyzer_compat": compat,
        "feature_schema": record.get("feature_schema"),
        "config_feature_schema": (config.get("calibration") or {}).get("feature_schema"),
        "text_chars": record["n_chars"],
        "n_sents": record["n_sents"],
        "prose": record["prose"],
        "composite_score": None,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if any(g["status"] == "fail" for g in gates.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
