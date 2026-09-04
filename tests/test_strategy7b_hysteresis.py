"""전략7b(히스테리시스)가 전략7의 부분집합(신호를 새로 만들지 않고 거르기만
함)인지, 그리고 실제로 하루짜리 경계값 흔들림을 걸러내는지 확인."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from strategies import strategy7_atlas_mvp_signals, strategy7b_atlas_hysteresis_signals  # noqa: E402


def _synthetic_trending_ohlcv(n: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    trend = np.linspace(100, 180, n)
    noise = rng.normal(0, 1.5, n).cumsum() * 0.2
    close = trend + noise
    high = close + rng.uniform(0.2, 1.0, n)
    low = close - rng.uniform(0.2, 1.0, n)
    open_ = close + rng.uniform(-0.5, 0.5, n)
    volume = rng.uniform(1e6, 2e6, n)
    idx = pd.bdate_range("2022-01-03", periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def test_hysteresis_signals_are_a_subset_of_raw_signals():
    df = _synthetic_trending_ohlcv()
    raw = {(s.date, s.spread_type) for s in strategy7_atlas_mvp_signals(df)}
    hyst = {(s.date, s.spread_type) for s in strategy7b_atlas_hysteresis_signals(df, confirm_days=2)}
    assert hyst.issubset(raw)


def test_hysteresis_never_flags_a_day_disagreeing_with_the_day_before():
    """strategy7 only loops from min_history=60 onward, so its output list is
    silent (not "disagreeing") about the one day just before that boundary —
    exclude that single edge case rather than asserting on it."""
    df = _synthetic_trending_ohlcv(seed=1)
    min_history = 60
    raw_by_date = {s.date: s.spread_type for s in strategy7_atlas_mvp_signals(df, min_history=min_history)}
    for s in strategy7b_atlas_hysteresis_signals(df, min_history=min_history, confirm_days=2):
        idx = df.index.get_loc(s.date)
        if idx - 1 < min_history:
            continue
        prev_date = df.index[idx - 1]
        assert raw_by_date.get(prev_date) == s.spread_type


def test_higher_confirm_days_never_emits_more_signals_than_lower():
    df = _synthetic_trending_ohlcv(seed=2)
    n2 = len(strategy7b_atlas_hysteresis_signals(df, confirm_days=2))
    n3 = len(strategy7b_atlas_hysteresis_signals(df, confirm_days=3))
    assert n3 <= n2
