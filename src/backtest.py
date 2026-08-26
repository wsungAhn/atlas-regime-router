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
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indicators as ind  # noqa: E402
from strategies import ALL_STRATEGIES, StrategySignal  # noqa: E402
from portfolio import risk_pct_for_atr_pct, scale_trades_to_dollars  # noqa: E402
from vendor.credit_spread_simulator import price_spread_to_close, run_portfolio_simulation  # noqa: E402
from vendor.iv_approximation import estimate_historical_vol  # noqa: E402

STARTING_EQUITY = 100_000.0

DTE_DAYS = 7  # 주간옵션 — 2026-08-24 실측: 30일 DTE는 회전율이 너무 낮아 연 1.45%뿐
# (25거래/3년, 월 0.7건). 승률 88%로 엣지 자체는 있는데 회전이 병목이라는 걸
# 확인하고 사용자 지시로 단축 테스트. SPY/QQQ는 실제로 주간옵션이 유동성 좋게
# 거래돼 비현실적인 가정 아님.
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

# 백테스트가 매번 Alpaca API를 새로 호출해 라이브 봇(15분 루프)과 레이트리밋을
# 다퉜다(2026-08-26 실측 429) — 과거 봉 데이터는 그날 안에는 바뀌지 않으므로
# 로컬에 캐싱한다. 하루 지나면 최신 봉을 받기 위해 자동 재요청.
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
CACHE_MAX_AGE = timedelta(hours=20)


def _cached(cache_path: Path, fetch_fn) -> pd.DataFrame:
    if cache_path.exists():
        age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
        if age < CACHE_MAX_AGE:
            return pd.read_parquet(cache_path)
    df = fetch_fn()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)
    return df


def fetch_daily_bars(client: StockHistoricalDataClient, symbol: str, years: int = 3) -> pd.DataFrame:
    def _fetch() -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=365 * years + 30),
        )
        bars = client.get_stock_bars(req).df
        df = bars.xs(symbol, level="symbol") if isinstance(bars.index, pd.MultiIndex) else bars
        df.index = pd.DatetimeIndex(df.index.date)  # tz 제거 — 시뮬레이터가 날짜 인덱스 기대
        df = df[~df.index.duplicated(keep="last")]
        return df[["open", "high", "low", "close", "volume"]].sort_index()

    return _cached(CACHE_DIR / f"{symbol}_{years}y_daily.parquet", _fetch)


def fetch_intraday_bars(client: StockHistoricalDataClient, symbol: str, years: int = 3) -> pd.DataFrame:
    """일봉 백테스트가 이미 결정한 거래(진입일/만기일/신용액)를 15분봉으로
    재평가하기 위한 원자재 — vendor 엔진(하루 1회 정산, IV 연율화가 "행=거래일"을
    상수로 가정)은 안 건드리고 그 위에 얹는 오버레이용. 2026-08-24: 사용자가
    "루프가 15분마다 도는데 백테스트는 왜 일봉이냐"고 정확히 지적 — 진입판정
    자체는 일봉 레짐(스윙전략, ADX14/EMA20·50)이라 15분봉으로 바꾸면 다른
    전략이 되지만(오늘 검증한 챔피언 무효화), 청산감시·서킷브레이커는 15분
    단위로 실제 반응해야 한다는 지적은 맞다 — 그 부분만 이 함수로 보강."""
    def _fetch() -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=datetime.now(timezone.utc) - timedelta(days=365 * years + 30),
        )
        bars = client.get_stock_bars(req).df
        df = bars.xs(symbol, level="symbol") if isinstance(bars.index, pd.MultiIndex) else bars
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[~df.index.duplicated(keep="last")]
        return df[["close"]].sort_index()

    return _cached(CACHE_DIR / f"{symbol}_{years}y_15min.parquet", _fetch)


