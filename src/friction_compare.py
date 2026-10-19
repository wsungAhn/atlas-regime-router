"""Compare fixed replay exit policies under calibrated friction scenarios."""

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
from friction_calibration import replay  # noqa: E402
from friction_calibrate import data_period, load_inputs, to_jsonable  # noqa: E402
from friction_stats import bootstrap_ci, paired_diff  # noqa: E402


VARIANTS = {
    "X0": {"policy": "V0", "pt": 0.5, "sl": -1.0},
    "X1": {"policy": "V0", "pt": None, "sl": -1.0},
    "X2": {"policy": "V3", "pt": None, "sl": None},
    "A1": {"policy": "V5", "pt": None, "sl": None, "stop_account_pct": 0.01},
    "A2": {"policy": "V5", "pt": None, "sl": None, "stop_account_pct": 0.02},
    "A5": {"policy": "V5", "pt": None, "sl": None, "stop_account_pct": 0.05},
    "C2": {"policy": "V6", "pt": None, "sl": None, "stop_underlying_pct": 0.02},
    "C3": {"policy": "V6", "pt": None, "sl": None, "stop_underlying_pct": 0.03},
    "C5": {"policy": "V6", "pt": None, "sl": None, "stop_underlying_pct": 0.05},
}


def _episode_key(row):
    timestamp = pd.Timestamp(row.get("entry_ts"))
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return row.get("symbol"), timestamp


def _width(episode):
    strikes = {}
    for occ in episode["legs"]:
        _, _, option_type, strike = parse_occ(occ)
        strikes.setdefault(option_type, []).append(strike)
    return max((max(values) - min(values) for values in strikes.values()), default=0.0)


def _binding_metadata(episodes, equity):
    result = {}
    for episode in episodes:
        key = _episode_key(episode)
        result[key] = {"max_loss_over_equity": (_width(episode) - episode["limit_credit"]) * 100.0 * episode["qty"] / equity}
    return result


def run_variants(episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, scenarios, r, s, equity):
    output = {}
    binding = _binding_metadata(episodes, equity)
    for scenario, calibration in scenarios.items():
        output[scenario] = {}
        for variant_id, spec in VARIANTS.items():
            kwargs = {"policy": spec["policy"], "pt": spec["pt"], "sl": spec["sl"], "s": s}
            if spec["policy"] == "V5":
                kwargs.update(account_equity=equity, stop_account_pct=spec["stop_account_pct"])
            elif spec["policy"] == "V6":
                kwargs["stop_underlying_pct"] = spec["stop_underlying_pct"]
            rows, skip_counts = replay(episodes, bars_by_symbol, mtm_iv_by_symbol, table,
                                       beta=calibration["beta"], h_mult=calibration["h_mult"], r=r, **kwargs)
            for row in rows:
                key = _episode_key(row)
                row.update(binding.get(key, {}))
            output[scenario][variant_id] = {"rows": rows, "skip_counts": skip_counts}
    return output


def _common_rows(results_by_variant):
    keys_by_variant = {variant: {_episode_key(row) for row in value["rows"]}
                       for variant, value in results_by_variant.items()}
    common = set.intersection(*keys_by_variant.values()) if keys_by_variant else set()
    dropped = len(set.union(*keys_by_variant.values()) - common) if keys_by_variant else 0
    indexed = {}
    for variant, value in results_by_variant.items():
        indexed[variant] = {_episode_key(row): row for row in value["rows"] if _episode_key(row) in common}
    return common, dropped, indexed


def _mean(values):
    return float(np.mean(values)) if values else None


