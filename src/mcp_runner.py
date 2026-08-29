"""
Atlas Options Hackathon — 완전자동 실행 루프 (MCP 클라이언트, LLM 없음).

launchd로 장중에만 주기적으로 트리거된다(9:30~16:00 ET, launchd StartCalendarInterval
— 정확한 시각은 config/launchd/com.atlas.options-runner.plist 참고). 매 사이클:
  1. Alpaca MCP `get_clock`으로 실제 장 개폐 확인(공휴일·조기폐장 자동 반영) — 닫혀
     있으면 아무것도 안 하고 조용히 종료(NOOP 로그만).
  2. Alpaca MCP 서버를 서브프로세스로 띄우고 stdio MCP 클라이언트로 접속
     (LLM이 도구를 호출하는 게 아니라, 이 결정론적 스크립트가 직접 MCP
     프로토콜로 place_option_order 등을 호출한다 — 대회요건 "Trading API +
     MCP 서버" 둘 다 충족하면서 완전 자동화도 깨지지 않는다)
  3. **청산 감시부터** — 열린 포지션마다 signals.py::evaluate_exit()로 익절/손절/
     DTE임박 판단, 해당되면 build_close_intent()로 반대매매 주문 제출(Alpaca
     멀티레그 주문은 브라켓을 지원 안 해서 이 감시를 사이클마다 직접 해야 한다
     — 2026-08-24 뒤늦게 발견한 진짜 공백, 처음엔 진입만 있고 청산이 없었다)
  4. **신규진입** — signals.py::decide_for_symbol()로 신호계산(순수 함수,
     결정론적), 이미 포지션/미체결주문 있는 심볼은 건너뜀
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
    CRYPTO_SYMBOLS,
    CryptoPositionState,
    MacroGate,
    RiskGateDecision,
    RiskGateState,
    build_close_intent,
    build_crypto_close_intent,
    close_limit_price,
    decide_crypto_for_symbol,
    decide_for_symbol,
    evaluate_crypto_exit,
    evaluate_exit,
    evaluate_risk_gates,
    load_macro_gate,
)
from alpaca.data.requests import OptionLatestQuoteRequest  # noqa: E402
from alpaca.data.historical.crypto import CryptoHistoricalDataClient  # noqa: E402
from alpaca.data.historical.option import OptionHistoricalDataClient  # noqa: E402
from alpaca.data.historical.stock import StockHistoricalDataClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DECISION_LOG = REPO_ROOT / "registry" / "decisions.jsonl"
RUNNER_LOG = REPO_ROOT / "logs" / "mcp_runner.log"
RISK_GATE_STATE_PATH = REPO_ROOT / "registry" / "risk_gate_state.json"
CRYPTO_POSITIONS_PATH = REPO_ROOT / "registry" / "crypto_positions.json"
ENV_FILE = REPO_ROOT / ".env.competition"
SYMBOLS = ("SPY", "QQQ", "GLD", "TLT", "SLV", "IWM")  # 2026-08-25: 지수 2 +
# 금(GLD)+장기채(TLT)+은(SLV)+소형주(IWM) 4개 추가 — SPY/QQQ만 있으면 "동일종목
# 여러 개 보유"와 리스크 성격이 비슷해서(사용자 지적) 진짜 다른 자산군으로 분산.
# 6종목 모두 일봉 백테스트(P&L 실측) + 라이브 옵션체인 유동성 드라이런 둘 다
# 통과. **XLE/XLF/EEM은 백테스트에서 이 챔피언 전략으로 손실이 나서 제외**
# (각각 -$17k/-$21k/-$20k, 3yr) — "검증됐다"고 우기지 않고 실제로 진 걸 안 씀.
# VNQ/HYG는 백테스트는 좋았지만(+$52k/+$36k) 실제 옵션체인이 5~9일 위클리
# 만기를 못 채워서(chain_insufficient) 제외 — 백테스트가 못 보는 걸 라이브
# 드라이런이 잡아낸 경우. DIA는 손익이 사실상 0(+$1.6k/3yr)이라 추가 가치 없어
# 제외. 목표였던 8종목(최대 80% 예산)엔 못 미치지만, 검증 안 된 걸 억지로
# 채우는 것보다 정직한 6종목(최대 60%)이 낫다고 판단.


def _load_env_file(path: Path) -> None:
    """launchd는 셸 프로필을 안 거쳐서 .env.competition을 자동으로 안 읽는다.
    alpaca-mcp-server 서브프로세스는 --env-file로 자체 로드하지만, 이 프로세스
    자신도 StockHistoricalDataClient/OptionHistoricalDataClient를 직접 생성하려고
    os.environ["ALPACA_API_KEY"]를 그대로 읽는다 — 2026-08-25 07:15 첫 실거래
    사이클이 이 누락으로 KeyError 크래시(장 열림 확인·잔고조회까지는 성공하고
    시장데이터 클라이언트 생성에서 죽음). generate_report.py엔 같은 문제를 전날
    밤에 미리 고쳐놓고 정작 이 파일엔 빠뜨렸었다."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file(ENV_FILE)

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


