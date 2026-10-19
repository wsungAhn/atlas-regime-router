"""Polling exit-path engine for two-leg vertical spreads."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from friction import (  # noqa: E402
    fill_debit,
    leg_halves,
    leg_mids,
    mark_pnl_pct,
    parse_occ,
    spread_mid_debit,
)
from vendor.credit_spread_simulator import price_spread_to_close  # noqa: E402


POLICY_V0 = "V0"
POLICY_V1 = "V1"
LIVE_CLOSE_PATH_LIMIT_FALLBACK = "limit_fallback"
LIVE_CLOSE_PATH_MARKET = "market"
MARKET_S = 1.0
OPTION_MULTIPLIER = 100.0
LIMIT_FALLBACK_MARKUP = 0.05
LIMIT_FILL_EPSILON = 1e-12
V1_LIMIT_POLLS = 3
ORATS_S_BY_LEGS = {2: 0.66, 4: 0.56}


def s_orats(n_legs):
    try:
        return ORATS_S_BY_LEGS[n_legs]
    except (KeyError, TypeError):
        raise ValueError("s_orats supports only 2 or 4 legs") from None


def scaled_table(table: pd.DataFrame, h_mult: float) -> pd.DataFrame:
    if not isinstance(table, pd.DataFrame) or "h" not in table.columns:
        raise ValueError("friction table must be a DataFrame with an h column")
    if not isinstance(h_mult, (int, float)) or h_mult < 0:
        raise ValueError("h_mult must be non-negative")
    result = table.copy(deep=True)
    result["h"] = result["h"] * float(h_mult)
    return result


def legacy_window(entry_date, exit_date):
    entry = pd.Timestamp(entry_date).normalize()
    end = pd.Timestamp(exit_date).normalize()
    return entry + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1), end + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)


def _exit_result(position, qty, bar_time, reason, trigger_mark, close_debit, n_polls, n_limit_retries):
    return {
        "exit_ts": bar_time,
        "exit_reason": reason,
        "trigger_mark_pnl_pct": float(trigger_mark),
        "close_debit": float(close_debit),
        "realized_pnl": (float(position["credit"]) - float(close_debit)) * OPTION_MULTIPLIER * float(qty),
        "n_polls": n_polls,
        "n_limit_retries": n_limit_retries,
    }


def _validate_position(position):
    if not isinstance(position, dict) or not isinstance(position.get("legs"), list):
        raise ValueError("position must contain a legs list")
    if len(position["legs"]) != 2:
        raise ValueError("4-leg paths arrive in T4d")
    if not isinstance(position.get("credit"), (int, float)) or position["credit"] <= 0:
        raise ValueError("position credit must be positive")
    for leg in position["legs"]:
        if not isinstance(leg, dict) or not isinstance(leg.get("occ"), str):
            raise ValueError("each leg must contain an OCC symbol")
        if leg.get("side") not in {"short", "long"}:
            raise ValueError("leg side must be short or long")
    parsed = [parse_occ(leg["occ"]) for leg in position["legs"]]
    if parsed[0][2] != parsed[1][2]:
        raise ValueError("two legs must be the same option type")
    if {leg["side"] for leg in position["legs"]} != {"short", "long"}:
        raise ValueError("position must contain one short and one long leg")


def _spread_type(position):
    parsed = {leg["side"]: parse_occ(leg["occ"]) for leg in position["legs"]}
    option_type = parsed["short"][2]
    short_strike = parsed["short"][3]
    long_strike = parsed["long"][3]
    if option_type == "put" and short_strike > long_strike:
        return "bull_put", short_strike, long_strike
    if option_type == "call" and short_strike < long_strike:
        return "bear_call", short_strike, long_strike
    raise ValueError("legs do not form a vertical credit spread")


def _validate_inputs(position, bars, mtm_iv, expiry, entry_iv, r, qty, policy, live_close_path,
                     start_after, end_at, pt, sl, friction, iv_scale):
    _validate_position(position)
    if not isinstance(bars, pd.DataFrame) or "close" not in bars.columns:
        raise ValueError("bars must be a DataFrame with a close column")
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is not None:
        raise ValueError("bars index must be a timezone-naive DatetimeIndex")
    if not bars.index.is_monotonic_increasing:
        raise ValueError("bars index must be sorted")
    if not isinstance(mtm_iv, pd.Series):
        raise ValueError("mtm_iv must be a Series")
    if not isinstance(expiry, (date, pd.Timestamp)):
        raise ValueError("expiry must be a date or Timestamp")
    if not isinstance(entry_iv, (int, float)) or entry_iv <= 0:
        raise ValueError("entry_iv must be positive")
    if not isinstance(r, (int, float)):
        raise ValueError("r must be numeric")
    if not isinstance(qty, (int, float)) or qty <= 0:
        raise ValueError("qty must be positive")
    if policy not in {POLICY_V0, POLICY_V1}:
        raise ValueError("policy must be V0 or V1")
    if live_close_path not in {None, LIVE_CLOSE_PATH_MARKET, LIVE_CLOSE_PATH_LIMIT_FALLBACK}:
        raise ValueError("live_close_path must be None, market, or limit_fallback")
    if friction is None and (policy == POLICY_V1 or live_close_path == LIVE_CLOSE_PATH_LIMIT_FALLBACK):
        raise ValueError("limit paths require friction")
    if not isinstance(pt, (int, float)) or not isinstance(sl, (int, float)):
        raise ValueError("pt and sl must be numeric")
    if not isinstance(iv_scale, (int, float)) or iv_scale <= 0:
        raise ValueError("iv_scale must be positive")
    start = pd.Timestamp(start_after)
    end = pd.Timestamp(end_at)
    if start.tzinfo is not None or end.tzinfo is not None or start >= end:
        raise ValueError("start_after and end_at must be ordered, naive timestamps")
    if friction is not None:
        if not isinstance(friction, dict):
            raise ValueError("friction must be a dict or None")
        required = {"table", "beta", "h_mult", "s", "k", "rng"}
        if set(friction) != required:
            raise ValueError("friction must contain exactly table, beta, h_mult, s, k, rng")
        if not isinstance(friction["s"], (int, float)) or not 0.0 <= friction["s"] <= 1.0:
            raise ValueError("friction s must be in [0, 1]")


def run_exit_path(position, bars, mtm_iv, *, expiry, entry_iv, r, qty, policy=POLICY_V0,
                  live_close_path=None, start_after, end_at, pt=0.5, sl=-1.0,
                  friction=None, iv_scale=1.0):
    _validate_inputs(position, bars, mtm_iv, expiry, entry_iv, r, qty, policy, live_close_path,
                     start_after, end_at, pt, sl, friction, iv_scale)
    spread_type, short_strike, long_strike = _spread_type(position)
    expiry_day = pd.Timestamp(expiry).normalize()
    start = pd.Timestamp(start_after)
    end = pd.Timestamp(end_at)
    table = scaled_table(friction["table"], friction["h_mult"]) if friction is not None else None
    window = bars.loc[(bars.index > start) & (bars.index <= end)]
    n_polls = 0
    pending_limit = False
    limit_debit = None
    limit_reason = None
    limit_trigger_mark = None
    limit_misses = 0
    for bar_time, row in window.iterrows():
        n_polls += 1
        S = float(row["close"])
        bar_day = bar_time.normalize()
        days_to_expiry = (expiry_day - bar_day).days
        T = max(days_to_expiry, 0) / 365.0
        iv = float(mtm_iv.get(bar_day, entry_iv)) * float(iv_scale)
        if friction is None:
            close_debit = price_spread_to_close(S_t=S, short_strike=short_strike, long_strike=long_strike,
                                                 T_remaining=T, r=r, iv_t=iv, spread_type=spread_type)
            mark_pnl = (float(position["credit"]) - close_debit) / float(position["credit"])
        else:
            mark_pnl = mark_pnl_pct(position, S, T, r, iv, friction["beta"], table, friction["k"], friction["rng"])
            mids = leg_mids(position, S, T, r, iv)
            halves = leg_halves(position, mids, table)
            legs = []
            for leg, mid, half in zip(position["legs"], mids, halves):
                legs.append({"side": leg["side"], "bid": max(mid - half, 0.0), "ask": mid + half})
            close_debit = fill_debit(legs, MARKET_S)
        reason = "profit_target" if mark_pnl >= pt else "stop_loss" if mark_pnl <= sl else None

        if policy == POLICY_V1 and pending_limit:
            if fill_debit(legs, friction["s"]) <= limit_debit + LIMIT_FILL_EPSILON:
                close_debit = limit_debit
                return _exit_result(position, qty, bar_time, limit_reason, limit_trigger_mark, close_debit,
                                    n_polls, limit_misses)
            limit_misses += 1
            if limit_misses == V1_LIMIT_POLLS:
                close_debit = fill_debit(legs, MARKET_S)
                return _exit_result(position, qty, bar_time, limit_reason, limit_trigger_mark, close_debit,
                                    n_polls, limit_misses)
            continue

        if policy == POLICY_V0 and live_close_path == LIVE_CLOSE_PATH_LIMIT_FALLBACK and pending_limit:
            if reason is None:
                pending_limit = False
                limit_debit = None
                limit_misses = 0
            else:
                limit_misses += 1
                mid_debit = spread_mid_debit(position, mids)
                limit_debit = round(mid_debit + LIMIT_FALLBACK_MARKUP * abs(mid_debit), 2)
                if fill_debit(legs, friction["s"]) <= limit_debit + LIMIT_FILL_EPSILON:
                    close_debit = limit_debit
                    return _exit_result(position, qty, bar_time, reason, mark_pnl, close_debit,
                                        n_polls, limit_misses)
                continue

        if reason is not None:
            if policy == POLICY_V1:
                pending_limit = True
                limit_reason = reason
                limit_trigger_mark = mark_pnl
                limit_debit = spread_mid_debit(position, mids)
                limit_misses = 0
                if fill_debit(legs, friction["s"]) <= limit_debit + LIMIT_FILL_EPSILON:
                    close_debit = limit_debit
                    return _exit_result(position, qty, bar_time, reason, mark_pnl, close_debit, n_polls, 0)
                limit_misses = 1
                continue
            if live_close_path == LIVE_CLOSE_PATH_LIMIT_FALLBACK:
                pending_limit = True
                mid_debit = spread_mid_debit(position, mids)
                limit_debit = round(mid_debit + LIMIT_FALLBACK_MARKUP * abs(mid_debit), 2)
                if fill_debit(legs, friction["s"]) <= limit_debit + LIMIT_FILL_EPSILON:
                    close_debit = limit_debit
                    return _exit_result(position, qty, bar_time, reason, mark_pnl, close_debit, n_polls, 0)
                continue
            return _exit_result(position, qty, bar_time, reason, mark_pnl, close_debit, n_polls, 0)
    return None
