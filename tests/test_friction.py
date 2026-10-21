import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import src.friction as friction
from src.friction import (
    DTE_MAX,
    DTE_MIN,
    MID_BUCKETS,
    TICK_FLOOR,
    SYMBOL_CLASS,
    _mid_bucket,
    build_half_spread_table,
    half_spread,
    write_half_spread_table,
    anchor_scales,
    episode_rng,
    fill_debit,
    implied_iv_scale,
    leg_halves,
    leg_mids,
    leg_noise,
    mark_pnl_pct,
    parse_occ,
    spread_mid_debit,
)
from src.vendor.credit_spread_simulator import price_spread_to_close


def _write_panel(path, rows, name="2026-10-01.parquet"):
    destination = path / "date=2026-10-01"
    destination.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(destination / name, index=False)


def _row(underlying="SPY", mid=1.0, h=0.01, dte=7):
    return {"ts": "2026-10-01", "underlying": underlying, "occ": "x", "bid": mid - h, "ask": mid + h, "mid": mid, "delta": 0.5, "dte": dte, "feed": "test"}


def test_half_spread_lookup_floor_and_buckets(tmp_path):
    points = ((0.0999, 0.006), (0.10, 0.007), (0.4999, 0.008), (0.50, 0.009), (1.9999, 0.010), (2.00, 0.011))
    rows = [r for mid, h in points for r in [_row(mid=mid, h=h)] * 30]
    rows += [_row(underlying="GLD", mid=0.2, h=0.00001)]
    with pytest.raises(ValueError, match="no quote panel files"):
        build_half_spread_table(tmp_path)
    _write_panel(tmp_path, rows)
    table = build_half_spread_table(tmp_path)
    assert [_mid_bucket(mid) for mid, _ in points] == ["<0.10", "0.10-0.50", "0.10-0.50", "0.50-2.00", "0.50-2.00", ">2.00"]
    assert table.loc[table["class"] == "index_etf", "n"].tolist() == [30, 60, 60, 30], "mid bucket boundary classification"
    assert (table.h >= TICK_FLOOR).all()
    assert list(table.bucket.unique()) == list(MID_BUCKETS)


def test_cell_formula_chooses_larger_median_term(tmp_path):
    rows = [_row(mid=0.6, h=0.01) for _ in range(30)] + [_row(mid=1.8, h=0.20) for _ in range(30)]
    _write_panel(tmp_path, rows)
    table = build_half_spread_table(tmp_path)
    cell = table[(table["class"] == "index_etf") & (table["bucket"] == "0.50-2.00")]
    assert cell.iloc[0].h == pytest.approx(0.105), "formula must choose max(h median, rel median times mid median, floor)"
    rows = [_row(underlying="GLD", mid=0.6, h=0.20) for _ in range(30)] + [_row(underlying="GLD", mid=1.8, h=0.01) for _ in range(30)]
    _write_panel(tmp_path, rows, "2026-10-02.parquet")
    table = build_half_spread_table(tmp_path)
    cell = table[(table["class"] == "other_etf") & (table["bucket"] == "0.50-2.00")]
    assert cell.iloc[0].h == pytest.approx(0.20333333333333334), "formula must choose the rel-times-mid term when larger"


def test_dte_filter_and_sample_fallback(tmp_path):
    rows = [_row(mid=1.0, h=0.01, dte=DTE_MIN), _row(mid=1.0, h=0.01, dte=DTE_MAX), _row(mid=1.0, h=100, dte=4), _row(mid=1.0, h=100, dte=10)]
    rows += [_row(mid=1.0, h=0.02) for _ in range(30)]
    _write_panel(tmp_path, rows)
    table = build_half_spread_table(tmp_path)
    cell = table[(table["class"] == "index_etf") & (table["bucket"] == "0.50-2.00")].iloc[0]
    assert cell.n == 32 and not cell.fallback, "DTE 5 and 9 included; DTE 4 and 10 excluded"
    assert len(table) == 12


def test_all_unreadable_quote_panels_raise(tmp_path):
    destination = tmp_path / "date=2026-10-01"
    destination.mkdir()
    (destination / "x.parquet").write_bytes(b"not a parquet file")
    with pytest.raises(ValueError, match="no quote panel files"):
        build_half_spread_table(tmp_path)


