"""
Atlas Options Hackathon — 독립 헬스체크 + 크립토 상태 리컨실러.

mcp_runner.py의 트레이딩 사이클과 별개로 15분마다 launchd(com.atlas.health-check)로
돈다. 왜 별도 프로세스인가: mcp_runner.py 자신이 만드는 이상 상태(상태파일 유실,
계좌 손상, 방치 주문)를 mcp_runner.py 자신이 알아챌 방법이 없다 — 관찰자가 관찰
대상과 같은 프로세스면 관찰자도 같이 죽거나 같이 조용해진다.

**2026-08-28 실증 사고**: 크립토 사이클(mcp_runner.py:518-522)은 브로커에 열린
포지션이 있는데 registry/crypto_positions.json에 그 심볼의 저장된 stop_pct/
target_pct가 없으면 "사람이 볼 때까지" fail-closed로 청산판정 자체를 건너뛰고
로그 한 줄만 남긴다. 이 시스템엔 알림 채널이 전혀 없었으므로(텔레그램/슬랙 등
grep 0건), 그 로그를 아무도 안 보면 손실이 무한정 방치된다 — 사용자가 실제로
"손해가 늘어나는데 그냥 보유한다"고 보고해서 발견.

이 파일의 책임 두 가지, 명확히 분리:
1. **감시(watch)**: 크립토/옵션 양쪽의 브로커-로컬 상태 불일치, 손상파일,
   방치 미체결주문, 리스크게이트 fail-closed 지속을 확인하고 macOS 네이티브
   알림 + logs/health_alerts.log로 남긴다. 새 봇/새 서비스 도입 없음.
2. **리컨실(reconcile)**: crypto_positions.json 전용. 진짜 원인(엔트리 시점
   ATR)은 복구 불가능하므로, "오늘" 시점 ATR로 근사 재구성해 즉시 써넣는다 —
   완벽한 복구가 아니라 "무한 방치보다 근사 보호가 낫다"는 판단. 재구성
   사실은 알림으로 크게 남겨 사람이 검토할 수 있게 한다. 옵션은 로컬 상태파일
   자체가 없으므로(evaluate_exit이 브로커 cost_basis/unrealized_pl만 씀)
   리컨실 대상이 아니다 — 이 갭은 크립토 구조 고유의 것.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from mcp_runner import (  # noqa: E402
    CRYPTO_POSITIONS_PATH,
    ENV_FILE,
    RISK_GATE_STATE_PATH,
    _load_env_file,
)
from signals import (  # noqa: E402
    CRYPTO_R_MULTIPLE,
    CRYPTO_STOP_ATR_MULT,
    CRYPTO_SYMBOLS,
    CryptoPositionState,
    fetch_and_classify_crypto_regime,
)
from alpaca.data.historical.crypto import CryptoHistoricalDataClient  # noqa: E402
from alpaca.trading.client import TradingClient  # noqa: E402

_load_env_file(ENV_FILE)

import os  # noqa: E402

ALERT_LOG = REPO_ROOT / "logs" / "health_alerts.log"
STALE_ORDER_ALERT_MINUTES = 30  # mcp_runner._cancel_stale_orders와 같은 문턱 —
# 그쪽이 이미 취소하지만, 취소 자체가 반복 실패하는 경우를 여기서 잡는다.

logger = logging.getLogger("health_check")


def _setup_logging() -> None:
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = RotatingFileHandler(ALERT_LOG, maxBytes=2_000_000, backupCount=2)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)


def _notify(title: str, message: str) -> None:
    """macOS 네이티브 알림. 새 봇/새 서비스 없음 — 이 Mac에 사람이 앉아있을 때만
    유효하다는 한계는 있지만(§다음 레이어: 원격 알림), 지금 당장 0비용으로 되는
    선에서는 이게 맞다."""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            check=False, timeout=5,
        )
    except Exception:
        logger.exception("[ERROR] macOS notification failed (alert still logged)")


def _alert(reason: str, detail: str) -> None:
    logger.error("[ALERT] reason=%s detail=%s", reason, detail)
    _notify("Atlas Health Check", f"{reason}: {detail}")


def _load_crypto_positions() -> dict[str, CryptoPositionState]:
    if not CRYPTO_POSITIONS_PATH.exists():
        return {}
    try:
        raw = json.loads(CRYPTO_POSITIONS_PATH.read_text())
        return {symbol: CryptoPositionState.from_dict(d) for symbol, d in raw.items()}
    except (json.JSONDecodeError, KeyError, ValueError):
        return {}


def _save_crypto_positions(positions: dict[str, CryptoPositionState]) -> None:
    """mcp_runner._save_crypto_positions와 동일한 원자적 치환 패턴(임시파일 쓰고
    os.replace) — 리컨실 도중 죽어도 절반만 쓰인 손상 파일을 안 남긴다."""
    tmp_path = CRYPTO_POSITIONS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps({s: p.to_dict() for s, p in positions.items()}))
    os.replace(tmp_path, CRYPTO_POSITIONS_PATH)


def reconcile_crypto_positions(
    broker_symbols: set[str],
    stored: dict[str, CryptoPositionState],
    crypto_client: CryptoHistoricalDataClient,
    today: date | None = None,
) -> dict[str, CryptoPositionState]:
    """브로커엔 열려 있는데 stored엔 없는 심볼마다, 오늘 ATR로 근사 stop_pct/
    target_pct를 재구성해 써넣는다. 진짜 진입시점 ATR은 영원히 복구 불가 —
    이건 "정답 복원"이 아니라 "무방비 방치를 근사 보호로 바꾸는" 처치이므로
    반드시 크게 알림을 남긴다. 순수 계산은 fetch_and_classify_crypto_regime
    (실거래 진입 로직과 동일 함수) 재사용, 새 공식 없음."""
    today = today or datetime.now(timezone.utc).date()
    repaired = dict(stored)
    changed = False
    for symbol in sorted(broker_symbols - set(stored)):
        try:
            signal = fetch_and_classify_crypto_regime(crypto_client, symbol)
            if signal.atr <= 0 or signal.close <= 0:
                raise ValueError(f"invalid current atr/price for {symbol}")
            stop_pct = (CRYPTO_STOP_ATR_MULT * signal.atr) / signal.close
            target_pct = CRYPTO_R_MULTIPLE * stop_pct
        except Exception as exc:
            _alert(
                "crypto_state_missing_reconcile_failed",
                f"{symbol}: broker has an open position with no local state and "
                f"reconstruction failed ({exc}) — position remains unprotected, "
                f"needs manual review",
            )
            continue
        repaired[symbol] = CryptoPositionState(entry_date=today, stop_pct=stop_pct, target_pct=target_pct)
        changed = True
        _alert(
            "crypto_state_reconciled",
            f"{symbol}: had no local state despite an open broker position "
            f"(same failure class as the 2026-08-28 incident) — reconstructed "
            f"stop_pct={stop_pct:.4f} target_pct={target_pct:.4f} from today's ATR "
            f"(approximate; true entry-time ATR is unrecoverable) and wrote it back "
            f"so the next crypto cycle can evaluate exit instead of holding indefinitely",
        )
    if changed:
        _save_crypto_positions(repaired)
    return repaired


def check_risk_gate_state() -> None:
    if not RISK_GATE_STATE_PATH.exists():
        return
    try:
        json.loads(RISK_GATE_STATE_PATH.read_text())
    except (json.JSONDecodeError, ValueError):
        _alert(
            "risk_gate_state_corrupt",
            "registry/risk_gate_state.json failed to parse — both runners are "
            "fail-closed (all new entries blocked) until this is fixed manually",
        )


def check_stale_orders(trading_client: TradingClient, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    orders = trading_client.get_orders()
    for o in orders:
        submitted_at = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
        if not submitted_at:
            continue
        age_minutes = (now - submitted_at).total_seconds() / 60
        if age_minutes >= STALE_ORDER_ALERT_MINUTES:
            _alert(
                "stale_order_not_cleared",
                f"order {o.id} ({o.symbol}) has been open {age_minutes:.0f}min — "
                f"mcp_runner's own {STALE_ORDER_ALERT_MINUTES}min auto-cancel should "
                f"have caught this by now; its cancel attempt may be failing repeatedly",
            )


def check_crypto_reconciliation(trading_client: TradingClient, crypto_client: CryptoHistoricalDataClient) -> None:
    positions = trading_client.get_all_positions()
    broker_crypto_symbols = {
        symbol
        for symbol in CRYPTO_SYMBOLS
        if symbol.replace("/", "") in {p.symbol for p in positions}
    }
    stored = _load_crypto_positions()
    missing = broker_crypto_symbols - set(stored)
    if not missing:
        return
    reconcile_crypto_positions(broker_crypto_symbols, stored, crypto_client)


def run_health_check() -> None:
    _setup_logging()
    trading_client = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    crypto_client = CryptoHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])

    check_risk_gate_state()
    check_stale_orders(trading_client)
    check_crypto_reconciliation(trading_client, crypto_client)
    logger.info("[HEALTH_CHECK] cycle complete")


if __name__ == "__main__":
    run_health_check()
