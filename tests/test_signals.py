"""브로커 키 없이 도는 순수함수 유닛테스트 — 규칙 로직만 검증."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from signals import (  # noqa: E402
    CryptoPositionState,
    MacroGate,
    RiskGateState,
    _vertical_width,
    build_close_intent,
    build_credit_spread_intent,
    build_crypto_close_intent,
    build_crypto_order_intent,
    build_iron_condor_intent,
    classify_regime_from_bars,
    contracts_for_max_loss,
    decide_for_symbol,
    evaluate_crypto_exit,
    evaluate_exit,
    evaluate_risk_gates,
    pick_by_delta,
    risk_pct_for_atr_pct,
)
from alpaca.trading.enums import ContractType  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402


def _trending_up_bars(n=80):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    close = pd.Series(range(100, 100 + n), index=idx, dtype=float)
    return pd.DataFrame({
        "high": close + 1, "low": close - 1, "close": close,
    }, index=idx)


def _flat_bars(n=80):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    close = pd.Series([100 + (i % 3) * 0.1 for i in range(n)], index=idx, dtype=float)
    return pd.DataFrame({
        "high": close + 0.3, "low": close - 0.3, "close": close,
    }, index=idx)


def test_classify_regime_trend_up():
    signal = classify_regime_from_bars(_trending_up_bars(), "TEST")
    assert signal.regime == "trend_up"
    assert signal.adx > 20


def test_classify_regime_range():
    signal = classify_regime_from_bars(_flat_bars(), "TEST")
    assert signal.regime == "range"
    assert signal.adx < 18


def test_risk_pct_scales_down_with_high_volatility():
    low_vol = risk_pct_for_atr_pct(atr_pct=0.01, atr_pct_low_q=0.01, atr_pct_high_q=0.05)
    high_vol = risk_pct_for_atr_pct(atr_pct=0.05, atr_pct_low_q=0.01, atr_pct_high_q=0.05)
    assert low_vol > high_vol
    assert 0.02 <= high_vol <= 0.10
    assert 0.02 <= low_vol <= 0.10


def test_risk_pct_clips_to_bounds():
    assert risk_pct_for_atr_pct(0.10, 0.01, 0.05) == pytest.approx(0.02)
    assert risk_pct_for_atr_pct(-1.0, 0.01, 0.05) == pytest.approx(0.10)


def test_contracts_for_max_loss_floor_division():
    assert contracts_for_max_loss(3000, 380) == 7
    assert contracts_for_max_loss(300, 380) == 0
    assert contracts_for_max_loss(1000, 0) == 0


def test_credit_spread_intent_sign_convention_is_negative_for_credit():
    """MCP place_option_order 규약: limit_price 양수=데빗, 음수=크레딧.
    net_credit을 양수로 넣어도 intent의 limit_price는 반드시 음수여야 한다."""
    intent = build_credit_spread_intent("SPY_SHORT", "SPY_LONG", qty=3, net_credit=1.25, client_order_id="x")
    assert intent["limit_price"] == "-1.25"
    assert intent["order_class"] == "mleg"
    assert len(intent["legs"]) == 2
    assert intent["legs"][0]["side"] == "sell"
    assert intent["legs"][0]["position_intent"] == "sell_to_open"
    assert intent["legs"][1]["side"] == "buy"
    assert intent["legs"][1]["position_intent"] == "buy_to_open"


def test_iron_condor_intent_has_four_legs_correct_sides():
    intent = build_iron_condor_intent("SP", "LP", "SC", "LC", qty=2, net_credit=0.8, client_order_id="y")
    assert intent["limit_price"] == "-0.8"
    assert len(intent["legs"]) == 4
    sides = [leg["side"] for leg in intent["legs"]]
    assert sides == ["sell", "buy", "sell", "buy"]


class _FakeGreeks:
    def __init__(self, delta):
        self.delta = delta


class _FakeQuote:
    def __init__(self, bid_price, ask_price):
        self.bid_price = bid_price
        self.ask_price = ask_price


class _FakeSnapshot:
    def __init__(self, delta, bid=1.0, ask=1.2):
        self.greeks = _FakeGreeks(delta)
        self.latest_quote = _FakeQuote(bid, ask)


class _FakeOptionDataClient:
    """market-data 조회를 흉내내는 페이크 — 실제 브로커 왕복 없이 decide_for_symbol을 검증."""

    def __init__(self, chains: dict):
        self._chains = chains  # {(symbol, contract_type): {occ_symbol: _FakeSnapshot}}

    def get_option_chain(self, req):
        return self._chains[(req.underlying_symbol, req.type)]


class _FakeStockDataClient:
    def __init__(self, bars_df: pd.DataFrame, symbol: str):
        self._bars_df = bars_df
        self._symbol = symbol

    def get_stock_bars(self, req):
        class _Wrapper:
            df = self._bars_df

        return _Wrapper()


def test_vertical_width_computes_real_strike_distance():
    """2026-08-25 Codex 감사 발견(실측 확인): 사이징이 signal.atr*2.5 근사만
    쓰고 있었는데 그날 아침 실제로 "insufficient options buying power" 거부가
    났다 — 실제 선택된 strike로 사이징해야 한다."""
    assert _vertical_width("SPY260902P00750000", "SPY260902P00737000", ContractType.PUT) == pytest.approx(13.0)
    assert _vertical_width("SPY260902C00778000", "SPY260902C00784000", ContractType.CALL) == pytest.approx(6.0)


def test_vertical_width_rejects_wrong_direction_or_same_contract():
    """보호행사가가 방향을 잘못 잡으면(풋인데 롱행사가가 더 높다든가) 진짜
    보호가 아니다 — None을 반환해서 주문 생성 자체를 막아야 한다."""
    assert _vertical_width("SPY260902P00737000", "SPY260902P00750000", ContractType.PUT) is None  # 방향 반대
    assert _vertical_width("SPY260902C00784000", "SPY260902C00778000", ContractType.CALL) is None  # 방향 반대
    assert _vertical_width("SPY260902P00750000", "SPY260902P00750000", ContractType.PUT) is None  # 같은 계약


def test_pick_by_delta_expiration_filter_excludes_other_expirations():
    """2026-08-25 실거래 사고 회귀 방지 — SPY 콜스프레드가 숏레그 만기(9/3)와
    보호레그 만기(9/2)가 갈려서 Alpaca가 "uncovered option contracts"로 거부한
    바로 그 케이스. expiration을 넘기면 다른 만기 후보는 델타가 더 가까워도
    제외돼야 한다."""
    chain = {
        "SPY260902C00784000": _FakeSnapshot(delta=0.05),   # 9/2 만기, 목표델타(0.06)와 차이 0.01
        "SPY260903C00780000": _FakeSnapshot(delta=0.061),  # 9/3 만기, 차이 0.001(더 가까움)이지만 만기가 다름
    }
    picked = pick_by_delta(chain, ContractType.CALL, target_abs_delta=0.06, expiration=date(2026, 9, 2))
    assert picked == "SPY260902C00784000"


def test_decide_for_symbol_macro_gate_blocks_regardless_of_regime():
    macro = MacroGate(ok=False, reason="stage4_declining", stage="stage4_declining")
    decision = decide_for_symbol(
        stock_client=None, option_client=None, symbol="SPY", equity=100_000, macro=macro,
    )
    assert decision.order_intent is None
    assert decision.skip_reason == "macro_gate_blocked:stage4_declining"


def test_decide_for_symbol_trend_up_builds_put_credit_spread():
    macro = MacroGate(ok=True, reason="clear", stage="stage2_advancing")
    stock_client = _FakeStockDataClient(_trending_up_bars(), "SPY")
    chains = {
        ("SPY", __import__("alpaca.trading.enums", fromlist=["ContractType"]).ContractType.PUT): {
            "SPY260902P00750000": _FakeSnapshot(delta=-0.15, bid=2.0, ask=2.2),  # 숏(높은 행사가)
            "SPY260902P00737000": _FakeSnapshot(delta=-0.06, bid=0.5, ask=0.7),  # 보호(낮은 행사가, 같은 만기)
        },
    }
    option_client = _FakeOptionDataClient(chains)
    decision = decide_for_symbol(stock_client, option_client, "SPY", equity=100_000, macro=macro)
    assert decision.regime == "trend_up"
    assert decision.order_intent is not None
    assert decision.order_intent["order_class"] == "mleg"
    assert len(decision.order_intent["legs"]) == 2
    # 2026-08-25 실거래 발견 회귀 방지 — net_credit이 하드코딩 1.0이었을 때 GLD/TLT처럼
    # 실제 프리미엄이 $1 미만인 종목은 영원히 미체결이었다. 숏(mid 2.1)-롱(mid 0.6)=1.5,
    # 5% 헤어컷 반영해 1.0이 아닌 실제 계산값이 나와야 한다.
    assert float(decision.order_intent["limit_price"]) == pytest.approx(-1.5 * 0.95, abs=0.01)


def test_decide_for_symbol_never_mixes_expirations_across_short_and_protective_leg():
    """2026-08-25 실거래 사고 회귀 방지 — 체인에 두 개 만기(9/2, 9/3)가 섞여
    있을 때 숏레그와 보호레그가 서로 다른 만기로 갈리면 안 된다(보호레그가
    숏레그보다 먼저 만기되면 그 사이 네이키드 — Alpaca가 실제로 거부했음)."""
    macro = MacroGate(ok=True, reason="clear", stage="stage2_advancing")
    stock_client = _FakeStockDataClient(_trending_up_bars(), "SPY")
    chains = {
        ("SPY", ContractType.PUT): {
            "SPY260902P00749000": _FakeSnapshot(delta=-0.15),  # 숏레그, 9/2 만기 — 목표델타(0.15) 정확
            "SPY260903P00737000": _FakeSnapshot(delta=-0.061),  # 보호레그 후보, 9/3 — 델타가 더 가깝지만 만기가 다름
            "SPY260902P00737000": _FakeSnapshot(delta=-0.05),   # 보호레그 후보, 9/2 — 숏레그와 같은 만기
        },
    }
    option_client = _FakeOptionDataClient(chains)
    decision = decide_for_symbol(stock_client, option_client, "SPY", equity=100_000, macro=macro)
    legs = decision.order_intent["legs"]
    expirations = {leg["symbol"][3:9] for leg in legs}
    assert len(expirations) == 1, f"legs span multiple expirations: {legs}"


# ── 청산 감시 (2026-08-24 추가 — 이전엔 청산 판단 로직이 아예 없었다) ──

def _far_expiry_symbol(days_out=30):
    exp = (date.today() + timedelta(days=days_out)).strftime("%y%m%d")
    return f"SPY{exp}P00600000"


def test_evaluate_exit_holds_when_pnl_is_neutral():
    legs = [
        {"symbol": _far_expiry_symbol(), "cost_basis": -100.0, "unrealized_pl": 10.0, "side": "short", "qty": "1"},
        {"symbol": _far_expiry_symbol(), "cost_basis": 0.0, "unrealized_pl": 0.0, "side": "long", "qty": "1"},
    ]
    decision = evaluate_exit(legs)
    assert decision.should_close is False
    assert decision.reason == "hold"


def test_evaluate_exit_closes_at_profit_target():
    """수취크레딧 100 중 60(60%) 이익 실현 — PROFIT_TARGET_PCT=0.5 초과."""
    legs = [{"symbol": _far_expiry_symbol(), "cost_basis": -100.0, "unrealized_pl": 60.0, "side": "short", "qty": "1"}]
    decision = evaluate_exit(legs)
    assert decision.should_close is True
    assert decision.reason == "profit_target"


def test_evaluate_exit_closes_at_stop_loss():
    """손실이 크레딧의 100%(=STOP_LOSS_MULTIPLE 2.0의 손실분) 도달."""
    legs = [{"symbol": _far_expiry_symbol(), "cost_basis": -100.0, "unrealized_pl": -100.0, "side": "short", "qty": "1"}]
    decision = evaluate_exit(legs)
    assert decision.should_close is True
    assert decision.reason == "stop_loss"


def test_evaluate_exit_forces_close_near_expiry_regardless_of_pnl():
    """DTE가 FORCE_CLOSE_DTE(2, 주간옵션 기준 감마리스크 회피용) 이하면 손익이
    좋아도 강제청산."""
    legs = [{"symbol": _far_expiry_symbol(days_out=1), "cost_basis": -100.0, "unrealized_pl": 5.0, "side": "short", "qty": "1"}]
    decision = evaluate_exit(legs)
    assert decision.should_close is True
    assert decision.reason == "dte_forced"


def test_build_close_intent_reverses_short_and_long_sides():
    legs = [
        {"symbol": "SPY_SHORT", "side": "short", "qty": "3"},
        {"symbol": "SPY_LONG", "side": "long", "qty": "3"},
    ]
    intent = build_close_intent(legs, client_order_id="close-1")
    assert intent["type"] == "market"
    assert intent["order_class"] == "mleg"
    by_symbol = {leg["symbol"]: leg for leg in intent["legs"]}
    assert by_symbol["SPY_SHORT"]["side"] == "buy"
    assert by_symbol["SPY_SHORT"]["position_intent"] == "buy_to_close"
    assert by_symbol["SPY_LONG"]["side"] == "sell"
    assert by_symbol["SPY_LONG"]["position_intent"] == "sell_to_close"


def test_build_close_intent_rejects_mismatched_leg_quantities():
    """2026-08-25 Codex 감사 지적: qty를 legs[0]에서만 가져와서 나머지 레그가
    다른 수량이어도(부분체결·수동개입) 조용히 무시하고 있었다. fail-closed로
    바꿔 예외를 던지게 함 — 잘못된 수량 청산보다 사람이 볼 때까지 열어두는 게 낫다."""
    legs = [
        {"symbol": "SPY_SHORT", "side": "short", "qty": "3"},
        {"symbol": "SPY_LONG", "side": "long", "qty": "2"},  # 수량 불일치
    ]
    with pytest.raises(ValueError):
        build_close_intent(legs, client_order_id="close-1")


# ── 계좌 레벨 리스크게이트 (2026-08-24 라이브 배선 — 내일 개장 전 마지막 안전장치) ──

def test_risk_gate_ok_when_no_drawdown():
    state = RiskGateState(high_water_mark=100_000.0)
    decision = evaluate_risk_gates(100_000.0, state, now=datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc))
    assert decision.blocked is False
    assert decision.reason == "ok"


def test_risk_gate_daily_loss_kill_blocks_same_day():
    """2026-08-25 오전: 킬스위치 임계값을 3%/6%→20%/50%로 재조정(사용자 지시,
    포지션 1건 최대리스크 10%→5% 하향과 맞물려)."""
    state = RiskGateState(high_water_mark=100_000.0, day_key=date(2026, 1, 5), day_start_equity=100_000.0)
    decision = evaluate_risk_gates(75_000.0, state, now=datetime(2026, 1, 5, 11, 0, tzinfo=timezone.utc))  # -25%
    assert decision.blocked is True
    assert decision.reason == "daily_loss_kill"


def test_risk_gate_portfolio_drawdown_halts_and_does_not_erase_hwm():
    """HWM 대비 -20% 도달 시 정지, 그리고 HWM 자체는 낮아지지 않아야 한다
    (정지=잔고 리셋 아님 — 사용자가 명시적으로 요구한 불변식)."""
    state = RiskGateState(high_water_mark=100_000.0)
    breach_time = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    decision = evaluate_risk_gates(79_000.0, state, now=breach_time)  # -21%
    assert decision.blocked is True
    assert decision.reason == "portfolio_dd_kill_triggered"
    assert decision.state.high_water_mark == 100_000.0  # 낮아지지 않음
    assert decision.state.halt_until == breach_time + timedelta(minutes=45)


def test_risk_gate_stays_halted_within_45_minutes_then_releases():
    state = RiskGateState(high_water_mark=100_000.0)
    breach_time = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    after_breach = evaluate_risk_gates(79_000.0, state, now=breach_time)

    still_halted = evaluate_risk_gates(79_000.0, after_breach.state, now=breach_time + timedelta(minutes=30))
    assert still_halted.blocked is True
    assert still_halted.reason == "portfolio_dd_halt_active"

    # 45분+다음 주(일일/주간 기준점도 새로 리셋)까지 지나고 잔고가 회복됐다면 재개돼야 함
    next_monday = breach_time + timedelta(days=7)
    recovered = evaluate_risk_gates(97_000.0, still_halted.state, now=next_monday)
    assert recovered.blocked is False
    assert recovered.reason == "ok"


def test_risk_gate_resumes_after_45min_even_if_equity_has_not_recovered():
    """2026-08-27 회귀 방지 — 45분 정지가 풀린 뒤 잔고가 여전히 -20% 밑이면
    원래 코드는 곧바로 재발동해서 사실상 영구 정지였다(백테스트 쪽에서 실측
    발견: 옵션 챔피언 6종목 합산 백테스트가 최초 -20% 이후 20개월간 거래
    0건). 설계 의도(docstring)는 "정지 후 재개, 손실은 그대로 이어간다"이므로
    45분 뒤엔 잔고가 그대로 -20%여도 신규진입이 재개돼야 한다."""
    state = RiskGateState(high_water_mark=100_000.0)
    breach_time = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    after_breach = evaluate_risk_gates(79_000.0, state, now=breach_time)
    assert after_breach.blocked is True

    resumed = evaluate_risk_gates(79_000.0, after_breach.state, now=breach_time + timedelta(minutes=46))
    assert resumed.blocked is False
    assert resumed.reason == "ok"
    assert resumed.state.high_water_mark == 100_000.0  # HWM은 여전히 안 낮아짐

    # 여전히 20% 밑인 상태에서 새 후보 평가가 또 들어와도 breach_active가 True로
    # 남아있어 재발동하지 않아야 한다(회복 전까지는 무장 해제 안 됨).
    still_ok = evaluate_risk_gates(79_500.0, resumed.state, now=breach_time + timedelta(minutes=50))
    assert still_ok.blocked is False

    # 회복(<20%) 후 다시 새로 20% 밑으로 떨어지면 새 서킷브레이커가 발동해야 한다.
    recovered = evaluate_risk_gates(85_000.0, still_ok.state, now=breach_time + timedelta(minutes=55))
    assert recovered.blocked is False
    re_breach = evaluate_risk_gates(79_000.0, recovered.state, now=breach_time + timedelta(minutes=60))
    assert re_breach.blocked is True
    assert re_breach.reason == "portfolio_dd_kill_triggered"


# ── 크립토 슬리브 ──

class _FakeCryptoDataClient:
    def __init__(self, bars_df: pd.DataFrame, symbol: str):
        self._bars_df = bars_df
        self._symbol = symbol

    def get_crypto_bars(self, req):
        class _Wrapper:
            df = self._bars_df

        return _Wrapper()


def test_decide_crypto_macro_gate_blocks_regardless_of_regime():
    from signals import decide_crypto_for_symbol
    macro = MacroGate(ok=False, reason="stage4_declining", stage="stage4_declining")
    decision = decide_crypto_for_symbol(client=None, symbol="BTC/USD", equity=100_000, macro=macro)
    assert decision.order_intent is None
    assert decision.skip_reason == "macro_gate_blocked:stage4_declining"


def test_decide_crypto_trend_up_builds_market_buy():
    from signals import decide_crypto_for_symbol
    macro = MacroGate(ok=True, reason="clear", stage="stage2_advancing")
    client = _FakeCryptoDataClient(_trending_up_bars(), "BTC/USD")
    decision = decide_crypto_for_symbol(client, "BTC/USD", equity=100_000, macro=macro)
    assert decision.regime == "trend_up"
    assert decision.order_intent is not None
    assert decision.order_intent["side"] == "buy"
    assert decision.order_intent["symbol"] == "BTC/USD"
    assert float(decision.order_intent["notional"]) > 0
    assert decision.stop_pct is not None and decision.stop_pct > 0
    assert decision.target_pct == pytest.approx(decision.stop_pct * 2.0)  # CRYPTO_R_MULTIPLE=2.0


def test_decide_crypto_notional_capped_by_available_cash():
    """2026-08-26 실거래 발견 회귀 방지 — equity(옵션 포지션 시가평가 포함)로만
    캡하면 실제 크립토 매수 가능 현금(non_marginable_buying_power)보다 훨씬 큰
    notional을 요청해서 브로커가 거부한다(실측: 요청 $44,553, 가용 $9,991)."""
    from signals import decide_crypto_for_symbol
    macro = MacroGate(ok=True, reason="clear", stage="stage2_advancing")
    client = _FakeCryptoDataClient(_trending_up_bars(), "BTC/USD")
    decision = decide_crypto_for_symbol(client, "BTC/USD", equity=100_000, macro=macro, available_cash=1000.0)
    assert decision.order_intent is not None
    assert float(decision.order_intent["notional"]) <= 500.0  # available_cash/len(CRYPTO_SYMBOLS)=500


def test_decide_crypto_skips_non_trend_up_regime():
    from signals import decide_crypto_for_symbol
    macro = MacroGate(ok=True, reason="clear", stage="stage2_advancing")
    client = _FakeCryptoDataClient(_flat_bars(), "BTC/USD")
    decision = decide_crypto_for_symbol(client, "BTC/USD", equity=100_000, macro=macro)
    assert decision.order_intent is None
    assert decision.skip_reason == "not_long_regime"  # range 레짐 — 롱/플랫 전용이라 진입 안 함


def test_evaluate_crypto_exit_holds_within_bounds():
    state = CryptoPositionState(entry_date=date(2026, 1, 1), stop_pct=0.05, target_pct=0.10)
    decision = evaluate_crypto_exit({"unrealized_plpc": 0.02}, state, today=date(2026, 1, 3))
    assert decision.should_close is False


def test_evaluate_crypto_exit_closes_at_profit_target():
    state = CryptoPositionState(entry_date=date(2026, 1, 1), stop_pct=0.05, target_pct=0.10)
    decision = evaluate_crypto_exit({"unrealized_plpc": 0.11}, state, today=date(2026, 1, 3))
    assert decision.should_close is True
    assert decision.reason == "profit_target"


def test_evaluate_crypto_exit_closes_at_stop_loss():
    state = CryptoPositionState(entry_date=date(2026, 1, 1), stop_pct=0.05, target_pct=0.10)
    decision = evaluate_crypto_exit({"unrealized_plpc": -0.06}, state, today=date(2026, 1, 3))
    assert decision.should_close is True
    assert decision.reason == "stop_loss"


def test_evaluate_crypto_exit_forces_close_after_max_hold_days_regardless_of_pnl():
    state = CryptoPositionState(entry_date=date(2026, 1, 1), stop_pct=0.05, target_pct=0.10)
    decision = evaluate_crypto_exit({"unrealized_plpc": 0.01}, state, today=date(2026, 1, 11))  # 10일 경과
    assert decision.should_close is True
    assert decision.reason == "max_hold_days"


def test_crypto_position_state_roundtrip():
    state = CryptoPositionState(entry_date=date(2026, 3, 5), stop_pct=0.048, target_pct=0.096)
    restored = CryptoPositionState.from_dict(state.to_dict())
    assert restored == state


def test_build_crypto_order_intent_uses_notional_market_buy():
    intent = build_crypto_order_intent("ETH/USD", 4321.5, "cid-1")
    assert intent == {
        "symbol": "ETH/USD", "side": "buy", "notional": "4321.50",
        "type": "market", "time_in_force": "gtc", "client_order_id": "cid-1",
    }


def test_build_crypto_close_intent_sells_full_qty():
    intent = build_crypto_close_intent("BTC/USD", "0.0512", "cid-close")
    assert intent["side"] == "sell"
    assert intent["qty"] == "0.0512"
    assert intent["symbol"] == "BTC/USD"
