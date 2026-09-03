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

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    OptionChainRequest,
    OptionLatestQuoteRequest,
    StockBarsRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import ContractType

import indicators as ind

# ── 대회 리스크 게이트(문서 §리스크 게이트와 1:1 대응, 값 변경 시 문서도 같이 고칠 것) ──
RISK_PCT_MIN = 0.02
RISK_PCT_MAX = 0.10  # 2026-08-25 오전: 5%→10% 재상향, 사용자 지시("포지션 10%로
# 유지해") — 대신 일일/주간 킬스위치를 20%/50%로 넓혀서(아래) 포지션 1건의
# 최악의 경우가 계좌 킬스위치를 혼자 뚫는 문제를 해결. portfolio.py의 동일
# 공식과 값을 맞출 것(주석 참고).
SAME_UNDERLYING_RISK_CAP_PCT = 0.03
PORTFOLIO_RISK_CAP_PCT = 0.06
CASH_RESERVE_PCT = 0.10
DAILY_LOSS_KILL_PCT = 0.20  # 2026-08-25 오전: 0.03→0.20, 사용자 지시
WEEKLY_LOSS_KILL_PCT = 0.50  # 2026-08-25 오전: 0.06→0.50, 사용자 지시
# 2026-08-24 추가 — 사용자 지시: 계좌 사상최고치(HWM) 대비 -20% 낙폭 시 전체
# 시스템 신규진입 45분 정지(라이브 15분 루프 기준 2회 패스, 3번째 루프에서
# 재개). portfolio.py(백테스트)와 값을 맞춘 사본 — 정지는 잔고를 리셋하지
# 않는다(HWM은 절대 안 낮아지고, 정지 풀려도 이미 난 손실은 그대로 이어간다).
PORTFOLIO_DD_KILL_PCT = 0.20
PORTFOLIO_DD_HALT_MINUTES = 45
TARGET_DTE_RANGE = (5, 9)  # 2026-08-24: 30~45일(월간)→5~9일(주간)로 축소 — 오늘
# 백테스트가 검증한 챔피언(전략7)이 DTE_DAYS=7(주간옵션)로 회전율을 올려서
# 나온 결과라, 라이브도 그대로 맞춰야 백테스트=라이브가 유지된다(안 맞추면
# 오늘 검증한 숫자와 무관한 다른 전략이 라이브에서 돎).
SHORT_DELTA_TARGET = 0.15
PROTECTIVE_DELTA_TARGET = 0.06  # 보호레그는 숏레그보다 델타가 훨씬 낮은(더 바깥) 쪽

# ── 청산 규칙(백테스트 backtest.py와 동일 상수 — 실행에도 반드시 배선할 것.
#    2026-08-24 발견: 이전엔 진입만 있고 청산 감시가 아예 없었다) ──
PROFIT_TARGET_PCT = 0.5   # 수취 크레딧의 50% 이익 실현 시 청산
STOP_LOSS_MULTIPLE = 2.0  # 청산비용이 수취크레딧의 2배 도달 시 손절
FORCE_CLOSE_DTE = 2  # 2026-08-24: MIN_DTE(14, 월간 시절 값)에서 분리 — 이 DTE
# 이하로 내려가면 손익 무관 강제청산. 주간옵션(목표 진입 DTE 5~9)에서 14는
# 진입 직후 항상 강제청산되는 값이라 쓸 수 없었다 — 만기 임박 감마리스크만
# 피하면 되므로 2일로 재설정(백테스트는 이 강제청산 자체를 시뮬레이션 안 해서
# 참고할 검증값이 없다 — 감마리스크 회피라는 목적에 맞춘 판단값).

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
        # mode=ro는 WAL 동반파일(-shm/-wal)이 이미 있어야 여는데, writer가 잠깐이라도
        # 연결을 안 잡고 있으면(체크포인트 시 자동삭제) 그 파일들이 없어져 매번
        # "unable to open database file"로 실패한다(2026-08-29 실측: 상시 fail-open
        # 상태였음). 일반 연결은 필요시 그 파일을 스스로 만들 수 있어 이 문제가 없다 —
        # 이 함수는 SELECT만 하므로 쓰기 권한이 있어도 안전.
        conn = sqlite3.connect(str(db_path))
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
    risk_pct: float = RISK_PCT_MIN  # 변동성 기반 2~5%, atr_pct_percentile_bounds로 계산


ATR_PCT_LOOKBACK_DAYS = 252


def atr_pct_percentile_bounds(
    df: pd.DataFrame, lookback: int = ATR_PCT_LOOKBACK_DAYS, atr_period: int = 14,
) -> tuple[float, float, float]:
    """(현재 ATR%, 25th pct, 75th pct) — risk_pct_for_atr_pct에 그대로 먹인다.
    데이터가 lookback보다 짧으면 있는 만큼으로 근사(콜드스타트, 상장 얼마 안 된
    종목 대비 — SPY/QQQ엔 해당 없지만 함수 자체는 일반화해 둔다).

    atr_period 기본값 14는 **거래일(주5일)** 기준 자산을 가정한 값 — 24/7
    거래되는 크립토에 그대로 쓰면 14봉이 캘린더 14일(주식의 14거래일=캘린더
    약 20일)밖에 안 돼 실제로 보는 과거 기간이 짧아진다(2026-09-02 실측:
    같은 함수를 크립토에 그대로 재사용하다 발견). 호출부(크립토)가 20을
    넘겨 같은 캘린더 기간을 보도록 맞춘다."""
    atr = ind.wilder_atr(df, atr_period)
    atr_pct = (atr / df["close"]).dropna()
    window = atr_pct.tail(lookback)
    if window.empty:
        return 0.0, 0.0, 0.0
    current = float(window.iloc[-1])
    low_q = float(window.quantile(0.25))
    high_q = float(window.quantile(0.75))
    return current, low_q, high_q


