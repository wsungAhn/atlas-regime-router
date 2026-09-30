"""Utilities for parsing live option trading decisions into episodes."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("ts must be an ISO8601 string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _legs(order_intent: Any) -> list[str] | None:
    if not isinstance(order_intent, dict) or not isinstance(order_intent.get("legs"), list):
        return None
    symbols = [leg.get("symbol") for leg in order_intent["legs"] if isinstance(leg, dict)]
    if any(not isinstance(symbol, str) for symbol in symbols):
        return None
    return symbols


def _episode(entry: dict[str, Any], closes: list[dict[str, Any]], legs_unverified: bool) -> dict[str, Any]:
    first = closes[0]
    entry_ts = entry["_parsed_ts"]
    exit_ts = first["_parsed_ts"]
    has_profit = "profit_pct" in first
    if len(closes) == 1:
        kind = "clean" if has_profit else "single_limit"
    else:
        kind = "retry"
    reasons = {close.get("exit_reason") for close in closes}
    return {
        "symbol": entry["symbol"],
        "entry_ts": entry_ts,
        "regime": entry.get("regime"),
        "legs": entry["_legs"],
        "n_legs": len(entry["_legs"]),
        "qty": int(entry["order_intent"]["qty"]),
        "limit_credit": abs(float(entry["order_intent"]["limit_price"])),
        "kind": kind,
        "reason_flip": len(reasons) > 1 if len(closes) > 1 else False,
        "legs_unverified": legs_unverified,
        "exit_reason": first.get("exit_reason"),
        "exit_ts": exit_ts,
        "minutes_held": (exit_ts - entry_ts).total_seconds() / 60.0,
        "trigger_profit_pct": first.get("profit_pct") if has_profit else None,
        "live_close_path": "limit_fallback" if "limit_fallback_price" in first else "market",
        "n_close_records": len(closes),
    }


def load_live_episodes(path: str | Path) -> tuple[list[dict], dict[str, int]]:
    """Load option entries and their subsequent close records as position episodes."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"decision log not found: {source}")

    records: list[dict[str, Any]] = []
    exclusion_counts: Counter[str] = Counter()
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("record must be an object")
                record["_parsed_ts"] = _parse_timestamp(record.get("ts"))
                records.append(record)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                exclusion_counts["malformed_lines"] += 1
                LOGGER.warning("Skipping malformed decision line %d: %s", line_number, exc)

    records.sort(key=lambda record: record["_parsed_ts"])
    episodes: list[dict[str, Any]] = []
    active: dict[str, tuple[dict[str, Any], list[dict[str, Any]], bool]] = {}

    def finish(symbol: str) -> None:
        state = active.pop(symbol, None)
        if state is None:
            return
        entry, closes, unverified = state
        if not closes:
            exclusion_counts["open_no_close"] += 1
            return
        episodes.append(_episode(entry, closes, unverified))

    for record in records:
        if record.get("sleeve") == "crypto":
            exclusion_counts["crypto"] += 1
            continue

        is_close = record.get("action") == "close" and record.get("submitted") is True
        is_entry = (
            record.get("submitted") is True
            and record.get("action") != "close"
            and record.get("sleeve") is None
            and isinstance(record.get("order_intent"), dict)
        )
        if is_entry:
            if isinstance(record.get("order_result"), dict) and "error" in record["order_result"]:
                exclusion_counts["entry_with_error"] += 1
                continue
            entry_legs = _legs(record["order_intent"])
            if entry_legs is None:
                continue
            symbol = record.get("symbol")
            if not isinstance(symbol, str):
                continue
            finish(symbol)
            record["_legs"] = entry_legs
            active[symbol] = (record, [], False)
            continue

        if not is_close:
            continue
        symbol = record.get("symbol")
        if not isinstance(symbol, str) or symbol not in active:
            continue
        entry, closes, unverified = active[symbol]
        close_legs = _legs(record.get("order_intent"))
        if close_legs is not None and set(close_legs) != set(entry["_legs"]):
            exclusion_counts["leg_mismatch"] += 1
            continue
        if close_legs is None:
            unverified = True
        closes.append(record)
        active[symbol] = (entry, closes, unverified)

    for symbol in list(active):
        finish(symbol)

    return episodes, dict(exclusion_counts)


def limit_fallback_rate_by_symbol(episodes: list[dict]) -> dict[str, float]:
    """Return each symbol's limit-fallback share among episodes with closes."""
    totals: Counter[str] = Counter()
    fallbacks: Counter[str] = Counter()
    for episode in episodes:
        if not episode.get("n_close_records"):
            continue
        symbol = episode["symbol"]
        totals[symbol] += 1
        if episode.get("live_close_path") == "limit_fallback":
            fallbacks[symbol] += 1
    return {symbol: fallbacks[symbol] / totals[symbol] for symbol in totals}


def pooled_limit_fallback_rate(episodes: list[dict]) -> float:
    """Return the pooled limit-fallback share among episodes with closes."""
    with_closes = [episode for episode in episodes if episode.get("n_close_records")]
    if not with_closes:
        return 0.0
    return sum(episode.get("live_close_path") == "limit_fallback" for episode in with_closes) / len(with_closes)
