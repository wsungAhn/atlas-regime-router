"""
Atlas Options Hackathon — 순수 신호계산 계층 (MVP 2레짐).

이 모듈은 **주문을 제출하지 않는다** — 시장데이터 조회(read-only, alpaca-py)만 하고
"이런 주문을 넣어야 한다"는 order-intent dict를 만든다. 실제 제출은 Alpaca MCP 서버의
`place_option_order` 도구를 에이전트 루프가 호출해서 한다(대회요건: Trading API +
MCP서버/CLI 사용). 이 분리 덕분에:
  - 신호계산은 pytest로 완전 유닛테스트 가능(브로커 왕복 불필요)
  - 실제 주문 경로는 MCP 도구 1곳(place_option_order)으로만 나가서 감사가 쉽다

레짐 분류: ADX(14)/EMA(20/50) 종목별 기술적 판정.
매크로 오버레이: regime-signals의 검증된 macro_stage(SPY/QQQ Weinstein Stage)를
신규 진입 게이트로 사용 — TR-016(trader 레포)과 동일 패턴, 신규 게이트 로직 재발명 안 함.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import ContractType

import indicators as ind

# ── 대회 리스크 게이트(문서 §리스크 게이트와 1:1 대응, 값 변경 시 문서도 같이 고칠 것) ──
RISK_PCT_MIN = 0.02
RISK_PCT_MAX = 0.05
SAME_UNDERLYING_RISK_CAP_PCT = 0.03
PORTFOLIO_RISK_CAP_PCT = 0.06
CASH_RESERVE_PCT = 0.10
DAILY_LOSS_KILL_PCT = 0.03
WEEKLY_LOSS_KILL_PCT = 0.06
MIN_DTE = 14
TARGET_DTE_RANGE = (30, 45)
SHORT_DELTA_TARGET = 0.15
PROTECTIVE_DELTA_TARGET = 0.06  # 보호레그는 숏레그보다 델타가 훨씬 낮은(더 바깥) 쪽

MACRO_DB_PATH = Path.home() / ".local/share/regime-signals/verdicts.db"
MACRO_BLOCK_STAGES = {"stage4_declining"}


# ── 매크로 오버레이 (regime-signals RS-011 재사용 — trader TR-016과 동일 소스) ──

@dataclass
class MacroGate:
    ok: bool  # False면 신규 진입 전체 억제(equities BUY만이 아니라 이 시스템은 신규 진입 전체)
    reason: str
    stage: str | None


def load_macro_gate(db_path: Path = MACRO_DB_PATH) -> MacroGate:
    """SPY의 최신 Weinstein stage를 읽어 stage4(하락추세)면 신규 진입을 억제한다.
    DB 부재/오류는 fail-open(ok=True) — 매크로 데이터 장애가 청산을 막으면 안 된다는
    trader TR-016의 D2 원칙을 그대로 따른다(단, 이 시스템은 신규 진입만 다루므로
    fail-open이 곧 "게이트 없이 진행"이라는 의미 — 안전측이 아니라 가용측 선택,
    대회 8일 안에는 데이터 장애로 통째 멈추는 게 더 나쁘다고 판단)."""
    if not db_path.exists():
        return MacroGate(ok=True, reason="db_missing_fail_open", stage=None)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            """
            SELECT stage, status FROM macro_stage_verdicts
            WHERE benchmark_ticker = 'SPY' AND producer = 'macro-batch'
            ORDER BY as_of_date DESC LIMIT 1
            """
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return MacroGate(ok=True, reason="db_error_fail_open", stage=None)
    if row is None:
        return MacroGate(ok=True, reason="no_data_fail_open", stage=None)
    stage, status = row
    if status != "ok":
        return MacroGate(ok=True, reason="not_ok_fail_open", stage=stage)
    if stage in MACRO_BLOCK_STAGES:
        return MacroGate(ok=False, reason="stage4_declining", stage=stage)
    return MacroGate(ok=True, reason="clear", stage=stage)


# ── 종목별 기술적 레짐 (indicators.py 공용 함수 사용) ──

@dataclass
class RegimeSignal:
    symbol: str
    regime: Literal["range", "trend_up", "trend_down", "cash"]
    atr: float
    adx: float
    close: float


def classify_regime_from_bars(df: pd.DataFrame, symbol: str) -> RegimeSignal:
    """순수 함수 — 테스트에서 합성 DataFrame을 바로 넣을 수 있도록 데이터조회와 분리.

    "range" 트리거는 strategies.py의 전략5(평균회귀 콘도르 리셋) 조건을 그대로
    쓴다 — SPY/QQQ 3년 백테스트에서 5번이 승률 77~85%·Calmar 0.4~2.8로 7전략 중
    1위였다(2026-08-24 실측). ADX 단독 임계값(구버전)보다 RSI·VWAP 근접까지
    같이 보는 이 조건이 실제로 검증된 것 — MVP를 그 결과에 맞춰 승자 중심으로
    재구성."""
    df = df.tail(80)
    atr = float(ind.wilder_atr(df, 14).iloc[-1])
    adx = float(ind.adx(df, 14).iloc[-1])
    ema20 = float(ind.ema(df["close"], 20).iloc[-1])
    ema50 = float(ind.ema(df["close"], 50).iloc[-1])
    rsi = float(ind.rsi(df, 14).iloc[-1])
    vwap = float(ind.vwap_session(df, window=20).iloc[-1])
    close = float(df["close"].iloc[-1])

    if atr > 0 and adx < 18 and abs(close - vwap) / atr < 1.0 and 40 < rsi < 60:
        regime = "range"
    elif adx > 20 and ema20 > ema50:
        regime = "trend_up"
    elif adx > 20 and ema20 < ema50:
        regime = "trend_down"
    else:
        regime = "cash"
    return RegimeSignal(symbol=symbol, regime=regime, atr=atr, adx=adx, close=close)


def fetch_and_classify_regime(client: StockHistoricalDataClient, symbol: str) -> RegimeSignal:
    req = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=120),
    )
    bars = client.get_stock_bars(req).df
    df = bars.xs(symbol, level="symbol") if isinstance(bars.index, pd.MultiIndex) else bars
    return classify_regime_from_bars(df, symbol)


# ── 옵션 체인에서 목표 델타 근접 계약 선택 ──

def pick_by_delta(chain: dict, contract_type: ContractType, target_abs_delta: float) -> str | None:
    candidates = []
    for sym, snap in chain.items():
        if snap.greeks is None:
            continue
        is_call = "C" in sym[-9:]
        if contract_type == ContractType.CALL and not is_call:
            continue
        if contract_type == ContractType.PUT and is_call:
            continue
        candidates.append((sym, abs(abs(snap.greeks.delta) - target_abs_delta)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


def fetch_chain(
    data_client: OptionHistoricalDataClient, symbol: str, contract_type: ContractType,
    dte_min: int = TARGET_DTE_RANGE[0], dte_max: int = TARGET_DTE_RANGE[1],
) -> dict:
    today = date.today()
    req = OptionChainRequest(
        underlying_symbol=symbol, type=contract_type,
        expiration_date_gte=today + timedelta(days=dte_min),
        expiration_date_lte=today + timedelta(days=dte_max),
    )
    return data_client.get_option_chain(req)


# ── 리스크 사이징 ──

def risk_pct_for_atr_pct(atr_pct: float, atr_pct_low_q: float, atr_pct_high_q: float) -> float:
    if atr_pct_high_q <= atr_pct_low_q:
        return RISK_PCT_MIN
    scaled = (atr_pct - atr_pct_low_q) / (atr_pct_high_q - atr_pct_low_q)
    r = RISK_PCT_MAX - 0.03 * max(0.0, min(1.0, scaled))
    return max(RISK_PCT_MIN, min(RISK_PCT_MAX, r))


def contracts_for_max_loss(risk_budget_usd: float, max_loss_per_contract_usd: float) -> int:
    if max_loss_per_contract_usd <= 0:
        return 0
    return int(risk_budget_usd // max_loss_per_contract_usd)


# ── order-intent 생성 (MCP place_option_order 스키마 그대로 — 실제 제출은 에이전트가 함) ──
# 주의: MCP place_option_order의 limit_price 부호 규약은 "양수=데빗(지불), 음수=크레딧(수취)".
# alpaca-py LimitOrderRequest.limit_price(양수=크레딧)와 부호가 반대이니 섞어쓰지 말 것.

def build_credit_spread_intent(
    short_symbol: str, long_symbol: str, qty: int, net_credit: float, client_order_id: str,
) -> dict:
    return {
        "qty": str(qty),
        "type": "limit",
        "time_in_force": "day",
        "order_class": "mleg",
        "limit_price": str(round(-abs(net_credit), 2)),  # 크레딧 = 음수
        "client_order_id": client_order_id,
        "legs": [
            {"symbol": short_symbol, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": long_symbol, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
        ],
    }


def build_iron_condor_intent(
    short_put: str, long_put: str, short_call: str, long_call: str,
    qty: int, net_credit: float, client_order_id: str,
) -> dict:
    return {
        "qty": str(qty),
        "type": "limit",
        "time_in_force": "day",
        "order_class": "mleg",
        "limit_price": str(round(-abs(net_credit), 2)),
        "client_order_id": client_order_id,
        "legs": [
            {"symbol": short_put, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": long_put, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
            {"symbol": short_call, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": long_call, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
        ],
    }


@dataclass
class CycleDecision:
    symbol: str
    regime: str
    macro_gate: MacroGate
    order_intent: dict | None
    skip_reason: str | None


def decide_for_symbol(
    stock_client: StockHistoricalDataClient,
    option_client: OptionHistoricalDataClient,
    symbol: str,
    equity: float,
    macro: MacroGate,
) -> CycleDecision:
    """한 종목에 대해 이번 사이클의 결정을 만든다 — 순수 결정 로직, 실제 주문 제출은 안 함.
    이 함수는 MCP 미연결 상태에서도(시장데이터만 있으면) 전부 테스트 가능해야 한다."""
    if not macro.ok:
        return CycleDecision(symbol, "n/a", macro, None, f"macro_gate_blocked:{macro.reason}")

    signal = fetch_and_classify_regime(stock_client, symbol)
    if signal.regime == "cash":
        return CycleDecision(symbol, signal.regime, macro, None, "regime_cash")

    risk_budget = min(equity * RISK_PCT_MIN, equity * SAME_UNDERLYING_RISK_CAP_PCT)
    cid_base = f"atlas-{symbol}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"

    if signal.regime == "range":
        puts = fetch_chain(option_client, symbol, ContractType.PUT)
        calls = fetch_chain(option_client, symbol, ContractType.CALL)
        short_put = pick_by_delta(puts, ContractType.PUT, SHORT_DELTA_TARGET)
        short_call = pick_by_delta(calls, ContractType.CALL, SHORT_DELTA_TARGET)
        long_put = pick_by_delta(puts, ContractType.PUT, PROTECTIVE_DELTA_TARGET)
        long_call = pick_by_delta(calls, ContractType.CALL, PROTECTIVE_DELTA_TARGET)
        if not all([short_put, long_put, short_call, long_call]):
            return CycleDecision(symbol, signal.regime, macro, None, "chain_insufficient")
        max_loss_est = signal.atr * 2.5 * 100
        qty = contracts_for_max_loss(risk_budget, max_loss_est)
        if qty < 1:
            return CycleDecision(symbol, signal.regime, macro, None, "qty_below_1")
        intent = build_iron_condor_intent(
            short_put, long_put, short_call, long_call, qty, net_credit=1.0,
            client_order_id=f"{cid_base}-condor",
        )
        return CycleDecision(symbol, signal.regime, macro, intent, None)

    # trend_up / trend_down
    put_side = signal.regime == "trend_up"
    ctype = ContractType.PUT if put_side else ContractType.CALL
    chain = fetch_chain(option_client, symbol, ctype)
    short_leg = pick_by_delta(chain, ctype, SHORT_DELTA_TARGET)
    long_leg = pick_by_delta(chain, ctype, PROTECTIVE_DELTA_TARGET)
    if not short_leg or not long_leg:
        return CycleDecision(symbol, signal.regime, macro, None, "chain_insufficient")
    max_loss_est = signal.atr * 1.25 * 100
    qty = contracts_for_max_loss(risk_budget, max_loss_est)
    if qty < 1:
        return CycleDecision(symbol, signal.regime, macro, None, "qty_below_1")
    intent = build_credit_spread_intent(
        short_leg, long_leg, qty, net_credit=1.0, client_order_id=f"{cid_base}-spread",
    )
    return CycleDecision(symbol, signal.regime, macro, intent, None)
