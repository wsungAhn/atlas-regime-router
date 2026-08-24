"""
7개 전략 백테스트 러너 — vendor(AlphaBot R1-B) Black-Scholes 시뮬레이터 재사용.

각 전략의 신호(strategies.py)를 받아 run_portfolio_simulation으로 실제 스프레드
손익을 시뮬레이션한다. iron_condor 신호는 풋사이드+콜사이드를 각각 독립 시뮬레이션
(원논문 §5.5도 두 사이드를 별도 청산관리하는 걸 전제로 함) 후 손익을 합산한다.

IV는 실제 옵션체인 과거자료 대신 realized vol 근사(vendor/iv_approximation)를
쓴다 — 대회 8일 예산 안에서 다년간 옵션체인 이력을 구하는 게 비현실적이라
AlphaBot R1-B가 이미 검증한 근사식을 그대로 재사용(레버리지 배수는 SPY/QQQ라
1.0 고정).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

sys.path.insert(0, str(Path(__file__).resolve().parent))
from strategies import ALL_STRATEGIES, StrategySignal  # noqa: E402
from vendor.credit_spread_simulator import run_portfolio_simulation  # noqa: E402
from vendor.iv_approximation import estimate_historical_vol  # noqa: E402

DTE_DAYS = 30
RISK_FREE_RATE = 0.045
DELTA_SHORT_LEG = 0.20
SPREAD_WIDTH_PCT = 0.05
PROFIT_TARGET_PCT = 0.5
STOP_LOSS_MULTIPLE = 2.0
STRIKE_INCREMENT = 0.5
CREDIT_HAIRCUT_PCT = 0.05
IV_LOOKBACK_DAYS = 252


def fetch_daily_bars(client: StockHistoricalDataClient, symbol: str, years: int = 3) -> pd.DataFrame:
    req = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=365 * years + 30),
    )
    bars = client.get_stock_bars(req).df
    df = bars.xs(symbol, level="symbol") if isinstance(bars.index, pd.MultiIndex) else bars
    df.index = pd.DatetimeIndex(df.index.date)  # tz 제거 — 시뮬레이터가 날짜 인덱스 기대
    df = df[~df.index.duplicated(keep="last")]
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def _rolling_iv_series(df: pd.DataFrame, lookback: int = IV_LOOKBACK_DAYS) -> pd.Series:
    values = {}
    for i in range(lookback, len(df)):
        window = df.iloc[i - lookback:i]
        try:
            values[df.index[i]] = estimate_historical_vol(window, lookback_days=lookback)
        except ValueError:
            continue
    return pd.Series(values).sort_index()


@dataclass
class StrategyResult:
    name: str
    n_trades: int
    total_pnl: float
    win_rate: float
    max_drawdown: float
    sharpe: float
    calmar: float
    profit_factor: float


def _run_side(
    df: pd.DataFrame, mtm_iv: pd.Series, dates: list[pd.Timestamp], spread_type: str,
) -> list[dict]:
    if not dates:
        return []
    result = run_portfolio_simulation(
        price_df=df, mtm_iv_by_date=mtm_iv, leverage_factor=1.0,
        candidate_signal_dates=dates, dte_days=DTE_DAYS, r=RISK_FREE_RATE,
        delta_short_leg=DELTA_SHORT_LEG, spread_width_pct=SPREAD_WIDTH_PCT,
        spread_type=spread_type, profit_target_pct=PROFIT_TARGET_PCT,
        stop_loss_multiple=STOP_LOSS_MULTIPLE, strike_increment=STRIKE_INCREMENT,
        credit_haircut_pct=CREDIT_HAIRCUT_PCT, max_concurrent_positions=3,
    )
    return result["trade_log"]


def _metrics_from_trades(trades: list[dict], n_years: float) -> StrategyResult | None:
    if not trades:
        return None
    pnls = np.array([t["realized_pnl"] for t in trades])
    wins = pnls[pnls > 0]
    total_pnl = float(pnls.sum())
    win_rate = float(len(wins) / len(pnls)) if len(pnls) else 0.0
    equity_curve = np.cumsum(pnls)
    running_max = np.maximum.accumulate(equity_curve)
    drawdowns = running_max - equity_curve
    max_dd = float(drawdowns.max()) if len(drawdowns) else 0.0
    daily_like_returns = pnls  # per-trade returns as proxy (거래빈도가 낮아 일별 재구성 대신 거래단위)
    sharpe = float(daily_like_returns.mean() / daily_like_returns.std() * np.sqrt(len(pnls) / max(n_years, 0.1))) if daily_like_returns.std() > 0 else 0.0
    cagr_like = total_pnl / max(n_years, 0.1)
    calmar = float(cagr_like / max_dd) if max_dd > 0 else float("inf") if total_pnl > 0 else 0.0
    gross_profit = float(pnls[pnls > 0].sum())
    gross_loss = float(-pnls[pnls < 0].sum())
    profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
    return StrategyResult(
        name="", n_trades=len(pnls), total_pnl=total_pnl, win_rate=win_rate,
        max_drawdown=max_dd, sharpe=sharpe, calmar=calmar, profit_factor=profit_factor,
    )


def backtest_strategy(name: str, df: pd.DataFrame, mtm_iv: pd.Series, n_years: float) -> StrategyResult | None:
    signal_fn = ALL_STRATEGIES[name]
    signals: list[StrategySignal] = signal_fn(df)
    if not signals:
        return None

    put_dates = [s.date for s in signals if s.spread_type in ("bull_put", "iron_condor")]
    call_dates = [s.date for s in signals if s.spread_type in ("bear_call", "iron_condor")]

    trades = _run_side(df, mtm_iv, put_dates, "bull_put") + _run_side(df, mtm_iv, call_dates, "bear_call")
    result = _metrics_from_trades(trades, n_years)
    if result is not None:
        result.name = name
    return result


def run_all(symbol: str = "SPY", years: int = 3) -> list[StrategyResult]:
    import os
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    df = fetch_daily_bars(client, symbol, years=years)
    mtm_iv = _rolling_iv_series(df)
    n_years = (df.index[-1] - df.index[0]).days / 365.0

    results = []
    for name in ALL_STRATEGIES:
        try:
            r = backtest_strategy(name, df, mtm_iv, n_years)
        except Exception as exc:  # noqa: BLE001 — 백테스트 스캔이라 한 전략 실패가 나머지를 막으면 안 됨
            print(f"[{name}] FAILED: {exc}")
            continue
        if r is None:
            print(f"[{name}] no trades")
            continue
        results.append(r)
    return results


def print_report(results: list[StrategyResult], symbol: str) -> None:
    print(f"\n=== {symbol} — 7전략 백테스트 결과 ===")
    print(f"{'전략':<28}{'거래수':>6}{'총손익':>12}{'승률':>8}{'MDD':>10}{'Sharpe':>8}{'Calmar':>8}{'PF':>8}")
    for r in sorted(results, key=lambda x: x.total_pnl, reverse=True):
        print(
            f"{r.name:<28}{r.n_trades:>6}{r.total_pnl:>12.2f}{r.win_rate:>8.1%}"
            f"{r.max_drawdown:>10.2f}{r.sharpe:>8.2f}{r.calmar:>8.2f}{r.profit_factor:>8.2f}"
        )


if __name__ == "__main__":
    for sym in ("SPY", "QQQ"):
        res = run_all(sym, years=3)
        print_report(res, sym)
