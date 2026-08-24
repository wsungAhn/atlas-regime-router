"""
Atlas Options Hackathon — 완전자동 실행 루프 (MCP 클라이언트, LLM 없음).

이 스크립트는 launchd/cron으로 스케줄돼 사람 개입 없이 돈다. 매 사이클:
  1. Alpaca MCP 서버를 서브프로세스로 띄우고 stdio MCP 클라이언트로 접속
     (LLM이 도구를 호출하는 게 아니라, 이 결정론적 스크립트가 직접 MCP
     프로토콜로 place_option_order 등을 호출한다 — 대회요건 "Trading API +
     MCP 서버" 둘 다 충족하면서 완전 자동화도 깨지지 않는다)
  2. signals.py::decide_for_symbol()로 신호계산(순수 함수, 결정론적)
  3. 결정이 나오면 MCP get_account로 잔고 재확인 → place_option_order 호출
  4. 결과를 registry/decisions.jsonl에 감사로그로 남긴다

# ponytail: 재시도·백오프는 없음(cron이 다음 사이클에 자연 재시도) — 이 시스템은
# 사이클 실패를 심각하게 다루지 않는다(장 시간 동안 여러 번 도니까). 프로덕션
# trader처럼 정교한 ready_queue가 필요하면 그건 대회 스코프 밖.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signals import (  # noqa: E402
    MacroGate,
    decide_for_symbol,
    load_macro_gate,
)
from alpaca.data.historical.option import OptionHistoricalDataClient  # noqa: E402
from alpaca.data.historical.stock import StockHistoricalDataClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DECISION_LOG = REPO_ROOT / "registry" / "decisions.jsonl"
SYMBOLS = ("SPY", "QQQ")


def _mcp_data(tool_result) -> dict:
    """alpaca-mcp-server는 모든 응답을 {"_alpaca_mcp_security": {...}, "data": {...}}로
    감싼다 — 이 헬퍼가 실제 페이로드만 벗겨낸다(실측 확인, 문서에 안 나와 있었음)."""
    payload = json.loads(tool_result.content[0].text)
    return payload["data"]


def _log_decision(record: dict) -> None:
    DECISION_LOG.parent.mkdir(parents=True, exist_ok=True)
    record["ts"] = datetime.now(timezone.utc).isoformat()
    with open(DECISION_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


async def run_cycle_once() -> None:
    stock_client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    )
    option_client = OptionHistoricalDataClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    )
    macro: MacroGate = load_macro_gate()

    env_file = REPO_ROOT / ".env.competition"
    params = StdioServerParameters(
        command=str(REPO_ROOT / ".venv/bin/alpaca-mcp-server"),
        args=["--env-file", str(env_file)],
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            equity_result = await session.call_tool("get_account_info", {})
            equity = float(_mcp_data(equity_result)["equity"])

            for symbol in SYMBOLS:
                decision = decide_for_symbol(
                    stock_client, option_client, symbol, equity, macro
                )
                if decision.order_intent is None:
                    _log_decision({
                        "symbol": symbol, "regime": decision.regime,
                        "macro_reason": decision.macro_gate.reason,
                        "skip_reason": decision.skip_reason, "submitted": False,
                    })
                    continue

                order_result = await session.call_tool(
                    "place_option_order", decision.order_intent
                )
                order_payload = _mcp_data(order_result)
                _log_decision({
                    "symbol": symbol, "regime": decision.regime,
                    "macro_reason": decision.macro_gate.reason,
                    "order_intent": decision.order_intent,
                    "order_result": order_payload, "submitted": True,
                })


if __name__ == "__main__":
    asyncio.run(run_cycle_once())
