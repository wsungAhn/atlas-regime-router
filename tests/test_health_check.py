import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import health_check  # noqa: E402
from signals import CryptoPositionState  # noqa: E402


def _fake_signal(atr: float, close: float) -> SimpleNamespace:
    return SimpleNamespace(atr=atr, close=close, regime="trend_up")


def test_reconcile_reconstructs_missing_state_for_open_broker_position(monkeypatch):
    """The exact 2026-08-28 incident: broker has an open position, local state
    doesn't. The reconciler must fill it in (not just alert) so the next
    mcp_runner cycle can evaluate_crypto_exit instead of holding forever."""
    monkeypatch.setattr(health_check, "fetch_and_classify_crypto_regime", lambda client, symbol: _fake_signal(atr=2.0, close=100.0))

    repaired = health_check.reconcile_crypto_positions(
        broker_symbols={"BTC/USD"},
        stored={},
        crypto_client=object(),
        today=date(2026, 8, 28),
    )

    assert "BTC/USD" in repaired
    state = repaired["BTC/USD"]
    assert state.entry_date == date(2026, 8, 28)
    assert state.stop_pct == (health_check.CRYPTO_STOP_ATR_MULT * 2.0) / 100.0
    assert state.target_pct == health_check.CRYPTO_R_MULTIPLE * state.stop_pct


def test_reconcile_leaves_already_tracked_symbols_untouched(monkeypatch):
    monkeypatch.setattr(
        health_check, "fetch_and_classify_crypto_regime",
        lambda client, symbol: (_ for _ in ()).throw(AssertionError("should not be called for a symbol that already has stored state")),
    )
    existing = CryptoPositionState(entry_date=date(2026, 8, 26), stop_pct=0.05, target_pct=0.10)

    repaired = health_check.reconcile_crypto_positions(
        broker_symbols={"BTC/USD"},
        stored={"BTC/USD": existing},
        crypto_client=object(),
        today=date(2026, 8, 28),
    )

    assert repaired["BTC/USD"] is existing


def test_reconcile_alerts_but_does_not_crash_when_reconstruction_fails(monkeypatch):
    alerts = []
    monkeypatch.setattr(health_check, "_alert", lambda reason, detail: alerts.append(reason))
    monkeypatch.setattr(health_check, "fetch_and_classify_crypto_regime", lambda client, symbol: _fake_signal(atr=0.0, close=100.0))

    repaired = health_check.reconcile_crypto_positions(
        broker_symbols={"ETH/USD"},
        stored={},
        crypto_client=object(),
        today=date(2026, 8, 28),
    )

    assert "ETH/USD" not in repaired
    assert "crypto_state_missing_reconcile_failed" in alerts


def test_reconcile_writes_to_disk_atomically(monkeypatch, tmp_path):
    positions_path = tmp_path / "crypto_positions.json"
    monkeypatch.setattr(health_check, "CRYPTO_POSITIONS_PATH", positions_path)
    monkeypatch.setattr(health_check, "fetch_and_classify_crypto_regime", lambda client, symbol: _fake_signal(atr=3.0, close=50.0))

    health_check.reconcile_crypto_positions(
        broker_symbols={"BTC/USD"},
        stored={},
        crypto_client=object(),
        today=date(2026, 8, 28),
    )

    on_disk = json.loads(positions_path.read_text())
    assert "BTC/USD" in on_disk
    assert not positions_path.with_suffix(".json.tmp").exists()


def test_check_risk_gate_state_alerts_on_corrupt_json(monkeypatch, tmp_path):
    corrupt_path = tmp_path / "risk_gate_state.json"
    corrupt_path.write_text("{not valid json")
    monkeypatch.setattr(health_check, "RISK_GATE_STATE_PATH", corrupt_path)
    alerts = []
    monkeypatch.setattr(health_check, "_alert", lambda reason, detail: alerts.append(reason))

    health_check.check_risk_gate_state()

    assert alerts == ["risk_gate_state_corrupt"]


def test_check_risk_gate_state_silent_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(health_check, "RISK_GATE_STATE_PATH", tmp_path / "does_not_exist.json")
    alerts = []
    monkeypatch.setattr(health_check, "_alert", lambda reason, detail: alerts.append(reason))

    health_check.check_risk_gate_state()

    assert alerts == []
