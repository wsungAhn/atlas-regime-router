from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from scripts import collect_option_quotes as collector


OCC = "AAPL260101C00100000"


def snap(bid=1, ask=2, delta=0.4):
    return SimpleNamespace(latest_quote=SimpleNamespace(bid_price=bid, ask_price=ask), greeks=SimpleNamespace(delta=delta))


def run(tmp_path, monkeypatch, symbols=("A",)):
    monkeypatch.setattr(collector, "fetch_chain", lambda *args: {OCC: snap()})
    collector.collect_once(object(), symbols, datetime(2026, 9, 30, tzinfo=timezone.utc), tmp_path)
    return pd.read_parquet(tmp_path / "2026-09-30.parquet")


def test_schema_and_mid(tmp_path, monkeypatch):
    frame = run(tmp_path, monkeypatch)
    assert list(frame.columns) == collector.QUOTE_COLUMNS
    assert frame.loc[0, "mid"] == (frame.loc[0, "bid"] + frame.loc[0, "ask"]) / 2


def test_invalid_quotes_excluded_and_equal_included(tmp_path, monkeypatch):
    # Use one synthetic chain per call to exercise each boundary independently.
    for bid, ask in ((0, 1), (1, 0), (2, 1), (2, 2)):
        monkeypatch.setattr(collector, "fetch_chain", lambda *args, bid=bid, ask=ask: {OCC: snap(bid, ask)})
        collector.collect_once(object(), ("A",), datetime(2026, 9, 30, tzinfo=timezone.utc), tmp_path)
    assert len(pd.read_parquet(tmp_path / "2026-09-30.parquet")) == 1


def test_symbol_exception_isolated(tmp_path, monkeypatch):
    def fetch(_, symbol, *args):
        if symbol == "B":
            raise RuntimeError("boom")
        return {OCC: snap()}
    monkeypatch.setattr(collector, "fetch_chain", fetch)
    collector.collect_once(object(), ("A", "B", "C"), datetime(2026, 9, 30, tzinfo=timezone.utc), tmp_path)
    assert set(pd.read_parquet(tmp_path / "2026-09-30.parquet")["underlying"]) == {"A", "C"}


def test_append(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch)
    run(tmp_path, monkeypatch)
    assert len(pd.read_parquet(tmp_path / "2026-09-30.parquet")) == 2


def test_position_failure_does_not_block_quotes(tmp_path, monkeypatch):
    class FailingTradingClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_all_positions(self):
            raise RuntimeError("positions unavailable")

    monkeypatch.setitem(__import__("sys").modules, "alpaca.trading.client", SimpleNamespace(TradingClient=FailingTradingClient))
    run(tmp_path, monkeypatch)
    assert (tmp_path / "2026-09-30.parquet").exists()


def test_unpadded_occ_expiration_for_various_ticker_lengths(tmp_path, monkeypatch):
    occs = {
        "A": "A260101C00100000",
        "AB": "AB260101C00100000",
        "SPY": "SPY260101C00100000",
        "AAPL": "AAPL260101C00100000",
        "ABCDE": "ABCDE260101C00100000",
    }

    def fetch(_, symbol, *args):
        return {occs[symbol]: snap()}

    monkeypatch.setattr(collector, "fetch_chain", fetch)
    collector.collect_once(object(), tuple(occs), datetime(2026, 1, 1, tzinfo=timezone.utc), tmp_path)
    frame = pd.read_parquet(tmp_path / "2026-01-01.parquet")
    assert frame["dte"].tolist() == [0] * len(occs)


def test_empty_chain_creates_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "fetch_chain", lambda *args: {})
    collector.collect_once(object(), ("A",), datetime(2026, 9, 30, tzinfo=timezone.utc), tmp_path)
    assert not (tmp_path / "2026-09-30.parquet").exists()
