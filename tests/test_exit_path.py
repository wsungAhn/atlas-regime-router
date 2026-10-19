from datetime import date
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import src.exit_path as exit_path
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


def condor(credit=2.0):
    return {"legs": [
        {"occ": occ("SPY", "2026-01-30", "P", 100), "side": "short"},
        {"occ": occ("SPY", "2026-01-30", "P", 95), "side": "long"},
        {"occ": occ("SPY", "2026-01-30", "C", 110), "side": "short"},
        {"occ": occ("SPY", "2026-01-30", "C", 115), "side": "long"},
    ], "credit": credit}


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
        run_exit_path(position(), bars([100]), pd.Series(dtype=float), policy="V2", **kwargs())
    with pytest.raises(ValueError):
        run_exit_path(position(), bars([100]), pd.Series(dtype=float), policy="V1", **kwargs())
    bad = {"table": table(), "beta": 0, "h_mult": 1, "s": 0.5, "k": 0, "rng": None}
    assert run_exit_path(position(), bars([100]), pd.Series(dtype=float), friction=bad, **kwargs()) is None
    for invalid in (-0.01, 1.01):
        with pytest.raises(ValueError):
            run_exit_path(position(), bars([100]), pd.Series(dtype=float), friction={**bad, "s": invalid}, **kwargs())
    with pytest.raises(ValueError):
        run_exit_path(position(), bars([100]), pd.Series(dtype=float), live_close_path="bad", **kwargs())
    with pytest.raises(ValueError):
        run_exit_path(position(), bars([100]), pd.Series(dtype=float), live_close_path="limit_fallback", **kwargs())
    assert exit_path.s_orats(2) == pytest.approx(0.66)
    assert exit_path.s_orats(4) == pytest.approx(0.56)
    with pytest.raises(ValueError):
        exit_path.s_orats(3)
    original = table()
    scaled = scaled_table(original, 1.5)
    assert np.allclose(scaled["h"], original["h"] * 1.5)
    assert np.allclose(original["h"], 0.01)


def test_expiry_uses_intrinsic_value():
    b = pd.DataFrame({"close": [94]}, index=pd.to_datetime(["2026-01-30 09:30"]))
    result = run_exit_path(position(), b, pd.Series(dtype=float), **kwargs(expiry=date(2026, 1, 30), pt=2.0, sl=-1.0, end_at=pd.Timestamp("2026-01-30 10:00")))
    assert result["close_debit"] == pytest.approx(5.0)


def friction_for(s):
    return {"table": table(0.1), "beta": 0.0, "h_mult": 1.0, "s": s, "k": 0.0, "rng": None}


def test_v1_limit_fill_rule(monkeypatch):
    b = bars([100, 100, 100])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    immediate = run_exit_path(position(), b, pd.Series(dtype=float), policy="V1", friction=friction_for(0.5), **kwargs(pt=0.5))
    assert immediate["close_debit"] == pytest.approx(1.0)
    assert immediate["n_limit_retries"] == 0

    market = run_exit_path(position(), b, pd.Series(dtype=float), policy="V1", friction=friction_for(0.66), **kwargs(pt=0.5))
    assert market["n_limit_retries"] == 3
    assert market["close_debit"] == pytest.approx(1.2)

    mids = iter([[2.0, 1.0], [1.95, 1.05], [2.0, 1.0]])
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: next(mids))
    later = run_exit_path(position(), b, pd.Series(dtype=float), policy="V1", friction=friction_for(0.66), **kwargs(pt=0.5))
    assert later["n_limit_retries"] == 1
    assert later["close_debit"] == pytest.approx(1.0)


