"""Pure sizing and quote validation helpers for OS-1."""

from math import floor, isnan


POSITION_RISK_CAP_PCT = 0.02
TOTAL_RISK_CAP_PCT = 0.10
DIRECTION_RISK_CAP_PCT = 0.06
MIN_CREDIT_RATIO = 0.10
ORDER_PRICE_DISCOUNT = 0.05
MIN_NET_CREDIT = 0.01
DEFAULT_MAX_OPEN_POSITIONS = 5


def _widths(width, side):
    if side == "condor":
        if not isinstance(width, (tuple, list)) or len(width) != 2:
            raise ValueError("condor width must contain put and call widths")
        widths = (float(width[0]), float(width[1]))
    else:
        if isinstance(width, (tuple, list)):
            raise ValueError("vertical width must be a number")
        widths = (float(width),)
    if any(w <= 0 for w in widths):
        raise ValueError("width must be positive")
    return widths


def size_os1(
    e0,
    e_now,
    width,
    credit,
    side,
    open_positions,
    max_open_positions=DEFAULT_MAX_OPEN_POSITIONS,
    verified_structure=False,
):
    """Return the OS-1 contract quantity and the decision reason."""
    if side not in {"bull_put", "bear_call", "condor"}:
        raise ValueError("invalid side")
    if e0 is None or e_now is None:
        raise ValueError("equity is required")
    if isinstance(e0, bool) or isinstance(e_now, bool):
        raise ValueError("equity must be numeric")
    if isnan(float(e0)) or float(e0) <= 0 or isnan(float(e_now)) or float(e_now) <= 0:
        raise ValueError("equity must be positive")

    widths = _widths(width, side)
    max_width = max(widths)
    credit = float(credit)
    if credit <= 0 or credit >= max_width:
        return 0, "bad_credit"
    if credit / max_width < MIN_CREDIT_RATIO:
        return 0, "low_credit_ratio"

    if len(open_positions) >= max_open_positions:
        return 0, "count_cap"

    if side == "condor" and verified_structure:
        width_used = max_width
    elif side == "condor":
        width_used = sum(widths)
    else:
        width_used = widths[0]
    l1 = (width_used - credit) * 100

    l_open = sum(float(position["L"]) for position in open_positions)
    bull_open = sum(
        float(position["L"])
        for position in open_positions
        if position["side"] in {"bull_put", "condor"}
    )
    bear_open = sum(
        float(position["L"])
        for position in open_positions
        if position["side"] in {"bear_call", "condor"}
    )
    e_cap = min(float(e0), float(e_now))
    if side == "bull_put":
        direction_remaining = DIRECTION_RISK_CAP_PCT * e_cap - bull_open
    elif side == "bear_call":
        direction_remaining = DIRECTION_RISK_CAP_PCT * e_cap - bear_open
    else:
        direction_remaining = min(
            DIRECTION_RISK_CAP_PCT * e_cap - bull_open,
            DIRECTION_RISK_CAP_PCT * e_cap - bear_open,
        )

    available = min(
        POSITION_RISK_CAP_PCT * e_cap,
        TOTAL_RISK_CAP_PCT * e_cap - l_open,
        direction_remaining,
    )
    qty = floor(available / l1)
    if qty < 1:
        return 0, "risk_cap"
    return qty, "ok"


def check_quote_credit(raw_net_credit, leg_quotes):
    """Validate quote completeness and the discounted net credit floor."""
    if any(bid <= 0 or ask <= 0 for bid, ask in leg_quotes):
        return "bad_credit"
    if raw_net_credit * (1 - ORDER_PRICE_DISCOUNT) <= MIN_NET_CREDIT:
        return "bad_credit"
    return None
