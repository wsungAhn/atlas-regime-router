"""backtest.py의 진입가 치환 로직(순수함수) 유닛테스트 — 네트워크 없이."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest import _early_intraday_close_series, _substitute_close_with_intraday  # noqa: E402


def test_early_intraday_close_series_takes_first_bar_of_each_day():
    idx = pd.to_datetime([
        "2026-01-05 09:30", "2026-01-05 09:45", "2026-01-05 10:00",
        "2026-01-06 09:30", "2026-01-06 09:45",
    ])
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0, 200.0, 201.0]}, index=idx)
    series = _early_intraday_close_series(df)
    assert series[pd.Timestamp("2026-01-05")] == 100.0
    assert series[pd.Timestamp("2026-01-06")] == 200.0


def test_substitute_close_with_intraday_replaces_matching_dates_only():
    daily_idx = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    daily_df = pd.DataFrame({"close": [100.0, 200.0, 300.0], "high": [101, 201, 301], "low": [99, 199, 299]}, index=daily_idx)
    early_close = pd.Series({pd.Timestamp("2026-01-05"): 95.0, pd.Timestamp("2026-01-06"): 205.0})

    sim_df = _substitute_close_with_intraday(daily_df, early_close)
    assert sim_df.loc[pd.Timestamp("2026-01-05"), "close"] == 95.0
    assert sim_df.loc[pd.Timestamp("2026-01-06"), "close"] == 205.0
    assert sim_df.loc[pd.Timestamp("2026-01-07"), "close"] == 300.0  # 데이터 없는 날은 원래 종가 유지
    assert daily_df.loc[pd.Timestamp("2026-01-05"), "close"] == 100.0  # 원본 df는 안 건드림