def test_v0_limit_fallback_retry_reprices_and_cancels(monkeypatch):
    b = bars([100, 100, 100])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    mids = iter([[2.0, 1.0], [1.8, 1.0], [2.0, 1.0]])
    halves = iter([[0.1, 0.1], [0.01, 0.01], [0.1, 0.1]])
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: next(mids))
    monkeypatch.setattr(exit_path, "leg_halves", lambda *args: next(halves))
    result = run_exit_path(position(), b, pd.Series(dtype=float), live_close_path="limit_fallback",
                           friction=friction_for(0.9), **kwargs(pt=0.5))
    assert result["close_debit"] == pytest.approx(0.84)
    assert result["n_limit_retries"] == 1

    mids = iter([[2.0, 1.0], [2.0, 1.0], [2.0, 1.0]])
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: next(mids))
    halves = iter([[0.1, 0.1], [0.1, 0.1], [0.1, 0.1]])
    monkeypatch.setattr(exit_path, "leg_halves", lambda *args: next(halves))
    marks = iter([1.0, 0.0, 1.0])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
    assert run_exit_path(position(), b, pd.Series(dtype=float), live_close_path="limit_fallback",
                         friction=friction_for(0.9), **kwargs(pt=0.5)) is None


def test_v1_trigger_metadata_and_limit_is_fixed_without_rechecking(monkeypatch):
    b = bars([100, 100, 100])
    marks = iter([1.0, -1.0, -1.0])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    result = run_exit_path(position(), b, pd.Series(dtype=float), policy="V1", friction=friction_for(0.66), **kwargs(pt=0.5))
    assert result["exit_reason"] == "profit_target"
    assert result["trigger_mark_pnl_pct"] == pytest.approx(1.0)
    assert result["exit_ts"] == b.index[2]


def test_limit_window_end_returns_none(monkeypatch):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    assert run_exit_path(position(), bars([100]), pd.Series(dtype=float), policy="V1", friction=friction_for(0.66), **kwargs(pt=0.5)) is None
    assert run_exit_path(position(), bars([100]), pd.Series(dtype=float), live_close_path="limit_fallback",
                         friction=friction_for(0.9), **kwargs(pt=0.5)) is None


def test_v0_limit_fallback_rounds_limit_price(monkeypatch):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [1.83, 1.0])
    result = run_exit_path(position(), bars([100]), pd.Series(dtype=float), live_close_path="limit_fallback",
                           friction=friction_for(0.5), **kwargs(pt=0.5))
    assert result["close_debit"] == pytest.approx(0.87)


def test_v1_limit_window_ignores_trigger_rechecks(monkeypatch):
    marks = iter([1.0, 0.0, 0.0])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    result = run_exit_path(position(), bars([100, 100, 100]), pd.Series(dtype=float), policy="V1",
                           friction=friction_for(0.66), **kwargs(pt=0.5))
    assert result["exit_ts"] == bars([100, 100, 100]).index[2]
    assert result["exit_reason"] == "profit_target"
    assert result["n_limit_retries"] == 3


def test_v0_negative_mid_uses_absolute_markup(monkeypatch):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    monkeypatch.setattr(exit_path, "spread_mid_debit", lambda *args: -2.0)
    monkeypatch.setattr(exit_path, "fill_debit", lambda legs, s: -2.0)
    result = run_exit_path(position(), bars([100]), pd.Series(dtype=float), live_close_path="limit_fallback",
                           friction=friction_for(0.5), **kwargs(pt=0.5))
    assert result["close_debit"] == pytest.approx(-1.9)


def test_v0_first_and_retry_limits_are_rounded(monkeypatch):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    mid_debits = iter([1.231, 1.234])
    monkeypatch.setattr(exit_path, "spread_mid_debit", lambda *args: next(mid_debits))
    fills = iter([99.0, 1.292, 99.0, 1.297])
    monkeypatch.setattr(exit_path, "fill_debit", lambda legs, s: next(fills))
    result = run_exit_path(position(), bars([100, 100]), pd.Series(dtype=float), live_close_path="limit_fallback",
                           friction=friction_for(0.5), **kwargs(pt=0.5))
    assert result["close_debit"] == pytest.approx(1.30)
    assert result["n_limit_retries"] == 1


def test_v0_trigger_clear_cancels_and_retrigger_resets_retries(monkeypatch):
    marks = iter([1.0, 0.0, 1.0])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    fills = iter([99.0, 2.0, 99.0, 99.0, 1.0])
    monkeypatch.setattr(exit_path, "fill_debit", lambda legs, s: next(fills))
    result = run_exit_path(position(), bars([100, 100, 100]), pd.Series(dtype=float),
                           live_close_path="limit_fallback", friction=friction_for(0.5), **kwargs(pt=0.5))
    assert result["n_limit_retries"] == 0