def test_default_quote_root_is_hub_store_and_is_used(tmp_path, monkeypatch):
    assert friction.HUB_QUOTE_ROOT == Path.home() / "data/universe-hub/options/quotes"
    _write_panel(tmp_path, [_row(mid=1.0, h=0.01) for _ in range(30)])
    monkeypatch.setattr(friction, "HUB_QUOTE_ROOT", tmp_path)
    table = build_half_spread_table()
    assert (table["n"] > 0).any()


def test_cli_default_hub_root_and_old_layout_fail(tmp_path, monkeypatch):
    _write_panel(tmp_path, [_row(mid=1.0, h=0.01) for _ in range(30)])
    out = tmp_path / "default.csv"
    monkeypatch.setattr(friction, "HUB_QUOTE_ROOT", tmp_path)
    friction.main(["--out", str(out)])
    assert (pd.read_csv(out)["n"] > 0).any()

    old_layout = tmp_path / "old-layout"
    old_layout.mkdir()
    pd.DataFrame([_row(mid=1.0, h=0.01)]).to_parquet(old_layout / "2026-10-01.parquet", index=False)
    with pytest.raises(ValueError, match="no quote panel files"):
        friction.main(["--panel-dir", str(old_layout), "--out", str(tmp_path / "old.csv")])


def test_symbol_class_and_invalid_mid(tmp_path):
    assert SYMBOL_CLASS("SPY") == "index_etf"
    assert SYMBOL_CLASS("GLD") == "other_etf"
    assert SYMBOL_CLASS("NVDA") == "single_stock"
    assert SYMBOL_CLASS("AAA") == "single_stock"
    _write_panel(tmp_path, [_row(mid=1.0, h=0.01) for _ in range(30)])
    table = build_half_spread_table(tmp_path)
    for bad in (np.nan, 0, -1):
        try:
            half_spread(table, "SPY", bad)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid mid did not raise ValueError")


def test_write_csv_has_twelve_rows_and_five_columns(tmp_path):
    _write_panel(tmp_path, [_row(mid=1.0, h=0.01) for _ in range(30)])
    out = tmp_path / "nested" / "table.csv"
    write_half_spread_table(tmp_path, out)
    saved = pd.read_csv(out)
    assert saved.shape == (12, 5)
    assert list(saved.columns) == ["class", "bucket", "n", "h", "fallback"]


