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


def test_reconcile_reconstructs_missing_state_for_open_broker_position(monkeypatch, tmp_path):
    """The exact 2026-08-28 incident: broker has an open position, local state
    doesn't. The reconciler must fill it in (not just alert) so the next
    mcp_runner cycle can evaluate_crypto_exit instead of holding forever.

    2026-09-02 라운드6 감사 발견: 이 테스트가 CRYPTO_POSITIONS_PATH를
    monkeypatch 안 해서, changed=True가 되는 순간 _save_crypto_positions가
    **진짜 라이브 registry/crypto_positions.json**을 이 테스트의 가짜 값으로
    덮어쓰고 있었다 — 오늘 밤 이 파일을 대상으로 pytest를 반복 실행하는 동안
    실제로 여러 차례 ETH/USD 상태가 통째로 사라지는 사고가 났다(그중 최소
    한 번은 원인불명으로 남겨뒀던 21:39 미스터리 write가 바로 이거였을
    가능성이 높다). tmp_path로 격리."""
    monkeypatch.setattr(health_check, "CRYPTO_POSITIONS_PATH", tmp_path / "crypto_positions.json")
    monkeypatch.setattr(health_check, "fetch_and_classify_crypto_regime", lambda client, symbol: _fake_signal(atr=2.0, close=100.0))
    monkeypatch.setattr(health_check, "_notify", lambda title, message: None)  # 실제 macOS 알림 팝업 방지

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


def test_reconcile_leaves_already_tracked_symbols_untouched(monkeypatch, tmp_path):
    # 이 테스트는 changed=False라 저장 자체가 안 되지만, 방어적으로 여기도
    # 격리한다(§위 라운드6 감사 발견 — 앞으로 이 함수가 바뀌어도 실제
    # registry를 건드릴 길이 아예 없게).
    monkeypatch.setattr(health_check, "CRYPTO_POSITIONS_PATH", tmp_path / "crypto_positions.json")
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


def test_reconcile_alerts_but_does_not_crash_when_reconstruction_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(health_check, "CRYPTO_POSITIONS_PATH", tmp_path / "crypto_positions.json")  # 방어적 격리, 위와 동일 사유
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
