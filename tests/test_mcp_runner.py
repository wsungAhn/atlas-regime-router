"""mcp_runner.py의 순수 로직(파싱·중복진입 방지) 유닛테스트 — MCP 서버 실행 없이."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_runner import _mcp_data, _symbols_with_open_exposure  # noqa: E402


def _tool_result(payload: dict):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"data": payload}))])


class _FakeSession:
    def __init__(self, positions: list[dict], orders: list[dict]):
        self._positions = positions
        self._orders = orders

    async def call_tool(self, name: str, args: dict):
        if name == "get_all_positions":
            return _tool_result({"result": self._positions})
        if name == "get_orders":
            return _tool_result({"result": self._orders})
        raise AssertionError(f"unexpected tool call: {name}")


def test_mcp_data_unwraps_alpaca_envelope():
    result = _tool_result({"equity": "100000"})
    assert _mcp_data(result) == {"equity": "100000"}


@pytest.mark.asyncio
async def test_no_exposure_when_no_positions_or_orders():
    session = _FakeSession(positions=[], orders=[])
    exposed = await _symbols_with_open_exposure(session)
    assert exposed == set()


@pytest.mark.asyncio
async def test_open_position_blocks_reentry_for_that_symbol_only():
    session = _FakeSession(
        positions=[{"symbol": "SPY260321P00600000", "qty": "-2"}],
        orders=[],
    )
    exposed = await _symbols_with_open_exposure(session)
    assert exposed == {"SPY"}


@pytest.mark.asyncio
async def test_pending_multileg_order_blocks_reentry():
    """미체결 mleg 주문은 legs 리스트 안에 심볼이 있다 — 최상위 symbol 필드가
    없어도 레그를 훑어서 잡아야 한다(이게 없으면 주문 낸 직후 다음 15분
    사이클에서 같은 종목에 또 진입해버림)."""
    session = _FakeSession(
        positions=[],
        orders=[{"legs": [{"symbol": "QQQ260321C00500000"}, {"symbol": "QQQ260321C00510000"}]}],
    )
    exposed = await _symbols_with_open_exposure(session)
    assert exposed == {"QQQ"}
