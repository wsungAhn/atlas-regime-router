"""
7개 전략 신호생성기 — 전부 순수 함수(가격 DataFrame → 진입일자 리스트).
실행(주문)과 분리돼 있어 vendor/credit_spread_simulator.py의 run_portfolio_simulation
으로 그대로 백테스트 가능하다.

1~5는 사용자가 준 ATR 그리드 논문(competition_one_page_atr_options_ai_writeup.md /
atr_grid_options_trading_competition_paper.md)의 5개 전략.
6은 AlphaBot R1-B 원 전략(find_entry_signals) 그대로 재사용.
7은 오늘 만든 Atlas MVP(ADX/EMA 2레짐).

전략2/3(추세 크레딧)은 원문 §5.2/§5.3의 **3단 래더**를 그대로 구현한다 — 스윙
고점/저점에서 j∈{1,2,3} ATR 떨어진 3개 가격대(G_j)를 각각 독립적으로 감시하다가
가격이 그 레벨에 닿을 때마다(레벨마다 별도 트리거) 진입 신호를 낸다. 리스크
배분은 원문 그대로 R1=0.2·R2=0.3·R3=0.5 (얕은 되돌림일수록 작게, 깊을수록
크게 — 단 원문 규칙대로 G_3는 추세 재확인 없이는 안 나가게 ADX/EMA 조건을
그 시점에도 다시 확인한다). `level`/`weight` 필드로 portfolio.py의 사이징에
그대로 전달된다.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

import indicators as ind
from vendor.credit_spread_simulator import find_entry_signals

LADDER_LEVELS = (1, 2, 3)  # j (ATR 배수)
LADDER_WEIGHTS = {1: 0.20, 2: 0.30, 3: 0.50}  # 원문 §5.2 R1/R2/R3
LADDER_TOLERANCE_ATR = 0.5  # 레벨 근접 판정 허용오차(±0.5 ATR)


@dataclass
class StrategySignal:
    date: pd.Timestamp
    spread_type: str  # "bull_put" | "bear_call" | "iron_condor"(=둘 다) | "debit"(미시뮬 대상)
    level: int = 1
    weight: float = 1.0


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


# ── 전략 2/3: 추세 방향 크레딧 스프레드 3단 래더 ──
def strategy23_trend_credit_signals(df: pd.DataFrame, min_history: int = 60) -> list[StrategySignal]:
    d = _common_indicators(df)
    out = []
    for i in range(min_history, len(d)):
        row = d.iloc[i]
        if pd.isna(row["adx"]) or pd.isna(row["ema20"]) or pd.isna(row["atr"]) or row["atr"] <= 0:
            continue
        swing_high = d["high"].iloc[max(0, i - 20):i + 1].max()
        swing_low = d["low"].iloc[max(0, i - 20):i + 1].min()

        if row["ema20"] > row["ema50"] and row["adx"] > 20:
            # 상승추세: 스윙고점에서 j ATR 되돌림한 레벨 G_j 각각 독립 감시
            for j in LADDER_LEVELS:
                if j == 3 and not (row["ema20"] > row["ema50"] and row["adx"] > 20):
                    continue  # G_3는 추세 재확인 필수(원문 §5.2) — 위에서 이미 확인됐으나 명시
                g_j = swing_high - j * row["atr"]
                if abs(row["close"] - g_j) <= LADDER_TOLERANCE_ATR * row["atr"]:
                    out.append(StrategySignal(d.index[i], "bull_put", level=j, weight=LADDER_WEIGHTS[j]))
        elif row["ema20"] < row["ema50"] and row["adx"] > 20:
            for j in LADDER_LEVELS:
                g_j = swing_low + j * row["atr"]
                if abs(row["close"] - g_j) <= LADDER_TOLERANCE_ATR * row["atr"]:
                    out.append(StrategySignal(d.index[i], "bear_call", level=j, weight=LADDER_WEIGHTS[j]))
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


# ── 전략 8: 일목균형표 구름 돌파 + RSI50 (사용자 제공 PDF "일목균형표+RSI
#    6개 전략" 문서의 전략1을 방향성 크레딧 스프레드로 매핑 — 롱 셋업(원문
#    그대로)은 bull_put, 숏 셋업(원문엔 없음, 이 엔진이 양방향 크레딧을
#    지원해서 대칭 미러링으로 추가)은 bear_call) ──
def strategy8_ichimoku_cloud_signals(df: pd.DataFrame, min_history: int = 60) -> list[StrategySignal]:
    d = _common_indicators(df)
    cloud = ind.ichimoku(df)
    d = d.join(cloud)
    out = []
    for i in range(min_history, len(d)):
        row = d.iloc[i]
        if pd.isna(row["cloud_top"]) or pd.isna(row["rsi"]):
            continue
        prev_close = d["close"].iloc[i - 26] if i >= 26 else float("nan")
        if pd.isna(prev_close):
            continue
        if (
            row["close"] > row["cloud_top"] and row["tenkan"] > row["kijun"]
            and row["rsi"] > 50 and row["close"] > prev_close
        ):
            out.append(StrategySignal(d.index[i], "bull_put"))
        elif (
            row["close"] < row["cloud_bottom"] and row["tenkan"] < row["kijun"]
            and row["rsi"] < 50 and row["close"] < prev_close
        ):
            out.append(StrategySignal(d.index[i], "bear_call"))
    return out


# ── 전략 9: 장기 골든/데드크로스(SMA50/200) + RSI 모멘텀 (사용자 제공 PDF
#    "이동평균선+RSI 6종" 문서의 전략1 — 원문은 롱 온리, 여기서도 숏 사이드는
#    대칭 미러링) ──
def strategy9_golden_cross_signals(df: pd.DataFrame, min_history: int = 200) -> list[StrategySignal]:
    d = _common_indicators(df)
    d["sma50"] = ind.sma(df["close"], 50)
    d["sma200"] = ind.sma(df["close"], 200)
    out = []
    for i in range(min_history, len(d)):
        row = d.iloc[i]
        if pd.isna(row["sma50"]) or pd.isna(row["sma200"]) or pd.isna(row["rsi"]):
            continue
        if row["sma50"] > row["sma200"] and row["rsi"] > 55:
            out.append(StrategySignal(d.index[i], "bull_put"))
        elif row["sma50"] < row["sma200"] and row["rsi"] < 45:
            out.append(StrategySignal(d.index[i], "bear_call"))
    return out


# ── 전략 10: EMA9/20 + MACD + RSI 재가속 모멘텀 (사용자 제공 PDF
#    "이동평균선+RSI 6종" 문서의 전략5 — 원문은 롱 온리, 숏 사이드는 대칭
#    미러링) ──
def strategy10_ema_macd_momentum_signals(df: pd.DataFrame, min_history: int = 200) -> list[StrategySignal]:
    d = _common_indicators(df)
    d["sma200"] = ind.sma(df["close"], 200)
    d["macd_hist"] = ind.macd_hist(df["close"])
    out = []
    for i in range(min_history, len(d)):
        row, prev = d.iloc[i], d.iloc[i - 1]
        if pd.isna(row["sma200"]) or pd.isna(row["macd_hist"]) or pd.isna(row["rsi"]):
            continue
        if (
            row["close"] > row["sma200"] and row["ema20"] > row["ema50"]
            and row["macd_hist"] > 0 and row["macd_hist"] > prev["macd_hist"]
            and 50 <= row["rsi"] <= 65 and row["rsi"] > prev["rsi"]
        ):
            out.append(StrategySignal(d.index[i], "bull_put"))
        elif (
            row["close"] < row["sma200"] and row["ema20"] < row["ema50"]
            and row["macd_hist"] < 0 and row["macd_hist"] < prev["macd_hist"]
            and 35 <= row["rsi"] <= 50 and row["rsi"] < prev["rsi"]
        ):
            out.append(StrategySignal(d.index[i], "bear_call"))
    return out


ALL_STRATEGIES = {
    "1_range_condor": strategy1_range_condor_signals,
    "2_3_trend_credit": strategy23_trend_credit_signals,
    "4_breakout": strategy4_breakout_signals,
    "5_mean_reversion_condor": strategy5_mean_reversion_condor_signals,
    "6_alphabot_pullback": strategy6_alphabot_pullback_signals,
    "7_atlas_mvp": strategy7_atlas_mvp_signals,
    "8_ichimoku_cloud": strategy8_ichimoku_cloud_signals,
    "9_golden_cross": strategy9_golden_cross_signals,
    "10_ema_macd_momentum": strategy10_ema_macd_momentum_signals,
}
