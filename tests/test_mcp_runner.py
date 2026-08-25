"""mcp_runner.py의 순수 로직(파싱·중복진입 방지·포지션 그룹핑) 유닛테스트 — MCP 서버 실행 없이."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_runner import (  # noqa: E402
    _cancel_stale_orders,
    _mcp_data,
    _order_error,
    _positions_by_symbol,
    _symbols_with_open_orders,
    _underlying_of,
)


def _tool_result(payload: dict):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"data": payload}))])


class _FakeSession:
    def __init__(self, positions: list[dict], orders: list[dict]):
        self._positions = positions
        self._orders = orders
        self.cancelled_order_ids: list[str] = []

    async def call_tool(self, name: str, args: dict):
        if name == "get_all_positions":
            return _tool_result({"result": self._positions})
        if name == "get_orders":
            return _tool_result({"result": self._orders})
        if name == "cancel_order_by_id":
            self.cancelled_order_ids.append(args["order_id"])
            return _tool_result({"result": "ok"})
        raise AssertionError(f"unexpected tool call: {name}")


def test_mcp_data_unwraps_alpaca_envelope():
    result = _tool_result({"equity": "100000"})
    assert _mcp_data(result) == {"equity": "100000"}


def test_order_error_detects_broker_rejection():
    """2026-08-25 07:15 실거래 발견 — 브로커가 주문을 거부해도 MCP 도구 호출
    자체는 예외를 안 던지고 {"error": {...}}가 담긴 정상 응답으로 돌아온다.
    이 헬퍼 없이는 거부된 주문이 submitted=true로 로깅됐다(실측: SPY 주문이
    "account not eligible to trade uncovered option contracts"로 거부됐는데도
    [SUBMIT] 로그가 찍힘)."""
    rejected = {"error": {"message": "API rejected the order", "http_status": 403,
                           "detail": {"code": 40310000, "message": "account not eligible"}}}
    assert _order_error(rejected) is not None
    assert "API rejected" in _order_error(rejected)


def test_order_error_none_when_no_error_key():
    assert _order_error({"id": "abc", "status": "filled"}) is None


def test_underlying_of_matches_occ_prefix():
    assert _underlying_of("SPY260321P00600000") == "SPY"
    assert _underlying_of("QQQ260321C00500000") == "QQQ"
    assert _underlying_of("AAPL260321C00150000") is None  # 관리 대상 심볼 아님


@pytest.mark.asyncio
async def test_positions_by_symbol_groups_legs_of_same_underlying():
    session = _FakeSession(
        positions=[
            {"symbol": "SPY260321P00600000", "side": "short"},
            {"symbol": "SPY260321P00590000", "side": "long"},
            {"symbol": "QQQ260321C00500000", "side": "short"},
        ],
        orders=[],
    )
    grouped = await _positions_by_symbol(session)
    assert len(grouped["SPY"]) == 2
    assert len(grouped["QQQ"]) == 1


@pytest.mark.asyncio
async def test_no_open_orders_when_none_pending():
    session = _FakeSession(positions=[], orders=[])
    exposed = await _symbols_with_open_orders(session)
    assert exposed == set()


@pytest.mark.asyncio
async def test_pending_multileg_order_blocks_reentry():
    """미체결 mleg 주문은 legs 리스트 안에 심볼이 있다 — 최상위 symbol 필드가
    없어도 레그를 훑어서 잡아야 한다(이게 없으면 주문 낸 직후 다음 15분
    사이클에서 같은 종목에 또 진입해버림)."""
    session = _FakeSession(
        positions=[],
        orders=[{"legs": [{"symbol": "QQQ260321C00500000"}, {"symbol": "QQQ260321C00510000"}]}],
    )
    exposed = await _symbols_with_open_orders(session)
    assert exposed == {"QQQ"}


@pytest.mark.asyncio
async def test_cancel_stale_orders_cancels_and_frees_symbol_after_threshold():
    """2026-08-25 실측 발견 회귀 방지 — GLD 주문이 63분, IWM 주문이 48분째
    미체결로 방치되고 있었다(취소·재시도가 전혀 없었음). STALE_ORDER_MAX_MINUTES
    (30분)보다 오래된 주문은 취소하고, 그 심볼을 재시도 가능하게 풀어줘야 한다."""
    now = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    old_order = {
        "id": "order-old", "submitted_at": (now - timedelta(minutes=45)).isoformat(),
        "legs": [{"symbol": "GLD260901P00400000"}, {"symbol": "GLD260901P00390000"}],
    }
    session = _FakeSession(positions=[], orders=[old_order])
    freed = await _cancel_stale_orders(session, [old_order], now=now)
    assert freed == {"GLD"}
    assert session.cancelled_order_ids == ["order-old"]


@pytest.mark.asyncio
async def test_cancel_stale_orders_leaves_fresh_orders_alone():
    now = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    fresh_order = {
        "id": "order-fresh", "submitted_at": (now - timedelta(minutes=10)).isoformat(),
        "legs": [{"symbol": "QQQ260321C00500000"}],
    }
    session = _FakeSession(positions=[], orders=[fresh_order])
    freed = await _cancel_stale_orders(session, [fresh_order], now=now)
    assert freed == set()
    assert session.cancelled_order_ids == []


@pytest.mark.asyncio
async def test_symbols_with_open_orders_frees_stale_symbol_for_same_cycle_retry():
    """오래된 주문이 취소되면 같은 사이클 안에서 그 종목이 노출 집합에서
    빠져야 한다 — 그래야 decide_for_symbol이 새 가격으로 바로 재시도한다."""
    now = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    import mcp_runner
    original_now = mcp_runner.datetime

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    mcp_runner.datetime = _FixedDatetime
    try:
        old_order = {
            "id": "order-old", "submitted_at": (now - timedelta(minutes=45)).isoformat(),
            "legs": [{"symbol": "GLD260901P00400000"}],
        }
        session = _FakeSession(positions=[], orders=[old_order])
        exposed = await _symbols_with_open_orders(session)
        assert exposed == set()
        assert session.cancelled_order_ids == ["order-old"]
    finally:
        mcp_runner.datetime = original_now