def classify_regime_from_bars(df: pd.DataFrame, symbol: str, atr_period: int = 14) -> RegimeSignal:
    """순수 함수 — 테스트에서 합성 DataFrame을 바로 넣을 수 있도록 데이터조회와 분리.

    atr_period: ATR(변동성) 계산에 쓸 봉 개수. 기본 14는 **거래일 기준
    자산**(옵션 underlying) 가정 — 주5일 거래라 14봉=캘린더 약 20일.
    24/7 거래되는 크립토는 14봉=캘린더 14일이라 같은 실제 기간을 보려면
    호출부에서 atr_period=20을 넘긴다(2026-09-02, 사용자 지적으로 발견 —
    ADX/EMA는 레짐 트리거 자체의 정의라 자산군 무관하게 14/20/50 그대로 둔다,
    변동성 측정(ATR)만 캘린더 기간 문제가 있었다).

    "range"/"trend" 트리거는 strategies.py의 전략7(Atlas MVP, ADX/EMA 2레짐)
    조건을 그대로 쓴다 — 2026-08-24 챔피언 교체: 리스크캡 상향(5%→10%)+4종목
    확대 재백테스트에서 7번이 raw $ P&L로 SPY/QQQ/IWM 3/4종목에서 5번(구 챔피언,
    RSI·VWAP 근접까지 보는 좁은 조건)을 앞질렀다(IWM +28.9% vs +12.1%/3yr).
    판정기준이 P&L이라 승률·Calmar가 아니라 이 결과를 따른다 — 트레이드오프는
    MDD 증가(같은 사용자 지시로 확인·수용됨). RSI/VWAP 필터를 걷어내 range
    트리거 조건이 넓어졌다(ADX<18 단독) — 신호 빈도 자체를 늘리는 게 이번
    교체의 목적이므로 의도된 변화.

    risk_pct는 전체 df(가능하면 ATR_PCT_LOOKBACK_DAYS=252일치)의 ATR% 분위수로
    계산한다 — 원래 risk_pct_for_atr_pct가 정의만 되고 어디서도 안 불리는 죽은
    코드였다(항상 고정 min(2%,3%)=2%만 씀, 백테스트가 검증한 5%와 불일치).
    사용자가 "첫 진입이 최대사이징 아니냐"고 물어서 확인하다가 발견해서 배선."""
    regime_window = df.tail(80)
    atr = float(ind.wilder_atr(regime_window, atr_period).iloc[-1])
    adx = float(ind.adx(regime_window, 14).iloc[-1])
    ema20 = float(ind.ema(regime_window["close"], 20).iloc[-1])
    ema50 = float(ind.ema(regime_window["close"], 50).iloc[-1])
    close = float(regime_window["close"].iloc[-1])

    current_atr_pct, low_q, high_q = atr_pct_percentile_bounds(df, atr_period=atr_period)
    risk_pct = risk_pct_for_atr_pct(current_atr_pct, low_q, high_q)

    if adx < 18:
        regime = "range"
    elif adx > 20 and ema20 > ema50:
        regime = "trend_up"
    elif adx > 20 and ema20 < ema50:
        regime = "trend_down"
    else:
        regime = "cash"
    return RegimeSignal(symbol=symbol, regime=regime, atr=atr, adx=adx, close=close, risk_pct=risk_pct)


def fetch_and_classify_regime(client: StockHistoricalDataClient, symbol: str) -> RegimeSignal:
    req = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=ATR_PCT_LOOKBACK_DAYS + 30),
    )
    bars = client.get_stock_bars(req).df
    df = bars.xs(symbol, level="symbol") if isinstance(bars.index, pd.MultiIndex) else bars
    return classify_regime_from_bars(df, symbol)


# ── 옵션 체인에서 목표 델타 근접 계약 선택 ──

def _occ_strike(symbol: str) -> float | None:
    """OCC 옵션심볼 고정폭 스펙(마지막 8자리=행사가*1000)에서 행사가 추출 —
    티커 길이와 무관하게 뒤에서부터 파싱(_occ_expiration은 앞에서부터 숫자를
    찾는 방식이라 서로 다른 접근, 여긴 고정폭이 더 안전해서 이걸 씀)."""
    if len(symbol) < 15:
        return None
    strike_str = symbol[-8:]
    if not strike_str.isdigit():
        return None
    return int(strike_str) / 1000.0


def _vertical_width(short_symbol: str | None, long_symbol: str | None, contract_type: ContractType) -> float | None:
    """숏/보호 레그의 실제 스프레드 폭(주가 단위, ×100 전) — 계약당 실제
    최대손실을 사이징에 쓰기 위함. **2026-08-25 Codex 감사 발견(실측 확인)**:
    사이징이 `signal.atr * 2.5`라는 러프한 근사만 쓰고 있었는데, 그날 아침
    실제로 "insufficient options buying power"(요청 마진이 예산 추정과
    완전히 다름) 거부가 났다 — 진짜 선택된 strike로 사이징해야 한다.
    방향도 함께 검증한다(풋은 보호행사가<숏행사가, 콜은 보호행사가>숏행사가,
    같은 계약이면 안 됨) — 아니면 보호가 아니라 의미없는 조합인데도 주문이
    나갈 뻔했다는 게 이 감사의 또 다른 지적."""
    if not short_symbol or not long_symbol or short_symbol == long_symbol:
        return None
    short_k = _occ_strike(short_symbol)
    long_k = _occ_strike(long_symbol)
    if short_k is None or long_k is None:
        return None
    if contract_type == ContractType.PUT and not (long_k < short_k):
        return None
    if contract_type == ContractType.CALL and not (long_k > short_k):
        return None
    return abs(short_k - long_k)


