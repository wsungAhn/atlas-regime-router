"""
Stateful Multi-Leg Backtest Engine for LEAP, PMCC, and Wheel Strategies.

Implements the audited design from design-wheel-pmcc-leap-strategy:
- §5.2 / §5.2.1: Single SleeveBook ledger (cash, premium_bank, reserved_collateral, available_cash)
- §5.3: Event loop (EXECUTE -> EXPIRY -> TRIGGER -> DECIDE -> MTM)
- §2.1: V1 PMCC Classic (SPY/QQQ, delta 0.80 LEAP + weekly 0.20 short call, dynamic net-cost rule, cash settlement)
- §2.2: V2 Wheel + LEAP Sweep (SLV/TLT wheel -> SPY LEAP sweep, assignment, covered call, stock stop)
- §2.3: V3 Strategy 7 Spread Financing LEAP Ladder (SPY/QQQ/IWM spread harvesting -> SPY/QQQ LEAP sweep)
- §5.5: Adapter for _metrics_from_dollar_trades and 7-day rolling realized P&L reporting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd
from vendor.options_pricing import black_scholes_delta, black_scholes_price, strike_for_delta


def _is_valid_iv(value: float) -> bool:
    """IV is usable only if finite and positive: vendor BS rejects sigma <= 0, NaN silently poisons equity."""
    return math.isfinite(value) and value > 0.0


def round_to_increment(value: float, increment: float = 0.5) -> float:
    """Round strike to standard increment (default $0.50)."""
    return round(value / increment) * increment


def _option_price_for_mtm(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
) -> float:
    """Price helper for MTM paths; vendor BS rejects T<=0, expiry MTM needs intrinsic."""
    if T <= 0.0:
        if option_type == "call":
            return max(0.0, float(S - K))
        if option_type == "put":
            return max(0.0, float(K - S))
        raise ValueError("option_type must be 'call' or 'put'")
    return black_scholes_price(S, K, T, r, sigma, option_type)


def _intrinsic_option_price(S: float, K: float, option_type: str) -> float:
    if option_type == "call":
        return max(0.0, float(S - K))
    if option_type == "put":
        return max(0.0, float(K - S))
    raise ValueError("option_type must be 'call' or 'put'")


def _spread_close_debit_for_mtm(
    spread_type: str,
    short_strike: float,
    long_strike: float,
    S: float,
    T: float,
    r: float,
    iv: float,
) -> float:
    opt_type = "put" if spread_type == "bull_put" else "call"
    short_p = _option_price_for_mtm(S, short_strike, T, r, iv, opt_type)
    long_p = _option_price_for_mtm(S, long_strike, T, r, iv, opt_type)
    return max(0.0, min(abs(short_strike - long_strike), short_p - long_p))


def _spread_intrinsic_debit(
    spread_type: str,
    short_strike: float,
    long_strike: float,
    S: float,
) -> float:
    width = abs(short_strike - long_strike)
    if spread_type == "bull_put":
        return max(0.0, min(width, short_strike - S))
    if spread_type == "bear_call":
        return max(0.0, min(width, S - short_strike))
    raise ValueError("spread_type must be 'bull_put' or 'bear_call'")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Data Models (§5.2)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OptionLeg:
    option_type: str        # "call" | "put"
    strike: float
    expiry: pd.Timestamp
    qty: int                # +N long / -N short (contracts)
    entry_price: float      # Entry price per share ($)
    entry_date: pd.Timestamp
    role: str               # "leap" | "short_call" | "csp" | "covered_call"
    symbol: str = ""        # Underlying symbol
    cumulative_short_credits: float = 0.0  # Realized short credits collected under this LEAP ($/share, V1)
    collateral_reserved: float = 0.0       # Cash collateral reserved ($) for this leg (CSP/spread)


@dataclass
class ActiveSpread:
    symbol: str
    spread_type: str        # "bull_put" | "bear_call"
    short_strike: float
    long_strike: float
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    contracts: int
    credit_received: float  # Per share credit received ($)
    max_loss: float         # Per share max loss ($)
    close_debit: float      # Per share exit debit ($)
    realized_pnl: float     # Per share realized pnl ($)
    exit_reason: str = ""


@dataclass
class PendingOrder:
    order_type: str         # "LEAP_BUY" | "LEAP_CLOSE" | "LEAP_ROLL" | "SHORT_CALL_SELL" | "CSP_SELL" | "CC_SELL" | "STOCK_STOP_CLOSE" | "LEAP_SWEEP_BUY" | "SPREAD_ENTRY"
    symbol: str
    target_delta: float = 0.0
    target_dte: int = 0
    qty: int = 0
    role: str = ""
    is_sweep: bool = False
    leg_idx: int = -1
    raw_trade: Optional[Dict[str, Any]] = None
    estimated_debit: float = 0.0
    reason: str = ""


@dataclass
class SleeveBook:
    """
    Sleeve-level single ledger (§5.2, §5.2.1).
    Cash, premium_bank, and reserved_collateral are shared across all symbols in the sleeve.
    """
    cash: float                                                 # Total sleeve cash (including collateral)
    premium_bank: float = 0.0                                   # Sub-ledger tracking funding sweep profits
    reserved_collateral: float = 0.0                            # Cash locked for CSPs (V2) or spreads (V3)
    pending_debits: float = 0.0                                 # Estimated cash required for queued pending orders
    legs: Dict[str, List[OptionLeg]] = field(default_factory=dict)         # symbol -> list of active OptionLeg
    shares: Dict[str, int] = field(default_factory=dict)                   # symbol -> assigned stock shares (V2)
    share_cost_basis: Dict[str, float] = field(default_factory=dict)       # symbol -> assignment cost basis per share
    active_spreads: List[ActiveSpread] = field(default_factory=list)       # Active spreads (V3)
    realized: List[Dict[str, Any]] = field(default_factory=list)           # Realized event log
    equity_curve: Dict[pd.Timestamp, float] = field(default_factory=dict)  # Daily total sleeve MTM equity

    @property
    def available_cash(self) -> float:
        """Available cash for new commitments (§5.2.1 Principle 4)."""
        return self.cash - self.reserved_collateral - self.pending_debits

    def assert_invariants(
        self,
        equity: float,
        current_prices: Dict[str, float],
        iv_by_symbol: Dict[str, float],
        current_date: pd.Timestamp,
        r: float = 0.045,
        tol: float = 1e-4,
    ) -> None:
        """
        Verify P0 accounting invariants (§5.2.1):
        1. premium_bank <= cash
        2. available_cash >= 0 (within numerical tolerance)
        3. equity == cash + sum(signed_leg_mtm) + sum(shares * close) + sum(active_spread_mtm)
        """
        # Invariant 1: Bank sub-ledger cannot exceed total cash
        if self.premium_bank > self.cash + tol:
            raise AssertionError(
                f"Invariant violation: premium_bank ({self.premium_bank:.4f}) > cash ({self.cash:.4f})"
            )

        # Invariant 2: Available cash cannot be negative
        if self.available_cash < -tol:
            raise AssertionError(
                f"Invariant violation: available_cash ({self.available_cash:.4f}) < 0 "
                f"(cash={self.cash:.4f}, reserved={self.reserved_collateral:.4f}, pending={self.pending_debits:.4f})"
            )

        # Invariant 3: Asset identity
        total_leg_mtm = 0.0
        for sym, leg_list in self.legs.items():
            S = current_prices.get(sym, 0.0)
            iv = iv_by_symbol.get(sym, 0.20)
            for leg in leg_list:
                t_rem = max(0.0, (leg.expiry - current_date).days / 365.0)
                px = (
                    _option_price_for_mtm(S, leg.strike, t_rem, r, iv, leg.option_type)
                    if sym in iv_by_symbol or t_rem <= 0.0
                    else _intrinsic_option_price(S, leg.strike, leg.option_type)
                )
                if leg.qty > 0:
                    total_leg_mtm += px * 100.0 * leg.qty
                else:
                    total_leg_mtm -= px * 100.0 * abs(leg.qty)

        # Active spread MTM (V3): liability is -current_close_debit * 100 * contracts
        spread_mtm = 0.0
        for sp in self.active_spreads:
            S = current_prices.get(sp.symbol, 0.0)
            iv = iv_by_symbol.get(sp.symbol, 0.20)
            t_rem = max(0.0, (sp.exit_date - current_date).days / 365.0)
            if current_date == sp.entry_date:
                close_debit = sp.credit_received
            elif current_date >= sp.exit_date:
                close_debit = sp.close_debit
            elif sp.symbol not in iv_by_symbol:
                close_debit = _spread_intrinsic_debit(
                    sp.spread_type, sp.short_strike, sp.long_strike, S
                )
            else:
                close_debit = _spread_close_debit_for_mtm(
                    sp.spread_type, sp.short_strike, sp.long_strike, S, t_rem, r, iv
                )
            spread_mtm -= close_debit * 100.0 * sp.contracts

        total_stock_value = sum(
            self.shares.get(sym, 0) * current_prices.get(sym, 0.0) for sym in self.shares
        )
        expected_equity = self.cash + total_leg_mtm + spread_mtm + total_stock_value

        if abs(equity - expected_equity) > tol:
            raise AssertionError(
                f"Asset identity violation at {current_date}: equity={equity:.4f}, expected={expected_equity:.4f}, "
                f"diff={equity - expected_equity:.4f} (cash={self.cash:.4f}, legs={total_leg_mtm:.4f}, "
                f"spreads={spread_mtm:.4f}, stock={total_stock_value:.4f})"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Adapter & Result Structs (§5.5)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DollarTrade:
    """Adapter struct compatible with _metrics_from_dollar_trades."""
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    contracts: float
    dollar_pnl: float
    raw_trade: Dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class StrategyResult:
    name: str
    n_trades: int
    total_pnl: float
    win_rate: float
    max_drawdown: float
    sharpe: float
    calmar: float
    profit_factor: float
    final_equity: float


@dataclass
class LeapBacktestReport:
    """Comprehensive backtest report output (§1, §5.5, §7)."""
    variant: str
    metrics: StrategyResult
    equity_series: pd.Series
    dollar_trades: List[DollarTrade]
    realized_events: List[Dict[str, Any]]
    rolling_7d_pnl: pd.Series
    rolling_7d_stats: Dict[str, float]
    premium_bank_history: pd.Series
    assignment_count: int
    called_away_count: int
    stock_stop_count: int
    short_skip_count: int
    leap_roll_count: int
    leap_sweep_count: int


def calculate_metrics_from_dollar_trades(
    dollar_trades: List[DollarTrade],
    equity_series: pd.Series,
    n_years: float,
    starting_equity: float = 30_000.0,
) -> Optional[StrategyResult]:
    """
    Calculate performance metrics matching _metrics_from_dollar_trades signature (§5.5).
    Correctly receives sleeve equity series ($30k basis) to prevent $100k fallback distortion.
    """
    if not dollar_trades:
        return None

    pnls = np.array([dt.dollar_pnl for dt in dollar_trades], dtype=float)
    wins = pnls[pnls > 0]
    total_pnl = float(pnls.sum())
    win_rate = float(len(wins) / len(pnls)) if len(pnls) > 0 else 0.0

    if len(equity_series) > 0:
        equity_curve = equity_series.values
    else:
        equity_curve = np.cumsum(pnls) + starting_equity

    running_max = np.maximum.accumulate(equity_curve)
    drawdowns = running_max - equity_curve
    max_dd = float(drawdowns.max()) if len(drawdowns) > 0 else 0.0

    per_trade_returns = pnls / starting_equity
    if len(pnls) > 1 and per_trade_returns.std() > 0:
        sharpe = float(
            per_trade_returns.mean()
            / per_trade_returns.std()
            * np.sqrt(len(pnls) / max(n_years, 0.1))
        )
    else:
        sharpe = 0.0

    cagr_like = (total_pnl / starting_equity) / max(n_years, 0.1)
    mdd_pct = max_dd / starting_equity
    calmar = (
        float(cagr_like / mdd_pct)
        if mdd_pct > 0
        else (float("inf") if total_pnl > 0 else 0.0)
    )

    gross_profit = float(pnls[pnls > 0].sum()) if len(pnls[pnls > 0]) > 0 else 0.0
    gross_loss = float(-pnls[pnls < 0].sum()) if len(pnls[pnls < 0]) > 0 else 0.0
    profit_factor = (
        float(gross_profit / gross_loss)
        if gross_loss > 0
        else (float("inf") if gross_profit > 0 else 0.0)
    )
    final_equity = float(equity_curve[-1]) if len(equity_curve) > 0 else starting_equity

    return StrategyResult(
        name="",
        n_trades=len(pnls),
        total_pnl=total_pnl,
        win_rate=win_rate,
        max_drawdown=max_dd,
        sharpe=sharpe,
        calmar=calmar,
        profit_factor=profit_factor,
        final_equity=final_equity,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Regime Indicator Helpers (§3)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_adx_ema_regime(
    df: pd.DataFrame,
    ema_fast: int = 20,
    ema_slow: int = 50,
    adx_period: int = 14,
    adx_threshold: float = 20.0,
) -> pd.Series:
    """
    Compute trailing ADX/EMA regime series (§3, strategy 7):
    - "bull": ema20 > ema50 and adx > 20
    - "bear": ema20 < ema50 and adx > 20
    - "neutral": all other conditions
    Uses trailing calculations only; zero lookahead.
    """
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    # Wilder's True Range and ATR
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / adx_period, adjust=False).mean()

    # Directional Movement (+DM, -DM)
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / adx_period, adjust=False).mean() / atr
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / adx_period, adjust=False).mean() / atr

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1.0 / adx_period, adjust=False).mean().fillna(0.0)

    ema20 = close.ewm(span=ema_fast, adjust=False).mean()
    ema50 = close.ewm(span=ema_slow, adjust=False).mean()

    regimes = []
    for i in range(len(df)):
        a = adx.iloc[i]
        e20 = ema20.iloc[i]
        e50 = ema50.iloc[i]
        if a > adx_threshold and e20 > e50:
            regimes.append("bull")
        elif a > adx_threshold and e20 < e50:
            regimes.append("bear")
        else:
            regimes.append("neutral")

    return pd.Series(regimes, index=df.index)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Core Engine & Event Loop (§5.3)
# ─────────────────────────────────────────────────────────────────────────────

class LeapEngine:
    """
    Multi-leg stateful backtest engine (§5.1, §5.2, §5.3).
    Maintains SleeveBook, executes 5-step daily event loop with next-day fill for strategic decisions.
    """

    def __init__(
        self,
        starting_cash: float = 30_000.0,
        risk_free_rate: float = 0.045,
        credit_haircut_pct: float = 0.05,
        profit_target_pct: float = 0.50,
        stop_loss_multiple: float = 2.0,
        strike_increment: float = 0.5,
    ):
        self.starting_cash = starting_cash
        self.r = risk_free_rate
        self.credit_haircut_pct = credit_haircut_pct
        self.profit_target_pct = profit_target_pct
        self.stop_loss_multiple = stop_loss_multiple
        self.strike_increment = strike_increment

        self.book = SleeveBook(cash=starting_cash)
        self.pending_queue: List[PendingOrder] = []

        # Diagnostics & counters
        self.short_skip_count = 0
        self.assignment_count = 0
        self.called_away_count = 0
        self.stock_stop_count = 0
        self.leap_roll_count = 0
        self.leap_sweep_count = 0
        self.bank_history: Dict[pd.Timestamp, float] = {}
        self._fresh_iv_symbols: set[str] = set()
        self._intrinsic_iv_fallback_events: set[tuple[pd.Timestamp, str]] = set()

    def run_simulation(
        self,
        bars_by_symbol: Dict[str, pd.DataFrame],
        iv_by_symbol: Dict[str, pd.Series],
        regime_by_symbol: Dict[str, pd.Series],
        decide_fn: Callable[[LeapEngine, pd.Timestamp, Dict[str, float], Dict[str, float], Dict[str, str]], None],
    ) -> SleeveBook:
        """
        Execute the master daily event loop across all trading days:
        1. EXECUTE (pending orders filled at today's close)
        2. EXPIRY  (settle today's expiring contracts)
        3. TRIGGER (price-touch PT/SL with close-to-close fill)
        4. DECIDE  (today's evaluation -> queue pending orders for tomorrow)
        5. MTM     (calculate sleeve equity, record curve, assert invariants)
        """
        # Normalize the IV input contract: prior-IV forward fill picks by position,
        # so an unsorted or duplicated index would silently pick the wrong prior.
        iv_by_symbol = {
            sym: series[~series.index.duplicated(keep="last")].sort_index()
            for sym, series in iv_by_symbol.items()
        }

        # Find shared trading days
        all_dates = sorted(
            list(
                set.intersection(
                    *[set(df.index) for df in bars_by_symbol.values()]
                )
            )
        )
        trading_days = [pd.Timestamp(d) for d in all_dates]

        for d in trading_days:
            # Current day market data snapshot
            current_prices = {sym: float(bars_by_symbol[sym].loc[d, "close"]) for sym in bars_by_symbol}
            current_ivs: Dict[str, float] = {}
            fresh_ivs: Dict[str, float] = {}
            missing_iv: List[str] = []
            missing_iv_no_prior: List[str] = []
            for sym in bars_by_symbol:
                series = iv_by_symbol.get(sym)
                if series is None or series.empty:
                    missing_iv.append(sym)
                    missing_iv_no_prior.append(sym)
                    continue
                iv_value = float(series.loc[d]) if d in series.index else float("nan")
                if _is_valid_iv(iv_value):
                    current_ivs[sym] = iv_value
                    fresh_ivs[sym] = iv_value
                    continue
                prior = series.loc[series.index < d]
                prior = prior[np.isfinite(prior) & (prior > 0.0)]
                missing_iv.append(sym)
                if prior.empty:
                    missing_iv_no_prior.append(sym)
                    continue
                current_ivs[sym] = float(prior.iloc[-1])
            self._fresh_iv_symbols = set(fresh_ivs)
            if missing_iv:
                self.book.realized.append({
                    "entry_date": d,
                    "exit_date": d,
                    "symbol": ",".join(sorted(missing_iv)),
                    "kind": "iv_coverage_skip",
                    "dollar_pnl": 0.0,
                    "detail": {
                        "reason": "missing_current_iv_forward_filled_for_mtm",
                        "no_prior_symbols": sorted(missing_iv_no_prior),
                    },
                })
            fallback_symbols = [
                sym for sym in missing_iv_no_prior if self._has_open_iv_sensitive_position(sym, d)
            ]
            if fallback_symbols:
                self._record_intrinsic_mtm_fallback(d, fallback_symbols)

            current_regimes = {
                sym: str(regime_by_symbol[sym].get(d, "neutral")) for sym in regime_by_symbol
            }

            # ── 1. EXECUTE ───────────────────────────────────────────────────
            self._step_execute(d, current_prices, current_ivs)

            # ── 2. EXPIRY ────────────────────────────────────────────────────
            self._step_expiry(d, current_prices, current_ivs)

            # ── 3. TRIGGER ───────────────────────────────────────────────────
            self._step_trigger(d, current_prices, current_ivs)

            # ── 4. DECIDE ────────────────────────────────────────────────────
            decide_fn(self, d, current_prices, fresh_ivs, current_regimes)

            # ── 5. MTM ───────────────────────────────────────────────────────
            self._step_mtm(d, current_prices, current_ivs)

        return self.book

    # ─────────────────────────────────────────────────────────────────────────
    # Event Loop Steps (§5.3)
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_equity(
        self,
        d: pd.Timestamp,
        current_prices: Dict[str, float],
        current_ivs: Dict[str, float],
    ) -> float:
        total_leg_mtm = 0.0
        for sym, leg_list in self.book.legs.items():
            S = current_prices.get(sym, 0.0)
            for leg in leg_list:
                t_rem = max(0.0, (leg.expiry - d).days / 365.0)
                iv = current_ivs.get(sym, 0.20)
                px = (
                    _option_price_for_mtm(S, leg.strike, t_rem, self.r, iv, leg.option_type)
                    if sym in current_ivs or t_rem <= 0.0
                    else _intrinsic_option_price(S, leg.strike, leg.option_type)
                )
                if leg.qty > 0:
                    total_leg_mtm += px * 100.0 * leg.qty
                else:
                    total_leg_mtm -= px * 100.0 * abs(leg.qty)

        spread_mtm = 0.0
        for sp in self.book.active_spreads:
            S = current_prices.get(sp.symbol, 0.0)
            t_rem = max(0.0, (sp.exit_date - d).days / 365.0)
            if d == sp.entry_date:
                close_debit = sp.credit_received
            elif d >= sp.exit_date:
                close_debit = sp.close_debit
            elif sp.symbol not in current_ivs:
                close_debit = _spread_intrinsic_debit(
                    sp.spread_type, sp.short_strike, sp.long_strike, S
                )
            else:
                iv = current_ivs[sp.symbol]
                close_debit = _spread_close_debit_for_mtm(
                    sp.spread_type, sp.short_strike, sp.long_strike, S, t_rem, self.r, iv
                )
            spread_mtm -= close_debit * 100.0 * sp.contracts

        total_stock_value = sum(
            self.book.shares.get(sym, 0) * current_prices.get(sym, 0.0)
            for sym in self.book.shares
        )
        return self.book.cash + total_leg_mtm + spread_mtm + total_stock_value

    def _can_calculate_equity(self, d: pd.Timestamp, current_ivs: Dict[str, float]) -> bool:
        return True

    def _assert_equity_continuity(
        self,
        before_equity: float,
        d: pd.Timestamp,
        current_prices: Dict[str, float],
        current_ivs: Dict[str, float],
        label: str,
        expected_delta: float = 0.0,
        tol: float = 1e-4,
    ) -> None:
        after_equity = self._calculate_equity(d, current_prices, current_ivs)
        actual_delta = after_equity - before_equity
        if abs(actual_delta - expected_delta) > tol:
            raise AssertionError(
                f"Equity continuity violation during {label} at {d}: "
                f"before={before_equity:.4f}, after={after_equity:.4f}, "
                f"diff={actual_delta:.4f}, expected={expected_delta:.4f}"
            )

    def _assert_event_equity_change(
        self,
        before_equity: Optional[float],
        d: pd.Timestamp,
        current_prices: Dict[str, float],
        current_ivs: Dict[str, float],
        label: str,
        expected_delta: float = 0.0,
    ) -> None:
        if before_equity is None:
            return
        self._assert_equity_continuity(
            before_equity, d, current_prices, current_ivs, label, expected_delta=expected_delta
        )

    def _step_execute(
        self,
        d: pd.Timestamp,
        current_prices: Dict[str, float],
        current_ivs: Dict[str, float],
    ) -> None:
        """Step 1: Execute pending orders queued from yesterday's DECIDE step."""
        if not self.pending_queue:
            self.book.pending_debits = 0.0
            return

        orders_to_process = list(self.pending_queue)
        self.pending_queue.clear()
        self.book.pending_debits = 0.0

        for order in orders_to_process:
            sym = order.symbol
            S = current_prices.get(sym, 0.0)
            requires_fresh_iv = self._order_requires_fresh_iv(order, d)
            if sym not in current_ivs and (requires_fresh_iv or order.order_type != "STOCK_STOP_CLOSE"):
                self.pending_queue.append(order)
                self.book.pending_debits += order.estimated_debit
                continue
            if requires_fresh_iv and sym not in self._fresh_iv_symbols:
                self.pending_queue.append(order)
                self.book.pending_debits += order.estimated_debit
                continue
            iv = current_ivs.get(sym, 0.20)
            if S <= 0:
                self.pending_queue.append(order)
                self.book.pending_debits += order.estimated_debit
                continue

            if order.order_type == "LEAP_BUY":
                before_equity = self._calculate_equity(d, current_prices, current_ivs)
                t_years = order.target_dte / 365.0
                strike = round_to_increment(
                    strike_for_delta(S, t_years, self.r, iv, order.target_delta, "call"),
                    self.strike_increment,
                )
                cost = black_scholes_price(S, strike, t_years, self.r, iv, "call")
                cost_per_contract = cost * 100.0

                if order.is_sweep:
                    # Funding sweep LEAP purchase (V2 / V3)
                    max_sweep_funds = min(self.book.premium_bank, self.book.available_cash)
                    if cost_per_contract <= max_sweep_funds and max_sweep_funds > 0:
                        self.book.cash -= cost_per_contract
                        # Clamped bank deduction (§5.2.1, 3R P3-1)
                        self.book.premium_bank = max(0.0, self.book.premium_bank - cost_per_contract)
                        expiry_date = d + pd.Timedelta(days=order.target_dte)
                        leg = OptionLeg(
                            option_type="call",
                            strike=strike,
                            expiry=expiry_date,
                            qty=1,
                            entry_price=cost,
                            entry_date=d,
                            role="leap",
                            symbol=sym,
                        )
                        self.book.legs.setdefault(sym, []).append(leg)
                        self.leap_sweep_count += 1
                        self.book.realized.append({
                            "entry_date": d,
                            "exit_date": d,
                            "symbol": sym,
                            "kind": "leap_sweep_buy",
                            "dollar_pnl": 0.0,
                            "detail": {"strike": strike, "cost": cost, "qty": 1, "is_sweep": True},
                        })
                        self._assert_event_equity_change(
                            before_equity, d, current_prices, current_ivs, "leap_sweep_buy"
                        )
                else:
                    # Standard LEAP entry (V1)
                    contracts = order.qty
                    total_cost = cost_per_contract * contracts
                    if contracts >= 1 and total_cost <= self.book.available_cash:
                        self.book.cash -= total_cost
                        expiry_date = d + pd.Timedelta(days=order.target_dte)
                        leg = OptionLeg(
                            option_type="call",
                            strike=strike,
                            expiry=expiry_date,
                            qty=contracts,
                            entry_price=cost,
                            entry_date=d,
                            role="leap",
                            symbol=sym,
                        )
                        self.book.legs.setdefault(sym, []).append(leg)
                        self.book.realized.append({
                            "entry_date": d,
                            "exit_date": d,
                            "symbol": sym,
                            "kind": "leap_entry",
                            "dollar_pnl": 0.0,
                            "detail": {"strike": strike, "cost": cost, "qty": contracts},
                        })
                        self._assert_event_equity_change(
                            before_equity, d, current_prices, current_ivs, "leap_entry"
                        )

            elif order.order_type == "LEAP_ROLL":
                # Close existing LEAP and attached short call, then enter new LEAP
                before_equity = self._calculate_equity(d, current_prices, current_ivs)
                sym_legs = self.book.legs.get(sym, [])
                leap_leg = None
                short_leg = None
                remaining_legs = []

                for leg in sym_legs:
                    if leg.role == "leap" and leap_leg is None:
                        leap_leg = leg
                    elif leg.role == "short_call" and short_leg is None:
                        short_leg = leg
                    else:
                        remaining_legs.append(leg)

                if leap_leg is not None:
                    t_rem = max(0.0, (leap_leg.expiry - d).days / 365.0)
                    close_px = _option_price_for_mtm(S, leap_leg.strike, t_rem, self.r, iv, "call")
                    proceeds = close_px * 100.0 * leap_leg.qty
                    self.book.cash += proceeds
                    dollar_pnl = (close_px - leap_leg.entry_price) * 100.0 * leap_leg.qty
                    self.book.realized.append({
                        "entry_date": leap_leg.entry_date,
                        "exit_date": d,
                        "symbol": sym,
                        "kind": "leap_roll",
                        "dollar_pnl": dollar_pnl,
                        "detail": {
                            "old_strike": leap_leg.strike,
                            "close_price": close_px,
                            "entry_price": leap_leg.entry_price,
                            "qty": leap_leg.qty,
                        },
                    })
                    self.leap_roll_count += 1

                if short_leg is not None:
                    t_rem = max(0.0, (short_leg.expiry - d).days / 365.0)
                    close_px = _option_price_for_mtm(S, short_leg.strike, t_rem, self.r, iv, "call")
                    cost_to_close = close_px * 100.0 * abs(short_leg.qty)
                    self.book.cash -= cost_to_close
                    dollar_pnl = (short_leg.entry_price - close_px) * 100.0 * abs(short_leg.qty)
                    self.book.realized.append({
                        "entry_date": short_leg.entry_date,
                        "exit_date": d,
                        "symbol": sym,
                        "kind": "short_cycle",
                        "dollar_pnl": dollar_pnl,
                        "detail": {"reason": "leap_roll_close", "close_price": close_px},
                    })

                self.book.legs[sym] = remaining_legs

                # Enter new rolled LEAP (DTE 365, delta 0.80)
                t_years = 365.0 / 365.0
                new_strike = round_to_increment(
                    strike_for_delta(S, t_years, self.r, iv, 0.80, "call"),
                    self.strike_increment,
                )
                new_cost = black_scholes_price(S, new_strike, t_years, self.r, iv, "call")
                roll_qty = leap_leg.qty if leap_leg else 1
                total_cost = new_cost * 100.0 * roll_qty
                if total_cost <= self.book.available_cash and roll_qty >= 1:
                    self.book.cash -= total_cost
                    new_leg = OptionLeg(
                        option_type="call",
                        strike=new_strike,
                        expiry=d + pd.Timedelta(days=365),
                        qty=roll_qty,
                        entry_price=new_cost,
                        entry_date=d,
                        role="leap",
                        symbol=sym,
                    )
                    self.book.legs.setdefault(sym, []).append(new_leg)
                if leap_leg is not None or short_leg is not None:
                    self._assert_event_equity_change(
                        before_equity, d, current_prices, current_ivs, "leap_roll"
                    )

            elif order.order_type == "LEAP_CLOSE":
                # Strategic LEAP close (bear regime or -50% hard stop)
                before_equity = self._calculate_equity(d, current_prices, current_ivs)
                sym_legs = self.book.legs.get(sym, [])
                remaining_legs = []
                closed_any = False
                for leg in sym_legs:
                    if leg.role == "leap":
                        t_rem = max(0.0, (leg.expiry - d).days / 365.0)
                        close_px = _option_price_for_mtm(S, leg.strike, t_rem, self.r, iv, "call")
                        proceeds = close_px * 100.0 * leg.qty
                        self.book.cash += proceeds
                        dollar_pnl = (close_px - leg.entry_price) * 100.0 * leg.qty
                        self.book.realized.append({
                            "entry_date": leg.entry_date,
                            "exit_date": d,
                            "symbol": sym,
                            "kind": "leap_close",
                            "dollar_pnl": dollar_pnl,
                            "detail": {"reason": order.reason, "close_price": close_px},
                        })
                        closed_any = True
                    elif leg.role == "short_call":
                        t_rem = max(0.0, (leg.expiry - d).days / 365.0)
                        close_px = _option_price_for_mtm(S, leg.strike, t_rem, self.r, iv, "call")
                        cost_to_close = close_px * 100.0 * abs(leg.qty)
                        self.book.cash -= cost_to_close
                        dollar_pnl = (leg.entry_price - close_px) * 100.0 * abs(leg.qty)
                        self.book.realized.append({
                            "entry_date": leg.entry_date,
                            "exit_date": d,
                            "symbol": sym,
                            "kind": "short_cycle",
                            "dollar_pnl": dollar_pnl,
                            "detail": {"reason": f"leap_close_{order.reason}", "close_price": close_px},
                        })
                        closed_any = True
                    else:
                        remaining_legs.append(leg)
                self.book.legs[sym] = remaining_legs
                if closed_any:
                    self._assert_event_equity_change(
                        before_equity, d, current_prices, current_ivs, "leap_close"
                    )

            elif order.order_type == "SHORT_CALL_SELL":
                # Sell covered call under LEAP (V1)
                before_equity = self._calculate_equity(d, current_prices, current_ivs)
                sym_legs = self.book.legs.get(sym, [])
                leap_leg = next((l for l in sym_legs if l.role == "leap"), None)
                if leap_leg is None:
                    continue

                t_years = order.target_dte / 365.0
                strike = round_to_increment(
                    strike_for_delta(S, t_years, self.r, iv, order.target_delta, "call"),
                    self.strike_increment,
                )
                # Dynamic net-cost rule (§2.1, 3R P2-2)
                net_cost = leap_leg.entry_price - (leap_leg.cumulative_short_credits / max(1, leap_leg.qty))
                if strike > leap_leg.strike + net_cost:
                    raw_credit = black_scholes_price(S, strike, t_years, self.r, iv, "call")
                    credit_received = raw_credit * (1.0 - self.credit_haircut_pct)
                    expected_delta = -(raw_credit - credit_received) * 100.0 * leap_leg.qty
                    self.book.cash += credit_received * 100.0 * leap_leg.qty
                    short_leg = OptionLeg(
                        option_type="call",
                        strike=strike,
                        expiry=d + pd.Timedelta(days=order.target_dte),
                        qty=-leap_leg.qty,
                        entry_price=credit_received,
                        entry_date=d,
                        role="short_call",
                        symbol=sym,
                    )
                    self.book.legs[sym].append(short_leg)
                    self._assert_event_equity_change(
                        before_equity,
                        d,
                        current_prices,
                        current_ivs,
                        "short_call_sell",
                        expected_delta=expected_delta,
                    )
                else:
                    self.short_skip_count += 1
                    self.book.realized.append({
                        "entry_date": d,
                        "exit_date": d,
                        "symbol": sym,
                        "kind": "short_skip",
                        "dollar_pnl": 0.0,
                        "detail": {
                            "strike": strike,
                            "threshold": leap_leg.strike + net_cost,
                            "net_cost": net_cost,
                        },
                    })

            elif order.order_type == "CSP_SELL":
                # Sell Cash-Secured Put (V2)
                before_equity = self._calculate_equity(d, current_prices, current_ivs)
                t_years = order.target_dte / 365.0
                strike = round_to_increment(
                    strike_for_delta(S, t_years, self.r, iv, order.target_delta, "put"),
                    self.strike_increment,
                )
                collateral_per_contract = strike * 100.0
                max_contracts = int(math.floor(self.book.available_cash / collateral_per_contract))
                if max_contracts >= 1:
                    raw_credit = black_scholes_price(S, strike, t_years, self.r, iv, "put")
                    credit_received = raw_credit * (1.0 - self.credit_haircut_pct)
                    expected_delta = -(raw_credit - credit_received) * 100.0 * max_contracts
                    total_collateral = collateral_per_contract * max_contracts
                    self.book.reserved_collateral += total_collateral
                    self.book.cash += credit_received * 100.0 * max_contracts
                    csp_leg = OptionLeg(
                        option_type="put",
                        strike=strike,
                        expiry=d + pd.Timedelta(days=order.target_dte),
                        qty=-max_contracts,
                        entry_price=credit_received,
                        entry_date=d,
                        role="csp",
                        symbol=sym,
                        collateral_reserved=total_collateral,
                    )
                    self.book.legs.setdefault(sym, []).append(csp_leg)
                    self._assert_event_equity_change(
                        before_equity,
                        d,
                        current_prices,
                        current_ivs,
                        "csp_sell",
                        expected_delta=expected_delta,
                    )

            elif order.order_type == "CC_SELL":
                # Sell Covered Call against assigned shares (V2)
                before_equity = self._calculate_equity(d, current_prices, current_ivs)
                shares_held = self.book.shares.get(sym, 0)
                contracts = shares_held // 100
                if contracts >= 1:
                    t_years = order.target_dte / 365.0
                    cost_basis = self.book.share_cost_basis.get(sym, S)
                    theo_strike = round_to_increment(
                        strike_for_delta(S, t_years, self.r, iv, order.target_delta, "call"),
                        self.strike_increment,
                    )
                    # Snap rule: strike >= cost_basis
                    strike = max(theo_strike, round_to_increment(cost_basis, self.strike_increment))
                    raw_credit = black_scholes_price(S, strike, t_years, self.r, iv, "call")
                    credit_received = raw_credit * (1.0 - self.credit_haircut_pct)
                    expected_delta = -(raw_credit - credit_received) * 100.0 * contracts
                    self.book.cash += credit_received * 100.0 * contracts
                    cc_leg = OptionLeg(
                        option_type="call",
                        strike=strike,
                        expiry=d + pd.Timedelta(days=order.target_dte),
                        qty=-contracts,
                        entry_price=credit_received,
                        entry_date=d,
                        role="covered_call",
                        symbol=sym,
                    )
                    self.book.legs.setdefault(sym, []).append(cc_leg)
                    self._assert_event_equity_change(
                        before_equity,
                        d,
                        current_prices,
                        current_ivs,
                        "cc_sell",
                        expected_delta=expected_delta,
                    )

            elif order.order_type == "STOCK_STOP_CLOSE":
                # Strategic stock stop loss (-20%) liquidation (V2)
                before_equity = self._calculate_equity(d, current_prices, current_ivs)
                shares_held = self.book.shares.get(sym, 0)
                if shares_held > 0:
                    proceeds = S * shares_held
                    self.book.cash += proceeds
                    cost_basis = self.book.share_cost_basis.get(sym, S)
                    dollar_pnl = (S - cost_basis) * shares_held
                    self.book.shares[sym] = 0
                    self.book.share_cost_basis[sym] = 0.0
                    self.stock_stop_count += 1
                    self.book.realized.append({
                        "entry_date": d,
                        "exit_date": d,
                        "symbol": sym,
                        "kind": "share_stop",
                        "dollar_pnl": dollar_pnl,
                        "detail": {"close_price": S, "cost_basis": cost_basis, "shares": shares_held},
                    })
                    # Also close open covered call if any
                    sym_legs = self.book.legs.get(sym, [])
                    remaining_legs = []
                    for leg in sym_legs:
                        if leg.role == "covered_call":
                            t_rem = max(0.0, (leg.expiry - d).days / 365.0)
                            close_px = _option_price_for_mtm(S, leg.strike, t_rem, self.r, iv, "call")
                            self.book.cash -= close_px * 100.0 * abs(leg.qty)
                            cc_pnl = (leg.entry_price - close_px) * 100.0 * abs(leg.qty)
                            self.book.realized.append({
                                "entry_date": leg.entry_date,
                                "exit_date": d,
                                "symbol": sym,
                                "kind": "short_cycle",
                                "dollar_pnl": cc_pnl,
                                "detail": {"reason": "stock_stop_close", "close_price": close_px},
                            })
                        else:
                            remaining_legs.append(leg)
                    self.book.legs[sym] = remaining_legs
                    self._assert_event_equity_change(
                        before_equity, d, current_prices, current_ivs, "stock_stop_close"
                    )

            elif order.order_type == "SPREAD_ENTRY":
                # Enter Strategy 7 raw credit spread (V3)
                if order.raw_trade is not None:
                    self._execute_spread_entry(d, order.raw_trade, current_prices, current_ivs)

    def _execute_spread_entry(
        self,
        d: pd.Timestamp,
        raw: Dict[str, Any],
        current_prices: Optional[Dict[str, float]] = None,
        current_ivs: Optional[Dict[str, float]] = None,
    ) -> None:
        """Enter a Strategy 7 raw spread on its already-confirmed vendor entry_date."""
        spread_symbol = str(raw.get("symbol", "SPY"))
        spread_type = str(raw["spread_type"])
        short_strike = float(raw["short_strike"])
        long_strike = float(raw["long_strike"])
        max_loss = float(raw["max_loss"])
        credit_received = float(raw["credit_received"])
        width = self._validate_spread_raw_invariants(
            spread_symbol, spread_type, short_strike, long_strike, credit_received, max_loss
        )
        contracts = int(math.floor(self.book.available_cash * 0.05 / (max_loss * 100.0)))
        if contracts < 1:
            return

        current_prices = current_prices or {}
        current_ivs = current_ivs or {}
        before_equity = (
            self._calculate_equity(d, current_prices, current_ivs)
            if spread_symbol in current_prices and self._can_calculate_equity(d, current_ivs)
            else None
        )
        collateral = max_loss * 100.0 * contracts
        expected_collateral = (width - credit_received) * 100.0 * contracts
        if abs(collateral - expected_collateral) > 1e-4:
            raise AssertionError(
                f"Spread collateral mismatch at {d}: symbol={spread_symbol}, "
                f"collateral={collateral:.4f}, expected={expected_collateral:.4f}"
            )

        sp = ActiveSpread(
            symbol=spread_symbol,
            spread_type=spread_type,
            short_strike=short_strike,
            long_strike=long_strike,
            entry_date=pd.Timestamp(raw.get("entry_date", d)),
            exit_date=pd.Timestamp(raw["exit_date"]),
            contracts=contracts,
            credit_received=credit_received,
            max_loss=max_loss,
            close_debit=float(raw.get("close_debit", 0.0)),
            realized_pnl=float(raw.get("realized_pnl", 0.0)),
            exit_reason=str(raw.get("exit_reason", "")),
        )

        self.book.reserved_collateral += collateral
        self.book.cash += credit_received * 100.0 * contracts
        self.book.active_spreads.append(sp)
        if before_equity is not None:
            self._assert_event_equity_change(
                before_equity,
                d,
                current_prices,
                current_ivs,
                "spread_entry",
                expected_delta=0.0,
            )

    @staticmethod
    def _validate_spread_raw_invariants(
        symbol: str,
        spread_type: str,
        short_strike: float,
        long_strike: float,
        credit_received: float,
        max_loss: float,
        tol: float = 1e-4,
    ) -> float:
        if spread_type not in {"bull_put", "bear_call"}:
            raise AssertionError(f"Unsupported spread_type for {symbol}: {spread_type}")
        width = abs(short_strike - long_strike)
        if width <= 0.0:
            raise AssertionError(f"Spread width must be positive for {symbol}")
        if not (0.0 < credit_received <= width + tol):
            raise AssertionError(
                f"Spread credit outside width for {symbol}: credit={credit_received:.4f}, width={width:.4f}"
            )
        expected_max_loss = width - credit_received
        if abs(max_loss - expected_max_loss) > tol:
            raise AssertionError(
                f"Spread max_loss mismatch for {symbol}: max_loss={max_loss:.4f}, "
                f"expected={expected_max_loss:.4f}"
            )
        if max_loss <= tol:
            raise AssertionError(f"Spread max_loss must be positive for {symbol}: max_loss={max_loss:.4f}")
        return width

    def _step_expiry(
        self,
        d: pd.Timestamp,
        current_prices: Dict[str, float],
        current_ivs: Dict[str, float],
    ) -> None:
        """Step 2: Settle options expiring on date d (§5.3)."""
        before_equity = (
            self._calculate_equity(d, current_prices, current_ivs)
            if self._can_calculate_equity(d, current_ivs)
            else None
        )

        # Settle active spreads expiring today (V3)
        remaining_spreads = []
        for sp in self.book.active_spreads:
            if d >= sp.exit_date:
                collateral = sp.max_loss * 100.0 * sp.contracts
                self.book.reserved_collateral -= collateral
                self.book.cash -= sp.close_debit * 100.0 * sp.contracts
                # Cash moved credit-in / debit-out, so reported P&L must be that same
                # difference; a vendor realized_pnl that disagrees would make metrics
                # diverge from the equity curve (§1 cash conservation).
                dollar_pnl = (sp.credit_received - sp.close_debit) * 100.0 * sp.contracts
                vendor_pnl = sp.realized_pnl * 100.0 * sp.contracts
                if abs(vendor_pnl - dollar_pnl) > 1e-4:
                    raise AssertionError(
                        f"Spread realized_pnl mismatch at {d}: symbol={sp.symbol}, "
                        f"vendor={vendor_pnl:.4f}, credit_minus_debit={dollar_pnl:.4f}"
                    )
                if dollar_pnl > 0:
                    self.book.premium_bank = min(self.book.cash, self.book.premium_bank + dollar_pnl)
                self.book.realized.append({
                    "entry_date": sp.entry_date,
                    "exit_date": d,
                    "symbol": sp.symbol,
                    "kind": "short_cycle",
                    "dollar_pnl": dollar_pnl,
                    "detail": {
                        "spread_type": sp.spread_type,
                        "exit_reason": sp.exit_reason,
                        "contracts": sp.contracts,
                        "credit_received": sp.credit_received,
                        "close_debit": sp.close_debit,
                    },
                })
            else:
                remaining_spreads.append(sp)
        self.book.active_spreads = remaining_spreads

        # Settle single legs expiring today (V1 / V2)
        for sym, leg_list in list(self.book.legs.items()):
            remaining_legs = []
            for leg in leg_list:
                if d >= leg.expiry:
                    S = current_prices.get(sym, 0.0)
                    q = abs(leg.qty)

                    if leg.role == "short_call":
                        # V1 Short Call Expiry: OTM -> expires; ITM -> cash settlement (§5.3)
                        if S <= leg.strike:
                            dollar_pnl = leg.entry_price * 100.0 * q
                            # Add credit to LEAP's cumulative credit tracking
                            self._record_leap_credit(sym, leg.entry_price * q)
                            self.book.realized.append({
                                "entry_date": leg.entry_date,
                                "exit_date": d,
                                "symbol": sym,
                                "kind": "short_cycle",
                                "dollar_pnl": dollar_pnl,
                                "detail": {"reason": "expiry_otm", "strike": leg.strike, "close_price": S},
                            })
                        else:
                            intrinsic = S - leg.strike
                            debit_dollar = intrinsic * 100.0 * q
                            self.book.cash -= debit_dollar
                            dollar_pnl = (leg.entry_price - intrinsic) * 100.0 * q
                            self._record_leap_credit(sym, (leg.entry_price - intrinsic) * q)
                            self.book.realized.append({
                                "entry_date": leg.entry_date,
                                "exit_date": d,
                                "symbol": sym,
                                "kind": "short_cycle",
                                "dollar_pnl": dollar_pnl,
                                "detail": {"reason": "expiry_itm_cash_settle", "strike": leg.strike, "close_price": S},
                            })

                    elif leg.role == "csp":
                        # V2 Cash-Secured Put Expiry
                        self.book.reserved_collateral -= leg.collateral_reserved
                        if S >= leg.strike:
                            # OTM expiry: full premium realized
                            dollar_pnl = leg.entry_price * 100.0 * q
                            self.book.premium_bank = min(self.book.cash, self.book.premium_bank + dollar_pnl)
                            self.book.realized.append({
                                "entry_date": leg.entry_date,
                                "exit_date": d,
                                "symbol": sym,
                                "kind": "short_cycle",
                                "dollar_pnl": dollar_pnl,
                                "detail": {"reason": "csp_expiry_otm", "strike": leg.strike, "close_price": S},
                            })
                        else:
                            # ITM assignment: buy stock at strike
                            assignment_cost = leg.strike * 100.0 * q
                            self.book.cash -= assignment_cost
                            self.book.shares[sym] = self.book.shares.get(sym, 0) + 100 * q
                            self.book.share_cost_basis[sym] = leg.strike - leg.entry_price
                            self.assignment_count += 1
                            self.book.realized.append({
                                "entry_date": leg.entry_date,
                                "exit_date": d,
                                "symbol": sym,
                                "kind": "assignment",
                                "dollar_pnl": 0.0,
                                "detail": {
                                    "strike": leg.strike,
                                    "cost_basis": leg.strike - leg.entry_price,
                                    "shares": 100 * q,
                                    "close_price": S,
                                },
                            })

                    elif leg.role == "covered_call":
                        # V2 Covered Call Expiry
                        if S <= leg.strike:
                            # OTM expiry: full premium realized
                            dollar_pnl = leg.entry_price * 100.0 * q
                            self.book.premium_bank = min(self.book.cash, self.book.premium_bank + dollar_pnl)
                            self.book.realized.append({
                                "entry_date": leg.entry_date,
                                "exit_date": d,
                                "symbol": sym,
                                "kind": "short_cycle",
                                "dollar_pnl": dollar_pnl,
                                "detail": {"reason": "cc_expiry_otm", "strike": leg.strike, "close_price": S},
                            })
                        else:
                            # ITM called away: sell stock at strike
                            proceeds = leg.strike * 100.0 * q
                            self.book.cash += proceeds
                            self.book.shares[sym] = max(0, self.book.shares.get(sym, 0) - 100 * q)
                            cost_basis = self.book.share_cost_basis.get(sym, leg.strike)
                            stock_pnl = (leg.strike - cost_basis) * 100.0 * q
                            option_pnl = leg.entry_price * 100.0 * q
                            total_pnl = stock_pnl + option_pnl
                            if total_pnl > 0:
                                self.book.premium_bank = min(self.book.cash, self.book.premium_bank + total_pnl)
                            self.called_away_count += 1
                            self.book.realized.append({
                                "entry_date": leg.entry_date,
                                "exit_date": d,
                                "symbol": sym,
                                "kind": "called_away",
                                "dollar_pnl": total_pnl,
                                "detail": {
                                    "strike": leg.strike,
                                    "cost_basis": cost_basis,
                                    "stock_pnl": stock_pnl,
                                    "option_pnl": option_pnl,
                                    "shares": 100 * q,
                                },
                            })

                    elif leg.role == "leap":
                        # LEAP reaches expiry without roll (unlikely, but settled intrinsically)
                        intrinsic = max(0.0, S - leg.strike)
                        proceeds = intrinsic * 100.0 * leg.qty
                        self.book.cash += proceeds
                        dollar_pnl = (intrinsic - leg.entry_price) * 100.0 * leg.qty
                        self.book.realized.append({
                            "entry_date": leg.entry_date,
                            "exit_date": d,
                            "symbol": sym,
                            "kind": "leap_close",
                            "dollar_pnl": dollar_pnl,
                            "detail": {"reason": "leap_natural_expiry", "intrinsic": intrinsic},
                        })
                else:
                    remaining_legs.append(leg)
            self.book.legs[sym] = remaining_legs

        if before_equity is not None:
            self._assert_equity_continuity(before_equity, d, current_prices, current_ivs, "expiry")

    def _step_trigger(
        self,
        d: pd.Timestamp,
        current_prices: Dict[str, float],
        current_ivs: Dict[str, float],
    ) -> None:
        """Step 3: Price-touch PT exits on open short legs (same-day close MTM fill, §5.3)."""
        before_equity = self._calculate_equity(d, current_prices, current_ivs)
        closed_any = False
        for sym, leg_list in list(self.book.legs.items()):
            if sym not in self._fresh_iv_symbols:
                continue
            remaining_legs = []
            for leg in leg_list:
                if leg.qty >= 0:  # Only short legs have PT
                    remaining_legs.append(leg)
                    continue

                S = current_prices.get(sym, 0.0)
                iv = current_ivs.get(sym, 0.20)
                t_rem = max(0.0, (leg.expiry - d).days / 365.0)
                current_price = _option_price_for_mtm(S, leg.strike, t_rem, self.r, iv, leg.option_type)

                if leg.role in {"csp", "covered_call"}:
                    # V2 wheel legs are held to expiry so assignment/called-away mechanics remain observable.
                    is_pt = False
                    is_sl = False
                else:
                    is_pt = current_price <= leg.entry_price * (1.0 - self.profit_target_pct)
                    is_sl = current_price >= leg.entry_price * self.stop_loss_multiple

                if is_sl or is_pt:
                    closed_any = True
                    exit_reason = "stop_loss" if is_sl else "profit_target"
                    q = abs(leg.qty)
                    cost_to_close = current_price * 100.0 * q
                    self.book.cash -= cost_to_close
                    if leg.role == "csp":
                        self.book.reserved_collateral -= leg.collateral_reserved

                    dollar_pnl = (leg.entry_price - current_price) * 100.0 * q
                    if leg.role == "short_call":
                        self._record_leap_credit(sym, (leg.entry_price - current_price) * q)
                    elif leg.role in {"csp", "covered_call"} and dollar_pnl > 0:
                        self.book.premium_bank = min(self.book.cash, self.book.premium_bank + dollar_pnl)

                    self.book.realized.append({
                        "entry_date": leg.entry_date,
                        "exit_date": d,
                        "symbol": sym,
                        "kind": "short_cycle",
                        "dollar_pnl": dollar_pnl,
                        "detail": {
                            "exit_reason": exit_reason,
                            "close_price": current_price,
                            "entry_price": leg.entry_price,
                            "strike": leg.strike,
                        },
                    })
                else:
                    remaining_legs.append(leg)
            self.book.legs[sym] = remaining_legs
        if closed_any:
            self._assert_equity_continuity(before_equity, d, current_prices, current_ivs, "trigger")

    def _step_mtm(
        self,
        d: pd.Timestamp,
        current_prices: Dict[str, float],
        current_ivs: Dict[str, float],
    ) -> None:
        """Step 5: Compute sleeve equity, record curve, and assert invariants (§5.2.1)."""
        equity = self._calculate_equity(d, current_prices, current_ivs)
        self.book.equity_curve[d] = equity
        self.bank_history[d] = self.book.premium_bank

        # Run P0 Gate invariant assertion
        self.book.assert_invariants(equity, current_prices, current_ivs, d, self.r)

    def _record_leap_credit(self, symbol: str, credit_dollars: float) -> None:
        """Helper to accumulate short credits into the underlying LEAP for dynamic net-cost tracking."""
        sym_legs = self.book.legs.get(symbol, [])
        for leg in sym_legs:
            if leg.role == "leap":
                leg.cumulative_short_credits += credit_dollars
                break

    def _has_open_iv_sensitive_position(self, symbol: str, d: pd.Timestamp) -> bool:
        return any((leg.expiry - d).days > 0 for leg in self.book.legs.get(symbol, [])) or any(
            sp.symbol == symbol and d < sp.exit_date for sp in self.book.active_spreads
        )

    def _record_intrinsic_mtm_fallback(self, d: pd.Timestamp, symbols: List[str]) -> None:
        new_symbols = []
        for sym in sorted(symbols):
            key = (d, sym)
            if key not in self._intrinsic_iv_fallback_events:
                self._intrinsic_iv_fallback_events.add(key)
                new_symbols.append(sym)
        if not new_symbols:
            return
        self.book.realized.append({
            "entry_date": d,
            "exit_date": d,
            "symbol": ",".join(new_symbols),
            "kind": "iv_intrinsic_mtm_fallback",
            "dollar_pnl": 0.0,
            "detail": {"reason": "missing_iv_no_prior_intrinsic_mtm"},
        })

    def _order_requires_fresh_iv(self, order: PendingOrder, d: pd.Timestamp) -> bool:
        if order.order_type == "STOCK_STOP_CLOSE":
            return any(
                leg.role == "covered_call" and (leg.expiry - d).days > 0
                for leg in self.book.legs.get(order.symbol, [])
            )
        return order.order_type in {
            "LEAP_BUY",
            "LEAP_CLOSE",
            "LEAP_ROLL",
            "SHORT_CALL_SELL",
            "CSP_SELL",
            "CC_SELL",
            "SPREAD_ENTRY",
        }


# ─────────────────────────────────────────────────────────────────────────────
# 6. Strategy Variant Decision Functions (§2)
# ─────────────────────────────────────────────────────────────────────────────

def decide_v1_pmcc(
    engine: LeapEngine,
    d: pd.Timestamp,
    current_prices: Dict[str, float],
    current_ivs: Dict[str, float],
    current_regimes: Dict[str, str],
) -> None:
    """
    V1: PMCC Classic Strategy Decision Logic (§2.1).
    - SPY / QQQ
    - Delta 0.80 LEAP entry when regime is bull. Sizing <= 40% sleeve equity per symbol, total <= 80%.
    - Weekly delta 0.20 short call when holding LEAP. Dynamic net-cost rule: short_strike > leap_strike + net_cost.
    - LEAP DTE < 90 roll, -50% stop loss, bear regime liquidation.
    """
    sleeve_equity = engine.book.equity_curve.get(d, engine.book.cash)

    for sym in ("SPY", "QQQ"):
        if sym not in current_prices or sym not in current_ivs:
            continue
        S = current_prices[sym]
        iv = current_ivs[sym]
        regime = current_regimes.get(sym, "neutral")

        sym_legs = engine.book.legs.get(sym, [])
        leap_leg = next((l for l in sym_legs if l.role == "leap"), None)
        short_leg = next((l for l in sym_legs if l.role == "short_call"), None)

        # Pending check
        has_pending_leap = any(p.symbol == sym and p.order_type in {"LEAP_BUY", "LEAP_ROLL"} for p in engine.pending_queue)
        has_pending_short = any(p.symbol == sym and p.order_type == "SHORT_CALL_SELL" for p in engine.pending_queue)

        # 1. Existing LEAP Management
        if leap_leg is not None:
            # Bear regime liquidation (§2.1)
            if regime == "bear":
                engine.pending_queue.append(
                    PendingOrder(order_type="LEAP_CLOSE", symbol=sym, reason="bear_regime_exit")
                )
                continue

            # LEAP -50% hard stop (§2.1)
            t_rem = max(0.0, (leap_leg.expiry - d).days / 365.0)
            current_leap_px = black_scholes_price(S, leap_leg.strike, t_rem, engine.r, iv, "call")
            if current_leap_px <= leap_leg.entry_price * 0.50:
                engine.pending_queue.append(
                    PendingOrder(order_type="LEAP_CLOSE", symbol=sym, reason="leap_hard_stop_50pct")
                )
                continue

            # LEAP DTE < 90 roll (§2.1, BCI rule)
            if (leap_leg.expiry - d).days < 90 and not has_pending_leap:
                engine.pending_queue.append(
                    PendingOrder(order_type="LEAP_ROLL", symbol=sym, reason="dte_under_90_roll")
                )
                continue

            # Short Call Entry (§2.1)
            if short_leg is None and not has_pending_short and not has_pending_leap:
                engine.pending_queue.append(
                    PendingOrder(
                        order_type="SHORT_CALL_SELL",
                        symbol=sym,
                        target_delta=0.20,
                        target_dte=7,
                    )
                )

        # 2. New LEAP Entry (§2.1)
        elif regime == "bull" and not has_pending_leap:
            # Check 80% sleeve limit across all LEAPs
            current_leap_cost = sum(
                l.entry_price * 100.0 * l.qty
                for l_list in engine.book.legs.values()
                for l in l_list
                if l.role == "leap"
            )
            if current_leap_cost < 0.80 * sleeve_equity:
                t_years = 365.0 / 365.0
                est_strike = round_to_increment(
                    strike_for_delta(S, t_years, engine.r, iv, 0.80, "call"),
                    engine.strike_increment,
                )
                est_cost = black_scholes_price(S, est_strike, t_years, engine.r, iv, "call")
                cost_per_contract = est_cost * 100.0
                max_contracts = int(math.floor(0.40 * sleeve_equity / cost_per_contract))
                if max_contracts >= 1 and (max_contracts * cost_per_contract) <= engine.book.available_cash:
                    engine.pending_queue.append(
                        PendingOrder(
                            order_type="LEAP_BUY",
                            symbol=sym,
                            target_delta=0.80,
                            target_dte=365,
                            qty=max_contracts,
                            role="leap",
                            estimated_debit=max_contracts * cost_per_contract,
                        )
                    )
                    engine.book.pending_debits += max_contracts * cost_per_contract


def decide_v2_wheel(
    engine: LeapEngine,
    d: pd.Timestamp,
    current_prices: Dict[str, float],
    current_ivs: Dict[str, float],
    current_regimes: Dict[str, str],
    month_end_dates: Optional[set] = None,
) -> None:
    """
    V2: Wheel + LEAP Sweep Strategy Decision Logic (§2.2).
    - SLV / TLT for wheel (CSP + CC + stock stop at -20%)
    - SPY for month-end LEAP sweep using accumulated premium_bank
    """
    sleeve_equity = engine.book.equity_curve.get(d, engine.book.cash)

    # 1. Wheel Management on SLV and TLT (SLV prioritized, then TLT)
    for sym in ("SLV", "TLT"):
        if sym not in current_prices:
            continue
        S = current_prices[sym]
        regime = current_regimes.get(sym, "neutral")

        shares_held = engine.book.shares.get(sym, 0)
        sym_legs = engine.book.legs.get(sym, [])
        csp_leg = next((l for l in sym_legs if l.role == "csp"), None)
        cc_leg = next((l for l in sym_legs if l.role == "covered_call"), None)

        has_pending_csp = any(p.symbol == sym and p.order_type == "CSP_SELL" for p in engine.pending_queue)
        has_pending_cc = any(p.symbol == sym and p.order_type == "CC_SELL" for p in engine.pending_queue)
        has_pending_stock_stop = any(p.symbol == sym and p.order_type == "STOCK_STOP_CLOSE" for p in engine.pending_queue)

        if shares_held > 0:
            # Stock stop loss check (-20% from cost basis)
            cost_basis = engine.book.share_cost_basis.get(sym, S)
            if S <= cost_basis * (1.0 - 0.20) and not has_pending_stock_stop:
                engine.pending_queue.append(
                    PendingOrder(order_type="STOCK_STOP_CLOSE", symbol=sym, reason="stock_stop_20pct")
                )
                continue

            # CC entry if holding shares and no CC open
            if sym in current_ivs and cc_leg is None and not has_pending_cc and not has_pending_stock_stop:
                engine.pending_queue.append(
                    PendingOrder(
                        order_type="CC_SELL",
                        symbol=sym,
                        target_delta=0.25,
                        target_dte=7,
                    )
                )

        else:
            # CSP entry when not in bear regime
            if sym in current_ivs and csp_leg is None and not has_pending_csp and regime != "bear":
                iv = current_ivs[sym]
                t_years = 7.0 / 365.0
                est_strike = round_to_increment(
                    strike_for_delta(S, t_years, engine.r, iv, 0.25, "put"),
                    engine.strike_increment,
                )
                collateral_req = est_strike * 100.0
                if engine.book.available_cash >= collateral_req:
                    engine.pending_queue.append(
                        PendingOrder(
                            order_type="CSP_SELL",
                            symbol=sym,
                            target_delta=0.25,
                            target_dte=7,
                        )
                    )

    # 2. SPY LEAP Management (Hold only, DTE<90 roll, -50% stop, bear exit)
    spy_legs = engine.book.legs.get("SPY", [])
    spy_leap = next((l for l in spy_legs if l.role == "leap"), None)
    if spy_leap is not None and "SPY" in current_prices and "SPY" in current_ivs:
        S_spy = current_prices["SPY"]
        iv_spy = current_ivs["SPY"]
        reg_spy = current_regimes.get("SPY", "neutral")

        if reg_spy == "bear":
            engine.pending_queue.append(
                PendingOrder(order_type="LEAP_CLOSE", symbol="SPY", reason="bear_regime_exit")
            )
        else:
            t_rem = max(0.0, (spy_leap.expiry - d).days / 365.0)
            curr_px = black_scholes_price(S_spy, spy_leap.strike, t_rem, engine.r, iv_spy, "call")
            if curr_px <= spy_leap.entry_price * 0.50:
                engine.pending_queue.append(
                    PendingOrder(order_type="LEAP_CLOSE", symbol="SPY", reason="leap_hard_stop_50pct")
                )
            elif (spy_leap.expiry - d).days < 90:
                engine.pending_queue.append(
                    PendingOrder(order_type="LEAP_ROLL", symbol="SPY", reason="dte_under_90_roll")
                )

    # 3. Month-End LEAP Sweep Decision (§2.2)
    is_month_end = False
    if month_end_dates is not None:
        is_month_end = d in month_end_dates
    else:
        next_d = d + pd.Timedelta(days=1)
        is_month_end = next_d.month != d.month or d.day >= 28

    if is_month_end and "SPY" in current_prices and "SPY" in current_ivs:
        S_spy = current_prices["SPY"]
        iv_spy = current_ivs["SPY"]
        t_years = 365.0 / 365.0
        est_strike = round_to_increment(
            strike_for_delta(S_spy, t_years, engine.r, iv_spy, 0.70, "call"),
            engine.strike_increment,
        )
        est_cost = black_scholes_price(S_spy, est_strike, t_years, engine.r, iv_spy, "call")
        cost_per_contract = est_cost * 100.0

        current_leap_cost = sum(
            l.entry_price * 100.0 * l.qty
            for l_list in engine.book.legs.values()
            for l in l_list
            if l.role == "leap"
        )
        # Sizing and 50% sleeve cap limit (§2.2)
        if (
            cost_per_contract <= min(engine.book.premium_bank, engine.book.available_cash)
            and (current_leap_cost + cost_per_contract) <= 0.50 * sleeve_equity
        ):
            engine.pending_queue.append(
                PendingOrder(
                    order_type="LEAP_BUY",
                    symbol="SPY",
                    target_delta=0.70,
                    target_dte=365,
                    qty=1,
                    role="leap",
                    is_sweep=True,
                    estimated_debit=cost_per_contract,
                )
            )
            engine.book.pending_debits += cost_per_contract


def decide_v3_spread_financing(
    engine: LeapEngine,
    d: pd.Timestamp,
    current_prices: Dict[str, float],
    current_ivs: Dict[str, float],
    current_regimes: Dict[str, str],
    raw_trades_by_entry_date: Dict[pd.Timestamp, List[Dict[str, Any]]],
) -> None:
    """
    V3: Strategy 7 Credit Spread Financing LEAP Ladder Decision Logic (§2.3).
    - SPY, QQQ, IWM: Replay raw trades from Strategy 7 (all 3 structures: bull_put, bear_call, iron_condor).
    - Sizing: contracts = floor(available_cash * 0.05 / (max_loss * 100)).
    - Friday funding sweep into SPY / QQQ LEAPs (delta 0.70 DTE 365 call) when underlying is in bull regime.
    """
    sleeve_equity = engine.book.equity_curve.get(d, engine.book.cash)

    # 1. Spread Entry Replay for Trades Entering Today
    trades_today = raw_trades_by_entry_date.get(d, [])
    for raw in trades_today:
        symbol = str(raw.get("symbol", "SPY"))
        if symbol in current_prices and symbol in current_ivs:
            engine._execute_spread_entry(d, raw, current_prices, current_ivs)

    # 2. Existing LEAP Management (SPY & QQQ)
    for sym in ("SPY", "QQQ"):
        sym_legs = engine.book.legs.get(sym, [])
        for leap_leg in [l for l in sym_legs if l.role == "leap"]:
            if sym not in current_prices or sym not in current_ivs:
                continue
            S = current_prices[sym]
            iv = current_ivs[sym]
            regime = current_regimes.get(sym, "neutral")

            if regime == "bear":
                engine.pending_queue.append(
                    PendingOrder(order_type="LEAP_CLOSE", symbol=sym, reason="bear_regime_exit")
                )
            else:
                t_rem = max(0.0, (leap_leg.expiry - d).days / 365.0)
                curr_px = black_scholes_price(S, leap_leg.strike, t_rem, engine.r, iv, "call")
                if curr_px <= leap_leg.entry_price * 0.50:
                    engine.pending_queue.append(
                        PendingOrder(order_type="LEAP_CLOSE", symbol=sym, reason="leap_hard_stop_50pct")
                    )
                elif (leap_leg.expiry - d).days < 90:
                    engine.pending_queue.append(
                        PendingOrder(order_type="LEAP_ROLL", symbol=sym, reason="dte_under_90_roll")
                    )

    # 3. Weekly Friday Funding Sweep Decision (§2.3)
    if d.dayofweek == 4:  # Friday
        target_sym = None
        if current_regimes.get("SPY") == "bull":
            target_sym = "SPY"
        elif current_regimes.get("QQQ") == "bull":
            target_sym = "QQQ"

        if target_sym is not None and target_sym in current_prices and target_sym in current_ivs:
            S = current_prices[target_sym]
            iv = current_ivs[target_sym]
            t_years = 365.0 / 365.0
            est_strike = round_to_increment(
                strike_for_delta(S, t_years, engine.r, iv, 0.70, "call"),
                engine.strike_increment,
            )
            est_cost = black_scholes_price(S, est_strike, t_years, engine.r, iv, "call")
            cost_per_contract = est_cost * 100.0

            current_leap_cost = sum(
                l.entry_price * 100.0 * l.qty
                for l_list in engine.book.legs.values()
                for l in l_list
                if l.role == "leap"
            )
            if (
                cost_per_contract <= min(engine.book.premium_bank, engine.book.available_cash)
                and (current_leap_cost + cost_per_contract) <= 0.50 * sleeve_equity
            ):
                engine.pending_queue.append(
                    PendingOrder(
                        order_type="LEAP_BUY",
                        symbol=target_sym,
                        target_delta=0.70,
                        target_dte=365,
                        qty=1,
                        role="leap",
                        is_sweep=True,
                        estimated_debit=cost_per_contract,
                    )
                )
                engine.book.pending_debits += cost_per_contract


# ─────────────────────────────────────────────────────────────────────────────
# 7. High-Level Orchestrators & Reporting (§5.5, §7)
# ─────────────────────────────────────────────────────────────────────────────

def run_v1_pmcc(
    bars_by_symbol: Dict[str, pd.DataFrame],
    iv_by_symbol: Dict[str, pd.Series],
    regime_by_symbol: Dict[str, pd.Series],
    starting_cash: float = 30_000.0,
) -> LeapBacktestReport:
    """Run V1 PMCC Classic Backtest (§2.1)."""
    engine = LeapEngine(starting_cash=starting_cash)

    def decide_fn(eng, d, px, iv, reg):
        decide_v1_pmcc(eng, d, px, iv, reg)

    book = engine.run_simulation(bars_by_symbol, iv_by_symbol, regime_by_symbol, decide_fn)
    return _build_report("V1_PMCC_Classic", book, engine)


def run_v2_wheel(
    bars_by_symbol: Dict[str, pd.DataFrame],
    iv_by_symbol: Dict[str, pd.Series],
    regime_by_symbol: Dict[str, pd.Series],
    starting_cash: float = 30_000.0,
) -> LeapBacktestReport:
    """Run V2 Wheel + LEAP Sweep Backtest (§2.2)."""
    engine = LeapEngine(starting_cash=starting_cash)

    all_dates = sorted(
        list(set.intersection(*[set(df.index) for df in bars_by_symbol.values()]))
    )
    df_dates = pd.DataFrame({"date": all_dates})
    df_dates["month"] = [d.month for d in all_dates]
    df_dates["year"] = [d.year for d in all_dates]
    month_ends = set(df_dates.groupby(["year", "month"])["date"].last())

    def decide_fn(eng, d, px, iv, reg):
        decide_v2_wheel(eng, d, px, iv, reg, month_end_dates=month_ends)

    book = engine.run_simulation(bars_by_symbol, iv_by_symbol, regime_by_symbol, decide_fn)
    return _build_report("V2_Wheel_LEAP_Sweep", book, engine)


def run_v3_spread_financing(
    bars_by_symbol: Dict[str, pd.DataFrame],
    iv_by_symbol: Dict[str, pd.Series],
    regime_by_symbol: Dict[str, pd.Series],
    raw_trades: List[Dict[str, Any]],
    starting_cash: float = 30_000.0,
) -> LeapBacktestReport:
    """Run V3 Strategy 7 Spread Financing LEAP Ladder Backtest (§2.3)."""
    engine = LeapEngine(starting_cash=starting_cash)

    trades_by_entry: Dict[pd.Timestamp, List[Dict[str, Any]]] = {}
    for t in raw_trades:
        trades_by_entry.setdefault(pd.Timestamp(t["entry_date"]), []).append(t)

    def decide_fn(eng, d, px, iv, reg):
        decide_v3_spread_financing(eng, d, px, iv, reg, trades_by_entry)

    book = engine.run_simulation(bars_by_symbol, iv_by_symbol, regime_by_symbol, decide_fn)
    return _build_report("V3_Spread_Financing_Ladder", book, engine)


def _build_report(variant: str, book: SleeveBook, engine: LeapEngine) -> LeapBacktestReport:
    """Construct complete performance report including 7-day rolling statistics (§1, §5.5, §7)."""
    equity_series = pd.Series(book.equity_curve).sort_index()
    n_years = (
        (equity_series.index[-1] - equity_series.index[0]).days / 365.0
        if len(equity_series) > 1
        else 1.0
    )

    dollar_trades = [
        DollarTrade(
            entry_date=pd.Timestamp(r["entry_date"]),
            exit_date=pd.Timestamp(r["exit_date"]),
            contracts=float(r.get("detail", {}).get("qty", r.get("detail", {}).get("contracts", 1))),
            dollar_pnl=float(r["dollar_pnl"]),
            raw_trade=r,
        )
        for r in book.realized
        if r["kind"] in {"short_cycle", "leap_roll", "leap_close", "called_away", "share_stop"}
    ]

    metrics = calculate_metrics_from_dollar_trades(
        dollar_trades, equity_series, n_years, starting_equity=engine.starting_cash
    )
    if metrics is None:
        final_equity = float(equity_series.iloc[-1]) if len(equity_series) > 0 else engine.starting_cash
        if len(equity_series) > 0:
            running_max = np.maximum.accumulate(equity_series.values)
            max_drawdown = float((running_max - equity_series.values).max())
        else:
            max_drawdown = 0.0
        metrics = StrategyResult(
            name=variant,
            n_trades=0,
            total_pnl=0.0,
            win_rate=0.0,
            max_drawdown=max_drawdown,
            sharpe=0.0,
            calmar=0.0,
            profit_factor=0.0,
            final_equity=final_equity,
        )
    else:
        metrics.name = variant

    realized_df = pd.DataFrame([
        {"date": dt.exit_date, "pnl": dt.dollar_pnl} for dt in dollar_trades
    ])
    if not realized_df.empty:
        realized_df["date"] = pd.to_datetime(realized_df["date"]).dt.normalize()
        realized_index = pd.DatetimeIndex(realized_df["date"])
        equity_index = pd.DatetimeIndex(equity_series.index).normalize() if len(equity_series) else pd.DatetimeIndex([])
        start = min(realized_index.min(), equity_index.min()) if len(equity_index) else realized_index.min()
        end = max(realized_index.max(), equity_index.max()) if len(equity_index) else realized_index.max()
        realized_calendar = pd.date_range(start=start, end=end, freq="D")
        daily_realized = realized_df.groupby("date")["pnl"].sum().reindex(realized_calendar, fill_value=0.0)
        rolling_7d = daily_realized.rolling(7, min_periods=1).sum()
        rolling_stats = {
            "mean": float(rolling_7d.mean()),
            "median": float(rolling_7d.median()),
            "p10": float(rolling_7d.quantile(0.10)),
            "p90": float(rolling_7d.quantile(0.90)),
            "min": float(rolling_7d.min()),
            "max": float(rolling_7d.max()),
            "pos_rate": float((rolling_7d > 0).mean()),
        }
    else:
        rolling_7d = pd.Series(dtype=float)
        rolling_stats = {
            "mean": 0.0,
            "median": 0.0,
            "p10": 0.0,
            "p90": 0.0,
            "min": 0.0,
            "max": 0.0,
            "pos_rate": 0.0,
        }

    bank_series = pd.Series(engine.bank_history).sort_index()

    return LeapBacktestReport(
        variant=variant,
        metrics=metrics,
        equity_series=equity_series,
        dollar_trades=dollar_trades,
        realized_events=book.realized,
        rolling_7d_pnl=rolling_7d,
        rolling_7d_stats=rolling_stats,
        premium_bank_history=bank_series,
        assignment_count=engine.assignment_count,
        called_away_count=engine.called_away_count,
        stock_stop_count=engine.stock_stop_count,
        short_skip_count=engine.short_skip_count,
        leap_roll_count=engine.leap_roll_count,
        leap_sweep_count=engine.leap_sweep_count,
    )
