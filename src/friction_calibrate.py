"""Command-line input loader and markdown renderer for friction calibration."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest import _rolling_iv_series  # noqa: E402
from friction_backtest import load_live_episodes  # noqa: E402
from friction_calibration import DEFAULT_RESAMPLES, DEFAULT_SEED, prepare_bars  # noqa: E402
from friction_validation import calibrate  # noqa: E402


LOGGER = logging.getLogger(__name__)

LIMITATIONS = (
    "validation n 작음/검정력 약함",
    "단일 레짐·train/validation 레짐 차이",
    "limit 체결 가정 s 밴드",
    "페이퍼 체결",
    "clean이 최악 마찰을 과소대표",
    "호가 피드 indicative 여부 미검증",
    "이 보고서의 달러 손익은 미보정",
)


def load_inputs(episodes, bars_dir):
    """Load and prepare both required datasets for every episode symbol."""
    directory = Path(bars_dir)
    symbols = list(dict.fromkeys(episode["symbol"] for episode in episodes))
    bars_by_symbol, mtm_iv_by_symbol, missing = {}, {}, []
    for symbol in symbols:
        intraday_path = directory / f"{symbol}_15min.parquet"
        daily_path = directory / f"{symbol}_daily.parquet"
        if not intraday_path.is_file() or not daily_path.is_file():
            missing.append(symbol)
            continue
        try:
            bars = pd.read_parquet(intraday_path)
            daily = pd.read_parquet(daily_path)
        except Exception:
            LOGGER.exception("Could not read calibration bars for %s", symbol)
            raise
        try:
            bars_by_symbol[symbol] = prepare_bars({symbol: bars})[symbol]
            mtm_iv_by_symbol[symbol] = _rolling_iv_series(daily)
        except Exception:
            LOGGER.exception("Could not prepare calibration bars for %s", symbol)
            raise
    return bars_by_symbol, mtm_iv_by_symbol, sorted(missing)


def data_period(bars_by_symbol):
    """Return the inclusive overall first and last bar timestamps."""
    if not bars_by_symbol:
        raise ValueError("bars_by_symbol must not be empty")
    starts = [bars.index.min() for bars in bars_by_symbol.values()]
    ends = [bars.index.max() for bars in bars_by_symbol.values()]
    return min(starts), max(ends)


def _value(obj, *keys, default=None):
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            return obj[key]
    return default


def _fmt(value):
    if value is None:
        return "n/a"
    if isinstance(value, (float, np.floating)):
        if not np.isfinite(value):
            return "n/a"
        return f"{float(value):.4f}"
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return str(int(value))
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (pd.Timestamp,)):  # deterministic and readable
        return value.isoformat()
    return str(value)


def _fmt_ci(value):
    if value is None:
        return "n/a"
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return f"({_fmt(value[0])}, {_fmt(value[1])})"
    return _fmt(value)


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _stats_text(stats):
    stats = stats or {}
    return "; ".join(f"{key}={_fmt(stats.get(key))}"
                      for key in ("t_a", "t_b", "t_c_i", "t_c_ii"))


def _stats(result, name):
    return _value(result, name, default={}) or {}


def render_report(result, meta):
    """Render a deterministic calibration report from the calibration result."""
    grade = _stats(result, "grade")
    fit = _stats(result, "fit")
    pessimistic = _stats(result, "fit_pessimistic")
    validation = _stats(result, "validation")
    controls = _stats(result, "controls")
    sensitivity = _stats(result, "sensitivity")
    low_credit = _stats(result, "low_credit_sensitivity")
    sim_stats = _value(validation, "sim_stats", default={}) or {}
    judgement = _value(validation, "judgement", default={}) or {}
    targets = _value(judgement, "targets", default={}) or {}
    period = meta.get("data_period")
    period_text = "n/a" if period is None else f"{_fmt(period[0])} – {_fmt(period[1])}"
    reasons = _value(grade, "reasons", default=[]) or []
    loss = _value(fit, "loss_by_beta", default={}) or {}
    residuals = _value(fit, "residuals_by_regime", default={}) or {}

    lines = [f"# Friction calibration {meta.get('date', 'n/a')}", "", "## Summary", "",
             f"- Grade: {_fmt(_value(grade, 'grade'))}",
             f"- Probation stop %: {_fmt(_value(grade, 'probation_stop_pct'))}",
             f"- n_train / n_validation: {_fmt(result.get('n_train'))} / {_fmt(result.get('n_validation'))}",
             f"- Boundary: {_fmt(result.get('boundary_ts'))}", f"- Data period: {period_text}",
             "- Reasons:"]
    if reasons:
        lines.extend(f"  - {reason}" for reason in reasons)
    else:
        lines.append("  - n/a")
    lines += ["", "## Fit", "", f"- Selected β: {_fmt(_value(fit, 'beta'))}",
              f"- Extended / model insufficient: {_fmt(_value(fit, 'extended'))} / {_fmt(_value(fit, 'model_insufficient'))}",
              f"- β (median / pessimistic): {_fmt(_value(fit, 'beta'))} / {_fmt(_value(pessimistic, 'beta'))}",
              f"- β·h (median / pessimistic): {_fmt(_value(fit, 'beta') * _value(fit, 'h_mult'))} / "
              f"{_fmt(_value(pessimistic, 'beta') * _value(pessimistic, 'h_mult'))}",
              "", _table(["β", "loss"], [[_fmt(beta), _fmt(value)] for beta, value in sorted(loss.items(), key=lambda item: item[0])]),
              "", f"- Train skips: {_fmt(_value(fit, 'skip_counts'))}",
              "", _table(["stat", "live", "sim"], [[key, _fmt(_value(_value(fit, 'live_stats', default={}), key)), _fmt(_value(_value(fit, 'sim_stats', default={}), key))]
                                                       for key in ("t_a", "t_b", "t_c_i", "t_c_ii")]),
              "", _table(["regime", "sim t_a", "live t_a", "n"],
                          [[regime, _fmt(_value(values, 'sim_t_a')), _fmt(_value(values, 'live_t_a')), _fmt(_value(values, 'n'))]
                           for regime, values in sorted(residuals.items(), key=lambda item: str(item[0]))]),
              "", "## Validation", "",
              _table(["target", "sim", "CI", "pass"],
                     [[key, _fmt(_value(sim_stats, key)), _fmt_ci(_value(_value(targets, key, default={}), 'ci')),
                       _fmt(_value(_value(targets, key, default={}), 'passed'))]
                      for key in ("t_a", "t_b", "t_c_i", "t_c_ii")] +
                     [["t_d (trend_up)", _fmt(_value(_value(sim_stats, 't_d', default={}), 'trend_up')),
                       _fmt_ci(_value(_value(targets, 't_d', default={}), 'ci')),
                       _fmt(_value(_value(targets, 't_d', default={}), 'passed'))]]),
              f"", f"- Overall pass: {_fmt(_value(judgement, 'passed'))}", "", "## Controls", "",
              _table(["control", "sim stats", "CI", "outside", "pass"],
                     [["A", _stats_text(_value(_value(controls, 'A', default={}), 'sim_stats')),
                       _fmt_ci((_value(_value(controls, 'ci', default={}), 't_a'),
                                _value(_value(controls, 'ci', default={}), 't_c_i'))),
                       f"t_a={_fmt(_value(_value(controls, 'A', default={}), 'outside_t_a'))}; "
                       f"t_c_i={_fmt(_value(_value(controls, 'A', default={}), 'outside_t_c_i'))}",
                       _fmt(_value(_value(controls, 'A', default={}), 'passed'))],
                      ["C", _stats_text(_value(_value(controls, 'C', default={}), 'sim_stats')),
                       _fmt_ci(_value(_value(controls, 'ci', default={}), 't_c_ii')),
                       f"t_c_ii={_fmt(_value(_value(controls, 'C', default={}), 'outside_t_c_ii'))}",
                       _fmt(_value(_value(controls, 'C', default={}), 'passed'))]]),
              f"- C k: {_fmt(_value(_value(controls, 'C', default={}), 'k'))}", "", "## Sensitivity", "",
              _table(["target", "sim", "CI", "pass"],
                     [[key, _fmt(_value(value, 'sim')), _fmt_ci(_value(value, 'ci')), _fmt(_value(value, 'in_ci'))]
                      for key, value in (("t_a", _value(sensitivity, 't_a', default={})),
                                         ("t_c_i", _value(sensitivity, 't_c_i', default={})))]) ,
              f"", f"- Sensitivity n: {_fmt(_value(sensitivity, 'n'))}",
              f"- Low-credit t_b sim / live: {_fmt(_value(low_credit, 'sim_t_b'))} / {_fmt(_value(low_credit, 'live_t_b'))}",
              f"- Low-credit excluded n: {_fmt(_value(low_credit, 'n_excluded'))}",
              "", "## Limitations", ""]
    lines.extend(f"- {item}" for item in LIMITATIONS)
    input_rows = []
    for key, value in meta.items():
        if key == "data_period" and value is not None:
            value = f"{_fmt(value[0])} – {_fmt(value[1])}"
        input_rows.append([key, _fmt(value)])
    lines += ["", "## Inputs", "", _table(["key", "value"], input_rows)]
    return "\n".join(lines) + "\n"


def to_jsonable(obj):
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, pd.Timedelta):
        return str(obj)
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, np.generic):
        return to_jsonable(obj.item())
    if isinstance(obj, np.ndarray):
        return [to_jsonable(value) for value in obj.tolist()]
    if isinstance(obj, dict):
        return {str(key): to_jsonable(value) for key, value in obj.items() if callable(value) is False}
    if isinstance(obj, (list, tuple, set)):
        values = obj if not isinstance(obj, set) else sorted(obj, key=str)
        return [to_jsonable(value) for value in values if callable(value) is False]
    if callable(obj):
        return None
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--bars-dir", required=True)
    parser.add_argument("--out-dir", default="reports/friction")
    parser.add_argument("--date", required=True)
    parser.add_argument("--r", required=True, type=float)
    parser.add_argument("--s", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    args = parser.parse_args(argv)
    episodes, exclusion_counts = load_live_episodes(args.decisions)
    bars, mtm_iv, missing = load_inputs(episodes, args.bars_dir)
    try:
        table = pd.read_csv(args.table)
    except Exception:
        LOGGER.exception("Could not read friction table %s", args.table)
        raise
    output = Path(args.out_dir)
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / f"calibration-{args.date}.md"
    json_path = output / f"calibration-{args.date}.json"
    if report_path.exists() or json_path.exists():
        raise FileExistsError(f"calibration output already exists for {args.date}")
    try:
        result = calibrate(episodes, bars, mtm_iv, table, r=args.r, s=args.s,
                           seed=args.seed, n_resamples=args.resamples)
    except ValueError:
        LOGGER.exception("Calibration failed")
        return 1
    period = data_period(bars) if bars else None
    meta = {"date": args.date, "n_episodes_total": len(episodes), "exclusion_counts": exclusion_counts,
            "missing_symbols": missing, "data_period": period, "r": args.r, "s": args.s,
            "seed": args.seed, "n_resamples": args.resamples}
    report_text = render_report(result, meta)
    written = []
    try:
        report_path.write_text(report_text, encoding="utf-8")
        written.append(report_path)
        json_text = json.dumps(to_jsonable(result), indent=2, sort_keys=True) + "\n"
        json_path.write_text(json_text, encoding="utf-8")
        written.append(json_path)
    except Exception:
        for path in written:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        LOGGER.exception("Could not write calibration outputs")
        raise
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
