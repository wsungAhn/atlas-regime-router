"""Build and query an options leg half-spread lookup table."""

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signals import _occ_expiration, _occ_strike  # noqa: E402
from vendor.options_pricing import black_scholes_price  # noqa: E402


INDEX_ETF = ("SPY", "QQQ", "IWM", "DIA")
OTHER_ETF = ("GLD", "TLT", "SLV", "XLE", "XLF")
MID_EDGES = (0.10, 0.50, 2.00)
MID_BUCKETS = ("<0.10", "0.10-0.50", "0.50-2.00", ">2.00")
TICK_FLOOR = 0.005
MIN_CELL_N = 30
DTE_MIN = 5
DTE_MAX = 9
_REPRESENTATIVE_MID = dict(zip(MID_BUCKETS, (0.05, 0.30, 1.00, 3.00)))

IV_SCALE_BOUNDS = (0.05, 5.0)
IV_ANCHOR_HAIRCUT = 0.05
IV_BISECTION_TOL = 1e-9
ZERO_MID_REPRESENTATIVE = 0.05
DEFAULT_EPISODE_SEED = 20260930
HUB_QUOTE_ROOT = Path.home() / "data/universe-hub/options/quotes"


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


def build_half_spread_table(panel_dir=None):
    panel_path = Path(HUB_QUOTE_ROOT if panel_dir is None else panel_dir)
    paths = sorted(panel_path.glob("date=*/*.parquet"))
    if not paths:
        raise ValueError(f"no quote panel files under {panel_path}")
    frames = []
    for path in paths:
        try:
            frames.append(pd.read_parquet(path))
        except Exception:
            logging.exception("Could not read quote panel %s; skipping", path)
    if not frames:
        raise ValueError(f"no quote panel files under {panel_path}")
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.loc[:, ["underlying", "bid", "ask", "mid", "dte"]]
    panel = panel.loc[panel["dte"].between(DTE_MIN, DTE_MAX)].copy()
    panel["h"] = (panel["ask"] - panel["bid"]) / 2
    panel["rel"] = panel["h"] / panel["mid"]
    panel["class"] = panel["underlying"].map(SYMBOL_CLASS)
    panel["bucket"] = panel["mid"].map(_mid_bucket)

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


def write_half_spread_table(panel_dir=None, out_path=None):
    if out_path is None:
        raise ValueError("out_path is required")
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


def _validate_position(position):
    if not isinstance(position, dict) or not isinstance(position.get("legs"), list):
        raise ValueError("position must contain a legs list")
    if len(position["legs"]) not in (2, 4):
        raise ValueError("position must have 2 or 4 legs")
    if not isinstance(position.get("credit"), (int, float)):
        raise ValueError("position credit must be numeric")
    for leg in position["legs"]:
        if not isinstance(leg, dict) or not isinstance(leg.get("occ"), str):
            raise ValueError("each leg must contain an OCC symbol")
        if leg.get("side") not in {"short", "long"}:
            raise ValueError("leg side must be short or long")


def parse_occ(occ):
    if not isinstance(occ, str) or not occ:
        raise ValueError("occ must be a non-empty string")
    strike = _occ_strike(occ)
    expiry = _occ_expiration(occ)
    if strike is None or expiry is None:
        raise ValueError("invalid OCC symbol")
    digits_start = next((i for i, char in enumerate(occ) if char.isdigit()), None)
    if digits_start is None or digits_start + 7 >= len(occ):
        raise ValueError("invalid OCC symbol")
    type_index = digits_start + 6
    option_type = {"C": "call", "P": "put"}.get(occ[type_index])
    underlying = occ[:digits_start]
    if not underlying or option_type is None:
        raise ValueError("invalid OCC symbol")
    return underlying, expiry, option_type, float(strike)


def _validate_pricing_inputs(S, T, iv):
    if S <= 0:
        raise ValueError("S must be positive")
    if T < 0:
        raise ValueError("T must be non-negative")
    if iv <= 0:
        raise ValueError("iv must be positive")


def leg_mids(position, S, T, r, iv):
    _validate_position(position)
    _validate_pricing_inputs(S, T, iv)
    mids = []
    for leg in position["legs"]:
        _, _, option_type, strike = parse_occ(leg["occ"])
        if T <= 0:
            mid = max(S - strike, 0.0) if option_type == "call" else max(strike - S, 0.0)
        else:
            mid = black_scholes_price(S, strike, T, r, iv, option_type)
        mids.append(float(mid))
    return mids


def spread_mid_debit(position, mids):
    _validate_position(position)
    if len(mids) != len(position["legs"]):
        raise ValueError("mids length must match legs")
    if any(not isinstance(mid, (int, float)) for mid in mids):
        raise ValueError("mids must be numeric")
    return float(sum(mid for leg, mid in zip(position["legs"], mids) if leg["side"] == "short") - sum(mid for leg, mid in zip(position["legs"], mids) if leg["side"] == "long"))