def _mid_price(snap) -> float | None:
    """호가 중간값 — 크레딧 계산용. **2026-08-25 실거래로 발견**: 이전엔
    net_credit이 전부 하드코딩 `1.0`이었다 — SPY/QQQ는 우연히(프리미엄이
    $1보다 커서 "1달러만 받아도 좋다"는 아주 유리한/체결되기 쉬운 가격이라)
    체결됐지만, GLD/TLT는 실제 프리미엄이 $1보다 작아서 $1 크레딧 요구가
    시장에 없는 가격이라 주문이 NEW 상태로 계속 미체결이었다. 실제 호가로
    계산해야 아무 종목에서나 정상 작동한다."""
    q = getattr(snap, "latest_quote", None)
    if q is None:
        return None
    return _mid_from_quote(q)


def _mid_from_quote(q) -> float | None:
    """호가 객체(bid_price/ask_price 속성)에서 중간값 계산 — _mid_price와
    close_limit_price(청산 limit 폴백)가 공유하는 핵심 로직."""
    bid, ask = getattr(q, "bid_price", None) or 0.0, getattr(q, "ask_price", None) or 0.0
    if bid <= 0 and ask <= 0:
        return None
    if bid <= 0:
        return float(ask)
    if ask <= 0:
        return float(bid)
    return (float(bid) + float(ask)) / 2.0


CREDIT_HAIRCUT_PCT = 0.05  # 호가 중간값보다 살짝 낮게 요구해 체결 가능성을 높임(backtest.py와 동일 개념)


def _vertical_credit(chain: dict, short_symbol: str | None, long_symbol: str | None) -> float | None:
    """숏/롱 레그의 실제 중간가로 순크레딧 계산 — None이면 호가데이터 부족."""
    if not short_symbol or not long_symbol:
        return None
    short_snap, long_snap = chain.get(short_symbol), chain.get(long_symbol)
    if short_snap is None or long_snap is None:
        return None
    short_mid, long_mid = _mid_price(short_snap), _mid_price(long_snap)
    if short_mid is None or long_mid is None:
        return None
    return short_mid - long_mid


def pick_by_delta(
    chain: dict, contract_type: ContractType, target_abs_delta: float,
    expiration: date | None = None,
) -> str | None:
    """expiration을 주면 그 만기의 계약으로만 후보를 제한한다. **2026-08-25 실거래
    사고로 발견**: TARGET_DTE_RANGE를 주간옵션(5~9일)으로 좁힌 뒤로 그 창 안에
    만기가 여러 개(월/수/금 위클리) 걸쳐 있는데, 숏레그·롱레그를 독립적으로
    "델타 최근접"만 보고 고르다 보니 SPY 콜스프레드가 숏 만기 9/3·롱(보호)
    만기 9/2로 갈렸다 — 보호레그가 숏레그보다 하루 먼저 만기돼서 그 하루 동안
    사실상 네이키드였고, Alpaca가 "uncovered option contracts"로 거부. 이제
    숏레그를 먼저 고른 뒤 그 만기로 보호레그 후보를 제한해서 같은 만기끼리만
    스프레드를 구성한다."""
    candidates = []
    for sym, snap in chain.items():
        if snap.greeks is None:
            continue
        is_call = "C" in sym[-9:]
        if contract_type == ContractType.CALL and not is_call:
            continue
        if contract_type == ContractType.PUT and is_call:
            continue
        if expiration is not None and _occ_expiration(sym) != expiration:
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
    r = RISK_PCT_MAX - (RISK_PCT_MAX - RISK_PCT_MIN) * max(0.0, min(1.0, scaled))
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
    signal: RegimeSignal | None = None,
    available_buying_power: float | None = None,
) -> CycleDecision:
    """한 종목에 대해 이번 사이클의 결정을 만든다 — 순수 결정 로직, 실제 주문 제출은 안 함.
    이 함수는 MCP 미연결 상태에서도(시장데이터만 있으면) 전부 테스트 가능해야 한다.

    signal: 호출부가 우선순위 정렬을 위해 이미 조회해둔 RegimeSignal이 있으면 그대로
    재사용(API 재호출 방지). available_buying_power: 실제 매수여력 상한 — 크립토
    쪽(decide_crypto_for_symbol)엔 2026-08-26부터 있었는데 옵션 쪽엔 없어서, 계좌
    매수여력이 부족한 날 신규진입이 전부 브로커 거부로 낭비되는 문제가 있었다
    (2026-09-02, 사용자 지적으로 발견)."""
    if not macro.ok:
        return CycleDecision(symbol, "n/a", macro, None, f"macro_gate_blocked:{macro.reason}")

    if signal is None:
        signal = fetch_and_classify_regime(stock_client, symbol)
    if signal.regime == "cash":
        return CycleDecision(symbol, signal.regime, macro, None, "regime_cash")

    # signal.risk_pct(2~5%, ATR 분위수 기반 동적)를 그대로 쓴다 — 이전엔
    # SAME_UNDERLYING_RISK_CAP_PCT(3%, "동일종목 여러 포지션 합산 상한" 개념)를
    # 여기 곱해서 사실상 항상 2%로 눌러버리는 결함이 있었다(백테스트 portfolio.py
    # 에서도 같은 유형의 혼동을 한 번 발견해 고친 적 있음 — 이번엔 라이브 쪽).
    # 종목당 동시보유 1개(_symbols_with_open_exposure 가드)라 same-underlying
    # 상한은 이 시점에 이미 자동 충족된다.
    risk_budget = equity * signal.risk_pct
    if available_buying_power is not None:
        risk_budget = min(risk_budget, max(0.0, available_buying_power))
        if risk_budget <= 0:
            return CycleDecision(symbol, signal.regime, macro, None, "insufficient_buying_power")
    cid_base = f"atlas-{symbol}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"

    if signal.regime == "range":
        puts = fetch_chain(option_client, symbol, ContractType.PUT)
        calls = fetch_chain(option_client, symbol, ContractType.CALL)
        short_put = pick_by_delta(puts, ContractType.PUT, SHORT_DELTA_TARGET)
        short_call = pick_by_delta(calls, ContractType.CALL, SHORT_DELTA_TARGET)
        # 보호레그는 반드시 그 숏레그와 같은 만기로만 고른다 — 아니면 보호레그가
        # 숏레그보다 먼저 만기돼 그 사이 네이키드가 되는 사고가 재발한다(2026-08-25 실측).
        long_put = pick_by_delta(puts, ContractType.PUT, PROTECTIVE_DELTA_TARGET, expiration=_occ_expiration(short_put) if short_put else None)
        long_call = pick_by_delta(calls, ContractType.CALL, PROTECTIVE_DELTA_TARGET, expiration=_occ_expiration(short_call) if short_call else None)
        if not all([short_put, long_put, short_call, long_call]):
            return CycleDecision(symbol, signal.regime, macro, None, "chain_insufficient")
        put_width = _vertical_width(short_put, long_put, ContractType.PUT)
        call_width = _vertical_width(short_call, long_call, ContractType.CALL)
        if put_width is None or call_width is None:
            return CycleDecision(symbol, signal.regime, macro, None, "malformed_spread")
        max_loss_est = max(put_width, call_width) * 100  # 실제 폭 기준(콘도르는 두 사이드 중 더 넓은 쪽이 최대손실)
        qty = contracts_for_max_loss(risk_budget, max_loss_est)
        if qty < 1:
            return CycleDecision(symbol, signal.regime, macro, None, "qty_below_1")
        put_credit = _vertical_credit(puts, short_put, long_put)
        call_credit = _vertical_credit(calls, short_call, long_call)
        if put_credit is None or call_credit is None:
            return CycleDecision(symbol, signal.regime, macro, None, "no_quote_data")
        net_credit = max(0.01, (put_credit + call_credit) * (1.0 - CREDIT_HAIRCUT_PCT))
        intent = build_iron_condor_intent(
            short_put, long_put, short_call, long_call, qty, net_credit=net_credit,
            client_order_id=f"{cid_base}-condor",
        )
        return CycleDecision(symbol, signal.regime, macro, intent, None)

    # trend_up / trend_down
    put_side = signal.regime == "trend_up"
    ctype = ContractType.PUT if put_side else ContractType.CALL
    chain = fetch_chain(option_client, symbol, ctype)
    short_leg = pick_by_delta(chain, ctype, SHORT_DELTA_TARGET)
    long_leg = pick_by_delta(chain, ctype, PROTECTIVE_DELTA_TARGET, expiration=_occ_expiration(short_leg) if short_leg else None)
    if not short_leg or not long_leg:
        return CycleDecision(symbol, signal.regime, macro, None, "chain_insufficient")
    width = _vertical_width(short_leg, long_leg, ctype)
    if width is None:
        return CycleDecision(symbol, signal.regime, macro, None, "malformed_spread")
    max_loss_est = width * 100  # 실제 폭 기준(전엔 signal.atr*1.25 근사 — 그날 아침 마진부족 거부의 원인)
    qty = contracts_for_max_loss(risk_budget, max_loss_est)
    if qty < 1:
        return CycleDecision(symbol, signal.regime, macro, None, "qty_below_1")
    credit = _vertical_credit(chain, short_leg, long_leg)
    if credit is None:
        return CycleDecision(symbol, signal.regime, macro, None, "no_quote_data")
    net_credit = max(0.01, credit * (1.0 - CREDIT_HAIRCUT_PCT))
    intent = build_credit_spread_intent(
        short_leg, long_leg, qty, net_credit=net_credit, client_order_id=f"{cid_base}-spread",
    )
    return CycleDecision(symbol, signal.regime, macro, intent, None)