def test_v1_metadata_stays_at_t0_when_later_mark_changes(monkeypatch):
    marks = iter([1.0, 0.0, -1.0])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    result = run_exit_path(position(), bars([100, 100, 100]), pd.Series(dtype=float), policy="V1",
                           friction=friction_for(0.66), **kwargs(pt=0.5))
    assert result["exit_reason"] == "profit_target"
    assert result["trigger_mark_pnl_pct"] == pytest.approx(1.0)


def test_v0_cleared_order_resets_retry_count_before_retrigger(monkeypatch):
    marks = iter([1.0, 1.0, 0.0, 1.0, 1.0])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    fills = iter([99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 1.0, 1.0])
    monkeypatch.setattr(exit_path, "fill_debit", lambda legs, s: next(fills))
    result = run_exit_path(position(), bars([100, 100, 100, 100, 100]), pd.Series(dtype=float),
                           live_close_path="limit_fallback", friction=friction_for(0.5),
                           **kwargs(pt=0.5, end_at=pd.Timestamp("2026-01-02 11:00")))
    assert result["n_limit_retries"] == 1


def test_v1_market_conversion_keeps_original_trigger_metadata(monkeypatch):
    marks = iter([1.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    result = run_exit_path(position(), bars([100, 100, 100, 100]), pd.Series(dtype=float), policy="V1",
                           friction=friction_for(0.66), **kwargs(pt=0.5))
    assert result["n_limit_retries"] == 3
    assert result["exit_reason"] == "profit_target"
    assert result["trigger_mark_pnl_pct"] == pytest.approx(1.0)


def test_v0_negative_mid_markup_adds_absolute_five_percent(monkeypatch):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    monkeypatch.setattr(exit_path, "spread_mid_debit", lambda *args: -2.0)
    monkeypatch.setattr(exit_path, "fill_debit", lambda legs, s: -2.0)
    result = run_exit_path(position(), bars([100]), pd.Series(dtype=float), live_close_path="limit_fallback",
                           friction=friction_for(0.5), **kwargs(pt=0.5))
    assert result["close_debit"] == pytest.approx(-1.90)


def test_v2_min_hold_boundary_and_pt_still_applies(monkeypatch):
    b = bars([100, 100, 100, 100])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: -1.0)
    common = kwargs(pt=2.0, sl=-0.5, end_at=b.index[-1], friction=friction_for(1.0))
    v0 = run_exit_path(position(), b, pd.Series(dtype=float), **common)
    v2 = run_exit_path(position(), b, pd.Series(dtype=float), policy="V2", entry_ts=b.index[0] - pd.Timedelta(minutes=15), **common)
    assert v0["exit_ts"] == b.index[0]
    assert v2["exit_ts"] == b.index[3]
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    pt = run_exit_path(position(), b, pd.Series(dtype=float), policy="V2", entry_ts=b.index[0] - pd.Timedelta(minutes=15),
                       **{**common, "pt": 0.5})
    assert pt["exit_ts"] == b.index[0]


def test_v3_no_stop_and_pt_none(monkeypatch):
    b = bars([100, 100, 100])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: -2.0)
    assert run_exit_path(position(), b, pd.Series(dtype=float), policy="V3", friction=friction_for(1.0), **kwargs(pt=2.0, sl=None)) is None
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    result = run_exit_path(position(), b, pd.Series(dtype=float), policy="V3", friction=friction_for(1.0), **kwargs(pt=0.5, sl=None))
    assert result["exit_reason"] == "profit_target"
    assert run_exit_path(position(), b, pd.Series(dtype=float), policy="V3", friction=friction_for(1.0), **kwargs(pt=None, sl=None)) is None