def _position_width(position):
    strikes = {"call": [], "put": []}
    for leg in position["legs"]:
        _, _, option_type, strike = parse_occ(leg["occ"])
        strikes[option_type].append(strike)
    return max((max(values) - min(values) for values in strikes.values() if values), default=0.0)


def implied_iv_scale(position, S, T, r, base_iv, haircut=IV_ANCHOR_HAIRCUT):
    _validate_position(position)
    _validate_pricing_inputs(S, T, base_iv)
    if not 0.0 <= haircut < 1.0:
        raise ValueError("haircut must be in [0, 1)")
    credit = float(position["credit"])
    if credit <= 0:
        raise ValueError("credit must be positive")
    if credit >= _position_width(position):
        return None
    target = credit / (1.0 - haircut)

    def residual(scale):
        return spread_mid_debit(position, leg_mids(position, S, T, r, base_iv * scale)) - target

    low, high = IV_SCALE_BOUNDS
    f_low, f_high = residual(low), residual(high)
    if f_low == 0:
        return float(low)
    if f_high == 0:
        return float(high)
    if f_low * f_high > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2.0
        f_mid = residual(mid)
        if abs(f_mid) <= IV_BISECTION_TOL:
            return float(mid)
        if f_low * f_mid <= 0:
            high = mid
            f_high = f_mid
        else:
            low = mid
            f_low = f_mid
    return float((low + high) / 2.0)


def anchor_scales(entries):
    if not isinstance(entries, list):
        raise ValueError("entries must be a list")
    scales = []
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 5:
            raise ValueError("each entry must be a five-tuple")
        scales.append(implied_iv_scale(*entry))
    return scales, sum(scale is None for scale in scales)


def leg_halves(position, mids, table):
    _validate_position(position)
    if len(mids) != len(position["legs"]):
        raise ValueError("mids length must match legs")
    halves = []
    for leg, mid in zip(position["legs"], mids):
        underlying, _, _, _ = parse_occ(leg["occ"])
        halves.append(half_spread(table, underlying, ZERO_MID_REPRESENTATIVE if mid <= 0 else mid))
    return halves


def leg_noise(rng, halves, k):
    if k < 0:
        raise ValueError("k must be non-negative")
    if k == 0:
        return [0.0] * len(halves)
    if rng is None or not hasattr(rng, "normal"):
        raise ValueError("rng is required when k is positive")
    return [float(rng.normal(0.0, k * h)) for h in halves]


def episode_rng(episode_index, seed=DEFAULT_EPISODE_SEED):
    if not isinstance(episode_index, (int, np.integer)) or episode_index < 0:
        raise ValueError("episode_index must be a non-negative integer")
    if not isinstance(seed, (int, np.integer)):
        raise ValueError("seed must be an integer")
    return np.random.default_rng([int(seed), int(episode_index)])


def mark_pnl_pct(position, S, T, r, iv, beta, table, k=0.0, rng=None):
    _validate_position(position)
    credit = float(position["credit"])
    if credit <= 0:
        raise ValueError("credit must be positive")
    if beta < 0 or k < 0:
        raise ValueError("beta and k must be non-negative")
    mids = leg_mids(position, S, T, r, iv)
    halves = leg_halves(position, mids, table)
    noises = leg_noise(rng, halves, k)
    marks = []
    for leg, mid, half, noise in zip(position["legs"], mids, halves, noises):
        adverse = beta * half
        mark = mid + adverse + noise if leg["side"] == "short" else mid - adverse + noise
        marks.append(max(0.0, float(mark)))
    return float((credit - sum(mark for leg, mark in zip(position["legs"], marks) if leg["side"] == "short") + sum(mark for leg, mark in zip(position["legs"], marks) if leg["side"] == "long")) / credit)


def fill_debit(legs, s):
    if not isinstance(legs, list) or not 0.0 <= s <= 1.0:
        raise ValueError("legs must be a list and s must be in [0, 1]")
    debit = 0.0
    for leg in legs:
        if leg.get("side") not in {"short", "long"}:
            raise ValueError("leg side must be short or long")
        bid, ask = float(leg["bid"]), float(leg["ask"])
        if bid < 0 or ask < bid:
            raise ValueError("invalid bid/ask")
        fill = bid + s * (ask - bid) if leg["side"] == "short" else ask - s * (ask - bid)
        debit += fill if leg["side"] == "short" else -fill
    return float(debit)


def main(argv=None):
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel-dir", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    write_half_spread_table(args.panel_dir, args.out)


if __name__ == "__main__":
    main()