# ── 청산 감시 (2026-08-24 추가 — 이전엔 진입만 있고 청산이 전혀 없었다) ──
# Alpaca 옵션 멀티레그 주문은 브라켓(진입 시 익절/손절 동시지정)을 지원하지 않는다
# (place_option_order 스키마에 take_profit/stop_loss 필드 자체가 없음, 실측 확인).
# 그래서 사이클마다 열린 포지션을 직접 조회해 손익을 계산하고 청산 여부를 판단해야
# 한다 — 이 파일은 그 판단(순수 함수)만 하고, 실제 포지션 조회·주문 제출은
# mcp_runner.py가 한다(신호계산과 실행을 분리하는 이 모듈의 기존 원칙 그대로).

def _occ_expiration(symbol: str) -> date | None:
    """OCC 옵션심볼(예: SPY260321P00600000)에서 만기일 추출. 파싱 실패 시 None
    (강제청산 판단을 못 하게 되므로 mcp_runner.py가 이 경우 로그만 남기고 스킵)."""
    for i, ch in enumerate(symbol):
        if ch.isdigit():
            digits = symbol[i:i + 6]
            if len(digits) == 6 and digits.isdigit():
                try:
                    return datetime.strptime(digits, "%y%m%d").date()
                except ValueError:
                    return None
            return None
    return None


@dataclass
class ExitDecision:
    should_close: bool
    reason: str  # "profit_target" | "stop_loss" | "dte_forced" | "hold"
    profit_pct: float


