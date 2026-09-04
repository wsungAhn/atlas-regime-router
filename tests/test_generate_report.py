import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import generate_report  # noqa: E402
from datetime import date  # noqa: E402


def test_record_equity_history_dedupes_by_date(monkeypatch, tmp_path):
    path = tmp_path / "equity_history.jsonl"
    monkeypatch.setattr(generate_report, "EQUITY_HISTORY_PATH", path)

    generate_report._record_equity_history(date(2026, 9, 3), 95000.0)
    generate_report._record_equity_history(date(2026, 9, 4), 96000.0)
    generate_report._record_equity_history(date(2026, 9, 3), 95500.0)  # 같은 날 재실행 — 덮어써야 함

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows == [
        {"date": "2026-09-03", "equity": 95500.0},
        {"date": "2026-09-04", "equity": 96000.0},
    ]
