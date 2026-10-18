import numpy as np
import pandas as pd
import pytest

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
)


def _write_panel(path, rows, name="2026-10-01.parquet"):
    pd.DataFrame(rows).to_parquet(path / name, index=False)


def _row(underlying="SPY", mid=1.0, h=0.01, dte=7):
    return {"ts": "2026-10-01", "underlying": underlying, "occ": "x", "bid": mid - h, "ask": mid + h, "mid": mid, "delta": 0.5, "dte": dte, "feed": "test"}


def test_half_spread_lookup_floor_and_buckets(tmp_path):
    points = ((0.0999, 0.006), (0.10, 0.007), (0.4999, 0.008), (0.50, 0.009), (1.9999, 0.010), (2.00, 0.011))
    rows = [r for mid, h in points for r in [_row(mid=mid, h=h)] * 30]
    rows += [_row(underlying="GLD", mid=0.2, h=0.00001)]
    table = build_half_spread_table(tmp_path)
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


def test_positions_files_are_ignored(tmp_path):
    _write_panel(tmp_path, [_row(mid=1.0, h=0.01) for _ in range(30)])
    _write_panel(tmp_path, [_row(mid=1.0, h=99.0)], "positions-2026-10-01.parquet")
    table = build_half_spread_table(tmp_path)
    assert len(table) == 12 and table.loc[(table["class"] == "index_etf") & (table["bucket"] == "0.50-2.00"), "n"].iloc[0] == 30, "positions-* files must be excluded"


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