def test_cli_script_writes_twelve_row_csv(tmp_path):
    _write_panel(tmp_path, [_row(mid=1.0, h=0.01) for _ in range(30)])
    out = tmp_path / "t.csv"
    result = subprocess.run(
        [sys.executable, "src/friction.py", "--panel-dir", str(tmp_path), "--out", str(out)],
        cwd=__import__("pathlib").Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert pd.read_csv(out).shape[0] == 12


def test_fallback_uses_pooled_representative_mid_and_floor(tmp_path):
    _write_panel(tmp_path, [_row(mid=1.0, h=0.02) for _ in range(2)] + [_row(mid=0.2, h=0.02) for _ in range(2)])
    table = build_half_spread_table(tmp_path)
    cell = table[(table["class"] == "index_etf") & (table["bucket"] == "<0.10")].iloc[0]
    assert cell.fallback and cell.n == 0 and cell.h == 0.005, "sparse cells must use pooled representative-mid fallback and floor"
    sparse = table[(table["class"] == "index_etf") & (table["bucket"] == "0.10-0.50")].iloc[0]
    assert sparse.fallback and sparse.n == 2, "nonempty cells below MIN_CELL_N must use fallback"


def test_cell_value_uses_medians_not_means(tmp_path):
    rows = [_row(mid=1.0, h=0.01) for _ in range(29)] + [_row(mid=1.0, h=10.0)]
    _write_panel(tmp_path, rows)
    table = build_half_spread_table(tmp_path)
    cell = table[(table["class"] == "index_etf") & (table["bucket"] == "0.50-2.00")].iloc[0]
    assert cell.n == 30 and not cell.fallback
    assert cell.h == pytest.approx(0.01), "cell formula must use medians, not means"


def test_min_cell_n_boundary_has_distinct_classes(tmp_path):
    rows = (
        [_row(underlying="SPY", mid=1.0, h=0.01) for _ in range(29)]
        + [_row(underlying="GLD", mid=1.0, h=0.02) for _ in range(30)]
        + [_row(underlying="NVDA", mid=1.0, h=0.03) for _ in range(31)]
    )
    _write_panel(tmp_path, rows)
    table = build_half_spread_table(tmp_path)
    cells = table[table["bucket"] == "0.50-2.00"].set_index("class")
    assert cells.loc["index_etf", "n"] == 29 and bool(cells.loc["index_etf", "fallback"])
    assert cells.loc["other_etf", "n"] == 30 and not bool(cells.loc["other_etf", "fallback"])
    assert cells.loc["single_stock", "n"] == 31 and not bool(cells.loc["single_stock", "fallback"])


def test_pooled_value_uses_each_bucket_representative_mid(tmp_path):
    rows = []
    for mid in (0.05, 0.08, 0.20, 0.40, 0.60, 1.50, 2.50, 4.00):
        rows.append(_row(mid=mid, h=0.2 * mid))
    _write_panel(tmp_path, rows)
    table = build_half_spread_table(tmp_path)
    cells = table[table["class"] == "index_etf"].set_index("bucket")
    expected = {"<0.10": 0.01, "0.10-0.50": 0.06, "0.50-2.00": 0.20, ">2.00": 0.60}
    for bucket, value in expected.items():
        assert cells.loc[bucket, "fallback"]
        assert cells.loc[bucket, "h"] == pytest.approx(value), f"pooled representative mid for {bucket}"


def test_pooled_fallback_is_class_local_but_global_for_empty_class(tmp_path):
    rows = [_row(underlying="SPY", mid=0.05, h=0.01), _row(underlying="NVDA", mid=1.0, h=0.80)]
    _write_panel(tmp_path, rows)
    table = build_half_spread_table(tmp_path)
    index_missing = table[(table["class"] == "index_etf") & (table["bucket"] == ">2.00")].iloc[0]
    empty_class = table[(table["class"] == "other_etf") & (table["bucket"] == ">2.00")].iloc[0]
    assert index_missing.fallback and index_missing.h == pytest.approx(0.60), "nonempty class fallback must use class rows"
    assert empty_class.fallback and empty_class.h == pytest.approx(1.50), "empty class fallback must use all panel rows"


def test_symbol_class_covers_all_etf_members():
    for symbol in ("SPY", "QQQ", "IWM", "DIA"):
        assert SYMBOL_CLASS(symbol) == "index_etf"
    for symbol in ("GLD", "TLT", "SLV", "XLE", "XLF"):
        assert SYMBOL_CLASS(symbol) == "other_etf"


def _occ(ticker, yymmdd, option_type, strike):
    return f"{ticker}{yymmdd}{option_type}{int(strike * 1000):08d}"


def _position(legs, credit=0.5):
    return {"legs": [leg if isinstance(leg, dict) else {"occ": leg[0], "side": leg[1]} for leg in legs], "credit": credit}


def test_leg_mid_sum_matches_price_spread_to_close():
    S, T, r, iv = 100.0, 7 / 365, 0.045, 0.25
    bull = _position([(_occ("SPY", "261017", "P", 95), "short"), (_occ("SPY", "261017", "P", 90), "long")])
    bear = _position([(_occ("SPY", "261017", "C", 105), "short"), (_occ("SPY", "261017", "C", 110), "long")])
    condor = _position(bull["legs"] + bear["legs"])
    for position, spread_type, short, long in ((bull, "bull_put", 95, 90), (bear, "bear_call", 105, 110)):
        assert spread_mid_debit(position, leg_mids(position, S, T, r, iv)) == pytest.approx(price_spread_to_close(S, short, long, T, r, iv, spread_type), abs=1e-9)
    assert spread_mid_debit(condor, leg_mids(condor, S, T, r, iv)) == pytest.approx(
        price_spread_to_close(S, 95, 90, T, r, iv, "bull_put") + price_spread_to_close(S, 105, 110, T, r, iv, "bear_call"), abs=1e-9
    )
    assert spread_mid_debit(bull, leg_mids(bull, S, 0.0, r, iv)) == pytest.approx(price_spread_to_close(S, 95, 90, 0.0, r, iv, "bull_put"), abs=1e-9)


def test_fill_direction_buy_at_ask_sell_at_bid():
    legs = [{"side": "short", "bid": 1.0, "ask": 1.2}, {"side": "long", "bid": 0.3, "ask": 0.5}]
    assert fill_debit(legs, 1) == pytest.approx(1.2 - 0.3)
    assert fill_debit(legs, 0.66) == pytest.approx((1 + 0.66 * 0.2) - (0.5 - 0.66 * 0.2))
    assert fill_debit(legs, 0) == pytest.approx(1.0 - 0.5)
    assert fill_debit(legs, 0.66) == fill_debit(legs, 0.66)
    for s in (-0.01, 1.01):
        with pytest.raises(ValueError):
            fill_debit(legs, s)


def test_iv_anchor_unanchorable_skipped():
    S, T, r, iv = 100.0, 30 / 365, 0.045, 0.25
    normal = _position([(_occ("SPY", "261017", "P", 95), "short"), (_occ("SPY", "261017", "P", 90), "long")], credit=0.5)
    unanchorable = _position([(_occ("SPY", "261017", "P", 95), "short"), (_occ("SPY", "261017", "P", 90), "long")], credit=5.0)
    condor = _position(normal["legs"] + [{"occ": _occ("SPY", "261017", "C", 105), "side": "short"}, {"occ": _occ("SPY", "261017", "C", 110), "side": "long"}], credit=0.5)
    assert implied_iv_scale(unanchorable, S, T, r, iv) is None
    scales, count = anchor_scales([(unanchorable, S, T, r, iv), (normal, S, T, r, iv), (condor, S, T, r, iv)])
    assert count == 1 and scales[0] is None
    assert spread_mid_debit(normal, leg_mids(normal, S, T, r, iv * scales[1])) * 0.95 == pytest.approx(normal["credit"], abs=1e-6)
    assert scales[2] is not None


def test_symmetric_noise_mark():
    halves = [0.1, 0.2]
    class Spy:
        def normal(self, *args):
            raise AssertionError("rng was called")
    assert leg_noise(Spy(), halves, 0) == [0.0, 0.0]
    rng = np.random.default_rng(11)
    a = np.array([leg_noise(rng, halves, 1.0) for _ in range(10000)])
    assert np.all(np.abs(a.mean(axis=0)) < 0.03 * np.array(halves))
    assert np.all(np.abs(a.std(axis=0) / halves - 1) < 0.05)
    assert abs(np.corrcoef(a.T)[0, 1]) < 0.05
    with pytest.raises(ValueError):
        leg_noise(None, halves, 1)
    assert episode_rng(3).normal() == episode_rng(3).normal()


def test_mark_pnl_beta_offset():
    table = pd.DataFrame([{"class": "index_etf", "bucket": bucket, "h": 0.1} for bucket in MID_BUCKETS])
    p = _position([(_occ("SPY", "261017", "P", 95), "short"), (_occ("SPY", "261017", "P", 90), "long")], credit=0.5)
    mids = leg_mids(p, 100, 30 / 365, 0.045, 0.25)
    expected = (p["credit"] - spread_mid_debit(p, mids)) / p["credit"]
    assert mark_pnl_pct(p, 100, 30 / 365, 0.045, 0.25, 0, table) == pytest.approx(expected)
    assert mark_pnl_pct(p, 100, 30 / 365, 0.045, 0.25, 0.2, table) == pytest.approx(expected - 0.2 * 0.2 / 0.5)


def test_mark_clamp_nonnegative():
    table = pd.DataFrame([{"class": "index_etf", "bucket": bucket, "h": 1.0} for bucket in MID_BUCKETS])
    p = _position([(_occ("SPY", "261017", "P", 100), "short"), (_occ("SPY", "261017", "P", 95), "long")], credit=0.5)
    assert mark_pnl_pct(p, 94.0, 0.0, 0.045, 0.25, 2, table) == pytest.approx((0.5 - 8.0 + 0.0) / 0.5)


def test_parse_occ_and_zero_mid_half():
    assert parse_occ("GLD260831P00414000") == ("GLD", __import__("datetime").date(2026, 8, 31), "put", 414.0)
    assert parse_occ("LONGER260831C00100000")[0] == "LONGER"
    with pytest.raises(ValueError):
        parse_occ("SPY2608")
    table = pd.DataFrame([{"class": "index_etf", "bucket": bucket, "h": 0.05} for bucket in MID_BUCKETS])
    p = _position([(_occ("SPY", "261017", "P", 100), "short"), (_occ("SPY", "261017", "P", 95), "long")])
    assert leg_halves(p, [0.0, 1.0], table) == [0.05, 0.05]
