"""공용 기술지표 — signals.py와 strategies.py가 공유. 전부 순수 함수, pandas 입력."""
from __future__ import annotations

import pandas as pd


def wilder_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    atr = wilder_atr(df, n)
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def vwap_session(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """일봉 기준 근사 VWAP — (H+L+C)/3 가중평균을 **롤링 윈도우**(기본 20일)로 계산.
    실시간 tick VWAP이 아니라 일봉으로 근사(대회 스코프 — 옵션 만기가 30~45일이라
    일중 VWAP 정밀도 차이가 구조 선택에 미치는 영향은 미미하다고 판단).
    최초 구현이 `expanding()`(데이터셋 시작부터 누적)이었던 버그를 수정 — 3년치
    백테스트에서 평균이 장기 고정값에 가까워져 "가격이 VWAP 근처"라는 조건이
    사실상 발동 안 함(전략5 거래 0건으로 실측 발견)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    volume = df.get("volume")
    if volume is None or volume.eq(0).all():
        return typical.rolling(window).mean()
    cum_vol = volume.rolling(window).sum()
    cum_pv = (typical * volume).rolling(window).sum()
    return cum_pv / cum_vol.replace(0, float("nan"))


def bollinger_band_width(df: pd.DataFrame, n: int = 20, k: float = 2.0) -> pd.Series:
    mid = df["close"].rolling(n).mean()
    std = df["close"].rolling(n).std()
    upper, lower = mid + k * std, mid - k * std
    return (upper - lower) / mid


def donchian_high(df: pd.DataFrame, n: int = 20) -> pd.Series:
    return df["high"].rolling(n).max()


def donchian_low(df: pd.DataFrame, n: int = 20) -> pd.Series:
    return df["low"].rolling(n).min()


def percentile_rank(series: pd.Series, value: float) -> float:
    """series 분포에서 value가 몇 퍼센타일인지(0~100). 결측 제거 후 계산."""
    clean = series.dropna()
    if clean.empty:
        return 50.0
    return float((clean < value).mean() * 100)
