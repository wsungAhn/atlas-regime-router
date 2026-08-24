"""Black-Scholes IV approximation from realized volatility.

브리프의 간단한 근사식만 구현한다:
IV = historical_vol(252d) * sqrt(leverage_factor)

원본(AlphaBot research/iv_approximation.py)에서 캐시 의존 함수(estimate_iv/
OHLCVCache)는 뺐다 — Atlas는 자체 데이터 소스(alpaca-py)로 직접 DataFrame을
공급하므로 필요 없다. 순수 계산 함수만 재사용.
"""
from __future__ import annotations

import math

import pandas as pd

_TRADING_DAYS_PER_YEAR = 252


def estimate_historical_vol(df: pd.DataFrame, lookback_days: int = 252) -> float:
    """일간 로그수익률 기준 연율화 변동성."""
    if lookback_days < 2:
        raise ValueError("lookback_days must be at least 2")
    if "close" not in df.columns:
        raise ValueError("DataFrame must contain a 'close' column")
    if len(df) < lookback_days:
        raise ValueError(f"need at least {lookback_days} rows, got {len(df)}")

    window = df.tail(lookback_days)
    close = pd.to_numeric(window["close"], errors="coerce")
    if close.isna().any():
        raise ValueError("close column contains non-numeric values")
    if (close <= 0).any():
        raise ValueError("close prices must be positive")

    log_returns = pd.Series(close).apply(math.log).diff().dropna()
    if log_returns.empty:
        raise ValueError("insufficient data to compute returns")

    return float(log_returns.std(ddof=1) * math.sqrt(_TRADING_DAYS_PER_YEAR))
