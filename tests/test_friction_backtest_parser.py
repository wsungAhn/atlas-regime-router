"""Tests for live option episode parsing."""

import json
from pathlib import Path

import pytest

from src.friction_backtest import (
    limit_fallback_rate_by_symbol,
    load_live_episodes,
    pooled_limit_fallback_rate,
)


BASE_TS = "2026-09-30T17:00:00+00:00"


def _record(symbol="A", offset=0, **kwargs):
    from datetime import datetime, timedelta

    ts = datetime.fromisoformat(BASE_TS) + timedelta(minutes=offset)
    record = {"symbol": symbol, "ts": ts.isoformat(), "submitted": True}
    record.update(kwargs)
    return record


def _entry(symbol="A", offset=0, legs=None, **kwargs):
    legs = legs or [f"{symbol}-LEG"]
    values = {
        "order_intent": {"order_class": "mleg", "limit_price": "-1.25", "qty": "2", "legs": [{"symbol": leg} for leg in legs]},
        "order_result": {"id": "ok"},
        "regime": "normal",
    }
    values.update(kwargs)
    return _record(symbol, offset, **values)


def _close(symbol="A", offset=1, legs=None, **kwargs):
    values = {"action": "close", "exit_reason": "profit_target"}
    values.update(kwargs)
    record = _record(symbol, offset, **values)
    if legs is not None:
        record["order_intent"] = {"type": "market", "legs": [{"symbol": leg} for leg in legs]}
    return record


def _load(tmp_path: Path, records, malformed=False):
    path = tmp_path / "decisions.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        if malformed:
            handle.write("{not json}\n")
    return load_live_episodes(path)


def test_entry_error_is_excluded(tmp_path):
    episodes, counts = _load(tmp_path, [_entry(order_result={"error": "rejected"})])
    assert episodes == []
    assert counts["entry_with_error"] == 1


def test_crypto_entry_and_close_are_excluded(tmp_path):
    records = [_entry(sleeve="crypto"), _close(sleeve="crypto", offset=3, exit_reason="max_hold_days")]
    episodes, counts = _load(tmp_path, records)
    assert episodes == []
    assert counts["crypto"] == 2


def test_leg_mismatch_is_excluded(tmp_path):
    episodes, counts = _load(tmp_path, [_entry(legs=["A-1", "A-2"]), _close(legs=["A-1", "A-3"], profit_pct=0.1)])
    assert episodes == []
    assert counts["leg_mismatch"] == 1
    assert counts["open_no_close"] == 1


def test_legacy_limit_close_is_unverified(tmp_path):
    episodes, _ = _load(tmp_path, [_entry(), _close(limit_fallback_price=1.0)])
    assert episodes[0]["legs_unverified"] is True
    assert episodes[0]["kind"] == "single_limit"


def test_clean_and_single_limit_kinds(tmp_path):
    records = [_entry("A"), _close("A", profit_pct=0.2), _entry("B", 10), _close("B", 11, limit_fallback_price=1.0)]
    episodes, _ = _load(tmp_path, records)
    assert [episode["kind"] for episode in episodes] == ["clean", "single_limit"]


def test_retry_uses_first_close_event_and_reason_flip(tmp_path):
    records = [_entry()]
    for offset in range(1, 21):
        records.append(_close(offset=offset, exit_reason="profit_target" if offset % 2 else "stop_loss", **({} if offset == 1 else {"profit_pct": 0.1})))
    episodes, _ = _load(tmp_path, records)
    episode = episodes[0]
    assert episode["kind"] == "retry"
    assert episode["reason_flip"] is True
    assert episode["exit_reason"] == "profit_target"
    assert episode["exit_ts"].minute == 1
    assert episode["minutes_held"] == 1.0
    assert episode["trigger_profit_pct"] is None


def test_entries_split_episodes_and_sort_input(tmp_path):
    records = [_entry(offset=10), _close(offset=11, profit_pct=0.1), _entry(offset=0), _close(offset=1, profit_pct=0.2)]
    episodes, _ = _load(tmp_path, list(reversed(records)))
    assert len(episodes) == 2
    assert [episode["trigger_profit_pct"] for episode in episodes] == [0.2, 0.1]


def test_open_entry_is_counted(tmp_path):
    episodes, counts = _load(tmp_path, [_entry()])
    assert episodes == []
    assert counts["open_no_close"] == 1


def test_limit_fallback_rates():
    episodes = [
        {"symbol": "A", "n_close_records": 1, "live_close_path": "limit_fallback"},
        {"symbol": "A", "n_close_records": 1, "live_close_path": "market"},
        {"symbol": "A", "n_close_records": 1, "live_close_path": "market"},
        {"symbol": "A", "n_close_records": 1, "live_close_path": "market"},
        {"symbol": "B", "n_close_records": 0, "live_close_path": "market"},
    ]
    assert limit_fallback_rate_by_symbol(episodes) == {"A": 0.25}
    assert pooled_limit_fallback_rate(episodes) == 0.25
    assert pooled_limit_fallback_rate([]) == 0.0


def test_malformed_line_is_counted(tmp_path):
    episodes, counts = _load(tmp_path, [_entry(), _close(profit_pct=0.1)], malformed=True)
    assert len(episodes) == 1
    assert counts["malformed_lines"] == 1


def test_missing_file_is_explicit(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_live_episodes(tmp_path / "missing.jsonl")
