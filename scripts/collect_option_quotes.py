#!/usr/bin/env python3
"""Collect live option quote snapshots into a daily parquet panel."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_DIR / "src"))

from signals import OPTION_SYMBOLS, _occ_expiration, fetch_chain  # noqa: E402

QUOTE_COLUMNS = ["ts", "underlying", "occ", "bid", "ask", "mid", "delta", "dte", "feed"]
POSITION_COLUMNS = ["ts", "occ", "current_price", "qty"]
LOG = logging.getLogger(__name__)
SYMBOL_PAUSE_SEC = 0.5
RETRY_BACKOFF_SEC = (5, 15, 45)  # after a 429; one pause per retry attempt


def _fetch_full_chain(option_client: Any, symbol: str) -> dict:
    from alpaca.trading.enums import ContractType

    for attempt in range(len(RETRY_BACKOFF_SEC) + 1):
        try:
            chain: dict = {}
            for contract_type in (ContractType.PUT, ContractType.CALL):
                chain.update(fetch_chain(option_client, symbol, contract_type))
            return chain
        except Exception as exc:
            if getattr(exc, "status_code", None) != 429 or attempt == len(RETRY_BACKOFF_SEC):
                raise
            LOG.warning("%s: 429, retry %d/%d in %ds", symbol, attempt + 1, len(RETRY_BACKOFF_SEC), RETRY_BACKOFF_SEC[attempt])
            time.sleep(RETRY_BACKOFF_SEC[attempt])
    raise AssertionError("unreachable")


def _load_env_file(path: Path) -> None:
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def _atomic_append(rows: list[dict[str, Any]], path: Path, columns: list[str]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows, columns=columns)
    if path.exists():
        frame = pd.concat([pd.read_parquet(path), frame], ignore_index=True)[columns]
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".parquet", delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        frame.to_parquet(temporary, index=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def collect_once(option_client: Any, symbols: Iterable[str], now: datetime, panel_dir: Path | str, feed: str | None = None) -> None:
    """Collect one quote snapshot and append it to the UTC day's panel."""
    panel_dir = Path(panel_dir)
    timestamp = now.astimezone(timezone.utc).isoformat()
    today = now.astimezone(timezone.utc).date()
    rows: list[dict[str, Any]] = []
    succeeded = failed = 0
    for symbol in symbols:
        excluded = 0
        try:
            chain = _fetch_full_chain(option_client, symbol)
            for occ, snapshot in chain.items():
                quote = _value(snapshot, "latest_quote")
                bid = _value(quote, "bid_price")
                ask = _value(quote, "ask_price")
                if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
                    excluded += 1
                    continue
                greeks = _value(snapshot, "greeks")
                rows.append({"ts": timestamp, "underlying": symbol, "occ": occ, "bid": bid,
                             "ask": ask, "mid": (bid + ask) / 2, "delta": _value(greeks, "delta"),
                             "dte": (_occ_expiration(occ) - today).days, "feed": feed or "default"})
            if excluded:
                LOG.info("%s: excluded %d invalid quotes", symbol, excluded)
            succeeded += 1
        except Exception:
            failed += 1
            LOG.exception("quote collection failed for %s", symbol)
        time.sleep(SYMBOL_PAUSE_SEC)
    quote_path = panel_dir / f"{today.isoformat()}.parquet"
    _atomic_append(rows, quote_path, QUOTE_COLUMNS)
    if not rows:
        LOG.info("no valid quote rows collected; no panel file created")
    LOG.info("quote collection summary: %d succeeded, %d failed, %d rows", succeeded, failed, len(rows))

    try:
        from alpaca.trading.client import TradingClient
        positions = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True).get_all_positions()
        position_rows = [{"ts": timestamp, "occ": _value(position, "symbol"),
                          "current_price": _value(position, "current_price"), "qty": _value(position, "qty")}
                         for position in positions]
        _atomic_append(position_rows, panel_dir / f"positions-{today.isoformat()}.parquet", POSITION_COLUMNS)
    except Exception:
        LOG.warning("position mark collection failed", exc_info=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path("/Users/Mung/dev/atlas-options-hackathon/.env.competition"))
    parser.add_argument("--panel-dir", type=Path, default=REPO_DIR / "data" / "quote_panel")
    parser.add_argument("--symbols", help="Comma-separated underlying symbols")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _load_env_file(args.env_file)
    from alpaca.data.historical.option import OptionHistoricalDataClient
    client = OptionHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    symbols = [item.strip() for item in args.symbols.split(",")] if args.symbols else OPTION_SYMBOLS
    collect_once(client, symbols, datetime.now(timezone.utc), args.panel_dir)


if __name__ == "__main__":
    main()
