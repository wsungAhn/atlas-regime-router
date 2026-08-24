"""Pure credit spread simulator for leveraged ETF short premium research.

The module is intentionally IO-free. It reuses the already-reviewed pricing and
historical-volatility helpers and only operates on provided pandas objects.
"""

from __future__ import annotations

import math

import pandas as pd

from vendor.iv_approximation import estimate_historical_vol
from vendor.options_pricing import black_scholes_price, strike_for_delta

IV_LOOKBACK_DAYS = 252
CONTRACT_MULTIPLIER = 100

_ATR_WINDOW_DAYS = 20
_VALID_SPREAD_TYPES = {"bull_put", "bear_call"}


def _validate_spread_type(spread_type: str) -> None:
    if spread_type not in _VALID_SPREAD_TYPES:
        raise ValueError("spread_type must be 'bull_put' or 'bear_call'")


def _option_type_for_spread_type(spread_type: str) -> str:
    _validate_spread_type(spread_type)
    return "put" if spread_type == "bull_put" else "call"


def _validate_orientation(short_strike: float, long_strike: float, spread_type: str) -> None:
    _validate_spread_type(spread_type)
    if spread_type == "bull_put" and short_strike <= long_strike:
        raise ValueError("bull_put requires short_strike > long_strike")
    if spread_type == "bear_call" and short_strike >= long_strike:
        raise ValueError("bear_call requires short_strike < long_strike")


def _round_to_increment(value: float, increment: float) -> float:
    return round(value / increment) * increment


def _ensure_numeric_series(series: pd.Series, label: str) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.isna().any():
        raise ValueError(f"{label} contains non-numeric values")
    return numeric.astype(float)


def _true_range(price_df: pd.DataFrame) -> pd.Series:
    required_columns = {"high", "low", "close"}
    missing = required_columns.difference(price_df.columns)
    if missing:
        raise ValueError(f"price_df is missing required columns: {sorted(missing)}")

    high = _ensure_numeric_series(price_df["high"], "high")
    low = _ensure_numeric_series(price_df["low"], "low")
    close = _ensure_numeric_series(price_df["close"], "close")

    previous_close = close.shift(1)
    components = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    )
    return components.max(axis=1)