@pytest.mark.parametrize("spread, values, breach", [("bull_put", [100, 100, 99], 2), ("bear_call", [100, 100, 101], 2)])
def test_v4_underlying_breach_is_strict_and_directional(monkeypatch, spread, values, breach):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 0.0)
    result = run_exit_path(position(spread), bars(values), pd.Series(dtype=float), policy="V4", friction=friction_for(1.0), **kwargs(pt=2.0, sl=-1.0))
    assert result["exit_ts"] == bars(values).index[breach]
    equal = run_exit_path(position(spread), bars(values[:2]), pd.Series(dtype=float), policy="V4", friction=friction_for(1.0), **kwargs(pt=2.0, sl=-1.0))
    assert equal is None


def test_v5_account_stop_boundary_uses_dollars_and_qty(monkeypatch):
    marks = iter([-0.009, -0.01])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
    result = run_exit_path(position(), bars([100, 100]), pd.Series(dtype=float), policy="V5", friction=friction_for(1.0),
                           account_equity=100, stop_account_pct=0.02, **kwargs(qty=2, pt=2.0, sl=-1.0))
    assert result["exit_ts"] == bars([100, 100]).index[1]
    with pytest.raises(ValueError):
        run_exit_path(position(), bars([100]), pd.Series(dtype=float), policy="V5", **kwargs(pt=2.0, sl=-1.0))


@pytest.mark.parametrize("spread, values", [("bull_put", [100, 95]), ("bear_call", [100, 105])])
def test_v6_underlying_move_boundary_and_direction(monkeypatch, spread, values):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 0.0)
    result = run_exit_path(position(spread), bars(values), pd.Series(dtype=float), policy="V6", friction=friction_for(1.0),
                           entry_underlying=100, stop_underlying_pct=0.05, **kwargs(pt=2.0, sl=-1.0))
    assert result["exit_ts"] == bars(values).index[1]
    favorable = [100, 105] if spread == "bull_put" else [100, 95]
    assert run_exit_path(position(spread), bars(favorable), pd.Series(dtype=float), policy="V6", friction=friction_for(1.0),
                         entry_underlying=100, stop_underlying_pct=0.05, **kwargs(pt=2.0, sl=-1.0)) is None
    with pytest.raises(ValueError):
        run_exit_path(position(spread), bars([100]), pd.Series(dtype=float), policy="V6", **kwargs(pt=2.0, sl=-1.0))


def test_v6_bull_put_uses_entry_underlying_as_denominator(monkeypatch):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 0.0)
    b = bars([181.8, 180.0])
    result = run_exit_path(position("bull_put"), b, pd.Series(dtype=float), policy="V6",
                           friction=friction_for(1.0), entry_underlying=200.0,
                           stop_underlying_pct=0.10, **kwargs(pt=2.0, sl=-1.0))
    assert result["exit_ts"] == b.index[1]


@pytest.mark.parametrize("policy, extra", [
    ("V5", {"account_equity": 0, "stop_account_pct": 0.5}),
    ("V5", {"account_equity": -1, "stop_account_pct": 0.5}),
    ("V5", {"account_equity": 100, "stop_account_pct": 0}),
    ("V5", {"account_equity": 100, "stop_account_pct": -0.1}),
    ("V5", {"account_equity": 100, "stop_account_pct": 1.1}),
    ("V6", {"entry_underlying": 0, "stop_underlying_pct": 0.5}),
    ("V6", {"entry_underlying": -1, "stop_underlying_pct": 0.5}),
    ("V6", {"entry_underlying": 100, "stop_underlying_pct": 0}),
    ("V6", {"entry_underlying": 100, "stop_underlying_pct": -0.1}),
    ("V6", {"entry_underlying": 100, "stop_underlying_pct": 1.1}),
])
def test_v5_v6_reject_invalid_parameter_boundaries(policy, extra):
    with pytest.raises(ValueError):
        run_exit_path(position(), bars([100]), pd.Series(dtype=float), policy=policy,
                       friction=friction_for(1.0), **extra, **kwargs(pt=2.0, sl=-1.0))


@pytest.mark.parametrize("policy, extra", [
    ("V5", {"account_equity": 100, "stop_account_pct": 1.0}),
    ("V6", {"entry_underlying": 100, "stop_underlying_pct": 1.0}),
])
def test_v5_v6_accept_one_boundary(monkeypatch, policy, extra):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 0.0)
    assert run_exit_path(position(), bars([100]), pd.Series(dtype=float), policy=policy,
                         friction=friction_for(1.0), **extra, **kwargs(pt=2.0, sl=-1.0)) is None


