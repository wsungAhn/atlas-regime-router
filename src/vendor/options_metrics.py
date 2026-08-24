"""Loss-focused metrics for this research track, kept separate from live risk gating."""

from __future__ import annotations

from typing import Iterable


def _realized_pnls(trades: Iterable[dict]) -> list[float]:
    return [float(trade["realized_pnl"]) for trade in trades]


def max_single_trade_loss(trades: list[dict]) -> float:
    """Return the absolute value of the worst realized loss, or 0.0 if there is no loss."""

    realized_pnls = _realized_pnls(trades)
    worst_loss = min((pnl for pnl in realized_pnls if pnl < 0.0), default=0.0)
    return abs(worst_loss)


def consecutive_loss_mdd(trades: list[dict]) -> float:
    """Return the maximum cumulative loss across consecutive losing trades as a positive value."""

    worst_run_loss = 0.0
    current_run_loss = 0.0

    for pnl in _realized_pnls(trades):
        if pnl < 0.0:
            current_run_loss += pnl
            worst_run_loss = max(worst_run_loss, -current_run_loss)
        else:
            current_run_loss = 0.0

    return worst_run_loss


def cvar_95(trade_returns: list[float]) -> float:
    """Return the average of the worst 5% trade returns; raise if the tail would be empty."""

    if not trade_returns:
        raise ValueError("trade_returns must contain at least one value")

    tail_count = int(len(trade_returns) * 0.05)
    if tail_count < 1:
        raise ValueError("trade_returns sample is too small for a 95% CVaR estimate")

    tail = sorted(float(value) for value in trade_returns)[:tail_count]
    return sum(tail) / tail_count


def calmar_ratio(annualized_return: float, mdd: float) -> float:
    """Return annualized_return / mdd; mdd must be positive."""

    if mdd <= 0.0:
        raise ValueError("mdd must be positive")
    return annualized_return / mdd