def summarize(results_by_variant, baseline_id="X0", *, alpha=0.10, n_resamples=2000, seed=20260930):
    common, dropped, indexed = _common_rows(results_by_variant)
    ordered_keys = sorted(common, key=lambda key: (str(key[0]), str(key[1])))
    baseline = [indexed[baseline_id][key]["realized_pnl"] for key in ordered_keys]
    variants = {}
    for variant_id in results_by_variant:
        rows = [indexed[variant_id][key] for key in ordered_keys]
        pnl = [row["realized_pnl"] for row in rows]
        diffs = paired_diff(pnl, baseline)
        if variant_id == baseline_id:
            ci = (0.0, 0.0)
        elif len(diffs):
            ci = bootstrap_ci(diffs, alpha=alpha, n_resamples=n_resamples, seed=seed)
        else:
            ci = (None, None)
        regimes = {}
        for regime in sorted({row.get("regime") for row in rows}, key=str):
            values = [row["realized_pnl"] for row in rows if row.get("regime") == regime]
            regimes[regime] = {"mean_pnl": _mean(values), "n": len(values)}
        stop_rows = [row for row in rows if row.get("exit_reason") == "stop_loss"]
        item = {
            "n": len(rows), "mean_pnl": _mean(pnl), "total_pnl": float(np.sum(pnl)) if pnl else 0.0,
            "win_rate": sum(value > 0 for value in pnl) / len(pnl) if pnl else None,
            "worst_pnl": min(pnl) if pnl else None,
            "exit_reason_counts": dict(sorted(Counter(row.get("exit_reason") for row in rows).items(), key=lambda x: str(x[0]))),
            "median_minutes_held": float(np.median([row["minutes_held"] for row in rows])) if rows else None,
            "paired": {"mean": float(np.mean(diffs)) if len(diffs) else 0.0, "ci": ci},
            "by_regime": regimes,
            "stop_rate": len(stop_rows) / len(rows) if rows else None,
        }
        if variant_id.startswith("A"):
            losses = [row["max_loss_over_equity"] for row in rows if "max_loss_over_equity" in row]
            threshold = VARIANTS[variant_id]["stop_account_pct"]
            item["binding"] = {"max": max(losses) if losses else None,
                                "median": float(np.median(losses)) if losses else None,
                                "structurally_untriggerable_rate": sum(value < threshold for value in losses) / len(losses) if losses else None}
        variants[variant_id] = item
    return {"n_common": len(common), "n_dropped": dropped, "variants": variants}


def _fmt(value):
    if value is None:
        return "n/a"
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.4f}"
    return str(value)