@pytest.mark.parametrize("policy, extra", [("V0", {}), ("V4", {}), ("V5", {"account_equity": 100, "stop_account_pct": 0.5}),
                                             ("V6", {"entry_underlying": 100, "stop_underlying_pct": 0.5})])
def test_pt_none_disables_profit_target_but_keeps_stop(monkeypatch, policy, extra):
    marks = iter([1.0, -1.0])
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
    values = [100, 95] if policy == "V6" else [100, 90]
    if policy == "V5":
        extra["stop_account_pct"] = 0.01
    if policy == "V6":
        extra["stop_underlying_pct"] = 0.05
    result = run_exit_path(position(), bars(values), pd.Series(dtype=float), policy=policy, friction=friction_for(1.0),
                           **extra, **kwargs(pt=None, sl=-0.5))
    assert result["exit_reason"] == "stop_loss"


def test_new_policies_keep_limit_fallback_execution(monkeypatch):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    monkeypatch.setattr(exit_path, "fill_debit", lambda legs, s: 10.0 if s == 1.0 else next(fills))
    for policy, extra in [("V4", {}), ("V5", {"account_equity": 100, "stop_account_pct": 0.01})]:
        fills = iter([99.0, 99.0, 1.0])
        result = run_exit_path(position(), bars([99, 99, 99]), pd.Series(dtype=float), policy=policy,
                               live_close_path="limit_fallback", friction=friction_for(0.66),
                               **extra, **kwargs(pt=0.5, sl=-1.0, end_at=pd.Timestamp("2026-01-02 10:00")))
        assert result["n_limit_retries"] == 2


def test_policy_registry_and_required_validation():
    for policy in ["V2", "V3", "V4", "V5", "V6"]:
        extra = {"entry_ts": pd.Timestamp("2026-01-02 09:30")} if policy == "V2" else {}
        if policy == "V5":
            extra.update(account_equity=100, stop_account_pct=1.0)
        if policy == "V6":
            extra.update(entry_underlying=100, stop_underlying_pct=0.1)
        assert run_exit_path(position(), bars([100]), pd.Series(dtype=float), policy=policy,
                             friction=friction_for(1.0), **extra, **kwargs(pt=2.0, sl=-1.0)) is None
    with pytest.raises(ValueError):
        run_exit_path(position(), bars([100]), pd.Series(dtype=float), policy="bad", **kwargs())


@pytest.mark.parametrize("policy, extra", [("V4", {}), ("V5", {"account_equity": 100, "stop_account_pct": 1.0}),
                                             ("V6", {"entry_underlying": 100, "stop_underlying_pct": 1.0})])
def test_new_policies_use_legacy_mark_for_profit_target(monkeypatch, policy, extra):
    monkeypatch.setattr(exit_path, "price_spread_to_close", lambda **args: 0.5)
    result = run_exit_path(position(), bars([100]), pd.Series(dtype=float), policy=policy,
                           **extra, **kwargs(pt=0.25, sl=-1.0))
    assert result["exit_reason"] == "profit_target"
    assert result["trigger_mark_pnl_pct"] == pytest.approx(0.5)


def test_v3_no_stop_and_expiry_settlement(monkeypatch):
    expiry_bars = pd.DataFrame({"close": [100]}, index=pd.to_datetime(["2026-01-30 09:30"]))
    for friction in (None, friction_for(1.0)):
        if friction is not None:
            friction.update(beta=0.0, h_mult=99.0)
        result = run_exit_path(position(credit=2.0), expiry_bars, pd.Series(dtype=float), policy="V3",
                                friction=friction, **kwargs(expiry=date(2026, 1, 30), pt=None, sl=None,
                                                            end_at=pd.Timestamp("2026-01-30 10:00")))
        assert result["exit_reason"] == "expiry"
        assert result["close_debit"] == pytest.approx(0.0)
        assert result["realized_pnl"] == pytest.approx(200.0)
        assert result["trigger_mark_pnl_pct"] == pytest.approx(1.0)
    for policy, extra in [("V0", {}), ("V4", {}), ("V5", {"account_equity": 100, "stop_account_pct": 1}),
                          ("V6", {"entry_underlying": 100, "stop_underlying_pct": 1})]:
        result = run_exit_path(position(), expiry_bars, pd.Series(dtype=float), policy=policy,
                               friction=friction_for(1.0), **extra,
                               **kwargs(expiry=date(2026, 1, 30), pt=None, sl=-1,
                                        end_at=pd.Timestamp("2026-01-30 10:00")))
        assert result["exit_reason"] == "expiry"


