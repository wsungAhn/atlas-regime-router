"""
Atlas Options Hackathon — 완전자동 실행 루프 (MCP 클라이언트, LLM 없음).

launchd로 장중에만 주기적으로 트리거된다(9:30~16:00 ET, launchd StartCalendarInterval
— 정확한 시각은 install_launchd.sh 참고). 매 사이클:
  1. Alpaca MCP `get_clock`으로 실제 장 개폐 확인(공휴일·조기폐장 자동 반영) — 닫혀
     있으면 아무것도 안 하고 조용히 종료(NOOP 로그만).
  2. Alpaca MCP 서버를 서브프로세스로 띄우고 stdio MCP 클라이언트로 접속
     (LLM이 도구를 호출하는 게 아니라, 이 결정론적 스크립트가 직접 MCP
     프로토콜로 place_option_order 등을 호출한다 — 대회요건 "Trading API +
     MCP 서버" 둘 다 충족하면서 완전 자동화도 깨지지 않는다)
  3. signals.py::decide_for_symbol()로 신호계산(순수 함수, 결정론적)
  4. 결정이 나오면 MCP get_account_info로 잔고 재확인 → place_option_order 호출
  5. 결과를 registry/decisions.jsonl(감사로그)와 logs/mcp_runner.log(운영로그)에 남긴다

# ponytail: 재시도·백오프는 없음(launchd가 다음 사이클에 자연 재시도) — 이 시스템은
# 사이클 1회 실패를 심각하게 다루지 않는다(장 시간 동안 여러 번 도니까). 프로덕션
# trader처럼 정교한 ready_queue가 필요하면 그건 대회 스코프 밖.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
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
RUNNER_LOG = REPO_ROOT / "logs" / "mcp_runner.log"
SYMBOLS = ("SPY", "QQQ")

logger = logging.getLogger("mcp_runner")


def _setup_logging() -> None:
    """콘솔(launchd StandardOutPath로도 잡힘) + 로테이팅 파일 핸들러 둘 다 단다 —
    launchd 로그만으로는 회전이 안 돼 무한정 커지므로 파일 쪽은 직접 회전시킨다."""
    RUNNER_LOG.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return  # 같은 프로세스에서 두 번 호출돼도 핸들러 중복 방지
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = RotatingFileHandler(RUNNER_LOG, maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)


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


async def _symbols_with_open_exposure(session: ClientSession) -> set[str]:
    """열린 포지션 + 대기 중(미체결) 주문의 기초자산 심볼 집합. OCC 옵션심볼은
    항상 기초자산 티커로 시작하므로(예: "SPY260321P00600000") prefix 매칭으로
    충분하다 — SPY/QQQ처럼 서로 다른 심볼끼리 겹칠 일이 없다."""
    exposed: set[str] = set()
    positions = _mcp_data(await session.call_tool("get_all_positions", {})).get("result", [])
    for p in positions:
        sym = p.get("symbol", "")
        for base in SYMBOLS:
            if sym.startswith(base):
                exposed.add(base)
    orders = _mcp_data(await session.call_tool("get_orders", {"status": "open"})).get("result", [])
    for o in orders:
        legs = o.get("legs") or [o]
        for leg in legs:
            sym = leg.get("symbol", "")
            for base in SYMBOLS:
                if sym.startswith(base):
                    exposed.add(base)
    return exposed


async def _new_mcp_session():
    env_file = REPO_ROOT / ".env.competition"
    params = StdioServerParameters(
        command=str(REPO_ROOT / ".venv/bin/alpaca-mcp-server"),
        args=["--env-file", str(env_file)],
        cwd=str(REPO_ROOT),
    )
    return stdio_client(params)


async def run_cycle_once() -> None:
    client_cm = await _new_mcp_session()
    async with client_cm as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            clock = _mcp_data(await session.call_tool("get_clock", {}))
            if not clock.get("is_open"):
                logger.info("[NOOP] market closed (next_open=%s) — skipping cycle", clock.get("next_open"))
                return

            equity = float(_mcp_data(await session.call_tool("get_account_info", {}))["equity"])
            logger.info("[CYCLE] market open, equity=%.2f", equity)

            stock_client = StockHistoricalDataClient(
                os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
            )
            option_client = OptionHistoricalDataClient(
                os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
            )
            macro: MacroGate = load_macro_gate()
            logger.info("[MACRO] ok=%s reason=%s stage=%s", macro.ok, macro.reason, macro.stage)

            symbols_with_exposure = await _symbols_with_open_exposure(session)
            if symbols_with_exposure:
                logger.info("[EXPOSURE] already open/pending: %s", sorted(symbols_with_exposure))

            for symbol in SYMBOLS:
                if symbol in symbols_with_exposure:
                    # 15분마다 도는데 레짐이 몇 시간 지속되면 매 사이클 새 포지션을
                    # 계속 쌓게 된다 — 이미 열린 포지션(체결됨) 또는 대기 중인 주문이
                    # 있으면 그 심볼은 이번 사이클을 건너뛴다. 심볼 단위 배타 진입
                    # (한 시점에 종목당 최대 1개)이지 더 정교한 슬롯 관리는 아니다
                    # — 대회 8일 스코프에서는 이 정도가 "과다 진입 방지"의 최소선.
                    logger.info("[SKIP] %s already has open position/order — no re-entry", symbol)
                    _log_decision({"symbol": symbol, "submitted": False, "skip_reason": "already_exposed"})
                    continue
                try:
                    decision = decide_for_symbol(stock_client, option_client, symbol, equity, macro)
                except Exception:
                    logger.exception("[ERROR] decide_for_symbol failed for %s — skipping this symbol", symbol)
                    _log_decision({"symbol": symbol, "submitted": False, "skip_reason": "decision_error"})
                    continue

                if decision.order_intent is None:
                    logger.info("[SKIP] %s regime=%s reason=%s", symbol, decision.regime, decision.skip_reason)
                    _log_decision({
                        "symbol": symbol, "regime": decision.regime,
                        "macro_reason": decision.macro_gate.reason,
                        "skip_reason": decision.skip_reason, "submitted": False,
                    })
                    continue

                try:
                    order_result = await session.call_tool("place_option_order", decision.order_intent)
                    order_payload = _mcp_data(order_result)
                except Exception:
                    logger.exception("[ERROR] place_option_order failed for %s", symbol)
                    _log_decision({
                        "symbol": symbol, "regime": decision.regime,
                        "order_intent": decision.order_intent, "submitted": False,
                        "skip_reason": "order_submit_error",
                    })
                    continue

                logger.info("[SUBMIT] %s regime=%s intent=%s", symbol, decision.regime, decision.order_intent)
                _log_decision({
                    "symbol": symbol, "regime": decision.regime,
                    "macro_reason": decision.macro_gate.reason,
                    "order_intent": decision.order_intent,
                    "order_result": order_payload, "submitted": True,
                })


async def main() -> None:
    _setup_logging()
    try:
        await run_cycle_once()
    except Exception:
        logger.exception("[FATAL] cycle crashed before completing")
        # 종료코드 0으로 두지 않는다 — launchd 로그·ThrottleInterval이 이 실패를 보고
        # 다음 스케줄까지 정상 재시도하게 둔다(예외를 삼키면 조용한 무한장애가 됨).
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
