"""
Orchestrator for LEAP / PMCC / Wheel family backtests.

This module wires existing repository data and raw-trade helpers into the
stateful `leap_engine` without modifying the established backtest/vendor flows.
"""
from __future__ import annotations

from typing import Dict, Iterable, Literal

import pandas as pd

from backtest import _generate_raw_trades, _risk_pct_series, _rolling_iv_series, fetch_daily_bars
from leap_engine import (
    LeapBacktestReport,
    calculate_adx_ema_regime,
    run_v1_pmcc,
    run_v2_wheel,
    run_v3_spread_financing,
)

LEAP_UNIVERSE = ("SPY", "QQQ", "GLD", "TLT", "SLV", "IWM")
V1_SYMBOLS = ("SPY", "QQQ")
V2_SYMBOLS = ("SLV", "TLT", "SPY")
V3_SYMBOLS = ("SPY", "QQQ", "IWM")
VariantName = Literal["v1", "v2", "v3", "pmcc", "wheel", "spread_financing"]


def prepare_leap_inputs(
    client,
    symbols: Iterable[str],
    years: int = 3,
) -> tuple[Dict[str, pd.DataFrame], Dict[str, pd.Series], Dict[str, pd.Series]]:
    """Fetch daily bars and derive trailing IV/regime series for the requested symbols."""
    bars_by_symbol: Dict[str, pd.DataFrame] = {}
    iv_by_symbol: Dict[str, pd.Series] = {}
    regime_by_symbol: Dict[str, pd.Series] = {}

    for symbol in symbols:
        df = fetch_daily_bars(client, symbol, years=years)
        bars_by_symbol[symbol] = df
        iv_by_symbol[symbol] = _rolling_iv_series(df)
        regime_by_symbol[symbol] = calculate_adx_ema_regime(df)

    return bars_by_symbol, iv_by_symbol, regime_by_symbol


def generate_v3_raw_trades(
    bars_by_symbol: Dict[str, pd.DataFrame],
    iv_by_symbol: Dict[str, pd.Series],
    strategy_name: str = "7_atlas_mvp",
) -> list[dict]:
    """Generate Strategy 7 raw spread trades and tag each trade with its symbol."""
    raw_trades: list[dict] = []
    for symbol in V3_SYMBOLS:
        if symbol not in bars_by_symbol:
            continue
        df = bars_by_symbol[symbol]
        mtm_iv = iv_by_symbol[symbol]
        risk_pct_series = _risk_pct_series(df)
        for trade in _generate_raw_trades(strategy_name, df, mtm_iv, risk_pct_series):
            tagged = dict(trade)
            tagged["symbol"] = symbol
            raw_trades.append(tagged)
    return raw_trades


def run_leap_family(
    client,
    variant: VariantName,
    years: int = 3,
    starting_cash: float = 30_000.0,
    strategy_name: str = "7_atlas_mvp",
) -> LeapBacktestReport:
    """Run one LEAP-family variant using the repository's existing data pipeline."""
    normalized = variant.lower()
    if normalized in {"v1", "pmcc"}:
        bars, ivs, regimes = prepare_leap_inputs(client, V1_SYMBOLS, years=years)
        return run_v1_pmcc(bars, ivs, regimes, starting_cash=starting_cash)

    if normalized in {"v2", "wheel"}:
        bars, ivs, regimes = prepare_leap_inputs(client, V2_SYMBOLS, years=years)
        return run_v2_wheel(bars, ivs, regimes, starting_cash=starting_cash)

    if normalized in {"v3", "spread_financing"}:
        bars, ivs, regimes = prepare_leap_inputs(client, V3_SYMBOLS, years=years)
        raw_trades = generate_v3_raw_trades(bars, ivs, strategy_name=strategy_name)
        return run_v3_spread_financing(bars, ivs, regimes, raw_trades, starting_cash=starting_cash)

    raise ValueError("variant must be one of: v1, v2, v3, pmcc, wheel, spread_financing")
