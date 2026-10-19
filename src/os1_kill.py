"""OS-1 account-level kill switch helpers."""


KILL_DRAWDOWN = -0.05
FLATTEN_DRAWDOWN = -0.10


def kill_action(E0, equity_mtm, enabled=False):
    """Return the account action for the drawdown from the fixed starting equity."""
    if E0 is None or E0 != E0 or E0 <= 0:
        raise ValueError("E0 must be a positive, non-NaN value")
    if equity_mtm is None or equity_mtm != equity_mtm:
        raise ValueError("equity_mtm must be non-NaN")

    if not enabled:
        return "none"

    dd = (equity_mtm - E0) / E0
    if dd <= FLATTEN_DRAWDOWN:
        return "flatten"
    if dd <= KILL_DRAWDOWN:
        return "halt_entries"
    return "none"


def restore_close_only(flag_path):
    """Create an empty close-only flag, without changing an existing flag."""
    try:
        with open(flag_path, "x"):
            pass
    except FileExistsError:
        return False
    return True