def _order_error(payload: dict) -> str | None:
    """place_option_order가 브로커에 거부당해도(계좌 옵션등급 부족, 마진부족 등)
    MCP 도구 호출 자체는 예외를 안 던진다 — 응답 데이터 안에 {"error": {...}}로
    담겨 정상 반환된다. **2026-08-25 07:15 첫 실거래에서 실측**: SPY 주문이
    "account not eligible to trade uncovered option contracts"(403)로 거부됐는데
    로그·decisions.jsonl엔 submitted=true/[SUBMIT]로 찍혀 있었다 — 이 함수 없이는
    거부된 주문이 "성공"으로 조용히 기록된다. QQQ 동일 구조 주문은 정상 체결돼서
    계좌 전체 문제가 아니라 이 주문 하나의 문제였음을 확인."""
    if isinstance(payload, dict) and "error" in payload:
        err = payload["error"]
        if isinstance(err, dict):
            return f"{err.get('message', 'unknown')} (detail: {err.get('detail')})"
        return str(err)
    return None


def _log_decision(record: dict) -> None:
    DECISION_LOG.parent.mkdir(parents=True, exist_ok=True)
    record["ts"] = datetime.now(timezone.utc).isoformat()
    with open(DECISION_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def _load_risk_gate_state(equity: float) -> tuple[RiskGateState | None, bool]:
    """프로세스가 사이클마다 새로 뜨므로 상태는 파일로만 지속된다. 파일이
    없으면(최초 실행) 지금 잔고를 HWM으로 삼아 새로 시작 — 정지 없음.

    **2026-08-25 Codex 감사 지적, fail-closed로 수정**: 파일이 손상됐을 때
    예전엔 HWM을 현재 잔고로 "리셋"했다 — 이러면 실제로 -20% 아래로 빠져있는
    상태에서 손상이 나면 서킷브레이커가 조용히 풀려버린다(fail-open, 정확히
    이 게이트가 막아야 할 상황을 손상 하나로 우회). 이제 손상 시 (None, True)를
    반환 — 호출자는 신규진입 전체를 이번 사이클 무조건 차단하고, 파일도
    덮어쓰지 않는다(사후분석 위해 원본 보존)."""
    if not RISK_GATE_STATE_PATH.exists():
        return RiskGateState(high_water_mark=equity), False
    try:
        return RiskGateState.from_dict(json.loads(RISK_GATE_STATE_PATH.read_text())), False
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.exception("[ERROR] risk_gate_state.json corrupt — fail-closed (신규진입 전체 차단, 파일은 안 건드림)")
        return None, True


def _save_risk_gate_state(state: RiskGateState) -> None:
    """임시파일에 쓰고 os.replace로 원자적 치환 — 사이클 중간에 프로세스가
    죽어도(launchd kill, crash) 절반만 쓰인 손상 파일이 안 남는다."""
    RISK_GATE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = RISK_GATE_STATE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state.to_dict()))
    os.replace(tmp_path, RISK_GATE_STATE_PATH)