def _atr_filter_series(price_df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    atr = _true_range(price_df).rolling(_ATR_WINDOW_DAYS).mean()
    atr_baseline = atr.rolling(_ATR_WINDOW_DAYS).mean()
    return atr, atr_baseline


def _historical_vol_as_of(
    price_df: pd.DataFrame,
    as_of_date: pd.Timestamp,
    leverage_factor: float,
    lookback_days: int = IV_LOOKBACK_DAYS,
) -> float:
    if leverage_factor <= 0:
        raise ValueError("leverage_factor must be positive")

    sliced = price_df.loc[:as_of_date]
    historical_vol = estimate_historical_vol(sliced, lookback_days=lookback_days)
    return float(historical_vol * math.sqrt(leverage_factor))


def construct_credit_spread(
    S: float,
    iv: float,
    dte_days: int,
    r: float,
    delta_short_leg: float,
    spread_width_pct: float,
    spread_type: str,
    strike_increment: float = 0.5,
    credit_haircut_pct: float = 0.05,
) -> dict:
    """Construct a credit spread contract using Black-Scholes pricing."""

    if S <= 0:
        raise ValueError("S must be positive")
    if iv <= 0:
        raise ValueError("iv must be positive")
    if dte_days <= 0:
        raise ValueError("dte_days must be positive")
    if delta_short_leg <= 0 or delta_short_leg >= 1:
        raise ValueError("delta_short_leg must be between 0 and 1")
    if spread_width_pct <= 0:
        raise ValueError("spread_width_pct must be positive")
    if strike_increment <= 0:
        raise ValueError("strike_increment must be positive")
    if not 0.0 <= credit_haircut_pct <= 1.0:
        raise ValueError("credit_haircut_pct must be between 0 and 1")

    _validate_spread_type(spread_type)
    option_type = _option_type_for_spread_type(spread_type)
    t_years = dte_days / 365.0

    theoretical_short_strike = strike_for_delta(
        S,
        t_years,
        r,
        iv,
        delta_short_leg,
        option_type,
    )
    short_strike = _round_to_increment(theoretical_short_strike, strike_increment)

    spread_distance = S * spread_width_pct
    if spread_type == "bull_put":
        long_theoretical = short_strike - spread_distance
    else:
        long_theoretical = short_strike + spread_distance
    long_strike = _round_to_increment(long_theoretical, strike_increment)

    _validate_orientation(short_strike, long_strike, spread_type)
    width = abs(short_strike - long_strike)
    if width <= 0:
        raise ValueError("spread width must be positive")

    short_premium = black_scholes_price(S, short_strike, t_years, r, iv, option_type)
    long_premium = black_scholes_price(S, long_strike, t_years, r, iv, option_type)
    credit = short_premium - long_premium
    credit_received = credit * (1.0 - credit_haircut_pct)

    if not (0.0 < credit_received < width):
        raise ValueError("the parameter combination does not form a valid credit spread")

    max_loss = width - credit_received
    breakeven = short_strike - credit_received if spread_type == "bull_put" else short_strike + credit_received

    return {
        "short_strike": float(short_strike),
        "long_strike": float(long_strike),
        "width": float(width),
        "credit_received": float(credit_received),
        "max_loss": float(max_loss),
        "breakeven": float(breakeven),
    }


def price_spread_to_close(
    S_t: float,
    short_strike: float,
    long_strike: float,
    T_remaining: float,
    r: float,
    iv_t: float,
    spread_type: str,
) -> float:
    """Price the cost to buy back a credit spread."""

    _validate_orientation(short_strike, long_strike, spread_type)
    width = abs(short_strike - long_strike)
    option_type = _option_type_for_spread_type(spread_type)

    if T_remaining > 0:
        short_price = black_scholes_price(S_t, short_strike, T_remaining, r, iv_t, option_type)
        long_price = black_scholes_price(S_t, long_strike, T_remaining, r, iv_t, option_type)
        close_debit = short_price - long_price
    else:
        if spread_type == "bull_put":
            close_debit = max(0.0, min(width, short_strike - S_t))
        else:
            close_debit = max(0.0, min(width, S_t - short_strike))

    assert 0.0 <= close_debit <= width
    return float(close_debit)


def find_entry_signals(
    price_df: pd.DataFrame,
    entry_trigger_drawdown_pct: float,
    lookback_days: int = 20,
    atr_filter: bool = True,
    atr_threshold_pct: float = 1.30,
    min_history_days: int = IV_LOOKBACK_DAYS,
) -> list[pd.Timestamp]:
    """Return dates where the close is down from the trailing high by the requested threshold."""

    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    if entry_trigger_drawdown_pct <= 0 or entry_trigger_drawdown_pct >= 1:
        raise ValueError("entry_trigger_drawdown_pct must be between 0 and 1")
    if atr_threshold_pct <= 0:
        raise ValueError("atr_threshold_pct must be positive")
    if min_history_days <= 0:
        raise ValueError("min_history_days must be positive")

    if "close" not in price_df.columns:
        raise ValueError("price_df must contain a 'close' column")
    if atr_filter:
        missing = {"high", "low"}.difference(price_df.columns)
        if missing:
            raise ValueError(f"price_df is missing required columns: {sorted(missing)}")

    close = _ensure_numeric_series(price_df["close"], "close")
    atr_series = atr_baseline = None
    if atr_filter:
        atr_series, atr_baseline = _atr_filter_series(price_df)

    signals: list[pd.Timestamp] = []
    for idx, current_date in enumerate(price_df.index):
        if idx + 1 < min_history_days:
            continue
        if idx + 1 < lookback_days:
            continue

        current_close = float(close.iloc[idx])
        trailing_high = float(close.iloc[idx + 1 - lookback_days : idx + 1].max())
        drawdown = current_close / trailing_high - 1.0
        if drawdown > -entry_trigger_drawdown_pct:
            continue

        if atr_filter:
            current_atr = atr_series.iloc[idx]
            current_baseline = atr_baseline.iloc[idx]
            if pd.isna(current_atr) or pd.isna(current_baseline):
                continue
            if current_atr > current_baseline * atr_threshold_pct:
                continue

        signals.append(pd.Timestamp(current_date))

    return signals


def simulate_trade(
    price_df: pd.DataFrame,
    signal_date: pd.Timestamp,
    entry_date: pd.Timestamp,
    expiry_date: pd.Timestamp,
    entry_iv: float,
    mtm_iv_by_date: pd.Series,
    r: float,
    delta_short_leg: float,
    spread_width_pct: float,
    spread_type: str,
    profit_target_pct: float = 0.5,
    stop_loss_multiple: float | None = None,
    strike_increment: float = 0.5,
    credit_haircut_pct: float = 0.05,
) -> dict:
    """Simulate the life cycle of a single trade using daily mark-to-market prices."""

    signal_date = pd.Timestamp(signal_date)
    entry_date = pd.Timestamp(entry_date)
    expiry_date = pd.Timestamp(expiry_date)

    if not 0.0 <= profit_target_pct <= 1.0:
        raise ValueError("profit_target_pct must be between 0 and 1")
    if stop_loss_multiple is not None and stop_loss_multiple <= 0:
        raise ValueError("stop_loss_multiple must be positive when provided")
    if entry_date not in price_df.index:
        raise ValueError("entry_date must be present in price_df")
    if expiry_date not in price_df.index:
        raise ValueError("expiry_date must be present in price_df")

    trading_sessions = price_df.index[(price_df.index > entry_date) & (price_df.index <= expiry_date)]
    missing_sessions = trading_sessions.difference(mtm_iv_by_date.index)
    if not missing_sessions.empty:
        raise ValueError("mtm_iv_by_date coverage gap")

    entry_price = float(price_df.loc[entry_date, "close"])
    effective_dte_days = (expiry_date - entry_date).days
    spread = construct_credit_spread(
        S=entry_price,
        iv=entry_iv,
        dte_days=effective_dte_days,
        r=r,
        delta_short_leg=delta_short_leg,
        spread_width_pct=spread_width_pct,
        spread_type=spread_type,
        strike_increment=strike_increment,
        credit_haircut_pct=credit_haircut_pct,
    )

    exit_reason = "expiry"
    exit_date = expiry_date
    close_debit = float(spread["width"])

    for session_date in trading_sessions:
        S_t = float(price_df.loc[session_date, "close"])
        iv_t = float(mtm_iv_by_date.loc[session_date])
        T_remaining = 0.0 if session_date == expiry_date else (expiry_date - session_date).days / 365.0
        close_debit = price_spread_to_close(
            S_t=S_t,
            short_strike=spread["short_strike"],
            long_strike=spread["long_strike"],
            T_remaining=T_remaining,
            r=r,
            iv_t=iv_t,
            spread_type=spread_type,
        )
        current_pnl = spread["credit_received"] - close_debit

        if current_pnl >= spread["credit_received"] * profit_target_pct:
            exit_reason = "profit_target"
            exit_date = pd.Timestamp(session_date)
            break
        if stop_loss_multiple is not None and close_debit >= spread["credit_received"] * stop_loss_multiple:
            exit_reason = "stop_loss"
            exit_date = pd.Timestamp(session_date)
            break
        if session_date == expiry_date:
            exit_reason = "expiry"
            exit_date = pd.Timestamp(session_date)
            break

    realized_pnl = spread["credit_received"] - close_debit

    return {
        "signal_date": signal_date,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "exit_reason": exit_reason,
        "spread_type": spread_type,
        "entry_iv": float(entry_iv),
        "short_strike": spread["short_strike"],
        "long_strike": spread["long_strike"],
        "width": spread["width"],
        "credit_received": spread["credit_received"],
        "max_loss": spread["max_loss"],
        "breakeven": spread["breakeven"],
        "close_debit": float(close_debit),
        "realized_pnl": float(realized_pnl),
        "effective_dte_days": effective_dte_days,
        "profit_target_pct": float(profit_target_pct),
        "stop_loss_multiple": stop_loss_multiple,
    }


def run_portfolio_simulation(
    price_df: pd.DataFrame,
    mtm_iv_by_date: pd.Series,
    leverage_factor: float,
    candidate_signal_dates: list[pd.Timestamp],
    dte_days: int,
    r: float,
    delta_short_leg: float,
    spread_width_pct: float,
    spread_type: str,
    profit_target_pct: float,
    stop_loss_multiple: float | None,
    strike_increment: float,
    credit_haircut_pct: float,
    max_concurrent_positions: int = 1,
) -> dict:
    """Run a sequential portfolio simulation over the candidate signal dates."""

    if max_concurrent_positions <= 0:
        raise ValueError("max_concurrent_positions must be positive")

    price_df = price_df.sort_index()
    mtm_iv_by_date = mtm_iv_by_date.sort_index()
    sorted_candidates = sorted(pd.Timestamp(value) for value in candidate_signal_dates)
    skipped_candidates: list[dict] = []
    trade_log: list[dict] = []
    open_positions: list[dict] = []

    mtm_warmup_start = None if mtm_iv_by_date.empty else pd.Timestamp(mtm_iv_by_date.index.min())

    for signal_date in sorted_candidates:
        if mtm_warmup_start is not None and signal_date < mtm_warmup_start:
            skipped_candidates.append(
                {"signal_date": signal_date, "reason": "signal_date precedes IV warm-up window"}
            )
            continue

        entry_candidates = price_df.index[price_df.index > signal_date]
        if entry_candidates.empty:
            skipped_candidates.append(
                {"signal_date": signal_date, "reason": "no next trading day for entry"}
            )
            continue
        entry_date = pd.Timestamp(entry_candidates[0])

        calendar_target = entry_date + pd.Timedelta(days=dte_days)
        expiry_candidates = price_df.index[price_df.index >= calendar_target]
        if expiry_candidates.empty:
            skipped_candidates.append(
                {"signal_date": signal_date, "reason": "no expiry trading day available"}
            )
            continue
        expiry_date = pd.Timestamp(expiry_candidates[0])

        required_sessions = price_df.index[(price_df.index > entry_date) & (price_df.index <= expiry_date)]
        if not required_sessions.difference(mtm_iv_by_date.index).empty:
            skipped_candidates.append(
                {"signal_date": signal_date, "reason": "mtm_iv_by_date coverage gap"}
            )
            continue

        open_positions = [position for position in open_positions if position["exit_date"] >= entry_date]
        if len(open_positions) >= max_concurrent_positions:
            skipped_candidates.append(
                {"signal_date": signal_date, "reason": "max_concurrent_positions reached"}
            )
            continue

        entry_iv = _historical_vol_as_of(
            price_df,
            signal_date,
            leverage_factor,
            lookback_days=IV_LOOKBACK_DAYS,
        )

        trade_record = simulate_trade(
            price_df=price_df,
            signal_date=signal_date,
            entry_date=entry_date,
            expiry_date=expiry_date,
            entry_iv=entry_iv,
            mtm_iv_by_date=mtm_iv_by_date,
            r=r,
            delta_short_leg=delta_short_leg,
            spread_width_pct=spread_width_pct,
            spread_type=spread_type,
            profit_target_pct=profit_target_pct,
            stop_loss_multiple=stop_loss_multiple,
            strike_increment=strike_increment,
            credit_haircut_pct=credit_haircut_pct,
        )
        trade_log.append(trade_record)
        open_positions.append(
            {
                "signal_date": signal_date,
                "exit_date": pd.Timestamp(trade_record["exit_date"]),
            }
        )

    return {"trade_log": trade_log, "skipped_candidates": skipped_candidates}
