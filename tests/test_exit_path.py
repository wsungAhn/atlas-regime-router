from datetime import date
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.exit_path import legacy_window, run_exit_path, scaled_table

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backtest import reprice_exits_intraday  # noqa: E402


def occ(symbol, expiry, option, strike):
    return f"{symbol}{pd.Timestamp(expiry):%y%m%d}{option}{int(strike * 1000):08d}"


def position(spread_type="bull_put", credit=1.0):
    if spread_type == "bull_put":
        legs = [{"occ": occ("SPY", "2026-01-30", "P", 100), "side": "short"}, {"occ": occ("SPY", "2026-01-30", "P", 95), "side": "long"}]
    else:
        legs = [{"occ": occ("SPY", "2026-01-30", "C", 100), "side": "short"}, {"occ": occ("SPY", "2026-01-30", "C", 105), "side": "long"}]
    return {"legs": legs, "credit": credit}


def bars(values):
    return pd.DataFrame({"close": values}, index=pd.date_range("2026-01-02 09:30", periods=len(values), freq="15min"))


def table(h=0.01):
    return pd.DataFrame({"class": ["index_etf"] * 4, "bucket": ["<0.10", "0.10-0.50", "0.50-2.00", ">2.00"], "h": [h] * 4})


def kwargs(**extra):
    base = dict(expiry=date(2026, 1, 30), entry_iv=0.2, r=0.0, qty=1, start_after=pd.Timestamp("2026-01-01 23:59"), end_at=pd.Timestamp("2026-01-02 10:00"))
    base.update(extra)
    return base


def test_legacy_window_and_boundaries_and_count():
    start, end = legacy_window("2026-01-01", "2026-01-02")
    b = pd.DataFrame({"close": [100, 100, 100]}, index=pd.to_datetime(["2026-01-02 00:00", "2026-01-02 00:15", "2026-01-03 00:00"]))
    result = run_exit_path(position(credit=2.0), b, pd.Series(dtype=float), **kwargs(start_after=start, end_at=end, pt=0.0))
    assert result["exit_ts"] == pd.Timestamp("2026-01-02 00:00")
    assert result["n_polls"] == 1


def test_legacy_mode_parity_with_reprice_exits_intraday():
    b = bars([100, 100, 100])
    iv = pd.Series({pd.Timestamp("2026-01-02"): 0.2})
    trade = {"symbol": "SPY", "entry_date": pd.Timestamp("2026-01-01"), "exit_date": pd.Timestamp("2026-01-02"),
             "effective_dte_days": 29, "short_strike": 100.0, "long_strike": 95.0, "width": 5.0,
             "credit_received": 2.0, "spread_type": "bull_put", "entry_iv": 0.2,
             "profit_target_pct": 0.0, "stop_loss_multiple": 2.0}
    legacy = reprice_exits_intraday([trade], {"SPY": b}, {"SPY": iv}, r=0.0)[0]
    result = run_exit_path(position(credit=2.0), b, iv, **kwargs(pt=0.0))
    assert result["exit_ts"] == legacy["exit_date"]
    assert result["close_debit"] == pytest.approx(legacy["close_debit"])
    assert result["exit_reason"] == legacy["exit_reason"].removesuffix("_intraday")


def test_iv_scale_changes_mark_and_one_is_default():
    b = bars([100])
    base = run_exit_path(position(credit=2.0), b, pd.Series(dtype=float), **kwargs(pt=-100.0, sl=-200.0))
    explicit_one = run_exit_path(position(credit=2.0), b, pd.Series(dtype=float), iv_scale=1.0, **kwargs(pt=-100.0, sl=-200.0))
    higher_iv = run_exit_path(position(credit=2.0), b, pd.Series(dtype=float), iv_scale=2.0, **kwargs(pt=-100.0, sl=-200.0))
    assert explicit_one["trigger_mark_pnl_pct"] == pytest.approx(base["trigger_mark_pnl_pct"])
    assert higher_iv["trigger_mark_pnl_pct"] < base["trigger_mark_pnl_pct"]


def test_legacy_parity_ignores_entry_day_trigger():
    b = pd.DataFrame({"close": [90.0, 100.0]}, index=pd.to_datetime(["2026-01-01 09:30", "2026-01-02 09:30"]))
    iv = pd.Series({pd.Timestamp("2026-01-02"): 0.2})
    trade = {"symbol": "SPY", "entry_date": pd.Timestamp("2026-01-01"), "exit_date": pd.Timestamp("2026-01-02"),
             "effective_dte_days": 29, "short_strike": 100.0, "long_strike": 95.0, "width": 5.0,
             "credit_received": 2.0, "spread_type": "bull_put", "entry_iv": 0.2,
             "profit_target_pct": 0.0, "stop_loss_multiple": 2.0}
    legacy = reprice_exits_intraday([trade], {"SPY": b}, {"SPY": iv}, r=0.0)[0]
    result = run_exit_path(position(credit=2.0), b, iv, **kwargs(
        start_after=legacy_window(trade["entry_date"], trade["exit_date"])[0],
        end_at=legacy_window(trade["entry_date"], trade["exit_date"])[1],
        pt=trade["profit_target_pct"], sl=-(trade["stop_loss_multiple"] - 1),
    ))
    assert legacy["exit_date"] == pd.Timestamp("2026-01-02 09:30")
    assert result["exit_ts"] == legacy["exit_date"]
    assert result["exit_reason"] == legacy["exit_reason"].removesuffix("_intraday")


