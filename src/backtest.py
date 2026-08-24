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
import indicators as ind  # noqa: E402
from strategies import ALL_STRATEGIES, StrategySignal  # noqa: E402
from portfolio import risk_pct_for_atr_pct, scale_trades_to_dollars  # noqa: E402
from vendor.credit_spread_simulator import run_portfolio_simulation  # noqa: E402
from vendor.iv_approximation import estimate_historical_vol  # noqa: E402

STARTING_EQUITY = 100_000.0

DTE_DAYS = 30
RISK_FREE_RATE = 0.045
DELTA_SHORT_LEG = 0.20
# AlphaBot R1-B 원값(0.05)은 SOXL류 $10~30대 레버리지 ETF 기준이었다 — SPY($630대)에
# 그대로 쓰면 폭 $31.5(계약당 최대손실 ~$3,000)짜리 비현실적으로 넓은 스프레드가
# 나와 리스크예산 대부분을 잡아먹는다(사용자가 "예산부족으로 진입 안 되면 사이징이
# 잘못된 거 아니냐"고 정확히 짚어서 재확인 — 사이징 공식이 아니라 이 폭 파라미터가
# 원인이었음). 실제 SPY/QQQ 크레딧스프레드 관행 폭($1~10)에 맞춰 1.5%로 재보정
# (630*0.015≈$9.45 — 시장 관행 범위 안).
SPREAD_WIDTH_PCT = 0.015
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
    total_pnl: float  # 실제 달러 손익(계약수·승수 반영)
    win_rate: float
    max_drawdown: float  # 달러
    sharpe: float
    calmar: float
    profit_factor: float
    final_equity: float


def _run_side(
    df: pd.DataFrame, mtm_iv: pd.Series, dates: list[pd.Timestamp], spread_type: str, weight: float,
) -> list[dict]:
    """dates에 실제 진입한 raw 거래를 시뮬레이션하고, portfolio.py가 사이징에 쓸
    weight(래더 레벨 가중치, 비래더 전략은 1.0)를 각 거래 dict에 태깅한다."""
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
    trades = result["trade_log"]
    for t in trades:
        t["weight"] = weight
    return trades


def _metrics_from_dollar_trades(
    dollar_trades: list, equity_series: pd.Series, n_years: float,
) -> StrategyResult | None:
    if not dollar_trades:
        return None
    pnls = np.array([dt.dollar_pnl for dt in dollar_trades])
    wins = pnls[pnls > 0]
    total_pnl = float(pnls.sum())
    win_rate = float(len(wins) / len(pnls)) if len(pnls) else 0.0
    equity_curve = equity_series.values if len(equity_series) else np.cumsum(pnls) + STARTING_EQUITY
    running_max = np.maximum.accumulate(equity_curve)
    drawdowns = running_max - equity_curve
    max_dd = float(drawdowns.max()) if len(drawdowns) else 0.0
    per_trade_returns = pnls / STARTING_EQUITY
    sharpe = float(per_trade_returns.mean() / per_trade_returns.std() * np.sqrt(len(pnls) / max(n_years, 0.1))) if per_trade_returns.std() > 0 else 0.0
    cagr_like = (total_pnl / STARTING_EQUITY) / max(n_years, 0.1)
    mdd_pct = max_dd / STARTING_EQUITY
    calmar = float(cagr_like / mdd_pct) if mdd_pct > 0 else float("inf") if total_pnl > 0 else 0.0
    gross_profit = float(pnls[pnls > 0].sum())
    gross_loss = float(-pnls[pnls < 0].sum())
    profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
    final_equity = float(equity_curve[-1]) if len(equity_curve) else STARTING_EQUITY
    return StrategyResult(
        name="", n_trades=len(pnls), total_pnl=total_pnl, win_rate=win_rate,
        max_drawdown=max_dd, sharpe=sharpe, calmar=calmar, profit_factor=profit_factor,
        final_equity=final_equity,
    )


