"""브로커 키 없이 도는 순수함수 유닛테스트 — 규칙 로직만 검증."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from signals import (  # noqa: E402
    MacroGate,
    build_close_intent,
    build_credit_spread_intent,
    build_iron_condor_intent,
    classify_regime_from_bars,
    contracts_for_max_loss,
    decide_for_symbol,
    evaluate_exit,
    risk_pct_for_atr_pct,
)
from datetime import date, timedelta  # noqa: E402


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


def test_classify_regime_low_adx_but_recent_spike_is_not_range():
    """전략5(백테스트 1위) 조건 검증 — ADX만 낮다고 range가 아니라, RSI가 극단이거나
    가격이 최근 VWAP에서 크게 벗어나 있으면 range로 판정하면 안 된다(구버전 버그:
    ADX 단독 조건이라 이런 케이스도 range로 오판했었음)."""
    n = 80
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    # 오래 횡보하다 마지막 5일 급등 — ADX는 아직 안 올라왔지만 RSI는 과매수,
    # 가격도 최근 VWAP보다 훨씬 위
    close = pd.Series([100 + (i % 3) * 0.1 for i in range(n - 5)] + [101, 103, 106, 110, 115], index=idx, dtype=float)
    df = pd.DataFrame({"high": close + 0.3, "low": close - 0.3, "close": close}, index=idx)
    signal = classify_regime_from_bars(df, "TEST")
    assert signal.regime != "range"


def test_risk_pct_scales_down_with_high_volatility():
    low_vol = risk_pct_for_atr_pct(atr_pct=0.01, atr_pct_low_q=0.01, atr_pct_high_q=0.05)
    high_vol = risk_pct_for_atr_pct(atr_pct=0.05, atr_pct_low_q=0.01, atr_pct_high_q=0.05)
    assert low_vol > high_vol
    assert 0.02 <= high_vol <= 0.05
    assert 0.02 <= low_vol <= 0.05


def test_risk_pct_clips_to_bounds():
    assert risk_pct_for_atr_pct(0.10, 0.01, 0.05) == pytest.approx(0.02)
    assert risk_pct_for_atr_pct(-1.0, 0.01, 0.05) == pytest.approx(0.05)


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


class _FakeSnapshot:
    def __init__(self, delta):
        self.greeks = _FakeGreeks(delta)


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
            "SPY_P_SHORT": _FakeSnapshot(delta=-0.15),
            "SPY_P_LONG": _FakeSnapshot(delta=-0.06),
        },
    }
    option_client = _FakeOptionDataClient(chains)
    decision = decide_for_symbol(stock_client, option_client, "SPY", equity=100_000, macro=macro)
    assert decision.regime == "trend_up"
    assert decision.order_intent is not None
    assert decision.order_intent["order_class"] == "mleg"
    assert len(decision.order_intent["legs"]) == 2


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
    """DTE가 FORCE_CLOSE_DTE(14) 이하면 손익이 좋아도 강제청산."""
    legs = [{"symbol": _far_expiry_symbol(days_out=5), "cost_basis": -100.0, "unrealized_pl": 5.0, "side": "short", "qty": "1"}]
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