def test_expiry_window_and_pending_limit_rules(monkeypatch):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    before = pd.DataFrame({"close": [100]}, index=pd.to_datetime(["2026-01-29 09:30"]))
    assert run_exit_path(position(), before, pd.Series(dtype=float), friction=friction_for(0.66),
                         **kwargs(expiry=date(2026, 1, 30), pt=2, sl=-1,
                                  end_at=pd.Timestamp("2026-01-29 10:00"))) is None
    expiry = pd.DataFrame({"close": [100]}, index=pd.to_datetime(["2026-01-30 09:30"]))
    result = run_exit_path(position(), expiry, pd.Series(dtype=float), policy="V1",
                           friction=friction_for(0.66), **kwargs(expiry=date(2026, 1, 30), pt=.5, sl=-1,
                                                                end_at=pd.Timestamp("2026-01-30 10:00")))
    assert result["exit_reason"] == "expiry"
    assert result["n_limit_retries"] == 1


def test_pending_limit_continues_past_first_expiry_bar_and_settles_at_window_end(monkeypatch):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
    monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
    b = pd.DataFrame({"close": [100, 100, 100, 100]}, index=pd.to_datetime([
        "2026-01-29 09:30", "2026-01-30 09:30", "2026-01-30 09:45", "2026-01-30 10:00",
    ]))
    result = run_exit_path(position(), b, pd.Series(dtype=float), policy="V1",
                           friction=friction_for(0.66), **kwargs(expiry=date(2026, 1, 30), pt=.5,
                                                                sl=-1, end_at=pd.Timestamp("2026-01-30 10:00")))
    assert result["exit_reason"] == "profit_target"
    assert result["exit_ts"] == b.index[2]
    assert result["n_limit_retries"] == 3

    unfilled = run_exit_path(position(), b.iloc[:2], pd.Series(dtype=float), policy="V1",
                             friction=friction_for(0.66), **kwargs(expiry=date(2026, 1, 30), pt=.5,
                                                                  sl=-1, end_at=pd.Timestamp("2026-01-30 09:30")))
    assert unfilled["exit_reason"] == "expiry"
    assert unfilled["exit_ts"] == b.index[1]
    assert unfilled["n_limit_retries"] == 2


@pytest.mark.parametrize("close", [94, 116])
def test_v4_underlying_breach_condor_is_strict(monkeypatch, close):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: -0.5)
    result = run_exit_path(condor(), pd.DataFrame({"close": [100, close]}, index=pd.to_datetime([
        "2026-01-02 09:30", "2026-01-02 09:45"])), pd.Series(dtype=float), policy="V4",
                           friction=friction_for(1), **kwargs(pt=2, sl=-1))
    assert result["exit_reason"] == "stop_loss"
    equal = run_exit_path(condor(), pd.DataFrame({"close": [100, 100, 110]}, index=pd.to_datetime([
        "2026-01-02 09:30", "2026-01-02 09:45", "2026-01-02 10:00"])), pd.Series(dtype=float),
                          policy="V4", friction=friction_for(1), **kwargs(pt=2, sl=-1))
    assert equal is None


