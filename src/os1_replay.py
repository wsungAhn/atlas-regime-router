"""Cap-applied OS-1 replay analysis."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from friction import parse_occ  # noqa: E402
from friction_backtest import load_live_episodes  # noqa: E402
from friction_calibrate import data_period, load_inputs, to_jsonable  # noqa: E402
from friction_compare import _episode_key, run_variants  # noqa: E402
from friction_stats import bootstrap_ci, paired_diff  # noqa: E402
from os1_sizing import size_os1  # noqa: E402


ETF9 = ("SPY", "QQQ", "GLD", "TLT", "SLV", "IWM", "XLE", "XLF", "DIA")
SKIP_KEYS = ("symbol_open", "count_cap", "risk_cap", "bad_credit", "low_credit_ratio")


def episode_structure(episode):
    parsed = [parse_occ(leg) for leg in episode["legs"]]
    types = {item[2] for item in parsed}
    puts = [item for item in parsed if item[2] == "put"]
    calls = [item for item in parsed if item[2] == "call"]
    if types == {"put"}:
        side = "bull_put"
    elif types == {"call"}:
        side = "bear_call"
    elif types == {"put", "call"}:
        side = "condor"
    else:
        raise ValueError("episode must contain put and/or call legs")
    widths = tuple(max(item[3] for item in group) - min(item[3] for item in group)
                   for group in (puts, calls) if group)
    if side == "condor":
        expiries_match = len({item[1] for item in parsed}) == 1
        non_overlapping = max(item[3] for item in puts) < min(item[3] for item in calls)
        verified = expiries_match and non_overlapping and len(parsed) == 4
        widths = (widths[0], widths[1])
    else:
        verified = False
        widths = (widths[0],)
    return {"side": side, "widths": widths, "credit": episode["limit_credit"], "verified": verified}


def unit_risk(structure):
    widths = structure["widths"]
    if structure["side"] == "condor":
        width = max(widths) if structure["verified"] else sum(widths)
    else:
        width = widths[0]
    return (width - float(structure["credit"])) * 100.0


def cap_replay(rows, episodes, equity, *, max_open_positions=5):
    episode_by_key = {_episode_key(episode): episode for episode in episodes}
    candidates = sorted(enumerate(rows), key=lambda item: (_episode_key(item[1])[1], item[0]))
    accepted = []
    skips = Counter({key: 0 for key in SKIP_KEYS})
    exposures = []
    for _, row in candidates:
        key = _episode_key(row)
        if key not in episode_by_key:
            raise ValueError(f"no episode for row key: {key!r}")
        entry = episode_by_key[key]
        entry_ts = key[1]
        open_now = [item for item in accepted if pd.Timestamp(item["exit_ts"]) > entry_ts]
        if any(item["symbol"] == row["symbol"] for item in open_now):
            skips["symbol_open"] += 1
            continue
        structure = episode_structure(entry)
        qty_new, reason = size_os1(equity, equity, structure["widths"] if structure["side"] == "condor" else structure["widths"][0],
                                   structure["credit"], structure["side"],
                                   [{"L": item["L"], "side": item["side"]} for item in open_now],
                                   max_open_positions, structure["verified"])
        if qty_new == 0:
            if reason not in skips:
                raise ValueError(f"unexpected sizing reason: {reason}")
            skips[reason] += 1
            continue
        factor = qty_new / entry["qty"]
        item = {"symbol": row["symbol"], "exit_ts": row["exit_ts"], "side": structure["side"],
                "L": unit_risk(structure) * qty_new, "pnl": float(row["realized_pnl"]) * factor}
        accepted.append(item)
        open_after = [position for position in accepted if pd.Timestamp(position["exit_ts"]) > entry_ts]
        total_l = sum(position["L"] for position in open_after)
        exposures.append((total_l / equity, len(open_after)))
    pnls = [item["pnl"] for item in accepted]
    return {"n_candidates": len(rows), "n_accepted": len(accepted), "skip_counts": dict(skips),
            "uncapped_total_pnl": float(sum(float(row["realized_pnl"]) for row in rows)),
            "capped_total_pnl": float(sum(pnls)), "capped_mean_pnl": float(np.mean(pnls)) if pnls else None,
            "capped_worst_pnl": float(min(pnls)) if pnls else None,
            "max_concurrent_L_over_equity": max((value[0] for value in exposures), default=0.0),
            "max_concurrent_count": max((value[1] for value in exposures), default=0),
            "median_concurrent_L_over_equity": float(np.median([value[0] for value in exposures])) if exposures else None}


def _common_variant_rows(rows_by_variant):
    key_sets = [{_episode_key(row) for row in value["rows"]} for value in rows_by_variant.values()]
    common = set.intersection(*key_sets) if key_sets else set()
    return {variant: {_episode_key(row): row for row in value["rows"] if _episode_key(row) in common}
            for variant, value in rows_by_variant.items()}, common


def subset_stats(rows_by_variant, episodes, *, alpha, n_resamples, seed):
    indexed, common = _common_variant_rows(rows_by_variant)
    ordered = sorted(common, key=lambda key: (str(key[0]), str(key[1])))
    def pair(keys, left="X2", right="X0"):
        values = paired_diff([indexed[left][key]["realized_pnl"] for key in keys],
                             [indexed[right][key]["realized_pnl"] for key in keys])
        return {"n": len(values), "mean": float(np.mean(values)) if len(values) else None,
                "ci": bootstrap_ci(values, alpha=alpha, n_resamples=n_resamples, seed=seed) if len(values) else (None, None)}
    etf_keys = [key for key in ordered if key[0] in ETF9]
    episode_by_key = {_episode_key(episode): episode for episode in episodes}
    ratio_keys = []
    for key in ordered:
        structure = episode_structure(episode_by_key[key])
        if structure["credit"] / max(structure["widths"]) >= 0.10:
            ratio_keys.append(key)
    x2_values = [indexed["X2"][key]["realized_pnl"] for key in ratio_keys]
    x2_vs = {}
    for variant in ("A1", "A2", "A5", "C2", "C3", "C5"):
        if variant in indexed:
            x2_vs[variant] = pair(ordered, left=variant, right="X2")
    return {"etf9": pair(etf_keys), "ratio_ge_010": {"n": len(ratio_keys), "x2_mean_pnl": float(np.mean(x2_values)) if x2_values else None}, "x2_vs": x2_vs}


def _fmt(value):
    if value is None:
        return "n/a"
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.4f}"
    return str(value)


def render_report(result, meta):
    lines = [f"# OS-1 replay {meta['date']}", "", "## Interpretation limits", "",
             "시뮬레이터 보정 등급은 FAIL(T-d). 이 분석은 탐색용·미보정이며 절대 달러 수준은 신뢰하지 않는다.",
             "만기 정산 교차 비용을 0으로 둔 모델이므로 X2 우위는 과대평가일 수 있다.",
             "캡 적용은 `e_now = equity`(마크 미반영)로 근사하고, 손익은 qty에 선형(`pnl × qty_new/qty_orig`)이라고 가정한다.",
             "표본: 라이브 진입 119개(2026-08-25~10-06 단일 레짐), 부분집합은 더 작다. 이것은 승격 근거가 아니다."]
    for scenario, title in (("central", "Cap-applied X2 (central)"), ("pessimistic", "Cap-applied X2 (pessimistic)")):
        cap = result[scenario]["cap"]
        lines += ["", f"## {title}", "", "| metric | value |", "|---|---:|"]
        for key in ("n_candidates", "n_accepted", "uncapped_total_pnl", "capped_total_pnl", "capped_mean_pnl", "capped_worst_pnl", "max_concurrent_L_over_equity", "max_concurrent_count", "median_concurrent_L_over_equity"):
            lines.append(f"| {key} | {_fmt(cap[key])} |")
        lines += ["", "| skip reason | count |", "|---|---:|"]
        for key in SKIP_KEYS:
            lines.append(f"| {key} | {cap['skip_counts'].get(key, 0)} |")
    lines += ["", "## Subsets", "", "| scenario | subset | n | mean | ci |", "|---|---|---:|---:|---|"]
    for scenario in ("central", "pessimistic"):
        subsets = result[scenario]["subsets"]
        for name in ("etf9", "ratio_ge_010"):
            item = subsets[name]
            mean = item.get("mean", item.get("x2_mean_pnl"))
            ci = f"{_fmt(item['ci'][0])}, {_fmt(item['ci'][1])}" if "ci" in item else "n/a"
            lines.append(f"| {scenario} | {name} | {item['n']} | {_fmt(mean)} | {ci} |")
    lines += ["", "## X2 vs stops", "", "| scenario | variant | n | mean | ci |", "|---|---|---:|---:|---|"]
    for scenario in ("central", "pessimistic"):
        for variant, item in result[scenario]["subsets"]["x2_vs"].items():
            lines.append(f"| {scenario} | {variant} | {item['n']} | {_fmt(item['mean'])} | {_fmt(item['ci'][0])}, {_fmt(item['ci'][1])} |")
    lines += ["", "## Inputs", ""]
    for key in sorted(meta):
        value = meta[key]
        if isinstance(value, (tuple, list)):
            value = " – ".join(map(str, value))
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def _atomic_write(path, content, *, json_value=False):
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        if json_value:
            Path(temporary).write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            Path(temporary).write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser()
    for name in ("decisions", "table", "bars-dir", "calibration-json"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--out-dir", default="reports/friction")
    parser.add_argument("--date", required=True)
    parser.add_argument("--r", required=True, type=float)
    parser.add_argument("--equity", required=True, type=float)
    parser.add_argument("--s", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument("--resamples", type=int, default=2000)
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    md_path, json_path = out_dir / f"os1-replay-{args.date}.md", out_dir / f"os1-replay-{args.date}.json"
    if md_path.exists() or json_path.exists():
        raise FileExistsError("OS-1 replay output already exists")
    with open(args.calibration_json, encoding="utf-8") as handle:
        calibration = json.load(handle)
    scenarios = {"central": {"beta": calibration["fit"]["beta"], "h_mult": calibration["fit"]["h_mult"]},
                 "pessimistic": {"beta": calibration["fit_pessimistic"]["beta"], "h_mult": calibration["fit_pessimistic"]["h_mult"]}}
    episodes, exclusion_counts = load_live_episodes(args.decisions)
    table = pd.read_csv(args.table)
    bars_by_symbol, mtm_iv_by_symbol, missing_symbols = load_inputs(episodes, args.bars_dir)
    variants = run_variants(episodes, bars_by_symbol, mtm_iv_by_symbol, table, scenarios=scenarios, r=args.r, s=args.s, equity=args.equity)
    result = {}
    for scenario, rows_by_variant in variants.items():
        result[scenario] = {"cap": cap_replay(rows_by_variant["X2"]["rows"], episodes, args.equity),
                            "subsets": subset_stats(rows_by_variant, episodes, alpha=0.10, n_resamples=args.resamples, seed=args.seed)}
    meta = {"date": args.date, "r": args.r, "equity": args.equity, "s": args.s, "seed": args.seed,
            "resamples": args.resamples, "data_period": data_period(bars_by_symbol) if bars_by_symbol else None,
            "missing_symbols": missing_symbols, "exclusion_counts": exclusion_counts, "n_episodes_total": len(episodes)}
    report = render_report(result, meta)
    serializable = to_jsonable({"result": result, "meta": meta})
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        _atomic_write(md_path, report)
        written.append(md_path)
        _atomic_write(json_path, serializable, json_value=True)
        written.append(json_path)
    except Exception:
        for path in written:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