def _load_crypto_positions() -> dict[str, CryptoPositionState]:
    """risk_gate_state.json과 같은 패턴 — 프로세스가 사이클마다 새로 뜨므로
    진입 시 계산한 stop_pct/target_pct/entry_date를 파일로 지속해야 다음
    사이클의 청산판정(evaluate_crypto_exit)이 그 값을 다시 쓸 수 있다."""
    if not CRYPTO_POSITIONS_PATH.exists():
        return {}
    try:
        raw = json.loads(CRYPTO_POSITIONS_PATH.read_text())
        return {symbol: CryptoPositionState.from_dict(d) for symbol, d in raw.items()}
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.exception("[ERROR] crypto_positions.json corrupt — treating as empty (신규진입 시 새로 기록됨)")
        return {}


def _save_crypto_positions(positions: dict[str, CryptoPositionState]) -> None:
    CRYPTO_POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CRYPTO_POSITIONS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps({symbol: s.to_dict() for symbol, s in positions.items()}))
    os.replace(tmp_path, CRYPTO_POSITIONS_PATH)


def _underlying_of(symbol: str) -> str | None:
    """OCC 옵션심볼 prefix로 기초자산 판별(예: "SPY260321P00600000" → "SPY")."""
    for base in SYMBOLS:
        if symbol.startswith(base):
            return base
    return None


async def _crypto_positions_by_symbol(session: ClientSession) -> dict[str, dict]:
    """_positions_by_symbol은 OCC 옵션심볼→기초자산 매칭(_underlying_of)만
    하므로 "BTC/USD" 같은 크립토 심볼은 그 함수에 넣으면 조용히 드롭된다
    (실측 확인 — 옵션 전용으로 설계된 기존 함수를 그대로 재사용하면 안 됨).
    크립토는 심볼이 곧 계약이라(멀티레그 없음) 별도의 단순 매칭이면 충분.

    **2026-08-26 실거래로 발견한 버그**: place_crypto_order는 "BTC/USD"(슬래시)를
    요구하지만 get_all_positions는 같은 포지션을 "BTCUSD"(슬래시 없음)로 반환한다
    — 그대로 매칭하면 방금 산 포지션을 다음 사이클이 못 찾아서 중복매수 가드와
    청산감시(evaluate_crypto_exit)가 둘 다 무력화된다. 슬래시를 지운 키로
    매칭하고, CryptoPositionState 조회용으로는 CRYPTO_SYMBOLS(슬래시 있는 정규
    표기)로 다시 매핑해서 반환한다."""
    positions = _mcp_data(await session.call_tool("get_all_positions", {})).get("result", [])
    by_stripped = {p["symbol"].replace("/", ""): p for p in positions if p.get("symbol")}
    return {
        symbol: by_stripped[symbol.replace("/", "")]
        for symbol in CRYPTO_SYMBOLS
        if symbol.replace("/", "") in by_stripped
    }


async def _positions_by_symbol(session: ClientSession) -> dict[str, list[dict]]:
    positions = _mcp_data(await session.call_tool("get_all_positions", {})).get("result", [])
    grouped: dict[str, list[dict]] = {}
    for p in positions:
        base = _underlying_of(p.get("symbol", ""))
        if base:
            grouped.setdefault(base, []).append(p)
    return grouped


STALE_ORDER_MAX_MINUTES = 30  # 라이브 15분 루프 기준 2사이클 — 그 이상 안 걸리면
# 제출 시점 가격이 시장과 멀어졌다고 보고 취소해서 자리를 되돌린다.
# **2026-08-25 실측 발견**: GLD 주문이 63분, IWM 주문이 48분째 미체결로 방치되고
# 있었다 — 한 번 제출하면 재조정·취소·재시도가 전혀 없어서 하루 종일 그 종목
# 자리만 막고 아무 일도 안 일어나는 상태였다.


