"""Build and query an options leg half-spread lookup table."""

import argparse
import logging
import os
import tempfile
from pathlib import Path

import pandas as pd


INDEX_ETF = ("SPY", "QQQ", "IWM", "DIA")
OTHER_ETF = ("GLD", "TLT", "SLV", "XLE", "XLF")
MID_EDGES = (0.10, 0.50, 2.00)
MID_BUCKETS = ("<0.10", "0.10-0.50", "0.50-2.00", ">2.00")
TICK_FLOOR = 0.005
MIN_CELL_N = 30
DTE_MIN = 5
DTE_MAX = 9
_REPRESENTATIVE_MID = dict(zip(MID_BUCKETS, (0.05, 0.30, 1.00, 3.00)))


def SYMBOL_CLASS(underlying):
    if underlying in INDEX_ETF:
        return "index_etf"
    if underlying in OTHER_ETF:
        return "other_etf"
    return "single_stock"


def _mid_bucket(mid):
    if mid < MID_EDGES[0]:
        return MID_BUCKETS[0]
    if mid < MID_EDGES[1]:
        return MID_BUCKETS[1]
    if mid < MID_EDGES[2]:
        return MID_BUCKETS[2]
    return MID_BUCKETS[3]


def _pooled_value(rows, bucket):
    if rows.empty:
        return TICK_FLOOR
    return max(float(rows["rel"].median()) * _REPRESENTATIVE_MID[bucket], TICK_FLOOR)


def build_half_spread_table(panel_dir):
    panel_path = Path(panel_dir)
    frames = []
    for path in sorted(panel_path.glob("20*.parquet")):
        try:
            frames.append(pd.read_parquet(path))
        except Exception:
            logging.exception("Could not read quote panel %s; skipping", path)
    if frames:
        panel = pd.concat(frames, ignore_index=True)
        panel = panel.loc[panel["dte"].between(DTE_MIN, DTE_MAX)].copy()
        panel["h"] = (panel["ask"] - panel["bid"]) / 2
        panel["rel"] = panel["h"] / panel["mid"]
        panel["class"] = panel["underlying"].map(SYMBOL_CLASS)
        panel["bucket"] = panel["mid"].map(_mid_bucket)
    else:
        panel = pd.DataFrame(columns=("h", "rel", "mid", "class", "bucket"))

    all_rows = []
    for class_name in ("index_etf", "other_etf", "single_stock"):
        class_rows = panel.loc[panel["class"] == class_name]
        fallback_rows = class_rows if not class_rows.empty else panel
        for bucket in MID_BUCKETS:
            cell = class_rows.loc[class_rows["bucket"] == bucket]
            n = len(cell)
            if n >= MIN_CELL_N:
                value = max(
                    float(cell["h"].median()),
                    float(cell["rel"].median()) * float(cell["mid"].median()),
                    TICK_FLOOR,
                )
                fallback = False
            else:
                value = _pooled_value(fallback_rows, bucket)
                fallback = True
            all_rows.append({"class": class_name, "bucket": bucket, "n": n, "h": value, "fallback": fallback})
    return pd.DataFrame(all_rows, columns=("class", "bucket", "n", "h", "fallback"))


def half_spread(table, underlying, mid):
    if pd.isna(mid) or mid <= 0:
        raise ValueError("mid must be positive and non-NaN")
    class_name = SYMBOL_CLASS(underlying)
    bucket = _mid_bucket(mid)
    match = table.loc[(table["class"] == class_name) & (table["bucket"] == bucket), "h"]
    if match.empty:
        raise KeyError(f"missing half-spread cell: {class_name}/{bucket}")
    return float(match.iloc[0])


def write_half_spread_table(panel_dir, out_path):
    table = build_half_spread_table(panel_dir)
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    try:
        table.to_csv(temporary, index=False)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    logging.info("half-spread table rows=%d fallback rows=%d\n%s", len(table), int(table["fallback"].sum()), table.to_string(index=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    write_half_spread_table(args.panel_dir, args.out)