def evaluate_exit(leg_positions: list[dict], today: date | None = None) -> ExitDecision:
    """한 스프레드를 구성하는 레그들(Alpaca position dict, 필드: symbol, cost_basis,
    unrealized_pl)을 받아 청산해야 하는지 판단한다. 순수 함수 — 실제 포지션
    리스트만 있으면 브로커 연결 없이 테스트 가능.

    부호규약: cost_basis 합의 음수 절대값을 "수취 크레딧"으로 본다(순크레딧
    포지션이면 진입 시 현금이 들어왔으므로 Alpaca가 음의 cost_basis로 기록).
    **2026-08-25 실체결로 검증 완료** — SPY/QQQ 콘도르 8레그 전부 확인:
    숏레그(SHORT) cost_basis 전부 음수(QQQ -348/-468, SPY -624/-516),
    롱레그(LONG) 전부 양수(QQQ 192/135, SPY 252/150) — 가정 그대로 맞았다."""
    if not leg_positions:
        return ExitDecision(False, "hold", 0.0)

    today = today or datetime.now(timezone.utc).date()
    total_cost_basis = sum(float(p.get("cost_basis", 0.0)) for p in leg_positions)
    total_unrealized_pl = sum(float(p.get("unrealized_pl", 0.0)) for p in leg_positions)
    credit_received = abs(total_cost_basis)

    expirations = [_occ_expiration(p.get("symbol", "")) for p in leg_positions]
    valid_expirations = [e for e in expirations if e is not None]
    if valid_expirations:
        min_dte = min((e - today).days for e in valid_expirations)
        if min_dte <= FORCE_CLOSE_DTE:
            return ExitDecision(True, "dte_forced", total_unrealized_pl / credit_received if credit_received else 0.0)

    if credit_received <= 0:
        return ExitDecision(False, "hold", 0.0)  # 정보 부족 — 손익 판단 불가, 강제청산(DTE)만 유효

    profit_pct = total_unrealized_pl / credit_received
    if profit_pct >= PROFIT_TARGET_PCT:
        return ExitDecision(True, "profit_target", profit_pct)
    if profit_pct <= -(STOP_LOSS_MULTIPLE - 1.0):
        return ExitDecision(True, "stop_loss", profit_pct)
    return ExitDecision(False, "hold", profit_pct)


def build_close_intent(leg_positions: list[dict], client_order_id: str, limit_price: float | None = None) -> dict:
    """열린 포지션의 반대 방향(숏→buy_to_close, 롱→sell_to_close)으로 청산 주문을
    만든다. 기본은 market(멀티레그도 Alpaca가 market order_class=mleg 지원) — 청산은
    방향성 베팅이 아니라 리스크 종료가 목적이므로 가격에 민감하게 굴 이유가 없다.

    **2026-08-29 실거래로 발견**: 유동성 낮은 종목(TLT 등)은 브로커가 market 청산을
    "no available quote for symbol, please reenter with a limit"로 거부하는데,
    거부 사유가 항상 동일해서 재시도해도 100% 다시 거부된다(TLT 하루 280회 실측) —
    limit_price를 넘기면 limit 폴백으로 재시도할 수 있게 함(close_limit_price 참고).

    **2026-08-25 Codex 감사 지적**: qty를 leg_positions[0]에서만 가져와
    나머지 레그가 다른 수량이어도(부분체결·수동개입·잔여레그) 조용히 무시하고
    있었다. 전량청산은 모든 레그가 같은 수량이어야 의미가 있으므로, 수량이
    안 맞으면 예외를 던져 청산 자체를 막는다(fail-closed — 잘못된 수량으로
    청산 주문을 내는 것보다 사람이 볼 때까지 포지션을 열어두는 게 낫다)."""
    # 2026-09-02 라운드3 감사 지적: qty 누락시 1로 기본값·side가 "short"가
    # 아니면 전부 long 취급하던 게 fail-open이었다 — 브로커 응답 shape가
    # 흔들리면(필드 누락, 오타, 예상 밖 값) 실제 수량과 다른 청산주문을
    # 조용히 만들어낼 수 있었다. 위 qty-mismatch 검증과 같은 fail-closed
    # 원칙을 qty 존재여부·side 값 자체에도 적용한다.
    for p in leg_positions:
        if "qty" not in p or p.get("qty") in (None, ""):
            raise ValueError(f"leg missing qty, refusing to build close intent: {p.get('symbol')!r}")
        if str(p.get("side", "")).lower() not in ("short", "long"):
            raise ValueError(f"leg has unknown side {p.get('side')!r}, refusing to build close intent: {p.get('symbol')!r}")
    qtys = {abs(float(p["qty"])) for p in leg_positions}
    if len(qtys) != 1:
        raise ValueError(f"leg quantities mismatch, refusing to build close intent: {qtys}")
    qty_value = qtys.pop()
    # 2026-09-02 라운드4 감사 지적: 그냥 int()로 캐스팅하면 qty="0"이 "0"짜리
    # 무의미한 주문 intent가 되고, qty="1.5" 같은 소수는 조용히 1로 잘려나가
    # 실제 보유수량과 다른 청산주문을 낸다. 옵션은 계약단위(정수)만 유효하므로
    # 양의 정수가 아니면 예외.
    # 2026-09-02 라운드5 감사 지적: qty="inf"는 math.isfinite 체크 없이 바로
    # int()를 부르면 ValueError가 아니라 OverflowError가 나서(qty="nan"은
    # 우연히 ValueError라 잡히지만 "inf"는 안 잡힘) 호출부의 `except ValueError`를
    # 뚫고 나간다 — 유한값인지 먼저 확인해서 항상 같은 예외 타입으로 fail-closed.
    if not math.isfinite(qty_value) or qty_value <= 0 or qty_value != int(qty_value):
        raise ValueError(f"leg qty must be a positive whole number of contracts, got {qty_value}: refusing to build close intent")
    qtys = {qty_value}
    legs = []
    for p in leg_positions:
        side_held = str(p["side"]).lower()
        if side_held == "short":
            side, intent = "buy", "buy_to_close"
        else:
            side, intent = "sell", "sell_to_close"
        legs.append({
            "symbol": p["symbol"], "ratio_qty": "1", "side": side, "position_intent": intent,
        })
    intent = {
        "qty": str(int(qtys.pop())),
        "type": "market",
        "time_in_force": "day",
        "order_class": "mleg",
        "client_order_id": client_order_id,
        "legs": legs,
    }
    if limit_price is not None:
        intent["type"] = "limit"
        intent["limit_price"] = str(round(limit_price, 2))
    return intent