def _order_age_minutes(order: dict, now: datetime) -> float | None:
    submitted_at = order.get("submitted_at") or order.get("created_at")
    if not submitted_at:
        return None
    try:
        submitted_dt = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - submitted_dt).total_seconds() / 60


async def _cancel_stale_orders(session: ClientSession, orders: list[dict], now: datetime | None = None) -> set[str]:
    """STALE_ORDER_MAX_MINUTES보다 오래 안 걸린 미체결 주문을 취소하고, 취소된
    주문이 걸려있던 기초자산 심볼 집합을 반환한다 — 호출자가 이 심볼들을
    "노출됨" 집합에서 빼서 이번 사이클에 새 가격으로 재시도할 수 있게 한다."""
    now = now or datetime.now(timezone.utc)
    freed: set[str] = set()
    for o in orders:
        age = _order_age_minutes(o, now)
        if age is None or age < STALE_ORDER_MAX_MINUTES:
            continue
        order_id = o.get("id")
        legs = o.get("legs") or [o]
        symbols = {base for leg in legs if (base := _underlying_of(leg.get("symbol", "")))}
        if not order_id or not symbols:
            continue
        try:
            await session.call_tool("cancel_order_by_id", {"order_id": order_id})
        except Exception:
            logger.exception("[ERROR] failed to cancel stale order %s (age=%.0fmin)", order_id, age)
            continue
        logger.info("[CANCEL_STALE] order_id=%s age=%.0fmin symbols=%s — freed for repricing", order_id, age, sorted(symbols))
        freed |= symbols
    return freed


