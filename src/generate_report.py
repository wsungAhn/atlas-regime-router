"""
장 마감 후 그날의 결과를 대회 제출용으로 요약하는 리포트 생성기.

mcp_runner.py가 하루 종일 쌓은 registry/decisions.jsonl(사이클마다 결정 1줄씩)과
Alpaca 계좌 상태(TradingClient, read-only)를 합쳐 reports/YYYY-MM-DD.md로 출력한다.
launchd로 장마감 직후(13:10 PT) 자동 실행 — config/launchd/com.atlas.report-generator.plist.

# ponytail: 재시도 없음(launchd가 실패해도 다음날까지 안 기다림 — 필요하면 수동 재실행:
# .venv/bin/python src/generate_report.py [YYYY-MM-DD])
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient

REPO_ROOT = Path(__file__).resolve().parents[1]
DECISION_LOG = REPO_ROOT / "registry" / "decisions.jsonl"
REPORTS_DIR = REPO_ROOT / "reports"
ENV_FILE = REPO_ROOT / ".env.competition"
MARKET_TZ = ZoneInfo("America/New_York")  # 거래일 경계는 항상 미 동부시간 기준
# (PT 늦은밤엔 UTC 날짜가 이미 다음날로 넘어가 있어서 UTC 기준으로 자르면
# 그날 리포트에 오늘 거래가 빠지거나 다음날로 새는 버그가 생긴다)


def _load_env_file(path: Path) -> None:
    """launchd는 셸 프로필을 안 거쳐서 .env.competition을 자동으로 안 읽는다 —
    mcp_runner.py는 alpaca-mcp-server가 --env-file로 자체 로드하지만, 이
    스크립트는 alpaca-py TradingClient를 직접 쓰므로 os.environ에 직접
    채워야 한다. **크레덴셜을 plist에 박아넣지 않기 위한 조치** — 처음에
    실수로 plist EnvironmentVariables에 실제 API 키를 평문으로 넣었다가
    바로 고쳤다(.env.competition만 gitignore 대상이고 config/launchd/는
    git 추적 대상이라 그러면 안 됨)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file(ENV_FILE)


def _load_decisions_for_date(target_date: date) -> list[dict]:
    if not DECISION_LOG.exists():
        return []
    out = []
    with open(DECISION_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = record.get("ts")
            if not ts:
                continue
            try:
                record_date = datetime.fromisoformat(ts).astimezone(MARKET_TZ).date()
            except ValueError:
                continue
            if record_date == target_date:
                out.append(record)
    return out


_FILLED_STATUSES = {"filled", "partially_filled"}


def _actually_filled(client: TradingClient, record: dict) -> bool:
    """submitted=true를 곧이곧대로 믿지 않는다 — **2026-08-25 실측**: MCP 도구
    호출은 브로커 거부에도 예외를 안 던져서 (지금은 고쳤지만) 그날 아침 세 번
    submitted=true로 기록된 SPY 주문이 실제로는 전부 거부/미체결이었다. 그
    기록들이 이미 registry/decisions.jsonl에 남아있으므로, 리포트는 로그의
    submitted 플래그가 아니라 브로커의 실제 주문상태를 재조회해서 판정한다."""
    client_order_id = (record.get("order_intent") or {}).get("client_order_id")
    if not client_order_id:
        return False
    try:
        order = client.get_order_by_client_id(client_order_id)
    except Exception:
        return False
    return str(order.status.value if hasattr(order.status, "value") else order.status).lower() in _FILLED_STATUSES


def build_report(target_date: date) -> str:
    decisions = _load_decisions_for_date(target_date)
    client = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)

    claimed_submitted = [d for d in decisions if d.get("submitted")]
    submitted = [d for d in claimed_submitted if _actually_filled(client, d)]
    stale_false_positives = len(claimed_submitted) - len(submitted)
    entries = [d for d in submitted if d.get("action") != "close"]
    closes = [d for d in submitted if d.get("action") == "close"]
    skips = [d for d in decisions if not d.get("submitted")] + [d for d in claimed_submitted if d not in submitted]
    skip_reasons: dict[str, int] = {}
    for d in skips:
        reason = d.get("skip_reason", "unknown") if d.get("skip_reason") else "logged_submitted_but_not_actually_filled"
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    account = client.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity)
    day_pnl = equity - last_equity
    day_pnl_pct = (day_pnl / last_equity * 100) if last_equity else 0.0
    positions = client.get_all_positions()

    lines = [
        f"# Atlas Options — Daily Report ({target_date.isoformat()})",
        "",
        f"- 계좌 잔고: ${equity:,.2f} (전일 대비 {day_pnl:+,.2f}, {day_pnl_pct:+.2f}%)",
        f"- 오늘 신규 진입: {len(entries)}건 / 청산: {len(closes)}건 / 스킵: {len(skips)}건",
        f"- 현재 열린 포지션: {len(positions)}개",
    ]
    if stale_false_positives:
        lines.append(f"- ⚠️ 로그엔 submitted=true였지만 실제 체결 재조회 결과 아니었던 기록 {stale_false_positives}건 — 위 집계에서 제외됨")
    lines += ["", "## 신규 진입"]
    if entries:
        for d in entries:
            lines.append(f"- {d.get('symbol')} regime={d.get('regime')} macro={d.get('macro_reason')}")
    else:
        lines.append("- 없음")

    lines += ["", "## 청산"]
    if closes:
        for d in closes:
            lines.append(
                f"- {d.get('symbol')} reason={d.get('exit_reason')} pnl_pct={d.get('profit_pct', 0)*100:.1f}%"
            )
    else:
        lines.append("- 없음")

    lines += ["", "## 스킵 사유 분포"]
    if skip_reasons:
        for reason, count in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {reason}: {count}건")
    else:
        lines.append("- 없음")

    risk_gate_events = [d for d in skips if str(d.get("skip_reason", "")).startswith("risk_gate:")]
    if risk_gate_events:
        lines += ["", "## 리스크게이트 발동", f"- {len(risk_gate_events)}개 사이클에서 신규진입 억제됨"]

    lines += ["", "## 현재 포지션"]
    if positions:
        for p in positions:
            lines.append(f"- {p.symbol} qty={p.qty} unrealized_pl={p.unrealized_pl}")
    else:
        lines.append("- 없음")

    return "\n".join(lines) + "\n"


def main() -> None:
    target_date = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(MARKET_TZ).date()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report(target_date)
    out_path = REPORTS_DIR / f"{target_date.isoformat()}.md"
    out_path.write_text(report)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
