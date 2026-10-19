from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from os1_kill import kill_action, restore_close_only


def test_kill_action_49_boundary_literals():
    assert kill_action(50000, 47505, enabled=True) == "none"
    assert kill_action(50000, 47500, enabled=True) == "halt_entries"
    assert kill_action(50000, 45005, enabled=True) == "halt_entries"
    assert kill_action(50000, 45000, enabled=True) == "flatten"


def test_kill_action_is_disabled_by_default():
    assert kill_action(50000, 10000) == "none"
    assert kill_action(50000, 10000, enabled=False) == "none"


def test_kill_action_uses_fixed_starting_equity_not_hwm():
    assert kill_action(50000, 60000, enabled=True) == "none"
    assert kill_action(50000, 55000, enabled=True) == "none"


def test_kill_action_uses_supplied_starting_equity_literals():
    assert kill_action(100000, 95005, enabled=True) == "none"
    assert kill_action(100000, 95000, enabled=True) == "halt_entries"
    assert kill_action(100000, 90000, enabled=True) == "flatten"
    assert kill_action(20000, 18000, enabled=True) == "flatten"
    assert kill_action(20000, 19001, enabled=True) == "none"


@pytest.mark.parametrize("bad_e0", [None, 0, -1, float("nan")])
def test_kill_action_rejects_bad_starting_equity(bad_e0):
    with pytest.raises(ValueError):
        kill_action(bad_e0, 50000)


@pytest.mark.parametrize("bad_equity", [None, float("nan")])
def test_kill_action_rejects_bad_marked_equity(bad_equity):
    with pytest.raises(ValueError):
        kill_action(50000, bad_equity)


def test_restore_close_only_creates_empty_flag(tmp_path):
    flag_path = tmp_path / "OPTIONS_CLOSE_ONLY"

    assert restore_close_only(flag_path) is True
    assert flag_path.exists()
    assert flag_path.read_bytes() == b""


def test_restore_close_only_preserves_existing_flag(tmp_path):
    flag_path = tmp_path / "OPTIONS_CLOSE_ONLY"
    flag_path.write_text("x")

    assert restore_close_only(flag_path) is False
    assert flag_path.read_text() == "x"


def test_injected_equity_path_restores_close_only_at_halt(tmp_path):
    flag_path = tmp_path / "OPTIONS_CLOSE_ONLY"

    assert kill_action(50000, 47500, enabled=True) == "halt_entries"
    assert restore_close_only(flag_path) is True
    assert flag_path.exists()
    assert kill_action(50000, 45000, enabled=True) == "flatten"


def test_injected_equity_gap_flattens_and_restores_close_only(tmp_path):
    flag_path = tmp_path / "OPTIONS_CLOSE_ONLY"

    assert kill_action(50000, 47550, enabled=True) == "none"
    assert kill_action(50000, 44500, enabled=True) == "flatten"
    assert restore_close_only(flag_path) is True
    assert flag_path.exists()


def test_kill_module_has_no_flag_deletion_api():
    source = Path(__file__).parents[1].joinpath("src", "os1_kill.py").read_text()
    for forbidden in ("remove", "unlink", "rmdir", "rmtree"):
        assert forbidden not in source