def close_limit_price(leg_positions: list[dict], quotes: dict) -> float | None:
    """market 청산이 견적없음으로 거부됐을 때 쓸 limit가 계산 — 각 레그 실호가
    중간값 합(숏레그=지불/buy_to_close, 롱레그=수취/sell_to_close), 체결 가능성을
    높이려 CREDIT_HAIRCUT_PCT만큼 우리 쪽에 불리하게(더 지불/덜 수취) 살짝 얹는다.
    quotes는 {symbol: quote객체(bid_price/ask_price)} — get_option_latest_quote 응답.
    부호 규약은 build_credit_spread_intent와 동일(양수=데빗, 음수=크레딧)."""
    total = 0.0
    for p in leg_positions:
        q = quotes.get(p["symbol"])
        if q is None:
            return None
        mid = _mid_from_quote(q)
        if mid is None:
            return None
        side_held = str(p.get("side", "")).lower()
        total += mid if side_held == "short" else -mid
    haircut = abs(total) * CREDIT_HAIRCUT_PCT
    return round(total + haircut, 2)


# ── 계좌 레벨 리스크게이트 (일일/주간/HWM 서킷브레이커) — 2026-08-24 배선 ──
# DAILY_LOSS_KILL_PCT/WEEKLY_LOSS_KILL_PCT/PORTFOLIO_DD_KILL_PCT는 처음부터
# 문서·상수로만 존재하고 백테스트·라이브 어디서도 실제로 안 걸려 있었다(4종목
# 합산 백테스트가 MDD 63~71%까지 찍는 걸 보고서야 발견) — mcp_runner.py가
# 매 사이클 이 함수를 호출해서 신규진입 전체를 막을지 판단한다. 청산감시는
# 이 게이트와 무관하게 항상 정상 진행(리스크 종료는 절대 안 막는다).

@dataclass
class RiskGateState:
    high_water_mark: float
    halt_until: datetime | None = None
    day_key: date | None = None
    day_start_equity: float = 0.0
    week_key: date | None = None
    week_start_equity: float = 0.0
    portfolio_dd_breach_active: bool = False  # 2026-08-27 버그 수정 — evaluate_risk_gates 참고

    def to_dict(self) -> dict:
        return {
            "high_water_mark": self.high_water_mark,
            "halt_until": self.halt_until.isoformat() if self.halt_until else None,
            "day_key": self.day_key.isoformat() if self.day_key else None,
            "day_start_equity": self.day_start_equity,
            "week_key": self.week_key.isoformat() if self.week_key else None,
            "week_start_equity": self.week_start_equity,
            "portfolio_dd_breach_active": self.portfolio_dd_breach_active,
        }

    @staticmethod
    def from_dict(d: dict) -> "RiskGateState":
        return RiskGateState(
            high_water_mark=float(d.get("high_water_mark", 0.0)),
            halt_until=datetime.fromisoformat(d["halt_until"]) if d.get("halt_until") else None,
            day_key=date.fromisoformat(d["day_key"]) if d.get("day_key") else None,
            day_start_equity=float(d.get("day_start_equity", 0.0)),
            week_key=date.fromisoformat(d["week_key"]) if d.get("week_key") else None,
            week_start_equity=float(d.get("week_start_equity", 0.0)),
            portfolio_dd_breach_active=bool(d.get("portfolio_dd_breach_active", False)),
        )


@dataclass
class RiskGateDecision:
    blocked: bool
    reason: str  # "ok" | "daily_loss_kill" | "weekly_loss_kill" | "portfolio_dd_halt_active" | "portfolio_dd_kill_triggered"
    state: RiskGateState


