from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from os1_sizing import check_quote_credit, size_os1


def test_position_total_and_direction_caps_use_literal_quantities():
    assert size_os1(50000, 50000, 1.5, 0.30, "bull_put", []) == (8, "ok")
    assert size_os1(50000, 50000, 1.5, 0.30, "bull_put", [{"L": 4800, "side": "bear_call"}]) == (1, "ok")
    assert size_os1(50000, 50000, 1.5, 0.30, "bull_put", [{"L": 2800, "side": "bull_put"}]) == (1, "ok")


def test_condor_verified_and_conservative_widths_and_both_directions():
    assert size_os1(50000, 50000, (1.0, 2.0), 0.50, "condor", [], verified_structure=True) == (6, "ok")
    assert size_os1(50000, 50000, (1.0, 2.0), 0.50, "condor", []) == (4, "ok")
    assert size_os1(
        50000,
        50000,
        (1.0, 2.0),
        0.50,
        "condor",
        [{"L": 2500, "side": "bull_put"}],
        verified_structure=True,
    ) == (3, "ok")
    assert size_os1(
        50000,
        50000,
        (1.0, 2.0),
        0.50,
        "condor",
        [{"L": 2500, "side": "bear_call"}],
        verified_structure=True,
    ) == (3, "ok")
    assert size_os1(
        50000,
        50000,
        1.5,
        0.30,
        "bull_put",
        [{"L": 2800, "side": "condor"}],
        verified_structure=True,
    ) == (1, "ok")


def test_bad_credit_and_quote_credit():
    assert size_os1(50000, 50000, 1.5, 1.5, "bull_put", []) == (0, "bad_credit")
    assert size_os1(50000, 50000, 1.5, 0, "bull_put", []) == (0, "bad_credit")
    assert size_os1(50000, 50000, 1.5, -0.05, "bull_put", []) == (0, "bad_credit")
    assert check_quote_credit(0.20, [(0, 0.30)]) == "bad_credit"
    assert check_quote_credit(0.20, [(0.10, 0)]) == "bad_credit"
    assert check_quote_credit(0.0105, [(0.01, 0.02)]) == "bad_credit"
    assert check_quote_credit(0.010526315789473685, [(0.01, 0.02)]) == "bad_credit"
    assert check_quote_credit(0.02, [(0.01, 0.02)]) is None


def test_credit_ratio_boundary_is_inclusive():
    assert size_os1(50000, 50000, 1.0, 0.10, "bull_put", []) == (11, "ok")
    assert size_os1(50000, 50000, 1.0, 0.099999999, "bull_put", []) == (0, "low_credit_ratio")


def test_count_cap_and_configured_count():
    five = [{"L": 1, "side": "bull_put"}] * 5
    four = [{"L": 1, "side": "bull_put"}] * 4
    assert size_os1(50000, 50000, 1.5, 0.30, "bull_put", five) == (0, "count_cap")
    assert size_os1(50000, 50000, 1.5, 0.30, "bull_put", four) == (8, "ok")
    assert size_os1(50000, 50000, 1.5, 0.30, "bull_put", five, max_open_positions=6) == (8, "ok")


def test_e_cap_uses_lower_equity():
    assert size_os1(50000, 47500, 1.5, 0.30, "bull_put", []) == (7, "ok")
    assert size_os1(50000, 52000, 1.5, 0.30, "bull_put", []) == (8, "ok")


def test_reserved_risk_and_negative_remaining_are_enforced():
    assert size_os1(
        50000,
        50000,
        1.5,
        0.30,
        "bull_put",
        [{"L": 4000, "side": "bear_call"}, {"L": 1000, "side": "bull_put"}],
    ) == (0, "risk_cap")
    assert size_os1(50000, 50000, 1.5, 0.30, "bull_put", [{"L": 6000, "side": "bull_put"}]) == (0, "risk_cap")


def test_invalid_inputs_and_rejection_order():
    for e0, e_now in ((None, 50000), (0, 50000), (-1, 50000), (50000, 0)):
        with pytest.raises(ValueError):
            size_os1(e0, e_now, 1.5, 0.30, "bull_put", [])
    with pytest.raises(ValueError):
        size_os1(50000, 50000, 1.5, 0.30, "invalid", [])
    assert size_os1(50000, 50000, 1.5, 1.5, "bull_put", [{"L": 1, "side": "bull_put"}] * 5) == (0, "bad_credit")


def test_bear_call_direction_cap_includes_existing_condor():
    assert size_os1(
        50000,
        50000,
        1.5,
        0.30,
        "bear_call",
        [{"L": 2800, "side": "condor"}],
    ) == (1, "ok")


def test_bear_call_direction_cap_uses_six_percent_limit():
    assert size_os1(
        50000,
        50000,
        1.5,
        0.30,
        "bear_call",
        [{"L": 2500, "side": "bear_call"}],
    ) == (4, "ok")


def test_zero_width_is_invalid_for_vertical_and_condor():
    with pytest.raises(ValueError):
        size_os1(50000, 50000, 0, 0.01, "bull_put", [])
    with pytest.raises(ValueError):
        size_os1(50000, 50000, (0, 1.0), 0.01, "condor", [])
