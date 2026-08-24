"""portfolio.py 유닛테스트 — 사이징 버그(0계약 거래가 손실로 잡히던 것) 회귀 방지."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio import scale_trades_to_dollars  # noqa: E402


def _trade(entry, exit_, realized_pnl, max_loss, weight=1.0):
    return {
        "entry_date": pd.Timestamp(entry), "exit_date": pd.Timestamp(exit_),
        "realized_pnl": realized_pnl, "max_loss": max_loss, "weight": weight,
    }


def test_zero_contract_trades_are_excluded_not_counted_as_losses():
    """예산이 max_loss*100보다 작으면 계약수가 0 — 이 거래는 '일어나지 않은 것'이라
    반환 리스트에서 빠져야 한다(과거 버그: realized_pnl이 양수였는데도 dollar_pnl=0이라
    win_rate 계산에서 패배로 잡혔었음)."""
    trades = [
        _trade("2026-01-01", "2026-01-30", realized_pnl=5.0, max_loss=1000.0, weight=0.2),  # 너무 큼 → 0계약
        _trade("2026-02-01", "2026-03-01", realized_pnl=3.0, max_loss=1.0, weight=1.0),  # 작음 → 여러 계약
    ]
    dollar_trades, equity = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    assert len(dollar_trades) == 1
    assert dollar_trades[0].contracts > 0
    assert dollar_trades[0].dollar_pnl > 0


def test_ladder_weight_scales_position_size():
    """같은 max_loss라도 weight가 크면(레벨3=0.5) 더 많은 계약이 들어가야 한다."""
    trades = [
        _trade("2026-01-01", "2026-01-30", realized_pnl=1.0, max_loss=5.0, weight=0.2),
        _trade("2026-01-01", "2026-01-30", realized_pnl=1.0, max_loss=5.0, weight=0.5),
    ]
    dollar_trades, _ = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    assert len(dollar_trades) == 2
    low_weight, high_weight = dollar_trades
    assert high_weight.contracts > low_weight.contracts


def test_equity_compounds_across_sequential_trades():
    trades = [
        _trade("2026-01-01", "2026-01-15", realized_pnl=10.0, max_loss=1.0),
        _trade("2026-02-01", "2026-02-15", realized_pnl=-5.0, max_loss=1.0),
    ]
    dollar_trades, equity = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    assert len(dollar_trades) == 2
    assert equity.iloc[-1] != 100_000