def _risk_pct_series(df: pd.DataFrame, lookback: int = 252) -> pd.Series:
    """날짜별 변동성 기반 리스크%(2~5%) — signals.py 라이브 코드가 쓰는 것과
    동일한 공식(risk_pct_for_atr_pct)을 백테스트에도 적용해 검증된 성과와 실제
    라이브 동작의 사이징 기준을 맞춘다(2026-08-24, 라이브에 이 배선을 넣은 뒤
    백테스트도 같은 조건으로 재실행). **미래정보 누출 금지** — 각 날짜의
    risk_pct는 그 날짜까지의(trailing) lookback일 ATR%만으로 계산한다, 이후
    데이터는 절대 안 본다."""
    atr = ind.wilder_atr(df, 14)
    atr_pct = (atr / df["close"]).dropna()
    values = {}
    for i in range(lookback, len(atr_pct)):
        window = atr_pct.iloc[i - lookback:i + 1]
        current = float(window.iloc[-1])
        low_q = float(window.quantile(0.25))
        high_q = float(window.quantile(0.75))
        values[atr_pct.index[i]] = risk_pct_for_atr_pct(current, low_q, high_q)
    return pd.Series(values).sort_index()


def backtest_strategy(
    name: str, df: pd.DataFrame, mtm_iv: pd.Series, n_years: float, risk_pct_series: pd.Series,
) -> StrategyResult | None:
    signal_fn = ALL_STRATEGIES[name]
    signals: list[StrategySignal] = signal_fn(df)
    if not signals:
        return None

    # (spread_type_side, level) 조합별로 묶어서 각각 run_portfolio_simulation을
    # 돌린다 — 래더 레벨마다 weight가 다르므로 별도 시뮬레이션 후 weight를 태깅.
    groups: dict[tuple[str, int], list[pd.Timestamp]] = {}
    weight_by_level: dict[int, float] = {}
    for s in signals:
        sides = ("bull_put", "bear_call") if s.spread_type == "iron_condor" else (s.spread_type,)
        for side in sides:
            groups.setdefault((side, s.level), []).append(s.date)
            weight_by_level[s.level] = s.weight

    raw_trades: list[dict] = []
    for (side, level), dates in groups.items():
        trades = _run_side(df, mtm_iv, dates, side, weight_by_level[level])
        for t in trades:
            t["risk_pct"] = float(risk_pct_series.get(t["entry_date"], risk_pct_series.iloc[-1] if len(risk_pct_series) else 0.05))
        raw_trades += trades

    dollar_trades, equity_series = scale_trades_to_dollars(raw_trades, STARTING_EQUITY)
    result = _metrics_from_dollar_trades(dollar_trades, equity_series, n_years)
    if result is not None:
        result.name = name
    return result


def run_all(symbol: str = "SPY", years: int = 3) -> list[StrategyResult]:
    import os
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    df = fetch_daily_bars(client, symbol, years=years)
    mtm_iv = _rolling_iv_series(df)
    risk_pct_series = _risk_pct_series(df)
    n_years = (df.index[-1] - df.index[0]).days / 365.0

    results = []
    for name in ALL_STRATEGIES:
        try:
            r = backtest_strategy(name, df, mtm_iv, n_years, risk_pct_series)
        except Exception as exc:  # noqa: BLE001 — 백테스트 스캔이라 한 전략 실패가 나머지를 막으면 안 됨
            print(f"[{name}] FAILED: {exc}")
            continue
        if r is None:
            print(f"[{name}] no trades")
            continue
        results.append(r)
    return results


def print_report(results: list[StrategyResult], symbol: str) -> None:
    print(f"\n=== {symbol} — 7전략 백테스트 결과 (${STARTING_EQUITY:,.0f} 시작, 실제 계약수 반영) ===")
    print(f"{'전략':<28}{'거래수':>6}{'총손익($)':>14}{'수익률':>8}{'승률':>8}{'MDD($)':>12}{'Sharpe':>8}{'Calmar':>8}{'PF':>8}")
    for r in sorted(results, key=lambda x: x.total_pnl, reverse=True):
        ret_pct = r.total_pnl / STARTING_EQUITY
        print(
            f"{r.name:<28}{r.n_trades:>6}{r.total_pnl:>14,.0f}{ret_pct:>8.1%}{r.win_rate:>8.1%}"
            f"{r.max_drawdown:>12,.0f}{r.sharpe:>8.2f}{r.calmar:>8.2f}{r.profit_factor:>8.2f}"
        )


if __name__ == "__main__":
    for sym in ("SPY", "QQQ"):
        res = run_all(sym, years=3)
        print_report(res, sym)
