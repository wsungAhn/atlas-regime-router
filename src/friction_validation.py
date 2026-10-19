"""Validation, negative controls, and grading for friction calibration."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from friction_calibration import (  # noqa: E402
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    fit_beta,
    live_rows,
    replay,
    sim_rows,
    split_train_validation,
    target_stats,
)


CI_LEVEL = 0.90
K_GRID = tuple(0.25 * i for i in range(13))
TREND_REGIME = "trend_up"
LOW_CREDIT = 0.02
PROBATION_STOP = -0.10
PROBATION_STOP_CONDITIONAL = -0.05


def _percentile(values, level):
    return (float(np.percentile(values, 100.0 * (1.0 - level) / 2.0)),
            float(np.percentile(values, 100.0 * (1.0 + level) / 2.0)))


def bootstrap_ci(rows, *, level=CI_LEVEL, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED):
    if not rows:
        raise ValueError("rows must not be empty")
    rng = np.random.default_rng(seed)
    samples = {key: [] for key in ("t_a", "t_b", "t_c_i", "t_c_ii")}
    sl_rows = [row for row in rows if row["exit_reason"] == "stop_loss"]
    pt_rows = [row for row in rows if row["exit_reason"] == "profit_target"]
    t_d_rows = [row for row in rows if row.get("regime") == TREND_REGIME]
    if not t_d_rows:
        raise ValueError("trend_up rows must not be empty")
    t_d_samples = []
    for _ in range(n_resamples):
        indices = rng.integers(0, len(rows), len(rows))
        stats = target_stats([rows[index] for index in indices])
        for key in ("t_a", "t_b", "t_c_i"):
            if stats[key] is not None:
                samples[key].append(stats[key])

        sl_indices = rng.integers(0, len(sl_rows), len(sl_rows)) if sl_rows else []
        pt_indices = rng.integers(0, len(pt_rows), len(pt_rows)) if pt_rows else []
        sampled_sl = [sl_rows[index] for index in sl_indices]
        sampled_pt = [pt_rows[index] for index in pt_indices]
        sl_early = sum(row["minutes_held"] <= 60.0 for row in sampled_sl)
        pt_early = sum(row["minutes_held"] <= 60.0 for row in sampled_pt)
        sl_rate = sl_early / len(sampled_sl) if sampled_sl else 0.0
        pt_rate = pt_early / len(sampled_pt) if sampled_pt else 0.0
        samples["t_c_ii"].append(sl_rate - pt_rate)

        trend_indices = rng.integers(0, len(t_d_rows), len(t_d_rows))
        sampled_trend = [t_d_rows[index] for index in trend_indices]
        t_d_samples.append(sum(row["exit_reason"] == "profit_target" for row in sampled_trend)
                           / len(sampled_trend))

    result = {}
    for key, values in samples.items():
        result[key] = (None, None) if not values else _percentile(values, level)
    result["t_d_trend_up"] = _percentile(t_d_samples, level)

    def in_ci(value, ci):
        return value is not None and ci != (None, None) and ci[0] <= value <= ci[1]

    result["in_ci"] = in_ci
    return result


def judge_validation(sim_stats, validation_live_rows, *, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED):
    ci = bootstrap_ci(validation_live_rows, n_resamples=n_resamples, seed=seed)
    targets = {}
    for key in ("t_a", "t_b", "t_c_i", "t_c_ii"):
        sim = sim_stats[key]
        passed = ci["in_ci"](sim, ci[key])
        targets[key] = {"sim": sim, "ci": ci[key], "passed": passed}

    trend_sim = sim_stats.get("t_d", {}).get(TREND_REGIME)
    trend_values = sim_stats.get("t_d", {})
    trend_is_lowest = bool(trend_values) and TREND_REGIME in trend_values and \
        trend_values[TREND_REGIME] == min(trend_values.values())
    trend_in_ci = ci["in_ci"](trend_sim, ci["t_d_trend_up"])
    targets["t_d"] = {"sim_trend_up": trend_sim, "ci": ci["t_d_trend_up"],
                       "in_ci": trend_in_ci, "lowest": trend_is_lowest,
                       "passed": trend_in_ci and trend_is_lowest}
    passed = all(item["passed"] for item in targets.values())
    return {"targets": targets, "undefined": sorted(sim_stats.get("undefined", [])),
            "passed": passed}


def fit_symmetric_k(train_episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, r,
                    h_mult=1.0, s=0.5, seed=DEFAULT_SEED):
    live_stats = target_stats(live_rows(train_episodes))
    live_t_c_i = live_stats["t_c_i"]
    diff_by_k = {}
    for k in K_GRID:
        kwargs = {"beta": 0.0, "r": r, "h_mult": h_mult, "s": s, "k": k}
        if k != 0.0:
            kwargs["rng"] = np.random.default_rng(seed)
        sim, _ = replay(train_episodes, bars_by_symbol, mtm_iv_by_symbol, table, **kwargs)
        diff_by_k[k] = abs(target_stats(sim_rows(sim))["t_c_i"] - live_t_c_i)
    k = min(K_GRID, key=lambda value: (diff_by_k[value], value))
    return {"k": k, "diff_by_k": diff_by_k}


def run_controls(train_episodes, validation_episodes, bars_by_symbol, mtm_iv_by_symbol, table,
                 *, r, s=0.5, seed=DEFAULT_SEED, n_resamples=DEFAULT_RESAMPLES):
    ci = bootstrap_ci(live_rows(validation_episodes), n_resamples=n_resamples, seed=seed)
    a_rows, _ = replay(validation_episodes, bars_by_symbol, mtm_iv_by_symbol, table,
                       beta=0.0, r=r, h_mult=0.0, s=s)
    a_stats = target_stats(sim_rows(a_rows))
    outside_a = a_stats["t_a"] is not None and not ci["in_ci"](a_stats["t_a"], ci["t_a"])
    outside_c_i = a_stats["t_c_i"] is not None and not ci["in_ci"](a_stats["t_c_i"], ci["t_c_i"])
    control_k = fit_symmetric_k(train_episodes, bars_by_symbol, mtm_iv_by_symbol, table,
                                r=r, h_mult=1.0, s=s, seed=seed)["k"]
    c_rows, _ = replay(validation_episodes, bars_by_symbol, mtm_iv_by_symbol, table,
                       beta=0.0, r=r, h_mult=1.0, s=s, k=control_k,
                       rng=np.random.default_rng(seed))
    c_stats = target_stats(sim_rows(c_rows))
    outside_c_ii = c_stats["t_c_ii"] is not None and \
        not ci["in_ci"](c_stats["t_c_ii"], ci["t_c_ii"])
    result_a = {"sim_stats": a_stats, "outside_t_a": outside_a,
                "outside_t_c_i": outside_c_i, "passed": outside_a or outside_c_i}
    result_c = {"sim_stats": c_stats, "k": control_k,
                "outside_t_c_ii": outside_c_ii, "passed": outside_c_ii}
    return {"A": result_a, "C": result_c, "ci": ci,
            "passed": result_a["passed"] and result_c["passed"]}


def grade_calibration(fit, validation_judgement, controls, sensitivity_in_ci):
    reasons = []
    if fit["model_insufficient"]:
        reasons.append("model_insufficient")
    if not validation_judgement["passed"]:
        reasons.extend("validation:" + key for key, value in validation_judgement["targets"].items()
                       if not value["passed"])
    if not controls["A"]["passed"]:
        reasons.append("control_A")
    if not controls["C"]["passed"]:
        reasons.append("control_C")
    if reasons:
        return {"grade": "FAIL", "probation_stop_pct": None, "reasons": reasons}
    for key in ("t_a", "t_c_i"):
        if not sensitivity_in_ci[key]:
            reasons.append("sensitivity:" + key)
    if reasons:
        return {"grade": "PASS_CONDITIONAL", "probation_stop_pct": PROBATION_STOP_CONDITIONAL,
                "reasons": reasons}
    return {"grade": "PASS", "probation_stop_pct": PROBATION_STOP, "reasons": []}


def sensitivity_all_kinds(all_episodes, boundary_ts, bars_by_symbol, mtm_iv_by_symbol, table,
                          *, fit_beta_value, r, s=0.5, seed=DEFAULT_SEED,
                          n_resamples=DEFAULT_RESAMPLES):
    selected = [episode for episode in all_episodes
                if episode["entry_ts"] >= boundary_ts]
    if not selected:
        raise ValueError("sensitivity episode set must not be empty")
    rows, _ = replay(selected, bars_by_symbol, mtm_iv_by_symbol, table,
                     beta=fit_beta_value, r=r, h_mult=1.0, s=s)
    sim_stats = target_stats(sim_rows(rows))
    ci = bootstrap_ci(live_rows(selected), n_resamples=n_resamples, seed=seed)
    return {"t_a": {"sim": sim_stats["t_a"], "ci": ci["t_a"],
                     "in_ci": ci["in_ci"](sim_stats["t_a"], ci["t_a"])},
            "t_c_i": {"sim": sim_stats["t_c_i"], "ci": ci["t_c_i"],
                       "in_ci": ci["in_ci"](sim_stats["t_c_i"], ci["t_c_i"])},
            "n": len(selected)}


def _safe_t_b(rows):
    if not rows:
        return None
    stats = target_stats(rows)
    return stats["t_b"]


def calibrate(episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, r, s=0.5,
              seed=DEFAULT_SEED, n_resamples=DEFAULT_RESAMPLES):
    clean = [episode for episode in episodes if episode["kind"] == "clean"]
    train, validation, boundary_ts = split_train_validation(clean)
    fit = fit_beta(train, bars_by_symbol, mtm_iv_by_symbol, table, boundary_ts=boundary_ts,
                   r=r, h_mult=1.0, s=s, seed=seed, n_resamples=n_resamples)
    fit_pessimistic = fit_beta(train, bars_by_symbol, mtm_iv_by_symbol, table,
                               boundary_ts=boundary_ts, r=r, h_mult=1.5, s=s,
                               seed=seed, n_resamples=n_resamples)
    validation_results, skip_counts = replay(validation, bars_by_symbol, mtm_iv_by_symbol,
                                             table, beta=fit["beta"], r=r, h_mult=1.0, s=s)
    sim_stats = target_stats(sim_rows(validation_results))
    validation_judgement = judge_validation(sim_stats, live_rows(validation),
                                            n_resamples=n_resamples, seed=seed)
    controls = run_controls(train, validation, bars_by_symbol, mtm_iv_by_symbol, table,
                            r=r, s=s, seed=seed, n_resamples=n_resamples)
    sensitivity = sensitivity_all_kinds(
        episodes, boundary_ts, bars_by_symbol, mtm_iv_by_symbol, table,
        fit_beta_value=fit["beta"], r=r, s=s, seed=seed, n_resamples=n_resamples)
    sensitivity_in_ci = {key: sensitivity[key]["in_ci"] for key in ("t_a", "t_c_i")}

    kept = [episode for episode in validation if episode["limit_credit"] > LOW_CREDIT]
    kept_live = live_rows(kept)
    kept_results, _ = replay(kept, bars_by_symbol, mtm_iv_by_symbol, table,
                             beta=fit["beta"], r=r, h_mult=1.0, s=s)
    low_credit = {"sim_t_b": _safe_t_b(sim_rows(kept_results)),
                  "live_t_b": _safe_t_b(kept_live),
                  "n_excluded": len(validation) - len(kept)}
    grade = grade_calibration(fit, validation_judgement, controls, sensitivity_in_ci)
    return {"boundary_ts": boundary_ts, "n_train": len(train), "n_validation": len(validation),
            "fit": fit, "fit_pessimistic": fit_pessimistic,
            "validation": {"sim_stats": sim_stats, "judgement": validation_judgement},
            "controls": controls, "sensitivity": sensitivity,
            "low_credit_sensitivity": low_credit, "grade": grade,
            "skip_counts_validation": skip_counts}