async def _symbols_with_open_orders(session: ClientSession) -> set[str]:
    """미체결 주문의 기초자산 심볼 집합 — 방금 진입 주문을 넣었는데 아직 안
    채워진 상태에서 다음 15분 사이클이 또 진입하는 걸 막는다(포지션으로는 아직
    안 잡히지만 실질적으로 이미 노출된 상태이므로). 오래된(STALE_ORDER_MAX_MINUTES
    초과) 주문은 여기서 취소하고 노출 집합에서 빼서 같은 사이클에 재시도되게 한다.

    **2026-08-25 Codex 감사 지적**: `nested`를 명시 안 하면 Alpaca API의
    실제 기본값에 이 가드가 암묵적으로 의존하게 된다 — nested=false면 멀티레그
    주문이 부모 없이 개별 레그로 평면화돼 나올 수 있고(그 자체는 leg.symbol로
    잡히니 안전), nested=true가 진짜 필요한 형태는 아래 코드가 원래 가정하고
    있던 "부모 order에 legs 배열" 모양이다(테스트 픽스처가 이 모양을 가정해
    작성돼 있었는데 정작 실제 호출은 nested를 안 넘기고 있었다) — 명시해서
    가정과 실제 호출을 일치시킨다."""
    exposed: set[str] = set()
    orders = _mcp_data(await session.call_tool("get_orders", {"status": "open", "nested": True})).get("result", [])
    freed = await _cancel_stale_orders(session, orders)
    for o in orders:
        legs = o.get("legs") or [o]
        for leg in legs:
            base = _underlying_of(leg.get("symbol", ""))
            if base and base not in freed:
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
            crypto_client = CryptoHistoricalDataClient(
                os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
            )
            macro: MacroGate = load_macro_gate()
            logger.info("[MACRO] ok=%s reason=%s stage=%s", macro.ok, macro.reason, macro.stage)

            risk_gate_state, state_corrupt = _load_risk_gate_state(equity)
            if state_corrupt:
                # 손상 파일은 그대로 둔다(덮어쓰면 사후분석 불가) — 사람이 고칠 때까지 매 사이클 차단.
                risk_gate = RiskGateDecision(True, "state_corrupt_fail_closed", RiskGateState(high_water_mark=equity))
            else:
                risk_gate = evaluate_risk_gates(equity, risk_gate_state)
                _save_risk_gate_state(risk_gate.state)
            if risk_gate.blocked:
                logger.info(
                    "[RISK_GATE] reason=%s hwm=%.2f halt_until=%s — 신규진입 억제(청산감시는 계속)",
                    risk_gate.reason, risk_gate.state.high_water_mark, risk_gate.state.halt_until,
                )

            # ── 1) 청산 감시 — 열린 포지션부터 먼저 확인 (신규진입보다 항상 우선,
            #    리스크게이트 정지 중에도 청산은 절대 안 막는다). 미체결 주문
            #    목록을 먼저 가져와서 이미 청산주문이 나가있는 심볼은 중복 제출
            #    안 하게 막는다(2026-08-25 Codex 감사 지적 — market 주문이라
            #    실제 걸릴 가능성은 낮지만 공짜로 막을 수 있는 방어선). ──
            symbols_with_pending_orders = await _symbols_with_open_orders(session)
            positions_by_symbol = await _positions_by_symbol(session)
            for symbol, legs in positions_by_symbol.items():
                if symbol in symbols_with_pending_orders:
                    logger.info("[SKIP] %s already has a pending order — no duplicate close/entry this cycle", symbol)
                    continue
                try:
                    exit_decision = evaluate_exit(legs)
                except Exception:
                    logger.exception("[ERROR] evaluate_exit failed for %s — leaving position open", symbol)
                    continue
                if not exit_decision.should_close:
                    logger.info("[HOLD] %s pnl_pct=%.1f%%", symbol, exit_decision.profit_pct * 100)
                    continue
                try:
                    close_intent = build_close_intent(legs, client_order_id=f"atlas-close-{symbol}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
                except ValueError:
                    logger.exception("[ERROR] build_close_intent rejected malformed position for %s — leaving open for manual review", symbol)
                    _log_decision({"symbol": symbol, "submitted": False, "skip_reason": "malformed_position", "exit_reason": exit_decision.reason})
                    continue
                try:
                    close_result = _mcp_data(await session.call_tool("place_option_order", close_intent))
                except Exception:
                    logger.exception("[ERROR] close order failed for %s (reason=%s) — will retry next cycle", symbol, exit_decision.reason)
                    _log_decision({"symbol": symbol, "submitted": False, "skip_reason": "close_order_error", "exit_reason": exit_decision.reason})
                    continue
                broker_error = _order_error(close_result)
                if broker_error and "reenter with a limit" in str(broker_error).lower():
                    # 2026-08-29: market 청산이 "견적없음, limit으로" 거부되면 재시도해도
                    # 매번 같은 이유로 100% 다시 거부된다(TLT 하루 280회 실측) — 실호가
                    # 중간값 기반 limit으로 그 자리에서 즉시 재시도.
                    try:
                        quotes = option_client.get_option_latest_quote(
                            OptionLatestQuoteRequest(symbol_or_symbols=[leg["symbol"] for leg in legs])
                        )
                        limit_price = close_limit_price(legs, quotes)
                    except Exception:
                        logger.exception("[ERROR] limit fallback quote fetch failed for %s — leaving open for next cycle", symbol)
                        limit_price = None
                    if limit_price is not None:
                        limit_close_intent = build_close_intent(
                            legs, client_order_id=f"atlas-close-lmt-{symbol}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}",
                            limit_price=limit_price,
                        )
                        limit_result = _mcp_data(await session.call_tool("place_option_order", limit_close_intent))
                        limit_error = _order_error(limit_result)
                        if not limit_error:
                            logger.info("[CLOSE] %s reason=%s limit_fallback=%.2f", symbol, exit_decision.reason, limit_price)
                            _log_decision({
                                "symbol": symbol, "submitted": True, "action": "close",
                                "exit_reason": exit_decision.reason, "limit_fallback_price": limit_price,
                            })
                            continue
                        broker_error = f"market rejected ({broker_error}); limit fallback also rejected: {limit_error}"
                if broker_error:
                    logger.error("[ERROR] close order REJECTED by broker for %s (reason=%s): %s — position still open, will retry next cycle", symbol, exit_decision.reason, broker_error)
                    _log_decision({
                        "symbol": symbol, "submitted": False, "skip_reason": "close_order_rejected",
                        "exit_reason": exit_decision.reason, "order_intent": close_intent, "order_result": close_result,
                    })
                    continue
                logger.info("[CLOSE] %s reason=%s pnl_pct=%.1f%%", symbol, exit_decision.reason, exit_decision.profit_pct * 100)
                _log_decision({
                    "symbol": symbol, "submitted": True, "action": "close",
                    "exit_reason": exit_decision.reason, "profit_pct": exit_decision.profit_pct,
                    "order_intent": close_intent, "order_result": close_result,
                })

            # ── 2) 신규진입 — 리스크게이트 정지 중이면 전체 스킵, 아니면 이미
            #    포지션/미체결주문 있는 심볼만 건너뛴다 ──
            if risk_gate.blocked:
                for symbol in SYMBOLS:
                    _log_decision({"symbol": symbol, "submitted": False, "skip_reason": f"risk_gate:{risk_gate.reason}"})
                return

            # symbols_with_pending_orders는 청산루프 전에 이미 조회함 — 재사용
            # (이 사이클에서 막 낸 청산주문은 아직 이 집합에 없지만, 포지션 자체가
            # positions_by_symbol에 남아있어서 아래 union이 여전히 올바르게 막는다).
            symbols_with_exposure = set(positions_by_symbol.keys()) | symbols_with_pending_orders
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

                broker_error = _order_error(order_payload)
                if broker_error:
                    logger.error("[ERROR] entry order REJECTED by broker for %s: %s", symbol, broker_error)
                    _log_decision({
                        "symbol": symbol, "regime": decision.regime,
                        "macro_reason": decision.macro_gate.reason,
                        "order_intent": decision.order_intent, "order_result": order_payload,
                        "submitted": False, "skip_reason": "order_rejected",
                    })
                    continue

                logger.info("[SUBMIT] %s regime=%s intent=%s", symbol, decision.regime, decision.order_intent)
                _log_decision({
                    "symbol": symbol, "regime": decision.regime,
                    "macro_reason": decision.macro_gate.reason,
                    "order_intent": decision.order_intent,
                    "order_result": order_payload, "submitted": True,
                })

async def run_crypto_cycle_once() -> None:
    """옵션 사이클(run_cycle_once)과 완전히 분리된 독립 루프 — 크립토는 24/7
    마켓이라 주식장 개폐(get_clock)와 무관하게 자체 스케줄(별도 launchd job,
    config/launchd/com.atlas.crypto-runner.plist)로 돈다.

    **2026-08-26 배선 변경**: 원래 이 로직은 run_cycle_once 안에 있어서
    get_clock의 주식장 개폐 체크를 그대로 물려받았다(크립토도 주식장 열려있을
    때만 돎) — 사용자가 "크립토는 24시간 마켓인데 왜 갇혀있냐"고 지적해서
    별도 함수·별도 스케줄로 분리했다. risk_gate_state.json은 옵션 사이클과
    그대로 공유(Task Contract: 같은 계좌·같은 리스크게이트)."""
    client_cm = await _new_mcp_session()
    async with client_cm as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            account_info = _mcp_data(await session.call_tool("get_account_info", {}))
            equity = float(account_info["equity"])
            # non_marginable_buying_power = 크립토 현물(비마진) 매수에 실제 쓸 수
            # 있는 현금 — 2026-08-26 실거래로 확인: equity는 옵션 포지션 시가평가를
            # 포함해서 훨씬 크게 보이지만(옵션이 마진을 이미 물고 있음), 크립토
            # 사이징은 이 값을 기준으로 캡해야 브로커 거부 없이 바로 체결된다.
            available_cash = float(account_info.get("non_marginable_buying_power", equity))
            logger.info("[CRYPTO_CYCLE] equity=%.2f available_cash=%.2f", equity, available_cash)

            crypto_client = CryptoHistoricalDataClient(
                os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
            )
            macro: MacroGate = load_macro_gate()
            logger.info("[MACRO] ok=%s reason=%s stage=%s", macro.ok, macro.reason, macro.stage)

            risk_gate_state, state_corrupt = _load_risk_gate_state(equity)
            if state_corrupt:
                risk_gate = RiskGateDecision(True, "state_corrupt_fail_closed", RiskGateState(high_water_mark=equity))
            else:
                risk_gate = evaluate_risk_gates(equity, risk_gate_state)
                _save_risk_gate_state(risk_gate.state)
            if risk_gate.blocked:
                logger.info(
                    "[RISK_GATE] reason=%s hwm=%.2f halt_until=%s — 크립토 신규진입 억제(청산감시는 계속)",
                    risk_gate.reason, risk_gate.state.high_water_mark, risk_gate.state.halt_until,
                )

            crypto_positions = _load_crypto_positions()
            crypto_open_positions = await _crypto_positions_by_symbol(session)

            for symbol in CRYPTO_SYMBOLS:
                position = crypto_open_positions.get(symbol)
                if not position:
                    continue
                state = crypto_positions.get(symbol)
                if state is None:
                    # 상태 파일이 없는데 실제 포지션이 있음(수동개입/상태유실) — fail-closed,
                    # 사람이 볼 때까지 청산 판단을 못 하니 그대로 둔다(진입은 already_exposed로 막힘).
                    logger.error("[ERROR] crypto position %s has no stored entry state — leaving open for manual review", symbol)
                    continue
                exit_decision = evaluate_crypto_exit(position, state)
                if not exit_decision.should_close:
                    logger.info("[HOLD] %s(crypto) pnl_pct=%.1f%%", symbol, exit_decision.profit_pct * 100)
                    continue
                qty = str(position.get("qty", "0"))
                close_intent = build_crypto_close_intent(
                    symbol, qty, client_order_id=f"atlas-crypto-close-{symbol.replace('/', '')}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}",
                )
                try:
                    close_result = _mcp_data(await session.call_tool("place_crypto_order", close_intent))
                except Exception:
                    logger.exception("[ERROR] crypto close order failed for %s (reason=%s) — will retry next cycle", symbol, exit_decision.reason)
                    _log_decision({"symbol": symbol, "sleeve": "crypto", "submitted": False, "skip_reason": "close_order_error", "exit_reason": exit_decision.reason})
                    continue
                broker_error = _order_error(close_result)
                if broker_error:
                    logger.error("[ERROR] crypto close REJECTED for %s (reason=%s): %s — position still open", symbol, exit_decision.reason, broker_error)
                    _log_decision({"symbol": symbol, "sleeve": "crypto", "submitted": False, "skip_reason": "close_order_rejected", "exit_reason": exit_decision.reason, "order_intent": close_intent, "order_result": close_result})
                    continue
                logger.info("[CLOSE] %s(crypto) reason=%s pnl_pct=%.1f%%", symbol, exit_decision.reason, exit_decision.profit_pct * 100)
                _log_decision({"symbol": symbol, "sleeve": "crypto", "submitted": True, "action": "close", "exit_reason": exit_decision.reason, "profit_pct": exit_decision.profit_pct, "order_intent": close_intent, "order_result": close_result})
                crypto_positions.pop(symbol, None)

            _save_crypto_positions(crypto_positions)

            if risk_gate.blocked:
                for symbol in CRYPTO_SYMBOLS:
                    _log_decision({"symbol": symbol, "sleeve": "crypto", "submitted": False, "skip_reason": f"risk_gate:{risk_gate.reason}"})
            else:
                # ponytail: 미체결(아직 안 채워진) 크립토 주문 가드는 없음(옵션의
                # _symbols_with_open_orders는 OCC 심볼 전용이라 그대로 재사용 불가) —
                # 크립토 시장가 주문은 사실상 즉시체결이라 15분 사이클 간격에서
                # 중복진입 확률이 낮다고 판단. 실측으로 문제 되면 그때 추가.
                for symbol in CRYPTO_SYMBOLS:
                    if symbol in crypto_open_positions:
                        logger.info("[SKIP] %s(crypto) already has open position — no re-entry", symbol)
                        _log_decision({"symbol": symbol, "sleeve": "crypto", "submitted": False, "skip_reason": "already_exposed"})
                        continue
                    try:
                        decision = decide_crypto_for_symbol(crypto_client, symbol, equity, macro, available_cash)
                    except Exception:
                        logger.exception("[ERROR] decide_crypto_for_symbol failed for %s — skipping", symbol)
                        _log_decision({"symbol": symbol, "sleeve": "crypto", "submitted": False, "skip_reason": "decision_error"})
                        continue
                    if decision.order_intent is None:
                        logger.info("[SKIP] %s(crypto) regime=%s reason=%s", symbol, decision.regime, decision.skip_reason)
                        _log_decision({"symbol": symbol, "sleeve": "crypto", "regime": decision.regime, "skip_reason": decision.skip_reason, "submitted": False})
                        continue
                    try:
                        order_payload = _mcp_data(await session.call_tool("place_crypto_order", decision.order_intent))
                    except Exception:
                        logger.exception("[ERROR] place_crypto_order failed for %s", symbol)
                        _log_decision({"symbol": symbol, "sleeve": "crypto", "regime": decision.regime, "order_intent": decision.order_intent, "submitted": False, "skip_reason": "order_submit_error"})
                        continue
                    broker_error = _order_error(order_payload)
                    if broker_error:
                        logger.error("[ERROR] crypto entry REJECTED for %s: %s", symbol, broker_error)
                        _log_decision({"symbol": symbol, "sleeve": "crypto", "regime": decision.regime, "order_intent": decision.order_intent, "order_result": order_payload, "submitted": False, "skip_reason": "order_rejected"})
                        continue
                    logger.info("[SUBMIT] %s(crypto) regime=%s intent=%s", symbol, decision.regime, decision.order_intent)
                    _log_decision({"symbol": symbol, "sleeve": "crypto", "regime": decision.regime, "order_intent": decision.order_intent, "order_result": order_payload, "submitted": True})
                    # 체결 확인은 다음 사이클 포지션 조회로 이뤄지지만, stop/target은
                    # 지금 계산한 값을 바로 저장해야 다음 사이클이 청산판정을 할 수 있다.
                    crypto_positions[symbol] = CryptoPositionState(
                        entry_date=datetime.now(timezone.utc).date(),
                        stop_pct=decision.stop_pct, target_pct=decision.target_pct,
                    )
                    _save_crypto_positions(crypto_positions)


async def main() -> None:
    _setup_logging()
    # 2026-08-26: 옵션(주식장 시간 게이트)과 크립토(24/7, 독립 스케줄)를
    # 같은 스크립트에서 인자로 갈라 launchd job 2개(com.atlas.options-runner,
    # com.atlas.crypto-runner)가 각자의 트리거로 이 파일을 호출한다.
    mode = sys.argv[1] if len(sys.argv) > 1 else "options"
    cycle_fn = {"options": run_cycle_once, "crypto": run_crypto_cycle_once}.get(mode)
    if cycle_fn is None:
        logger.error("[FATAL] unknown mode %r (expected 'options' or 'crypto')", mode)
        sys.exit(2)
    try:
        await cycle_fn()
    except Exception:
        logger.exception("[FATAL] %s cycle crashed before completing", mode)
        # 종료코드 0으로 두지 않는다 — launchd 로그·ThrottleInterval이 이 실패를 보고
        # 다음 스케줄까지 정상 재시도하게 둔다(예외를 삼키면 조용한 무한장애가 됨).
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