def reprice_exits_intraday(
    trades: list[dict], intraday_bars: dict[str, pd.DataFrame],
    mtm_iv_by_symbol: dict[str, pd.Series], r: float = RISK_FREE_RATE,
) -> list[dict]:
    """이미 완료된 일봉 거래로그를 15분봉으로 재평가 — 원래 vendor 엔진이 "그날
    종가"로만 확인하던 익절/손절을 그 안에서 실제로 몇 시 몇 분에 걸렸을지로
    당긴다(항상 같거나 더 빠른 청산 — 절대 더 늦어지지 않음).

    IV는 그날의 mtm_iv_by_date 값을 그대로 쓴다(장중 갱신은 안 함 — 근사, 하루
    안에서는 유지). **2026-08-24 발견한 버그를 고친 것** — 처음엔 진입일의
    entry_iv를 거래 내내 고정으로 썼는데, vendor의 원래 일봉 엔진은 매일
    mtm_iv_by_date로 IV를 갱신한다. 이 불일치 때문에 vendor가 실제로는 손절로
    판정한 거래가 재평가에서 하루 전 시점에 익절로 뒤집히는 등(같은 날짜인데
    가격모델이 서로 다른 IV를 쓰니 당연히 결과가 어긋남) 가짜 트리거가 대량
    발생했었다(총손익이 3년 $86,820→$219,034로 튀는 걸 보고 역추적해서 발견) —
    반드시 vendor와 같은 mtm_iv_by_date를 써야 "같은 가격모델, 더 잦은 확인"이
    된다.

    원래 exit_date/exit_reason/realized_pnl은 구간 안에서 더 빠른 트리거를 못
    찾으면 그대로 둔다(vendor의 일봉 판정을 신뢰 — 이 함수는 "더 빠른 청산을
    찾으면 당긴다"만 한다, 아예 새로 만들지 않는다)."""
    out = []
    for t in trades:
        symbol = t.get("symbol")
        bars = intraday_bars.get(symbol) if symbol else None
        mtm_iv = mtm_iv_by_symbol.get(symbol) if symbol else None
        if bars is None or bars.empty or mtm_iv is None or mtm_iv.empty:
            out.append(t)
            continue

        entry_date = pd.Timestamp(t["entry_date"])
        original_exit_date = pd.Timestamp(t["exit_date"])
        expiry_date = entry_date + pd.Timedelta(days=int(t["effective_dte_days"]))
        window = bars[(bars.index.normalize() > entry_date) & (bars.index.normalize() <= original_exit_date)]
        if window.empty:
            out.append(t)
            continue

        short_strike, long_strike = t["short_strike"], t["long_strike"]
        width, credit_received = t["width"], t["credit_received"]
        spread_type, entry_iv = t["spread_type"], t["entry_iv"]
        profit_target_pct = t["profit_target_pct"]
        stop_loss_multiple = t.get("stop_loss_multiple")

        triggered = None
        for bar_time, row in window.iterrows():
            S_t = float(row["close"])
            bar_day = bar_time.normalize()
            days_to_expiry = (expiry_date.normalize() - bar_day).days
            iv_t = float(mtm_iv.get(bar_day, entry_iv))
            if days_to_expiry <= 0:
                close_debit = max(0.0, min(width, short_strike - S_t)) if spread_type == "bull_put" \
                    else max(0.0, min(width, S_t - short_strike))
            else:
                close_debit = price_spread_to_close(
                    S_t=S_t, short_strike=short_strike, long_strike=long_strike,
                    T_remaining=days_to_expiry / 365.0, r=r, iv_t=iv_t, spread_type=spread_type,
                )
            current_pnl = credit_received - close_debit
            if current_pnl >= credit_received * profit_target_pct:
                triggered = (bar_time, "profit_target_intraday", close_debit)
                break
            if stop_loss_multiple is not None and close_debit >= credit_received * stop_loss_multiple:
                triggered = (bar_time, "stop_loss_intraday", close_debit)
                break

        if triggered is None:
            out.append(t)
            continue
        exit_time, exit_reason, close_debit = triggered
        new_t = dict(t)
        new_t["exit_date"] = exit_time
        new_t["exit_reason"] = exit_reason
        new_t["close_debit"] = close_debit
        new_t["realized_pnl"] = credit_received - close_debit
        out.append(new_t)
    return out


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
    weight(래더 레벨 가중치, 비래더 전략은 1.0)를 각 거래 dict에 태깅한다.

    max_concurrent_positions=1 — 라이브(mcp_runner.py의 _symbols_with_open_
    exposure 가드)가 종목당 동시보유 1개로 제한하는 것과 맞춘다. 원래 3으로
    돌리고 있었는데, 이러면 레짐이 며칠 지속될 때 백테스트가 동시에 최대 3개
    포지션을 겹쳐 쌓아 실제 라이브보다 더 많은 거래·더 큰 익스포저를 낸다
    (사용자가 "실제보다 더 많은 거래가 일어날 수 있는 거 아니냐"고 정확히
    지적해서 발견). 래더(전략2/3)는 레벨별로 별도 그룹 시뮬레이션이라 레벨1·2·3이
    각자 1개씩 동시보유는 가능(래더의 설계 의도 자체가 다단 동시진입이라 여기는
    예외) — 콘도르·단일스프레드 전략(1,4,5,6,7)만 "정확히 라이브와 동일"해진다."""
    if not dates:
        return []
    result = run_portfolio_simulation(
        price_df=df, mtm_iv_by_date=mtm_iv, leverage_factor=1.0,
        candidate_signal_dates=dates, dte_days=DTE_DAYS, r=RISK_FREE_RATE,
        delta_short_leg=DELTA_SHORT_LEG, spread_width_pct=SPREAD_WIDTH_PCT,
        spread_type=spread_type, profit_target_pct=PROFIT_TARGET_PCT,
        stop_loss_multiple=STOP_LOSS_MULTIPLE, strike_increment=STRIKE_INCREMENT,
        credit_haircut_pct=CREDIT_HAIRCUT_PCT, max_concurrent_positions=1,
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


def _early_intraday_close_series(intraday_df: pd.DataFrame) -> pd.Series:
    """그날 첫 15분봉 종가(대략 9:30~9:45 ET) — 라이브가 장 시작 직후 첫
    진입을 시도할 때의 가격 근사. **2026-08-25 사용자 지적**: 일봉전략
    백테스트는 원래 "신호 다음날 종가"로 진입가를 잡는데, 라이브는 레짐
    판정만 일봉이고 실제 체결가는 15분마다 그 순간의 실시간 호가로 잡는다
    — 이 함수는 그 진입가 기준 차이(종가 vs 장초반가)의 실제 성과 영향을
    측정하기 위한 것."""
    if intraday_df.empty:
        return pd.Series(dtype=float)
    return intraday_df.groupby(intraday_df.index.normalize())["close"].first()


def _substitute_close_with_intraday(df: pd.DataFrame, early_close: pd.Series) -> pd.DataFrame:
    """일봉 df의 'close' 열을 그날 장초반가로 치환한 사본 — 레짐판정에는 안
    쓰고(진짜 일봉 df 그대로 유지) 시뮬레이션(진입가·이후 MTM)에만 쓴다."""
    out = df.copy()
    aligned = early_close.reindex(out.index)
    out["close"] = aligned.combine_first(out["close"])
    return out


def _generate_raw_trades(
    name: str, df: pd.DataFrame, mtm_iv: pd.Series, risk_pct_series: pd.Series,
    sim_df: pd.DataFrame | None = None,
) -> list[dict]:
    """계약당 raw 거래(달러 사이징 전) — 종목별 리포트와 다종목 합산 포트폴리오
    둘 다 이 함수를 공유한다(사이징만 나중에 갈린다).

    sim_df를 주면 레짐판정(signal_fn)은 df(진짜 일봉)로 그대로 하되, 실제
    가격 시뮬레이션(진입가·MTM)은 sim_df로 한다 — 레짐=일봉, 체결가=장초반
    실시간가라는 라이브 동작을 백테스트에 반영하기 위함(2026-08-25)."""
    sim_df = sim_df if sim_df is not None else df
    signal_fn = ALL_STRATEGIES[name]
    signals: list[StrategySignal] = signal_fn(df)
    if not signals:
        return []

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
        trades = _run_side(sim_df, mtm_iv, dates, side, weight_by_level[level])
        for t in trades:
            t["risk_pct"] = float(risk_pct_series.get(t["entry_date"], risk_pct_series.iloc[-1] if len(risk_pct_series) else 0.05))
        raw_trades += trades
    return raw_trades


def backtest_strategy(
    name: str, symbol: str, df: pd.DataFrame, mtm_iv: pd.Series, n_years: float,
    risk_pct_series: pd.Series, intraday_bars: pd.DataFrame | None = None,
) -> StrategyResult | None:
    raw_trades = _generate_raw_trades(name, df, mtm_iv, risk_pct_series)
    if not raw_trades:
        return None
    for t in raw_trades:
        t["symbol"] = symbol
    if intraday_bars is not None:
        raw_trades = reprice_exits_intraday(raw_trades, {symbol: intraday_bars}, {symbol: mtm_iv})
    dollar_trades, equity_series = scale_trades_to_dollars(raw_trades, STARTING_EQUITY)
    result = _metrics_from_dollar_trades(dollar_trades, equity_series, n_years)
    if result is not None:
        result.name = name
    return result


def run_combined_portfolio(
    symbols: tuple[str, ...] = ("SPY", "QQQ", "IWM", "DIA"),
    strategy_name: str = "7_atlas_mvp",
    years: int = 3,
) -> StrategyResult | None:
    """4종목을 각자 $100k 독립계좌로 백테스트하던 것과 달리, **하나의 $100k
    계좌**가 4종목 신호를 전부 같은 리스크예산에서 순서대로 사이징하게 한다
    (진입일자 기준 정렬 → 그 시점의 실제 잔고로 사이징 — scale_trades_to_dollars의
    기존 근사를 종목간에도 그대로 확장). 종목별 합산치가 낙관적이었을 가능성
    (한 계좌를 나눠 쓰면 그만큼 계약수가 줄어든다)을 직접 검증하기 위한 것.

    청산은 15분봉으로 재평가한다(reprice_exits_intraday) — 진입판정(레짐)은
    검증된 일봉 로직 그대로, 청산·서킷브레이커만 라이브와 같은 15분 해상도로
    맞춘다(2026-08-24, 사용자 지시)."""
    import os
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])

    all_raw_trades: list[dict] = []
    n_years = float(years)
    for symbol in symbols:
        df = fetch_daily_bars(client, symbol, years=years)
        mtm_iv = _rolling_iv_series(df)
        risk_pct_series = _risk_pct_series(df)
        n_years = (df.index[-1] - df.index[0]).days / 365.0
        raw_trades = _generate_raw_trades(strategy_name, df, mtm_iv, risk_pct_series)
        for t in raw_trades:
            t["symbol"] = symbol
        intraday_df = fetch_intraday_bars(client, symbol, years=years)
        raw_trades = reprice_exits_intraday(raw_trades, {symbol: intraday_df}, {symbol: mtm_iv})
        all_raw_trades += raw_trades

    if not all_raw_trades:
        return None
    dollar_trades, equity_series = scale_trades_to_dollars(all_raw_trades, STARTING_EQUITY)
    result = _metrics_from_dollar_trades(dollar_trades, equity_series, n_years)
    if result is not None:
        result.name = f"{strategy_name} (combined: {'+'.join(symbols)})"
    return result


def run_combined_portfolio_intraday_entry(
    symbols: tuple[str, ...] = ("SPY", "QQQ", "GLD", "TLT", "SLV", "IWM"),
    strategy_name: str = "7_atlas_mvp",
    years: int = 3,
) -> StrategyResult | None:
    """run_combined_portfolio와 같지만 **진입가·이후 MTM도 15분봉(그날 첫 봉,
    ~9:30-9:45 ET)으로** 시뮬레이션한다 — run_combined_portfolio는 여전히
    "신호 다음날 종가"로 진입한다고 가정하는데, 실제 라이브는 레짐판정만
    일봉이고 체결가는 15분마다 그 순간의 실시간 호가로 잡는다(2026-08-25
    사용자 지적). 레짐판정 자체는 두 함수 다 동일한 진짜 일봉 df를 쓴다 —
    바뀌는 건 시뮬레이션에 먹이는 가격 계열뿐."""
    import os
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])

    all_raw_trades: list[dict] = []
    n_years = float(years)
    for symbol in symbols:
        df = fetch_daily_bars(client, symbol, years=years)
        mtm_iv = _rolling_iv_series(df)
        risk_pct_series = _risk_pct_series(df)
        n_years = (df.index[-1] - df.index[0]).days / 365.0
        intraday_df = fetch_intraday_bars(client, symbol, years=years)
        early_close = _early_intraday_close_series(intraday_df)
        sim_df = _substitute_close_with_intraday(df, early_close)
        raw_trades = _generate_raw_trades(strategy_name, df, mtm_iv, risk_pct_series, sim_df=sim_df)
        for t in raw_trades:
            t["symbol"] = symbol
        raw_trades = reprice_exits_intraday(raw_trades, {symbol: intraday_df}, {symbol: mtm_iv})
        all_raw_trades += raw_trades

    if not all_raw_trades:
        return None
    dollar_trades, equity_series = scale_trades_to_dollars(all_raw_trades, STARTING_EQUITY)
    result = _metrics_from_dollar_trades(dollar_trades, equity_series, n_years)
    if result is not None:
        result.name = f"{strategy_name} (combined, intraday entry: {'+'.join(symbols)})"
    return result


def run_all(symbol: str = "SPY", years: int = 3) -> list[StrategyResult]:
    import os
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    df = fetch_daily_bars(client, symbol, years=years)
    mtm_iv = _rolling_iv_series(df)
    risk_pct_series = _risk_pct_series(df)
    n_years = (df.index[-1] - df.index[0]).days / 365.0
    intraday_df = fetch_intraday_bars(client, symbol, years=years)

    results = []
    for name in ALL_STRATEGIES:
        try:
            r = backtest_strategy(name, symbol, df, mtm_iv, n_years, risk_pct_series, intraday_bars=intraday_df)
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
    # SPY/QQQ 2종목→4종목 확대 — 2026-08-24: 연환산 1.45%가 목표(18%)에 크게
    # 못 미쳐 사용자 지시로 신호 발생빈도 자체를 늘리는 테스트. IWM/DIA는 SPY/QQQ와
    # 같은 성격(대형 지수 ETF, 주간옵션 유동성 충분)이라 전략 로직 변경 없이 그대로
    # 적용 가능.
    for sym in ("SPY", "QQQ", "IWM", "DIA"):
        res = run_all(sym, years=3)
        print_report(res, sym)

    combined = run_combined_portfolio(symbols=("SPY", "QQQ", "IWM", "DIA"), strategy_name="7_atlas_mvp", years=3)
    if combined is not None:
        print_report([combined], "COMBINED (1개 $100k 계좌, 4종목 공유)")
    else:
        print("[COMBINED] no trades")