def test_condor_combined_trigger_and_legacy_debit(monkeypatch):
    marks = {"bull_put": 2.5, "bear_call": 0.2}
    monkeypatch.setattr(exit_path, "price_spread_to_close", lambda **args: marks[args["spread_type"]])
    bars_one = bars([100])
    assert run_exit_path(condor(), bars_one, pd.Series(dtype=float), **kwargs(pt=2, sl=-.5)) is None
    marks.update(bull_put=3.0)
    result = run_exit_path(condor(), bars_one, pd.Series(dtype=float), **kwargs(pt=2, sl=-.5))
    assert result["exit_reason"] == "stop_loss"
    calls = []
    monkeypatch.setattr(exit_path, "price_spread_to_close", lambda **args: calls.append(args["spread_type"]) or .5)
    run_exit_path(condor(), pd.DataFrame({"close": [100]}, index=pd.to_datetime(["2026-01-30 09:30"])),
                  pd.Series(dtype=float), **kwargs(expiry=date(2026, 1, 30), pt=2, sl=-1,
                                                   end_at=pd.Timestamp("2026-01-30 10:00")))
    assert calls == ["bull_put", "bear_call", "bull_put", "bear_call"]


@pytest.mark.parametrize("close", [94, 106])
def test_v6_condor_moves_both_directions_at_boundary(monkeypatch, close):
    monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 0.0)
    result = run_exit_path(condor(), bars([100, close]), pd.Series(dtype=float), policy="V6",
                           friction=friction_for(1), entry_underlying=100, stop_underlying_pct=.06,
                           **kwargs(pt=2, sl=-1))
    assert result["exit_reason"] == "stop_loss"
    result = run_exit_path(condor(), bars([100, close]), pd.Series(dtype=float), policy="V6",
                           friction=friction_for(1), entry_underlying=100, stop_underlying_pct=.05,
                           **kwargs(pt=2, sl=-1))
    assert result["exit_reason"] == "stop_loss"


def test_condor_validation_errors():
    invalids = [
        condor()["legs"][:3],
        [*condor()["legs"][:2], {"occ": occ("SPY", "2026-01-30", "P", 90), "side": "short"},
         {"occ": occ("SPY", "2026-01-30", "C", 115), "side": "long"}],
        [{"occ": occ("SPY", "2026-01-30", "P", 95), "side": "short"},
         {"occ": occ("SPY", "2026-01-30", "P", 100), "side": "long"},
         {"occ": occ("SPY", "2026-01-30", "C", 90), "side": "short"},
         {"occ": occ("SPY", "2026-01-30", "C", 115), "side": "long"}],
    ]
    for legs in invalids:
        with pytest.raises(ValueError):
            run_exit_path({"legs": legs, "credit": 1}, bars([100]), pd.Series(dtype=float), **kwargs())


def test_condor_validation_rejects_put_short_at_call_short():
    legs = [
        {"occ": occ("SPY", "2026-01-30", "P", 100), "side": "short"},
        {"occ": occ("SPY", "2026-01-30", "P", 95), "side": "long"},
        {"occ": occ("SPY", "2026-01-30", "C", 100), "side": "short"},
        {"occ": occ("SPY", "2026-01-30", "C", 105), "side": "long"},
    ]
    with pytest.raises(ValueError):
        exit_path._validate_position({"legs": legs, "credit": 1})


def test_condor_validation_rejects_put_short_at_put_long():
    legs = [
        {"occ": occ("SPY", "2026-01-30", "P", 100), "side": "short"},
        {"occ": occ("SPY", "2026-01-30", "P", 100), "side": "long"},
        {"occ": occ("SPY", "2026-01-30", "C", 110), "side": "short"},
        {"occ": occ("SPY", "2026-01-30", "C", 115), "side": "long"},
    ]
    with pytest.raises(ValueError):
        exit_path._validate_position({"legs": legs, "credit": 1})


def test_condor_validation_rejects_call_short_at_call_long():
    legs = [
        {"occ": occ("SPY", "2026-01-30", "P", 100), "side": "short"},
        {"occ": occ("SPY", "2026-01-30", "P", 95), "side": "long"},
        {"occ": occ("SPY", "2026-01-30", "C", 110), "side": "short"},
        {"occ": occ("SPY", "2026-01-30", "C", 110), "side": "long"},
    ]
    with pytest.raises(ValueError):
        exit_path._validate_position({"legs": legs, "credit": 1})
