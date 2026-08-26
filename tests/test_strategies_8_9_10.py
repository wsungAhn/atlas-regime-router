"""신규 전략 8/9/10(일목균형표/골든크로스/EMA+MACD)이 예외 없이 신호를 내는지
확인하는 최소 스모크 테스트. 백테스트 손익 검증은 backtest.py 실행 결과로 별도 확인."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from strategies import (  # noqa: E402
    strategy8_ichimoku_cloud_signals,
    strategy9_golden_cross_signals,
    strategy10_ema_macd_momentum_signals,
)


def _synthetic_trending_ohlcv(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    trend = np.linspace(100, 180, n)
    noise = rng.normal(0, 1.5, n).cumsum() * 0.2
    close = trend + noise
    high = close + rng.uniform(0.2, 1.0, n)
    low = close - rng.uniform(0.2, 1.0, n)
    open_ = close + rng.uniform(-0.5, 0.5, n)
    volume = rng.uniform(1e6, 2e6, n)
    idx = pd.bdate_range("2022-01-03", periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def test_new_strategies_run_and_return_valid_signals():
    df = _synthetic_trending_ohlcv()
    for fn in (strategy8_ichimoku_cloud_signals, strategy9_golden_cross_signals, strategy10_ema_macd_momentum_signals):
        signals = fn(df)
        assert isinstance(signals, list)
        for s in signals:
            assert s.spread_type in ("bull_put", "bear_call")
            assert s.date in df.index


if __name__ == "__main__":
    test_new_strategies_run_and_return_valid_signals()
    print("ok")
