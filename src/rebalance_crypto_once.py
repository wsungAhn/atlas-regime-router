"""일회성 크립토 비중 강제 정리 — 2026-09-05 사용자 지시: "크립토가 또다시
메인종목이 됐다" 지적 후 CRYPTO_SLEEVE_BUDGET_PCT_OF_MAX(10%)로 축소했는데,
그 축소 전 예산으로 이미 들어간 기존 포지션(BTC/ETH/PAXG)은 자동으로 안
줄어든다 — 손절/익절이 그 전에 자연스럽게 청산 안 시키면, 장이 다시 열리는
2026-09-08(화) 06:45 PDT에 이 스크립트가 강제로 새 비중에 맞춰 판다.

config/launchd/com.atlas.crypto-rebalance-2026-09-08.plist로 딱 한 번만 실행되게
등록(StartCalendarInterval에 정확한 날짜 지정 — launchd가 그 시각이 지나면 다시
안 돈다, 별도 unload 불필요). 수동 재실행: .venv/bin/python src/rebalance_crypto_once.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from mcp_runner import _mcp_data, _new_mcp_session  # noqa: E402
from mcp import ClientSession  # noqa: E402
from signals import (  # noqa: E402
    CASH_RESERVE_PCT,
    CRYPTO_CORRELATION_CLUSTER_THRESHOLD,
    CRYPTO_CORRELATION_PAIRS,
    CRYPTO_SLEEVE_BUDGET_PCT_OF_MAX,
    CRYPTO_SYMBOLS,
    build_crypto_close_intent,
    rebalance_targets_for_cluster_cap,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rebalance_crypto_once")

MIN_TRIM_NOTIONAL = 10.0  # Alpaca 크립토 시장가 최소주문 근사치 — decide_crypto_for_symbol과 동일 기준
QTY_INCREMENT = 0.0001


def _sell_qty_for_notional(current_qty: float, current_price: float, sell_notional: float) -> float:
    return max(0.0, (sell_notional / current_price // QTY_INCREMENT) * QTY_INCREMENT)


async def run() -> None:
    import json

    async with await _new_mcp_session() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            positions_payload = _mcp_data(await session.call_tool("get_all_positions", {}))
            positions = positions_payload.get("result", [])
            by_stripped = {p["symbol"].replace("/", ""): p for p in positions if p.get("symbol")}

            open_notional_by_symbol = {}
            pos_by_symbol = {}
            for symbol in CRYPTO_SYMBOLS:
                p = by_stripped.get(symbol.replace("/", ""))
                if p is None:
                    continue
                open_notional_by_symbol[symbol] = float(p["market_value"])
                pos_by_symbol[symbol] = p

            if not open_notional_by_symbol:
                logger.info("[NOOP] no open crypto positions — nothing to rebalance")
                return

            account_info = _mcp_data(await session.call_tool("get_account_info", {}))
            equity = float(account_info["equity"])
            slot_budget = equity * (1.0 - CASH_RESERVE_PCT) * CRYPTO_SLEEVE_BUDGET_PCT_OF_MAX / len(CRYPTO_SYMBOLS)

            targets = rebalance_targets_for_cluster_cap(
                open_notional_by_symbol, CRYPTO_CORRELATION_PAIRS, CRYPTO_CORRELATION_CLUSTER_THRESHOLD, slot_budget,
            )
            logger.info("[STATE] equity=%.2f slot_budget=%.2f current=%s targets=%s", equity, slot_budget, open_notional_by_symbol, targets)

            for symbol, current_notional in open_notional_by_symbol.items():
                target_notional = targets[symbol]
                sell_notional = current_notional - target_notional
                if sell_notional < MIN_TRIM_NOTIONAL:
                    logger.info("[SKIP] %s already within target (current=%.2f target=%.2f)", symbol, current_notional, target_notional)
                    continue
                pos = pos_by_symbol[symbol]
                qty = _sell_qty_for_notional(float(pos["qty"]), float(pos["current_price"]), sell_notional)
                if qty <= 0:
                    continue
                cid = f"atlas-rebalance-{symbol.replace('/', '')}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
                intent = build_crypto_close_intent(symbol, f"{qty:.4f}", cid)
                logger.info("[TRIM] %s selling qty=%.4f (~$%.2f) intent=%s", symbol, qty, sell_notional, intent)
                result = _mcp_data(await session.call_tool("place_crypto_order", intent))
                logger.info("[RESULT] %s -> %s", symbol, json.dumps(result))


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
