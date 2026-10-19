"""Replay live option episodes and fit the mark-bias parameter."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exit_path import run_exit_path  # noqa: E402
from friction import implied_iv_scale, parse_occ  # noqa: E402
from friction_stats import DEFAULT_RESAMPLES, DEFAULT_SEED  # noqa: E402


BETA_GRID = tuple(0.125 * i for i in range(13))
BETA_EXTENDED = (2.0, 3.0, 4.0)
TRAIN_FRACTION = 0.6
EARLY_MINUTES = 60.0
BAR_MINUTES = 15
SE_FLOOR_RATE = 0.01
SE_FLOOR_PNL = 0.05
UNDEFINED_LOSS = 1e6


def split_train_validation(clean_episodes):
    if any(episode.get("kind") != "clean" for episode in clean_episodes):
        raise ValueError("all episodes must be clean")
    ordered = sorted(clean_episodes, key=lambda episode: episode["entry_ts"])
    n_train = int(np.floor(TRAIN_FRACTION * len(ordered)))
    if n_train == 0 or n_train == len(ordered):
        raise ValueError("train and validation must both be non-empty")
    train, validation = ordered[:n_train], ordered[n_train:]
    return train, validation, validation[0]["entry_ts"]


def prepare_bars(bars_by_symbol):
    prepared = {}
    for symbol, bars in bars_by_symbol.items():
        if not isinstance(bars, pd.DataFrame) or not isinstance(bars.index, pd.DatetimeIndex):
            raise ValueError("bars must have a DatetimeIndex")
        if bars.index.tz is not None:
            raise ValueError("bars index must be timezone-naive")
        result = bars.copy(deep=True)
        result.index = result.index + pd.Timedelta(minutes=BAR_MINUTES)
        prepared[symbol] = result
    return prepared


def build_replay_position(episode):
    legs = episode.get("legs")
    if not isinstance(legs, list) or len(legs) != episode.get("n_legs") or len(legs) not in {2, 4}:
        raise ValueError("episode legs do not match n_legs")
    parsed = [(occ, parse_occ(occ)) for occ in legs]
    by_type = {option_type: [(occ, item[3]) for occ, item in parsed if item[2] == option_type]
               for option_type in ("put", "call")}
    if len(by_type["put"]) + len(by_type["call"]) != len(legs):
        raise ValueError("invalid option types")
    if len(legs) == 2 and any(len(items) != 2 for items in by_type.values() if items):
        raise ValueError("two legs must share an option type")
    if len(legs) == 4 and any(len(by_type[item]) != 2 for item in ("put", "call")):
        raise ValueError("four legs must contain two puts and two calls")
    side_by_occ = {}
    for option_type, items in by_type.items():
        if not items:
            continue
        strikes = [strike for _, strike in items]
        if len(set(strikes)) != 2:
            raise ValueError("each option type must form a pair")
        short_strike = max(strikes) if option_type == "put" else min(strikes)
        for occ, strike in items:
            side_by_occ[occ] = "short" if strike == short_strike else "long"
    return {"legs": [{"occ": occ, "side": side_by_occ[occ]} for occ in legs],
            "credit": episode["limit_credit"]}


def _naive_utc(value):
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.tz_localize(None)


def _last_at_or_before(series, timestamp):
    eligible = series.loc[series.index <= timestamp]
    return None if eligible.empty else eligible.iloc[-1]


def _replay_one(episode, bars, mtm_iv, table, *, beta, r, policy="V0", s=0.5, k=0.0,
                rng=None, h_mult=1.0, pt=0.5, sl=-1.0, **policy_kwargs):
    entry_naive = _naive_utc(episode["entry_ts"])
    entry_bar = bars.loc[bars.index <= entry_naive]
    if entry_bar.empty:
        return None, "no_entry_bar"
    S_entry = float(entry_bar.iloc[-1]["close"])
    iv_series = mtm_iv.copy()
    iv_series.index = pd.DatetimeIndex(iv_series.index)
    if iv_series.index.tz is not None:
        iv_series.index = iv_series.index.tz_convert("UTC").tz_localize(None)
    entry_iv = _last_at_or_before(iv_series, entry_naive.normalize())
    if entry_iv is None:
        return None, "no_iv"
    position = build_replay_position(episode)
    expiry = parse_occ(episode["legs"][0])[1]
    T_entry = (expiry - entry_naive.date()).days / 365.0
    iv_scale = implied_iv_scale(position, S_entry, T_entry, r, float(entry_iv))
    if iv_scale is None:
        return None, "unanchorable"
    end_at = pd.Timestamp(expiry) + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    result = run_exit_path(
        position, bars, mtm_iv, expiry=expiry, entry_iv=float(entry_iv), r=r,
        qty=episode["qty"], policy=policy, live_close_path=episode["live_close_path"],
        start_after=entry_naive, end_at=end_at,
        pt=pt, sl=sl,
        friction={"table": table, "beta": beta, "h_mult": h_mult, "s": s, "k": k, "rng": rng},
        iv_scale=iv_scale, entry_ts=entry_naive, entry_underlying=S_entry,
        **policy_kwargs,
    )
    if result is None:
        return None, "no_exit"
    row = dict(result)
    row.update({
        "regime": episode.get("regime"), "symbol": episode["symbol"],
        "entry_ts": entry_naive,
        "minutes_held": (pd.Timestamp(result["exit_ts"]) - entry_naive).total_seconds() / 60.0,
        "live_exit_reason": episode.get("exit_reason"),
        "live_trigger_profit_pct": episode.get("trigger_profit_pct"),
        "live_minutes_held": episode.get("minutes_held"),
    })
    return row, None


def replay_episode(episode, bars, mtm_iv, table, *, beta, r, policy="V0", s=0.5, k=0.0,
                    rng=None, h_mult=1.0, pt=0.5, sl=-1.0, **policy_kwargs):
    return _replay_one(episode, bars, mtm_iv, table, beta=beta, r=r, policy=policy,
                       s=s, k=k, rng=rng, h_mult=h_mult, pt=pt, sl=sl, **policy_kwargs)[0]


def replay(episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, beta, r, **kwargs):
    counts = {key: 0 for key in ("no_bars", "no_entry_bar", "no_iv", "unanchorable", "no_exit")}
    rows = []
    for episode in episodes:
        symbol = episode["symbol"]
        if symbol not in bars_by_symbol or symbol not in mtm_iv_by_symbol:
            counts["no_bars"] += 1
            continue
        row, reason = _replay_one(episode, bars_by_symbol[symbol], mtm_iv_by_symbol[symbol], table,
                                  beta=beta, r=r, **kwargs)
        if row is not None:
            rows.append(row)
        else:
            counts[reason] += 1
    return rows, counts


def target_stats(rows):
    if not rows:
        raise ValueError("rows must not be empty")
    total = len(rows)
    pt_rows = [row for row in rows if row["exit_reason"] == "profit_target"]
    sl_rows = [row for row in rows if row["exit_reason"] == "stop_loss"]
    early_sl = [row for row in sl_rows if row["minutes_held"] <= EARLY_MINUTES]
    early_pt = [row for row in pt_rows if row["minutes_held"] <= EARLY_MINUTES]
    t_b_values = [row["trigger_pnl"] for row in sl_rows if row.get("trigger_pnl") is not None]
    t_b = float(np.median(t_b_values)) if t_b_values else None
    t_c_i = len(early_sl) / len(sl_rows) if sl_rows else 0.0
    pt_rate_early = len(early_pt) / len(pt_rows) if pt_rows else 0.0
    undefined = set()
    if not sl_rows:
        undefined.update(("t_b", "t_c_i", "t_c_ii"))
    if not pt_rows:
        undefined.add("t_c_ii")
    regimes = {}
    for regime in {row.get("regime") for row in rows}:
        regime_rows = [row for row in rows if row.get("regime") == regime]
        regimes[regime] = sum(row["exit_reason"] == "profit_target" for row in regime_rows) / len(regime_rows)
    return {"t_a": len(pt_rows) / total, "t_b": t_b, "t_c_i": t_c_i,
            "t_c_ii": t_c_i - pt_rate_early, "t_d": regimes, "undefined": undefined}


def live_rows(episodes):
    return [{"exit_reason": row["exit_reason"], "trigger_pnl": row.get("trigger_profit_pct"),
             "minutes_held": row["minutes_held"], "regime": row.get("regime")} for row in episodes]


def sim_rows(results):
    return [{"exit_reason": row["exit_reason"], "trigger_pnl": row.get("trigger_mark_pnl_pct"),
             "minutes_held": row["minutes_held"], "regime": row.get("regime")} for row in results]


def calibration_loss(sim_stats, live_stats, se):
    if live_stats["t_b"] is None:
        raise ValueError("live t_b must be defined")
    loss = 0.0
    for key in ("t_a", "t_b", "t_c_i", "t_c_ii"):
        if key == "t_b" and sim_stats[key] is None:
            loss += UNDEFINED_LOSS
        else:
            loss += ((sim_stats[key] - live_stats[key]) / se[key]) ** 2
    return float(loss)


def bootstrap_ses(train_live_rows, *, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED):
    if not train_live_rows:
        raise ValueError("train_live_rows must not be empty")
    rng = np.random.default_rng(seed)
    samples = {key: [] for key in ("t_a", "t_b", "t_c_i", "t_c_ii")}
    for _ in range(n_resamples):
        indices = rng.integers(0, len(train_live_rows), len(train_live_rows))
        stats = target_stats([train_live_rows[index] for index in indices])
        for key in samples:
            samples[key].append(np.nan if stats[key] is None else stats[key])
    result = {}
    for key, values in samples.items():
        finite = np.asarray(values, dtype=float)
        se = float(np.nanstd(finite, ddof=1)) if np.count_nonzero(~np.isnan(finite)) >= 2 else float("nan")
        floor = SE_FLOOR_PNL if key == "t_b" else SE_FLOOR_RATE
        result[key] = max(se, floor) if not np.isnan(se) else floor
    return result


def select_beta(loss_fn, *, grid=BETA_GRID, extended=BETA_EXTENDED):
    loss_by_beta = {beta: loss_fn(beta) for beta in grid}
    did_extend = False
    best_grid_beta = min(grid, key=lambda value: (loss_by_beta[value], value))
    if best_grid_beta == max(grid):
        did_extend = True
        loss_by_beta.update({beta: loss_fn(beta) for beta in extended})
    beta = min(loss_by_beta, key=lambda value: (loss_by_beta[value], value))
    return {"beta": beta, "loss_by_beta": loss_by_beta, "extended": did_extend,
            "model_insufficient": did_extend and beta == max(extended)}


def fit_beta(train_episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, boundary_ts, r,
             h_mult=1.0, s=0.5, seed=DEFAULT_SEED, n_resamples=DEFAULT_RESAMPLES):
    boundary = pd.Timestamp(boundary_ts)
    if any(pd.Timestamp(episode["entry_ts"]) >= boundary for episode in train_episodes):
        raise ValueError("train contains validation episode")
    live = live_rows(train_episodes)
    live_stats = target_stats(live)
    se = bootstrap_ses(live, n_resamples=n_resamples, seed=seed)
    observed_counts = None
    losses = {}

    def loss_fn(beta):
        nonlocal observed_counts
        results, counts = replay(train_episodes, bars_by_symbol, mtm_iv_by_symbol, table,
                                 beta=beta, r=r, h_mult=h_mult, s=s)
        if observed_counts is None:
            observed_counts = counts
        elif counts != observed_counts:
            raise ValueError("replay skip counts changed across beta")
        losses[beta] = calibration_loss(target_stats(sim_rows(results)), live_stats, se)
        return losses[beta]

    selection = select_beta(loss_fn)
    results, counts = replay(train_episodes, bars_by_symbol, mtm_iv_by_symbol, table,
                             beta=selection["beta"], r=r, h_mult=h_mult, s=s)
    sim_stats = target_stats(sim_rows(results))
    residuals = {}
    for regime in {row.get("regime") for row in live}:
        live_regime = [row for row in live if row.get("regime") == regime]
        sim_regime = [row for row in sim_rows(results) if row.get("regime") == regime]
        residuals[regime] = {"sim_t_a": target_stats(sim_regime)["t_a"] if sim_regime else 0.0,
                             "live_t_a": target_stats(live_regime)["t_a"], "n": len(live_regime)}
    return {**selection, "skip_counts": counts, "live_stats": live_stats, "se": se,
            "h_mult": h_mult, "sim_stats": sim_stats, "residuals_by_regime": residuals}