def evaluate_risk_gates(equity: float, state: RiskGateState, now: datetime | None = None) -> RiskGateDecision:
    """순수 함수 — 상태(state)를 입력으로 받아 새 상태와 이번 사이클의 신규진입
    차단 여부를 반환한다. mcp_runner.py가 사이클마다 이전 상태를 파일에서
    읽어 넘기고, 반환된 새 상태를 다시 저장한다(프로세스가 사이클마다 새로
    뜨므로 상태는 파일로만 지속됨).

    **정지=잔고 리셋이 아니다**: high_water_mark는 절대 낮아지지 않고, 정지가
    풀려도 이미 난 손실은 그대로 이어간다 — 사용자가 명시적으로 강조한
    요구사항. day_start_equity/week_start_equity는 그 날/주의 "손실 한도
    계산 기준점"일 뿐 실제 잔고가 아니다(하루/한 주 지나면 갱신되지만,
    equity 자체는 계속 누적된 손익을 그대로 반영한다)."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())

    day_key = state.day_key
    day_start_equity = state.day_start_equity
    if day_key != today:
        day_key, day_start_equity = today, equity

    week_key = state.week_key
    week_start_equity = state.week_start_equity
    if week_key != week_start:
        week_key, week_start_equity = week_start, equity

    high_water_mark = max(state.high_water_mark, equity) if state.high_water_mark > 0 else equity
    halt_until = state.halt_until
    breach_active = state.portfolio_dd_breach_active

    if halt_until is not None and now < halt_until:
        new_state = RiskGateState(high_water_mark, halt_until, day_key, day_start_equity, week_key, week_start_equity, breach_active)
        return RiskGateDecision(True, "portfolio_dd_halt_active", new_state)

    daily_dd = 1.0 - equity / day_start_equity if day_start_equity > 0 else 0.0
    weekly_dd = 1.0 - equity / week_start_equity if week_start_equity > 0 else 0.0
    portfolio_dd = 1.0 - equity / high_water_mark if high_water_mark > 0 else 0.0

    if portfolio_dd < PORTFOLIO_DD_KILL_PCT:
        breach_active = False  # 회복 확인 — 다음 하락은 새 서킷브레이커로 취급

    if daily_dd >= DAILY_LOSS_KILL_PCT:
        reason = "daily_loss_kill"
    elif weekly_dd >= WEEKLY_LOSS_KILL_PCT:
        reason = "weekly_loss_kill"
    elif portfolio_dd >= PORTFOLIO_DD_KILL_PCT and not breach_active:
        # **2026-08-27 버그 수정**: 원래 halt_until이 지나도 portfolio_dd가
        # 아직 20% 밑이면 곧바로 재발동해서 사실상 영구 정지가 될 수 있었다
        # (백테스트 쪽 portfolio.py에서 실측으로 발견 — 옵션 챔피언 6종목
        # 합산 백테스트가 최초 -20% 시점 이후 20개월간 전혀 거래를 못 해서
        # 그 시점 잔고를 "3년 성과"로 잘못 보고했다). 라이브는 실제 브로커
        # 잔고가 마크투마켓으로 계속 움직여서 이 폐쇄루프 문제는 없지만,
        # "45분 정지 후 재개"라는 설계 의도 자체(docstring)는 여기도 동일하게
        # 적용해야 한다 — 엣지트리거로 통일.
        halt_until = now + timedelta(minutes=PORTFOLIO_DD_HALT_MINUTES)
        breach_active = True
        reason = "portfolio_dd_kill_triggered"
    else:
        new_state = RiskGateState(high_water_mark, None, day_key, day_start_equity, week_key, week_start_equity, breach_active)
        return RiskGateDecision(False, "ok", new_state)

    new_state = RiskGateState(high_water_mark, halt_until, day_key, day_start_equity, week_key, week_start_equity, breach_active)
    return RiskGateDecision(True, reason, new_state)


# ── 크립토 슬리브 (2026-08-26 추가, Task Contract: BTC/USD·ETH/USD 현물
#    롱/플랫 전용, 옵션과 같은 $100k 계좌·같은 리스크게이트 공유) ──
# docs/design-multi-asset-combined-backtest.md로 백테스트 검증한 것과 동일한
# 상수(STOP_ATR_MULT/R_MULTIPLE/MAX_HOLD_DAYS)를 그대로 쓴다 — 라이브가 백테스트와
# 다른 파라미터로 돌면 검증한 숫자가 무의미해진다(TARGET_DTE_RANGE 주석의 옵션
# 쪽 원칙과 동일).
CRYPTO_SYMBOLS = ("BTC/USD", "ETH/USD")
# 2026-09-02: 라이브 실측 재보정 — 2.0x ATR 손절폭이 BTC/ETH 실제 주간 변동폭
# (3~4%대)보다 훨씬 넓게(BTC 4%/8%, ETH 7.5%/14.9%) 잡혀 있어, 8/30에 BTC+ETH
# 합산 미실현이익이 ~$2,150까지 났다가 익절선 근처도 못 가고 그대로 반납되는
# 걸 실측했다. R_MULTIPLE(보상:리스크 2:1)은 그대로 두고 ATR 배수만 절반으로
# 줄여 손절·익절 폭을 실측 변동성에 맞춘다. **주의**: 이 상수는 원래
# docs/design-multi-asset-combined-backtest.md의 백테스트로 검증된 값과
# 동일해야 한다는 원칙이 있었는데(위 주석), 이번 변경은 대회 마감 임박으로
# 백테스트 재검증 없이 라이브 관측만으로 조정한 것 — 사후에 백테스트로
# 재검증 필요.
CRYPTO_STOP_ATR_MULT = 1.0
CRYPTO_R_MULTIPLE = 2.0
CRYPTO_MAX_HOLD_DAYS = 10
CRYPTO_LOOKBACK_DAYS = ATR_PCT_LOOKBACK_DAYS + 30


CRYPTO_ATR_PERIOD = 20  # 옵션쪽 14거래일(≈캘린더 20일)과 같은 실제 기간을 24/7
# 캘린더 봉으로 보려면 20이 필요(14*7/5≈19.6) — 2026-09-02, atr_pct_percentile_bounds 참고.


def fetch_and_classify_crypto_regime(client: CryptoHistoricalDataClient, symbol: str) -> RegimeSignal:
    """옵션의 fetch_and_classify_regime과 동일 패턴, 데이터소스만 크립토.
    레짐 판정 자체(classify_regime_from_bars)는 자산군 무관한 순수함수라
    재사용한다 — 신규 레짐로직 0줄(백테스트 multi_asset.py와 같은 결정).
    단 ATR 기간만 CRYPTO_ATR_PERIOD(20)로 넘겨 옵션쪽 14거래일과 같은
    캘린더 기간을 보게 맞춘다."""
    req = CryptoBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=CRYPTO_LOOKBACK_DAYS),
    )
    bars = client.get_crypto_bars(req).df
    df = bars.xs(symbol, level="symbol") if isinstance(bars.index, pd.MultiIndex) else bars
    df.index = pd.DatetimeIndex(df.index.date)
    df = df[~df.index.duplicated(keep="last")]
    return classify_regime_from_bars(df, symbol, atr_period=CRYPTO_ATR_PERIOD)


@dataclass
class CryptoCycleDecision:
    symbol: str
    regime: str
    macro_gate: MacroGate
    order_intent: dict | None
    skip_reason: str | None
    stop_pct: float | None = None  # 진입 시 계산한 손절 폭(%) — 청산판정에 저장해서 씀
    target_pct: float | None = None


def build_crypto_order_intent(symbol: str, notional: float, client_order_id: str) -> dict:
    """place_crypto_order MCP 도구용 — notional(달러) 시장가 매수. qty 대신
    notional을 쓰는 이유: 코인 소수점 정밀도를 브로커가 알아서 처리하게 해서
    (백테스트의 qty_increment=1e-4 근사를 라이브에서 재구현할 필요가 없음)."""
    return {
        "symbol": symbol, "side": "buy", "notional": f"{notional:.2f}",
        "type": "market", "time_in_force": "gtc", "client_order_id": client_order_id,
    }


def decide_crypto_for_symbol(
    client: CryptoHistoricalDataClient, symbol: str, equity: float, macro: MacroGate,
    available_cash: float | None = None,
) -> CryptoCycleDecision:
    """현물 롱/플랫 전용 — bear_call(하락) 신호는 숏이 불가능해 진입하지 않는다
    (Alpaca 크립토는 비마진 현물, §6.4 백테스트 설계와 동일 결정). range/cash
    레짐도 방향성 포지션이 아니므로 진입하지 않는다 — trend_up만 매수."""
    if not macro.ok:
        return CryptoCycleDecision(symbol, "n/a", macro, None, f"macro_gate_blocked:{macro.reason}")

    signal = fetch_and_classify_crypto_regime(client, symbol)
    if signal.regime != "trend_up":
        return CryptoCycleDecision(symbol, signal.regime, macro, None, "not_long_regime")

    risk_budget = equity * signal.risk_pct
    if signal.atr <= 0 or signal.close <= 0:
        return CryptoCycleDecision(symbol, signal.regime, macro, None, "invalid_atr_or_price")

    stop_distance = CRYPTO_STOP_ATR_MULT * signal.atr
    stop_pct = stop_distance / signal.close
    target_pct = CRYPTO_R_MULTIPLE * stop_pct
    if stop_pct <= 0:
        return CryptoCycleDecision(symbol, signal.regime, macro, None, "invalid_stop_distance")

    # risk_budget/stop_pct는 "손절 도달 시 risk_budget만큼만 잃도록" 포지션
    # 크기를 역산한 값 — 손절폭이 가격 대비 좁으면(변동성 낮은 구간) 이 값이
    # 계좌 전체보다 커질 수 있다(실측 2026-08-26: BTC 드라이런에서 계좌의 162%).
    # 크립토 현물은 레버리지가 없어 실제 매수 가능 금액이 잔고를 못 넘는다.
    # 상한을 equity 전체가 아니라 "현금유보(CASH_RESERVE_PCT) 제외하고 크립토
    # 심볼 수만큼 균등분배"로 잡는다 — 안 그러면 BTC/ETH가 같은 사이클에 동시
    # 신호를 내는 흔한 경우(둘 다 trend_up, 예: 2026-08-26 드라이런처럼) 각자
    # equity 100%씩 요구해서 둘째 주문이 매수여력 부족으로 거부되거나, 옵션
    # 포지션이 쓸 자리가 아예 안 남는다.
    #
    # **2026-08-26 실거래로 발견**: equity(=$98,953, 옵션 포지션 시가평가 포함)
    # 기준으로 캡을 잡아도 실제 크립토 매수 가능 현금은 훨씬 적었다(옵션 6개
    # 스프레드가 마진으로 이미 물고 있어서) — ETH 주문이 "requested $44,553,
    # available $9,991"로 브로커에 거부됨. available_cash(호출부가 계좌의
    # non_marginable_buying_power를 넘겨준다 — 크립토 현물 매수에 실제 쓸 수
    # 있는 현금)가 있으면 그것도 상한에 같이 반영한다.
    max_notional_per_symbol = equity * (1.0 - CASH_RESERVE_PCT) / len(CRYPTO_SYMBOLS)
    if available_cash is not None:
        max_notional_per_symbol = min(max_notional_per_symbol, available_cash / len(CRYPTO_SYMBOLS))
    notional = min(risk_budget / stop_pct, max_notional_per_symbol)
    if notional < 10.0:  # Alpaca 크립토 시장가 최소주문 근사치 — 그 이하는 의미없는 먼지주문
        return CryptoCycleDecision(symbol, signal.regime, macro, None, "notional_below_minimum")

    cid = f"atlas-crypto-{symbol.replace('/', '')}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    intent = build_crypto_order_intent(symbol, notional, cid)
    return CryptoCycleDecision(symbol, signal.regime, macro, intent, None, stop_pct, target_pct)


@dataclass
class CryptoPositionState:
    entry_date: date
    stop_pct: float
    target_pct: float

    def to_dict(self) -> dict:
        return {"entry_date": self.entry_date.isoformat(), "stop_pct": self.stop_pct, "target_pct": self.target_pct}

    @staticmethod
    def from_dict(d: dict) -> "CryptoPositionState":
        return CryptoPositionState(
            entry_date=date.fromisoformat(d["entry_date"]),
            stop_pct=float(d["stop_pct"]), target_pct=float(d["target_pct"]),
        )


def evaluate_crypto_exit(position: dict, state: CryptoPositionState, today: date | None = None) -> ExitDecision:
    """position은 Alpaca get_open_position 응답(unrealized_plpc 포함). 진입 시
    저장해둔 stop_pct/target_pct(ATR 기반, §§Task Contract — 백테스트와 동일
    2xATR 손절/2R 익절)와 보유일수(MAX_HOLD_DAYS)로 판단하는 순수 함수 —
    옵션의 evaluate_exit과 같은 스타일(포지션 dict + 저장된 상태만으로 테스트 가능)."""
    today = today or datetime.now(timezone.utc).date()
    held_days = (today - state.entry_date).days
    if held_days >= CRYPTO_MAX_HOLD_DAYS:
        return ExitDecision(True, "max_hold_days", float(position.get("unrealized_plpc", 0.0)))

    plpc = float(position.get("unrealized_plpc", 0.0))
    if plpc >= state.target_pct:
        return ExitDecision(True, "profit_target", plpc)
    if plpc <= -state.stop_pct:
        return ExitDecision(True, "stop_loss", plpc)
    return ExitDecision(False, "hold", plpc)


def build_crypto_close_intent(symbol: str, qty: str, client_order_id: str) -> dict:
    """전량 시장가 매도(롱/플랫 전용이라 청산은 항상 sell)."""
    return {
        "symbol": symbol, "side": "sell", "qty": qty,
        "type": "market", "time_in_force": "gtc", "client_order_id": client_order_id,
    }
