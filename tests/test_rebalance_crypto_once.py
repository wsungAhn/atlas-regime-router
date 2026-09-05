import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rebalance_crypto_once import _sell_qty_for_notional  # noqa: E402


def test_sell_qty_rounds_down_to_qty_increment():
    qty = _sell_qty_for_notional(current_qty=0.5, current_price=80_000.0, sell_notional=8_000.0)
    assert qty == 0.1


def test_sell_qty_never_negative():
    assert _sell_qty_for_notional(current_qty=0.5, current_price=80_000.0, sell_notional=-100.0) == 0.0