def test_friction_beta0_same_trigger_beta1_earlier_and_cross_cost():
    b = bars([100, 90, 90])
    legacy = run_exit_path(position(credit=0.03), b, pd.Series(dtype=float), **kwargs(pt=2.0, sl=-0.5))
    friction = {"table": table(0.0225), "beta": 0.0, "h_mult": 1.0, "s": 1.0, "k": 0.0, "rng": None}
    with_friction = run_exit_path(position(credit=0.03), b, pd.Series(dtype=float), friction=friction, **kwargs(pt=2.0, sl=-0.5))
    assert with_friction["exit_ts"] == legacy["exit_ts"]
    assert with_friction["realized_pnl"] == pytest.approx(legacy["realized_pnl"] - 2 * 0.0225 * 100)


def test_profit_precedes_stop_and_no_trigger():
    b = bars([100])
    result = run_exit_path(position(credit=2.0), b, pd.Series(dtype=float), **kwargs(pt=0.0, sl=0.0))
    assert result["exit_reason"] == "profit_target"
    assert run_exit_path(position(), b, pd.Series(dtype=float), **kwargs(pt=2.0, sl=-2.0)) is None


def test_start_is_exclusive_end_is_inclusive_and_iv_fallback_is_used():
    start = pd.Timestamp("2026-01-02 09:30")
    end = pd.Timestamp("2026-01-02 10:00")
    b = pd.DataFrame({"close": [90, 100, 100]}, index=pd.to_datetime([start, "2026-01-02 09:45", end]))
    result = run_exit_path(position(credit=2.0), b, pd.Series(dtype=float), **kwargs(start_after=start, end_at=end, sl=-0.5, pt=2.0))
    assert result is None
    b_end = pd.DataFrame({"close": [90]}, index=pd.to_datetime([end]))
    result = run_exit_path(position(credit=2.0), b_end, pd.Series(dtype=float), **kwargs(start_after=start, end_at=end, sl=-0.5, pt=2.0))
    assert result["exit_ts"] == end


def test_beta_one_can_trigger_first_and_h_mult_scales_crossing_cost():
    b = bars([100, 90])
    common = kwargs(pt=2.0, sl=-0.5)
    legacy = run_exit_path(position(credit=1.0), b, pd.Series(dtype=float), **{**common, "sl": -1.0})
    f0 = {"table": table(0.75), "beta": 0.0, "h_mult": 1.0, "s": 1.0, "k": 0.0, "rng": None}
    f1 = {**f0, "beta": 1.0}
    f15 = {**f0, "h_mult": 1.5}
    beta0 = run_exit_path(position(credit=1.0), b, pd.Series(dtype=float), friction=f0, **{**common, "sl": -1.0})
    beta1 = run_exit_path(position(credit=1.0), b, pd.Series(dtype=float), friction=f1, **{**common, "sl": -1.0})
    wide = run_exit_path(position(credit=1.0), b, pd.Series(dtype=float), friction=f15, **{**common, "sl": -1.0})
    assert beta0["exit_ts"] == legacy["exit_ts"]
    assert beta1["exit_ts"] < legacy["exit_ts"]
    assert wide["realized_pnl"] == pytest.approx(beta0["realized_pnl"] - 0.5 * 2 * 0.75 * 100)


def test_equal_thresholds_choose_profit_and_stop_sign_is_negative():
    b = pd.DataFrame({"close": [99]}, index=pd.to_datetime(["2026-01-30 09:30"]))
    equal = run_exit_path(position(credit=1.0), b, pd.Series(dtype=float), **kwargs(expiry=date(2026, 1, 30), pt=0.0, sl=0.0, end_at=pd.Timestamp("2026-01-30 10:00")))
    assert equal["exit_reason"] == "profit_target"
    stop = pd.DataFrame({"close": [94]}, index=pd.to_datetime(["2026-01-30 09:30"]))
    result = run_exit_path(position(credit=1.0), stop, pd.Series(dtype=float), **kwargs(expiry=date(2026, 1, 30), pt=2.0, sl=-1.0, end_at=pd.Timestamp("2026-01-30 10:00")))
    assert result["exit_reason"] == "stop_loss"


def test_validation_errors_and_scaled_table():
    with pytest.raises(ValueError, match="4-leg"):
        run_exit_path({"legs": position()["legs"] * 2, "credit": 1}, bars([100]), pd.Series(dtype=float), **kwargs())
    with pytest.raises(ValueError):
        run_exit_path(position(), bars([100]), pd.Series(dtype=float), policy="V1", **kwargs())
    bad = {"table": table(), "beta": 0, "h_mult": 1, "s": 0.5, "k": 0, "rng": None}
    with pytest.raises(ValueError):
        run_exit_path(position(), bars([100]), pd.Series(dtype=float), friction=bad, **kwargs())
    original = table()
    scaled = scaled_table(original, 1.5)
    assert np.allclose(scaled["h"], original["h"] * 1.5)
    assert np.allclose(original["h"], 0.01)


def test_expiry_uses_intrinsic_value():
    b = pd.DataFrame({"close": [94]}, index=pd.to_datetime(["2026-01-30 09:30"]))
    result = run_exit_path(position(), b, pd.Series(dtype=float), **kwargs(expiry=date(2026, 1, 30), pt=2.0, sl=-1.0, end_at=pd.Timestamp("2026-01-30 10:00")))
    assert result["close_debit"] == pytest.approx(5.0)
