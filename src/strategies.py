"""
7개 전략 신호생성기 — 전부 순수 함수(가격 DataFrame → 진입일자 리스트).
실행(주문)과 분리돼 있어 vendor/credit_spread_simulator.py의 run_portfolio_simulation
으로 그대로 백테스트 가능하다.

1~5는 사용자가 준 ATR 그리드 논문(competition_one_page_atr_options_ai_writeup.md /
atr_grid_options_trading_competition_paper.md)의 5개 전략 — 원문의 3단 래더(j=1,2,3 ATR)
대신 **레짐당 신호 1개**로 단순화했다(8일 빌드 예산 + 백테스트 시뮬레이터가 날짜당
1포지션을 가정하는 구조라 래더를 그대로 넣으면 시뮬레이터 자체를 다시 짜야 함 —
# ponytail: 래더 다단진입은 신호품질부터 검증한 다음 확장).
6은 AlphaBot R1-B 원 전략(find_entry_signals) 그대로 재사용.
7은 오늘 만든 Atlas MVP(ADX/EMA 2레짐).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

import indicators as ind
from vendor.credit_spread_simulator import find_entry_signals


@dataclass
class StrategySignal:
    date: pd.Timestamp
    spread_type: str  # "bull_put" | "bear_call" | "iron_condor"(=둘 다) | "debit"(미시뮬 대상)


def _common_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["atr"] = ind.wilder_atr(df)
    out["adx"] = ind.adx(df)
    out["ema20"] = ind.ema(df["close"], 20)
    out["ema50"] = ind.ema(df["close"], 50)
    out["rsi"] = ind.rsi(df)
    out["vwap"] = ind.vwap_session(df)
    out["bbw"] = ind.bollinger_band_width(df)
    out["donchian_high20"] = ind.donchian_high(df, 20)
    out["donchian_low20"] = ind.donchian_low(df, 20)
    return out


# ── 전략 1: 횡보형 양방향 크레딧 그리드 (아이언 콘도르) ──
def strategy1_range_condor_signals(df: pd.DataFrame, min_history: int = 60) -> list[StrategySignal]:
    d = _common_indicators(df)
    out = []
    for i in range(min_history, len(d)):
        row = d.iloc[i]
        if pd.isna(row["adx"]) or pd.isna(row["atr"]):
            continue
        if row["adx"] < 20 and abs(row["close"] - d["close"].iloc[i - 20:i].mean()) < 0.5 * row["atr"]:
            out.append(StrategySignal(d.index[i], "iron_condor"))
    return out


# ── 전략 2/3: 추세 방향 크레딧 스프레드 그리드 ──
def strategy23_trend_credit_signals(df: pd.DataFrame, min_history: int = 60) -> list[StrategySignal]:
    d = _common_indicators(df)
    out = []
    for i in range(min_history, len(d)):
        row = d.iloc[i]
        if pd.isna(row["adx"]) or pd.isna(row["ema20"]) or pd.isna(row["atr"]):
            continue
        swing_high = d["high"].iloc[max(0, i - 20):i + 1].max()
        swing_low = d["low"].iloc[max(0, i - 20):i + 1].min()
        if row["ema20"] > row["ema50"] and row["adx"] > 20:
            # 상승추세 풋 크레딧: 스윙고점에서 1~3 ATR 조정 시
            pullback = swing_high - row["close"]
            if row["atr"] > 0 and 1.0 * row["atr"] <= pullback <= 3.0 * row["atr"]:
                out.append(StrategySignal(d.index[i], "bull_put"))
        elif row["ema20"] < row["ema50"] and row["adx"] > 20:
            bounce = row["close"] - swing_low
            if row["atr"] > 0 and 1.0 * row["atr"] <= bounce <= 3.0 * row["atr"]:
                out.append(StrategySignal(d.index[i], "bear_call"))
    return out


# ── 전략 4: 변동성 수축 후 돌파 (원논문은 디빗 스프레드 — 이 백테스트 엔진은
#    크레딧 스프레드 전용이라 방향만 맞춰 반대 스프레드로 근사: 상승돌파→풋 크레딧
#    (약세베팅 아님, 방향성 신용포지션으로 대체), 하락돌파→콜 크레딧.
#    # ponytail: 진짜 디빗 스프레드 시뮬레이터는 vendor 엔진 확장 필요, 대회 스코프서 보류) ──
def strategy4_breakout_signals(df: pd.DataFrame, min_history: int = 60) -> list[StrategySignal]:
    d = _common_indicators(df)
    out = []
    for i in range(min_history, len(d)):
        row = d.iloc[i]
        if pd.isna(row["bbw"]) or pd.isna(row["donchian_high20"]):
            continue
        atr_pct = row["atr"] / row["close"] if row["close"] else 0
        atr_pct_hist = (d["atr"].iloc[:i + 1] / d["close"].iloc[:i + 1]).dropna()
        bbw_hist = d["bbw"].iloc[:i + 1].dropna()
        if atr_pct_hist.empty or bbw_hist.empty:
            continue
        compressed = (
            ind.percentile_rank(atr_pct_hist, atr_pct) < 20
            and ind.percentile_rank(bbw_hist, row["bbw"]) < 20
        )
        if not compressed:
            continue
        if row["close"] > d["high"].iloc[max(0, i - 20):i].max():
            out.append(StrategySignal(d.index[i], "bull_put"))
        elif row["close"] < d["low"].iloc[max(0, i - 20):i].min():
            out.append(StrategySignal(d.index[i], "bear_call"))
    return out


# ── 전략 5: 평균회귀 아이언 콘도르 리셋 그리드 ──
def strategy5_mean_reversion_condor_signals(df: pd.DataFrame, min_history: int = 60) -> list[StrategySignal]:
    d = _common_indicators(df)
    out = []
    for i in range(min_history, len(d)):
        row = d.iloc[i]
        if pd.isna(row["adx"]) or pd.isna(row["vwap"]) or pd.isna(row["rsi"]) or row["atr"] == 0:
            continue
        if row["adx"] < 18 and abs(row["close"] - row["vwap"]) / row["atr"] < 1.0 and 40 < row["rsi"] < 60:
            out.append(StrategySignal(d.index[i], "iron_condor"))
    return out


# ── 전략 6: AlphaBot R1-B 원 전략 (그대로 재사용) ──
def strategy6_alphabot_pullback_signals(
    df: pd.DataFrame,
    entry_trigger_drawdown_pct: float = 0.05,
    lookback_days: int = 20,
    atr_threshold_pct: float = 1.30,
) -> list[StrategySignal]:
    dates = find_entry_signals(
        df, entry_trigger_drawdown_pct=entry_trigger_drawdown_pct,
        lookback_days=lookback_days, atr_filter=True,
        atr_threshold_pct=atr_threshold_pct,
    )
    return [StrategySignal(dt, "bull_put") for dt in dates]


# ── 전략 7: Atlas MVP (ADX/EMA 2레짐, 오늘 빌드) ──
def strategy7_atlas_mvp_signals(df: pd.DataFrame, min_history: int = 60) -> list[StrategySignal]:
    d = _common_indicators(df)
    out = []
    for i in range(min_history, len(d)):
        row = d.iloc[i]
        if pd.isna(row["adx"]):
            continue
        if row["adx"] < 18:
            out.append(StrategySignal(d.index[i], "iron_condor"))
        elif row["adx"] > 20 and row["ema20"] > row["ema50"]:
            out.append(StrategySignal(d.index[i], "bull_put"))
        elif row["adx"] > 20 and row["ema20"] < row["ema50"]:
            out.append(StrategySignal(d.index[i], "bear_call"))
    return out


ALL_STRATEGIES = {
    "1_range_condor": strategy1_range_condor_signals,
    "2_3_trend_credit": strategy23_trend_credit_signals,
    "4_breakout": strategy4_breakout_signals,
    "5_mean_reversion_condor": strategy5_mean_reversion_condor_signals,
    "6_alphabot_pullback": strategy6_alphabot_pullback_signals,
    "7_atlas_mvp": strategy7_atlas_mvp_signals,
}