def render_report(summary, meta):
    date = meta["date"]
    lines = [f"# Friction compare {date}", "", "## Interpretation limits", "", "The calibration grade is FAIL because the simulator is optimistic on the trend-up win rate.", "This exploratory comparison is interpretable only as a paired relative comparison between policies.", "Absolute dollar profit levels are uncalibrated model output and should not be trusted.", "This result is not evidence for live promotion; promotion requires forward-paper probation.", "The sample covers 103+16 fixed live entries from 2026-08-25 to 2026-10-06 in one regime.", "", "## Variants", "", "| id | policy | pt | sl | policy argument |", "|---|---|---:|---:|---|"]
    for variant_id, spec in VARIANTS.items():
        if "stop_account_pct" in spec:
            arg = f"stop_account_pct={spec['stop_account_pct']}"
        elif "stop_underlying_pct" in spec:
            arg = f"stop_underlying_pct={spec['stop_underlying_pct']}"
        else:
            arg = "—"
        lines.append(f"| {variant_id} | {spec['policy']} | {_fmt(spec['pt'])} | {_fmt(spec['sl'])} | {arg} |")
    for scenario in ("central", "pessimistic"):
        lines += ["", f"## Results ({scenario})", "", "| id | n | mean | total | win | worst | paired mean | paired CI | stop_rate | expiry n |", "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|"]
        for variant_id, item in summary[scenario]["variants"].items():
            lines.append(f"| {variant_id} | {item['n']} | {_fmt(item['mean_pnl'])} | {_fmt(item['total_pnl'])} | {_fmt(item['win_rate'])} | {_fmt(item['worst_pnl'])} | {_fmt(item['paired']['mean'])} | {_fmt(item['paired']['ci'][0])}, {_fmt(item['paired']['ci'][1])} | {_fmt(item['stop_rate'])} | {item['exit_reason_counts'].get('expiry', 0)} |")
    lines += ["", "## By regime", "", "| scenario | variant | regime | n | mean_pnl |", "|---|---|---|---:|---:|"]
    for scenario in ("central", "pessimistic"):
        for variant_id, item in summary[scenario]["variants"].items():
            for regime, values in item["by_regime"].items():
                lines.append(f"| {scenario} | {variant_id} | {regime} | {values['n']} | {_fmt(values['mean_pnl'])} |")
    lines += ["", "## Stop-rule binding", "", "| scenario | variant | structurally untriggerable | max loss/equity | median loss/equity |", "|---|---|---:|---:|---:|"]
    for scenario in ("central", "pessimistic"):
        for variant_id, item in summary[scenario]["variants"].items():
            if "binding" in item:
                b = item["binding"]
                lines.append(f"| {scenario} | {variant_id} | {_fmt(b['structurally_untriggerable_rate'])} | {_fmt(b['max'])} | {_fmt(b['median'])} |")
    lines += ["", "## Inputs", ""]
    for key in sorted(meta):
        value = meta[key]
        if key == "data_period" and isinstance(value, (tuple, list)):
            value = " – ".join(map(str, value))
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def _atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--bars-dir", required=True)
    parser.add_argument("--calibration-json", required=True)
    parser.add_argument("--out-dir", default="reports/friction")
    parser.add_argument("--date", required=True)
    parser.add_argument("--r", required=True, type=float)
    parser.add_argument("--equity", required=True, type=float)
    parser.add_argument("--s", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument("--resamples", type=int, default=2000)
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    md_path, json_path = out_dir / f"compare-{args.date}.md", out_dir / f"compare-{args.date}.json"
    if md_path.exists() or json_path.exists():
        raise FileExistsError("comparison output already exists")
    try:
        with open(args.calibration_json, encoding="utf-8") as handle:
            calibration = json.load(handle)
        scenarios = {"central": {"beta": calibration["fit"]["beta"], "h_mult": calibration["fit"]["h_mult"]},
                     "pessimistic": {"beta": calibration["fit_pessimistic"]["beta"], "h_mult": calibration["fit_pessimistic"]["h_mult"]}}
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        logging.exception("Could not load calibration scenarios")
        raise
    episodes, exclusion_counts = load_live_episodes(args.decisions)
    table = pd.read_csv(args.table)
    bars_by_symbol, mtm_iv_by_symbol, missing_symbols = load_inputs(episodes, args.bars_dir)
    results = run_variants(episodes, bars_by_symbol, mtm_iv_by_symbol, table,
                           scenarios=scenarios, r=args.r, s=args.s, equity=args.equity)
    summary = {scenario: summarize(values, alpha=0.10, n_resamples=args.resamples, seed=args.seed)
               for scenario, values in results.items()}
    meta = {"date": args.date, "r": args.r, "equity": args.equity, "s": args.s,
            "seed": args.seed, "resamples": args.resamples,
            "data_period": data_period(bars_by_symbol) if bars_by_symbol else None,
            "missing_symbols": missing_symbols, "exclusion_counts": exclusion_counts,
            "n_episodes_total": len(episodes),
            "n_common": {scenario: value["n_common"] for scenario, value in summary.items()},
            "n_dropped": {scenario: value["n_dropped"] for scenario, value in summary.items()}}
    report = render_report(summary, meta)
    serializable = json.dumps(to_jsonable({"summary": summary, "meta": meta}), indent=2, sort_keys=True) + "\n"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{md_path.name}.", dir=md_path.parent)
        os.close(fd)
        try:
            Path(temporary).write_text(report, encoding="utf-8")
            os.replace(temporary, md_path)
            written.append(md_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _atomic_json(json_path, json.loads(serializable))
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
