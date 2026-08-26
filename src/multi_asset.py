"""
docs/design-multi-asset-combined-backtest.md 구현 — 옵션+주식+크립토 3슬리브가
하나의 $100k 계좌·하나의 리스크예산을 공유했을 때의 백테스트.

신호는 전 슬리브가 strategies.ALL_STRATEGIES의 같은 일봉 전략함수를 공유한다
(§3 — Casper 미채택, 신규 신호코드 0줄). 옵션 슬리브는 backtest.py의 기존
Black-Scholes 파이프라인을 그대로 쓰고, 주식/크립토 슬리브만 이 모듈에 신규
최소 롱/숏 모델을 추가한다(§4.2). 예산 경합·킬스위치는 portfolio.py의
scale_trades_to_dollars를 3슬리브가 raw trade를 합쳐 1회 호출하는 것으로
공유한다(§4.3) — 별도 배분엔진을 새로 만들지 않는다.

signal_to_weights(vendor/signal_alloc.py)는 넷팅기가 아니라 슬리브별 리스크
예산을 confidence 비율로 쪼개는 정규화기로만 쓴다(§5.1). "signal" 입력은
방향(부호)이 아니라 "그날 그 슬리브가 활성인가"(0|1)로 준다 — 설계 문서 §5.2가
쓴 mean(DIRECTION)은 같은 날 반대방향 신호가 섞이면(예: SPY bull_put +
QQQ bear_call) 평균이 0으로 상쇄돼 실제 진입이 있는데도 그 슬리브 예산이 0으로
잘리는 결함이 있다 — 방향은 이미 각 거래의 direction/spread_type 필드가
개별적으로 담당하므로, 배분 단계의 "signal"은 활성 여부(크기)만 있으면 된다.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone

import pandas as pd

import indicators as ind
from strategies import ALL_STRATEGIES, StrategySignal
from backtest import (
    CACHE_DIR, DTE_DAYS, STARTING_EQUITY, _cached, _early_intraday_close_series,
    _generate_raw_trades, _metrics_from_dollar_trades, _risk_pct_series,
    _rolling_iv_series, _substitute_close_with_intraday, fetch_daily_bars,
    fetch_intraday_bars, reprice_exits_intraday,
)
from backtest import StrategyResult  # noqa: F401 (재노출 — 호출부가 타입힌트로 쓸 수 있게)
from portfolio import scale_trades_to_dollars
from vendor.signal_alloc import signal_to_weights

DIRECTION = {"bull_put": 1, "bear_call": -1, "iron_condor": 1}  # §5.2 — iron_condor는
# 방향성 포지션으로 표현 불가하므로 부호에 의미 없음. 옵션 슬리브는 이 부호를
# 쓰지 않고(아래 signal_activity가 부호 대신 활성여부만 씀), 주식/크립토
# 슬리브만 이 부호로 롱/숏을 정한다.

OPTION_SYMBOLS = ("SPY", "QQQ", "GLD", "TLT", "SLV", "IWM")
EQUITY_SYMBOLS = ("SPY", "QQQ", "IWM")  # SPY/QQQ/IWM은 대차 가능(shortable) 가정 —
# §6.4: 실배선 전 페이퍼계좌 실제 shortable 플래그로 재검증 필요, 백테스트 단계 가정.
CRYPTO_SYMBOLS = ("BTC/USD", "ETH/USD")  # Alpaca 크립토 API는 슬래시 포맷 필수
# (BTCUSD는 400 invalid symbol — 실측 2026-08-26)
CRYPTO_QTY_INCREMENT = 1e-4

STOP_ATR_MULT = 2.0
R_MULTIPLE = 2.0
MAX_HOLD_DAYS = 10


def fetch_crypto_daily_bars(client, symbol: str, years: int = 3) -> pd.DataFrame:
    from alpaca.data.historical.crypto import CryptoHistoricalDataClient  # noqa: F401 (타입 문서화용)
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    def _fetch() -> pd.DataFrame:
        req = CryptoBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=365 * years + 30),
        )
        bars = client.get_crypto_bars(req).df
        df = bars.xs(symbol, level="symbol") if isinstance(bars.index, pd.MultiIndex) else bars
        df.index = pd.DatetimeIndex(df.index.date)
        df = df[~df.index.duplicated(keep="last")]
        return df[["open", "high", "low", "close", "volume"]].sort_index()

    cache_key = symbol.replace("/", "-")
    return _cached(CACHE_DIR / f"{cache_key}_{years}y_daily.parquet", _fetch)


def directional_signals(
    name: str, df: pd.DataFrame, allow_short: bool,
) -> tuple[list[tuple[pd.Timestamp, int]], int]:
    """StrategySignal -> (date, +1|-1) 진입. iron_condor(중립)는 방향성
    포지션으로 표현 불가하므로 주식/크립토 슬리브에서는 진입시키지 않는다.
    allow_short=False면 -1 신호를 억제하고, 억제된 건수를 같이 반환한다
    (크립토 슬리브의 롱 편향이 "전략이 좋아서"인지 "숏을 못 해서"인지
    리포트에서 분리하기 위함 — §6.4)."""
    signal_fn = ALL_STRATEGIES[name]
    signals: list[StrategySignal] = signal_fn(df)
    entries: list[tuple[pd.Timestamp, int]] = []
    suppressed_shorts = 0
    for s in signals:
        if s.spread_type == "iron_condor":
            continue
        direction = DIRECTION[s.spread_type]
        if direction < 0 and not allow_short:
            suppressed_shorts += 1
            continue
        entries.append((s.date, direction))
    return entries, suppressed_shorts


def simulate_directional_trades(
    df: pd.DataFrame, entries: list[tuple[pd.Timestamp, int]], symbol: str, sleeve: str,
    qty_increment: float = 1.0,
) -> list[dict]:
    """§4.2 — 옵션 그릭스 없는 최소 롱/숏 모델. 다음 봉 시가 진입, 이후 봉의
    high/low로 손절(2xATR)/익절(2R) 도달을 확인, MAX_HOLD_DAYS 초과시 종가청산.
    같은 봉에서 손절·익절 둘 다 닿으면 손절 우선(보수적 — 안 정하면 일봉
    백테스트가 조용히 낙관 편향된다).

    반환 dict는 scale_trades_to_dollars가 그대로 먹는 스키마(multiplier=1.0,
    qty_increment로 옵션과 구분).

    동시보유는 종목당 1개(옵션 슬리브의 max_concurrent_positions=1과 동일 규칙).

    # ponytail: 갭·부분체결·수수료·슬리피지 미반영 — 3년 일봉 ETF/메이저코인
    # 1차 근사로 충분하다고 판단, 경계선 결과면 trader의 슬리피지/수수료 공식을
    # 그때 가져온다.
    """
    if not entries or df.empty:
        return []
    atr = ind.wilder_atr(df, 14)
    idx = df.index
    trades: list[dict] = []
    open_until: pd.Timestamp | None = None
    for entry_signal_date, direction in sorted(entries, key=lambda e: e[0]):
        if open_until is not None and entry_signal_date <= open_until:
            continue  # 종목당 동시보유 1개
        pos = idx.searchsorted(entry_signal_date, side="right")
        if pos >= len(idx):
            continue
        entry_date = idx[pos]
        entry_px = float(df["open"].iloc[pos])
        entry_atr = float(atr.iloc[pos - 1]) if pos > 0 and not pd.isna(atr.iloc[pos - 1]) else float(atr.iloc[pos])
        if not entry_atr or math.isnan(entry_atr) or entry_atr <= 0:
            continue
        stop_px = entry_px - direction * STOP_ATR_MULT * entry_atr
        max_loss = abs(entry_px - stop_px)
        target_px = entry_px + direction * STOP_ATR_MULT * R_MULTIPLE * entry_atr

        exit_date, exit_px = None, None
        last_pos = min(pos + MAX_HOLD_DAYS, len(idx) - 1)
        for j in range(pos + 1, last_pos + 1):
            hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
            hit_stop = (lo <= stop_px) if direction > 0 else (hi >= stop_px)
            hit_target = (hi >= target_px) if direction > 0 else (lo <= target_px)
            if hit_stop:  # 손절 우선
                exit_date, exit_px = idx[j], stop_px
                break
            if hit_target:
                exit_date, exit_px = idx[j], target_px
                break
        if exit_date is None:
            exit_date = idx[last_pos]
            exit_px = float(df["close"].iloc[last_pos])

        realized_pnl = (exit_px - entry_px) * direction
        trades.append({
            "entry_date": entry_date, "exit_date": exit_date, "symbol": symbol,
            "sleeve": sleeve, "direction": direction, "max_loss": max_loss,
            "realized_pnl": realized_pnl, "multiplier": 1.0, "qty_increment": qty_increment,
        })
        open_until = exit_date
    return trades


def _signal_activity_and_confidence(
    signals_by_symbol: dict[str, list[StrategySignal]], adx_by_symbol: dict[str, pd.Series],
    date: pd.Timestamp,
) -> tuple[float, float]:
    """그날 이 슬리브의 (활성여부, 평균 확신도). 활성여부는 방향 평균이 아니라
    "신호가 하나라도 있었나"로 준다(모듈 docstring 참고 — 반대방향 신호가
    섞여도 예산이 0으로 상쇄되지 않게). 확신도는 전략7 레짐임계값(ADX 18/20)
    자체를 재사용한다(고정 1.0을 쓰면 signal_to_weights가 '신호 낸 슬리브
    균등분할'로 퇴화)."""
    confidences = []
    active = False
    for symbol, signals in signals_by_symbol.items():
        for s in signals:
            if s.date != date:
                continue
            active = True
            adx = adx_by_symbol[symbol].get(date, float("nan"))
            if pd.isna(adx):
                continue
            if s.spread_type == "iron_condor":
                confidences.append(max(0.0, min(1.0, (18 - adx) / 18)))
            else:
                confidences.append(max(0.0, min(1.0, (adx - 20) / 20)))
    if not active:
        return 0.0, 0.0
    return 1.0, (sum(confidences) / len(confidences) if confidences else 0.0)


def sleeve_scales(weights: dict[str, float]) -> dict[str, float]:
    """|w| 정규화 지분. 한 슬리브만 신호를 내면 그 슬리브가 1.0(=기존 단일
    슬리브 동작과 동일)."""
    total = sum(abs(w) for w in weights.values())
    return {k: (abs(w) / total if total > 0 else 0.0) for k, w in weights.items()}


def run_multi_asset_portfolio(
    sleeves: tuple[str, ...] = ("options", "equity", "crypto"),
    strategy_name: str = "7_atlas_mvp",
    years: int = 3,
) -> StrategyResult | None:
    import os as _os
    from alpaca.data.historical.crypto import CryptoHistoricalDataClient
    from alpaca.data.historical.stock import StockHistoricalDataClient

    stock_client = StockHistoricalDataClient(_os.environ["ALPACA_API_KEY"], _os.environ["ALPACA_SECRET_KEY"])
    crypto_client = CryptoHistoricalDataClient(_os.environ["ALPACA_API_KEY"], _os.environ["ALPACA_SECRET_KEY"])

    # 1개 슬리브만 요청되면(=환원 정합성 검증용) 슬리브간 경합이 원천적으로
    # 없으므로 signal_to_weights/scale 파이프라인을 건너뛰고 scale=1.0을 쓴다
    # — 이래야 옵션 단독 실행이 기존 run_combined_portfolio_intraday_entry와
    # 소수점까지 일치한다(§1 루브릭 게이트).
    single_sleeve = len(sleeves) == 1

    all_raw_trades: list[dict] = []
    per_symbol_data: dict[str, dict] = {}
    n_years = float(years)
    suppressed_shorts_total = 0

    if "options" in sleeves:
        for symbol in OPTION_SYMBOLS:
            df = fetch_daily_bars(stock_client, symbol, years=years)
            mtm_iv = _rolling_iv_series(df)
            risk_pct_series = _risk_pct_series(df)
            n_years = (df.index[-1] - df.index[0]).days / 365.0
            intraday_df = fetch_intraday_bars(stock_client, symbol, years=years)
            # run_combined_portfolio_intraday_entry와 동일하게 진입가/이후 MTM을
            # 장초반 15분봉 종가로 대체한 사본(sim_df)을 시뮬레이션에 먹인다 —
            # 레짐판정(signal_fn)은 df(진짜 일봉) 그대로. 이게 빠지면 진입가가
            # "신호 다음날 종가"로 갈라져 환원 정합성 게이트가 깨진다.
            early_close = _early_intraday_close_series(intraday_df)
            sim_df = _substitute_close_with_intraday(df, early_close)
            raw_trades = _generate_raw_trades(strategy_name, df, mtm_iv, risk_pct_series, sim_df=sim_df)
            for t in raw_trades:
                t["symbol"], t["sleeve"] = symbol, "options"
            raw_trades = reprice_exits_intraday(raw_trades, {symbol: intraday_df}, {symbol: mtm_iv})
            all_raw_trades += raw_trades
            per_symbol_data[f"options:{symbol}"] = {
                "df": df, "signals": ALL_STRATEGIES[strategy_name](df),
                "adx": ind.adx(df), "sleeve": "options",
            }

    if "equity" in sleeves:
        for symbol in EQUITY_SYMBOLS:
            df = fetch_daily_bars(stock_client, symbol, years=years)
            n_years = max(n_years, (df.index[-1] - df.index[0]).days / 365.0)
            entries, suppressed = directional_signals(strategy_name, df, allow_short=True)
            suppressed_shorts_total += suppressed
            raw_trades = simulate_directional_trades(df, entries, symbol, "equity")
            # 옵션 슬리브와 같은 변동성기반 동적 리스크%(2~10%)를 쓴다 — 안 그러면
            # 고정 5%(scale_trades_to_dollars 기본값)가 옵션의 ATR스케일 사이징보다
            # 훨씬 공격적이라 손절 연속발생 시 킬스위치가 즉시 걸려 대부분의 거래가
            # 사이징 단계에서 스킵된다(실측: 3년 140건 중 134건이 킬스위치로 스킵).
            risk_pct_series = _risk_pct_series(df)
            for t in raw_trades:
                t["risk_pct"] = float(risk_pct_series.get(t["entry_date"], risk_pct_series.iloc[-1] if len(risk_pct_series) else 0.05))
            all_raw_trades += raw_trades
            per_symbol_data[f"equity:{symbol}"] = {
                "df": df, "signals": ALL_STRATEGIES[strategy_name](df),
                "adx": ind.adx(df), "sleeve": "equity",
            }

    if "crypto" in sleeves:
        for symbol in CRYPTO_SYMBOLS:
            df = fetch_crypto_daily_bars(crypto_client, symbol, years=years)
            if df.empty:
                continue
            n_years = max(n_years, (df.index[-1] - df.index[0]).days / 365.0)
            entries, suppressed = directional_signals(strategy_name, df, allow_short=False)
            suppressed_shorts_total += suppressed
            raw_trades = simulate_directional_trades(df, entries, symbol, "crypto", CRYPTO_QTY_INCREMENT)
            risk_pct_series = _risk_pct_series(df)
            for t in raw_trades:
                t["risk_pct"] = float(risk_pct_series.get(t["entry_date"], risk_pct_series.iloc[-1] if len(risk_pct_series) else 0.05))
            all_raw_trades += raw_trades
            per_symbol_data[f"crypto:{symbol}"] = {
                "df": df, "signals": ALL_STRATEGIES[strategy_name](df),
                "adx": ind.adx(df), "sleeve": "crypto",
            }

    if not all_raw_trades:
        return None

    if not single_sleeve:
        # §5.4 — 날짜별 슬리브 배분을 미리 계산해 raw trade의 risk_pct에 곱한다.
        all_dates = sorted({t["entry_date"] for t in all_raw_trades})
        by_sleeve_symbols: dict[str, list[str]] = {}
        for key, data in per_symbol_data.items():
            by_sleeve_symbols.setdefault(data["sleeve"], []).append(key)

        scale_by_date_sleeve: dict[tuple[pd.Timestamp, str], float] = {}
        for d in all_dates:
            signal_in, conf_in = {}, {}
            for sleeve in sleeves:
                signals_by_symbol = {
                    k.split(":", 1)[1]: per_symbol_data[k]["signals"]
                    for k in by_sleeve_symbols.get(sleeve, [])
                }
                adx_by_symbol = {
                    k.split(":", 1)[1]: per_symbol_data[k]["adx"]
                    for k in by_sleeve_symbols.get(sleeve, [])
                }
                active, conf = _signal_activity_and_confidence(signals_by_symbol, adx_by_symbol, d)
                signal_in[sleeve] = active
                conf_in[sleeve] = conf
            weights = signal_to_weights(signal_in, conf_in, max_gross=1.0)
            scales = sleeve_scales(weights)
            for sleeve, scale in scales.items():
                scale_by_date_sleeve[(d, sleeve)] = scale

        for t in all_raw_trades:
            scale = scale_by_date_sleeve.get((t["entry_date"], t["sleeve"]), 0.0)
            base_risk_pct = t.get("risk_pct", 0.05)
            t["risk_pct"] = base_risk_pct * scale

    dollar_trades, equity_series = scale_trades_to_dollars(all_raw_trades, STARTING_EQUITY)
    result = _metrics_from_dollar_trades(dollar_trades, equity_series, n_years)
    if result is not None:
        result.name = f"multi_asset({'+'.join(sleeves)}, {strategy_name})"
    return result
