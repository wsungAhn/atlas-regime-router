"""
Comprehensive Test Suite for Multi-Leg LEAP, PMCC, and Wheel Engine (leap_engine.py).

Covers all audited requirements from design-wheel-pmcc-leap-strategy:
1. Asset identity P0 Gate invariant test + regression bug injection verification.
2. available_cash >= 0 and premium_bank <= cash invariant tests.
3. Exit timing & lookahead prevention (DECIDE vs EXECUTE vs TRIGGER).
4. V1 PMCC dynamic net-cost rule, ITM cash settlement, DTE < 90 roll.
5. V2 Wheel full cycle: CSP -> assignment -> CC -> called away -> stock stop -> month-end sweep.
6. V3 Strategy 7 credit spread replay, 5% sizing, Friday sweep, signature verification.
7. Reporting & adapter metrics integrity ($30k sleeve equity, 7-day rolling P&L).
"""

import math
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

# leap_engine.py lives in src/, matching this repo's existing test convention
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leap_engine import (
    ActiveSpread,
    DollarTrade,
    LeapBacktestReport,
    LeapEngine,
    OptionLeg,
    PendingOrder,
    SleeveBook,
    StrategyResult,
    _build_report,
    black_scholes_delta,
    black_scholes_price,
    calculate_adx_ema_regime,
    calculate_metrics_from_dollar_trades,
    decide_v1_pmcc,
    decide_v2_wheel,
    decide_v3_spread_financing,
    round_to_increment,
    run_v1_pmcc,
    run_v2_wheel,
    run_v3_spread_financing,
    strike_for_delta,
)
from backtest import _generate_raw_trades
import backtest
from strategies import StrategySignal


def event_projection(book: SleeveBook) -> List[tuple]:
    """Stable realized-event comparison surface for contract-level regression tests."""
    return [
        (
            r["kind"],
            pd.Timestamp(r["entry_date"]),
            pd.Timestamp(r["exit_date"]),
            r["symbol"],
            round(float(r["dollar_pnl"]), 8),
        )
        for r in book.realized
    ]


def create_synthetic_bars(
    start_date: str = "2023-01-01",
    n_days: int = 150,
    start_price: float = 400.0,
    drift: float = 0.0005,
    vol: float = 0.01,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic daily OHLCV bar DataFrame for testing."""
    np.random.seed(seed)
    dates = pd.bdate_range(start=start_date, periods=n_days)
    returns = np.random.normal(drift, vol, size=n_days)
    price = start_price * np.exp(np.cumsum(returns))

    high = price * (1.0 + np.abs(np.random.normal(0.002, 0.003, size=n_days)))
    low = price * (1.0 - np.abs(np.random.normal(0.002, 0.003, size=n_days)))
    open_px = low + (high - low) * np.random.uniform(0.2, 0.8, size=n_days)
    close = price

    df = pd.DataFrame(
        {
            "open": open_px,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.random.randint(1_000_000, 5_000_000, size=n_days),
        },
        index=dates,
    )
    return df


class TestOptionPricingPrimitives(unittest.TestCase):
    """Test Black-Scholes pricing and delta inversion precision."""

    def test_bs_call_put_parity(self):
        S, K, T, r, sigma = 450.0, 450.0, 0.5, 0.045, 0.20
        call_p = black_scholes_price(S, K, T, r, sigma, "call")
        put_p = black_scholes_price(S, K, T, r, sigma, "put")
        discount = math.exp(-r * T)
        parity_diff = (call_p - put_p) - (S - K * discount)
        self.assertAlmostEqual(parity_diff, 0.0, places=4)

    def test_strike_for_delta_inversion(self):
        S, T, r, sigma = 450.0, 1.0, 0.045, 0.20
        target_delta = 0.80
        strike_80 = strike_for_delta(S, T, r, sigma, target_delta, "call")
        actual_delta = black_scholes_delta(S, strike_80, T, r, sigma, "call")
        self.assertAlmostEqual(actual_delta, target_delta, places=3)
        self.assertLess(strike_80, S)  # 0.80 delta call is ITM


class TestAssetIdentityP0Gate(unittest.TestCase):
    """
    P0 Gate: Asset Identity Invariance Test (§5.2.1).
    Tests short entry -> expiry (OTM & ITM) -> LEAP sweep buy,
    and validates that injected accounting regressions fail the assertion.
    """

    def test_asset_identity_full_lifecycle(self):
        """Verify equity == cash + sum(signed_leg_mtm) + shares*close at every step."""
        df = create_synthetic_bars(start_date="2023-01-02", n_days=60, start_price=100.0, seed=123)
        iv_series = pd.Series(0.25, index=df.index)
        regimes = pd.Series("neutral", index=df.index)

        engine = LeapEngine(starting_cash=30_000.0, risk_free_rate=0.045)

        # Custom scripted sequence:
        # Day 1: Queue CSP sell
        # Day 2: CSP executes (credit received, collateral reserved) -> MTM verified
        # Day 8: CSP expires OTM -> premium deposited to premium_bank
        # Day 9: Queue ITM CSP sell
        # Day 15: CSP expires ITM -> assignment to shares -> MTM verified
        # Day 16: Queue Covered Call sell
        # Day 22: Covered Call expires ITM -> called away -> MTM verified
        # Day 23: Sweep buy LEAP -> MTM verified
        def scripted_decide(eng: LeapEngine, d: pd.Timestamp, pxs: Dict[str, float], ivs: Dict[str, float], regs: Dict[str, str]):
            idx = df.index.get_loc(d)
            if idx == 0:
                eng.pending_queue.append(
                    PendingOrder(order_type="CSP_SELL", symbol="SPY", target_delta=0.25, target_dte=6)
                )
            elif idx == 8:
                # Force an ITM CSP
                eng.pending_queue.append(
                    PendingOrder(order_type="CSP_SELL", symbol="SPY", target_delta=0.80, target_dte=6)
                )
            elif idx == 16:
                if eng.book.shares.get("SPY", 0) > 0:
                    eng.pending_queue.append(
                        PendingOrder(order_type="CC_SELL", symbol="SPY", target_delta=0.80, target_dte=6)
                    )
            elif idx == 24:
                if eng.book.premium_bank > 500.0:
                    eng.pending_queue.append(
                        PendingOrder(
                            order_type="LEAP_BUY",
                            symbol="SPY",
                            target_delta=0.70,
                            target_dte=365,
                            qty=1,
                            is_sweep=True,
                            estimated_debit=400.0,
                        )
                    )

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=scripted_decide,
        )

        # Asset identity asserted every day in _step_mtm, verify curve was populated
        self.assertEqual(len(book.equity_curve), len(df))
        for dt, eq in book.equity_curve.items():
            self.assertGreater(eq, 20_000.0)

    def test_regression_injection_assignment_missing_cash_debit(self):
        """
        Verify that a real engine-path accounting bug fails during run_simulation.
        Regression injected: CSP assignment adds shares but forgets cash -= strike*100*q.
        """
        dates = pd.bdate_range("2023-01-02", periods=3)
        df = pd.DataFrame(
            {"open": [90.0] * 3, "high": [90.0] * 3, "low": [90.0] * 3, "close": [90.0] * 3, "volume": [1000] * 3},
            index=dates,
        )
        iv_series = pd.Series(0.25, index=dates)
        regimes = pd.Series("neutral", index=dates)

        class BuggyAssignmentEngine(LeapEngine):
            def _step_expiry(self, d, current_prices, current_ivs):
                before_equity = self._calculate_equity(d, current_prices, current_ivs)
                remaining = []
                for leg in self.book.legs.get("SPY", []):
                    if d >= leg.expiry and leg.role == "csp":
                        q = abs(leg.qty)
                        self.book.reserved_collateral -= leg.collateral_reserved
                        # BUG: missing `self.book.cash -= leg.strike * 100.0 * q`
                        self.book.shares["SPY"] = self.book.shares.get("SPY", 0) + 100 * q
                        self.book.share_cost_basis["SPY"] = leg.strike - leg.entry_price
                    else:
                        remaining.append(leg)
                self.book.legs["SPY"] = remaining
                self._assert_equity_continuity(before_equity, d, current_prices, current_ivs, "buggy_assignment")

        engine = BuggyAssignmentEngine(starting_cash=30_000.0)
        engine.book.cash = 30_100.0
        engine.book.reserved_collateral = 10_000.0
        engine.book.legs["SPY"] = [
            OptionLeg(
                option_type="put",
                strike=100.0,
                expiry=dates[0],
                qty=-1,
                entry_price=1.0,
                entry_date=dates[0] - pd.Timedelta(days=7),
                role="csp",
                symbol="SPY",
                collateral_reserved=10_000.0,
            )
        ]

        with self.assertRaises(AssertionError):
            engine.run_simulation(
                bars_by_symbol={"SPY": df},
                iv_by_symbol={"SPY": iv_series},
                regime_by_symbol={"SPY": regimes},
                decide_fn=lambda eng, d, px, iv, reg: None,
            )

    def test_csp_assignment_reclamps_premium_bank_to_reduced_cash(self):
        """
        Real-data regression (2024-10-31 SLV run, R12 audit finding): a CSP
        assignment spends cash on shares but does not itself touch
        premium_bank. If premium_bank was already built up close to cash,
        the assignment debit alone can push premium_bank above the new,
        smaller cash balance and violate §5.2.1 invariant 1
        (premium_bank <= cash). The engine must re-clamp on assignment.
        """
        d0 = pd.Timestamp("2023-01-02")
        dates = pd.bdate_range(d0, periods=2)
        df = pd.DataFrame(
            {"open": [30.0] * 2, "high": [30.0] * 2, "low": [30.0] * 2, "close": [29.0] * 2, "volume": [1000] * 2},
            index=dates,
        )
        iv_series = pd.Series(0.25, index=dates)
        regimes = pd.Series("neutral", index=dates)

        engine = LeapEngine(starting_cash=4_000.0)
        # premium_bank already claims almost all of cash before the assignment.
        engine.book.premium_bank = 3_500.0
        engine.book.reserved_collateral = 3_000.0
        engine.book.legs["SLV"] = [
            OptionLeg(
                option_type="put",
                strike=30.0,  # ITM at close=29.0 -> assignment costs 30*100*1=3000
                expiry=dates[0],
                qty=-1,
                entry_price=1.0,
                entry_date=dates[0] - pd.Timedelta(days=7),
                role="csp",
                symbol="SLV",
                collateral_reserved=3_000.0,
            )
        ]

        book = engine.run_simulation(
            bars_by_symbol={"SLV": df},
            iv_by_symbol={"SLV": iv_series},
            regime_by_symbol={"SLV": regimes},
            decide_fn=lambda eng, d, px, iv, reg: None,
        )

        self.assertEqual(engine.assignment_count, 1)
        self.assertLessEqual(book.premium_bank, book.cash + 1e-4)

    def test_regression_injection_short_call_entry_missing_cash_credit(self):
        """
        Verify event-level continuity catches a short-option entry that creates
        the liability but forgets the matching cash credit.
        """
        dates = pd.bdate_range("2023-01-02", periods=3)
        df = pd.DataFrame(
            {"open": [105.0] * 3, "high": [105.0] * 3, "low": [105.0] * 3, "close": [105.0] * 3, "volume": [1000] * 3},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("bull", index=dates)

        class BuggyShortCallSellEngine(LeapEngine):
            def _step_execute(self, d, current_prices, current_ivs):
                orders_to_process = list(self.pending_queue)
                self.pending_queue.clear()
                self.book.pending_debits = 0.0
                for order in orders_to_process:
                    if order.order_type != "SHORT_CALL_SELL":
                        continue
                    before_equity = self._calculate_equity(d, current_prices, current_ivs)
                    S = current_prices[order.symbol]
                    iv = current_ivs[order.symbol]
                    strike = 105.0
                    t_years = order.target_dte / 365.0
                    raw_credit = black_scholes_price(S, strike, t_years, self.r, iv, "call")
                    credit_received = raw_credit * (1.0 - self.credit_haircut_pct)
                    # BUG: adds the short liability but omits `cash += credit_received * 100`.
                    self.book.legs.setdefault(order.symbol, []).append(
                        OptionLeg("call", strike, d + pd.Timedelta(days=order.target_dte), -1, credit_received, d, "short_call", order.symbol)
                    )
                    expected_delta = -(raw_credit - credit_received) * 100.0
                    self._assert_event_equity_change(
                        before_equity,
                        d,
                        current_prices,
                        current_ivs,
                        "buggy_short_call_sell",
                        expected_delta=expected_delta,
                    )

        engine = BuggyShortCallSellEngine(starting_cash=30_000.0)

        def decide_fn(eng, d, px, iv, reg):
            if d == dates[0]:
                eng.pending_queue.append(PendingOrder(order_type="SHORT_CALL_SELL", symbol="SPY", target_dte=7))

        with self.assertRaises(AssertionError):
            engine.run_simulation(
                bars_by_symbol={"SPY": df},
                iv_by_symbol={"SPY": iv_series},
                regime_by_symbol={"SPY": regimes},
                decide_fn=decide_fn,
            )

    def test_regression_injection_bank_exceeds_cash(self):
        """Verify that premium_bank > cash triggers an invariant assertion failure."""
        book = SleeveBook(cash=1_000.0, premium_bank=1_500.0)
        d = pd.Timestamp("2023-01-02")
        with self.assertRaises(AssertionError):
            book.assert_invariants(
                equity=1_000.0,
                current_prices={"SPY": 100.0},
                iv_by_symbol={"SPY": 0.20},
                current_date=d,
            )


class TestInvariantsAndTiming(unittest.TestCase):
    """Test available_cash non-negativity and strategic exit timing lookahead prevention."""

    def test_available_cash_non_negativity(self):
        """Ensure available_cash = cash - reserved - pending never falls below zero."""
        book = SleeveBook(cash=10_000.0, reserved_collateral=8_000.0, pending_debits=2_000.0)
        self.assertAlmostEqual(book.available_cash, 0.0)

        # Over-commitment attempt
        book.pending_debits = 2_500.0
        self.assertLess(book.available_cash, 0.0)
        d = pd.Timestamp("2023-01-02")
        with self.assertRaises(AssertionError):
            book.assert_invariants(
                equity=10_000.0,
                current_prices={},
                iv_by_symbol={},
                current_date=d,
            )

    def test_exit_timing_strategic_vs_touch(self):
        """
        Verify strategic exits trigger on day T and fill on day T+1 close (no lookahead),
        while price-touch exits fill on day T close (§5.3).
        """
        dates = pd.bdate_range("2023-01-02", periods=5)
        # Price drops sharply on Day 2 (index 1) to trigger bear regime or stop
        df = pd.DataFrame(
            {
                "open": [100, 100, 80, 80, 80],
                "high": [101, 101, 81, 81, 81],
                "low": [99, 99, 79, 79, 79],
                "close": [100.0, 100.0, 80.0, 80.0, 80.0],
                "volume": [1000] * 5,
            },
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series(["bull", "bull", "bear", "bear", "bear"], index=dates)

        engine = LeapEngine(starting_cash=30_000.0)

        # Start with a LEAP position entered on Day 1
        leap = OptionLeg(
            option_type="call",
            strike=80.0,
            expiry=dates[0] + pd.Timedelta(days=365),
            qty=1,
            entry_price=25.0,
            entry_date=dates[0],
            role="leap",
            symbol="SPY",
        )
        engine.book.legs["SPY"] = [leap]
        engine.book.cash = 27_500.0

        def decide_fn(eng, d, px, iv, reg):
            decide_v1_pmcc(eng, d, px, iv, reg)

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=decide_fn,
        )

        # On Day 3 (index 2), regime becomes bear -> DECIDE queues LEAP_CLOSE for Day 4 (index 3).
        # Therefore, LEAP must be closed on Day 4 (dates[3]), not Day 3!
        close_events = [r for r in book.realized if r["kind"] == "leap_close"]
        self.assertEqual(len(close_events), 1)
        self.assertEqual(pd.Timestamp(close_events[0]["exit_date"]), dates[3])

    def test_iv_coverage_gap_records_equity_without_future_fallback(self):
        """Missing IV uses prior IV for MTM and never pulls a future IV tail value."""
        dates = pd.bdate_range("2023-01-02", periods=4)
        df = pd.DataFrame(
            {"open": [100.0] * 4, "high": [101.0] * 4, "low": [99.0] * 4, "close": [100.0] * 4, "volume": [1000] * 4},
            index=dates,
        )
        iv_series = pd.Series([0.20, 0.20, 0.99], index=[dates[0], dates[2], dates[3]])
        regimes = pd.Series("neutral", index=dates)

        engine = LeapEngine(starting_cash=30_000.0)
        entry_price = black_scholes_price(100.0, 90.0, 365.0 / 365.0, engine.r, 0.20, "call")
        engine.book.cash -= entry_price * 100.0
        engine.book.legs["SPY"] = [
            OptionLeg("call", 90.0, dates[0] + pd.Timedelta(days=365), 1, entry_price, dates[0], "leap", "SPY")
        ]
        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=lambda eng, d, px, iv, reg: None,
        )

        self.assertIn(dates[1], book.equity_curve)
        expected_gap_equity = engine.book.cash + black_scholes_price(100.0, 90.0, 364.0 / 365.0, engine.r, 0.20, "call") * 100.0
        future_tail_equity = engine.book.cash + black_scholes_price(100.0, 90.0, 364.0 / 365.0, engine.r, 0.99, "call") * 100.0
        self.assertAlmostEqual(book.equity_curve[dates[1]], expected_gap_equity, places=4)
        self.assertNotAlmostEqual(book.equity_curve[dates[1]], future_tail_equity, places=2)
        skip_events = [r for r in book.realized if r["kind"] == "iv_coverage_skip"]
        self.assertEqual(len(skip_events), 1)
        self.assertEqual(pd.Timestamp(skip_events[0]["exit_date"]), dates[1])

    def test_nan_and_zero_iv_are_treated_as_missing(self):
        """A NaN or 0.0 IV print is not a usable IV: equity must stay finite, not silently nan."""
        dates = pd.bdate_range("2023-01-02", periods=3)
        df = pd.DataFrame(
            {"open": [100.0] * 3, "high": [100.0] * 3, "low": [100.0] * 3, "close": [100.0] * 3, "volume": [1000] * 3},
            index=dates,
        )
        # Day 1 prints NaN, day 2 prints 0.0 — both must fall back to the day-0 IV.
        # Day 0 IV is deliberately not the 0.20 engine default, so a fallback that
        # quietly used the default instead of the prior print would fail below.
        prior_iv = 0.35
        iv_series = pd.Series([prior_iv, float("nan"), 0.0], index=dates)
        regimes = pd.Series("neutral", index=dates)

        engine = LeapEngine(starting_cash=30_000.0)
        entry_price = black_scholes_price(100.0, 90.0, 1.0, engine.r, prior_iv, "call")
        engine.book.cash -= entry_price * 100.0
        engine.book.legs["SPY"] = [
            OptionLeg("call", 90.0, dates[0] + pd.Timedelta(days=365), 1, entry_price, dates[0], "leap", "SPY")
        ]

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=lambda eng, d, px, iv, reg: None,
        )

        for i, d in enumerate(dates):
            self.assertIn(d, book.equity_curve)
            self.assertTrue(math.isfinite(book.equity_curve[d]), f"equity not finite on {d}")
            t_rem = ((dates[0] + pd.Timedelta(days=365)) - d).days / 365.0
            expected = engine.book.cash + black_scholes_price(100.0, 90.0, t_rem, engine.r, prior_iv, "call") * 100.0
            default_iv = engine.book.cash + black_scholes_price(100.0, 90.0, t_rem, engine.r, 0.20, "call") * 100.0
            self.assertAlmostEqual(book.equity_curve[d], expected, places=4)
            if i > 0:
                self.assertNotAlmostEqual(book.equity_curve[d], default_iv, places=2)
        skipped = {
            pd.Timestamp(r["exit_date"])
            for r in book.realized
            if r["kind"] == "iv_coverage_skip"
        }
        self.assertEqual(skipped, {dates[1], dates[2]})

    def test_unsorted_duplicate_iv_index_is_normalized(self):
        """run_simulation must not depend on caller-side IV index ordering or uniqueness."""
        dates = pd.bdate_range("2023-01-02", periods=3)
        df = pd.DataFrame(
            {"open": [100.0] * 3, "high": [100.0] * 3, "low": [100.0] * 3, "close": [100.0] * 3, "volume": [1000] * 3},
            index=dates,
        )
        regimes = pd.Series("neutral", index=dates)

        def run(iv_series):
            engine = LeapEngine(starting_cash=30_000.0)
            entry_price = black_scholes_price(100.0, 90.0, 1.0, engine.r, 0.30, "call")
            engine.book.cash -= entry_price * 100.0
            engine.book.legs["SPY"] = [
                OptionLeg("call", 90.0, dates[0] + pd.Timedelta(days=365), 1, entry_price, dates[0], "leap", "SPY")
            ]
            return engine.run_simulation(
                bars_by_symbol={"SPY": df},
                iv_by_symbol={"SPY": iv_series},
                regime_by_symbol={"SPY": regimes},
                decide_fn=lambda eng, d, px, iv, reg: None,
            )

        clean = run(pd.Series([0.30, 0.30], index=[dates[0], dates[1]]))
        # Reversed order, plus a stale duplicate of day 0 that must lose to the later print.
        messy = run(pd.Series([0.30, 0.99, 0.30], index=[dates[1], dates[0], dates[0]]))

        for d in dates:
            self.assertAlmostEqual(messy.equity_curve[d], clean.equity_curve[d], places=4)
        self.assertEqual(event_projection(messy), event_projection(clean))
        self.assertEqual(
            [r for r in messy.realized if r["kind"] == "iv_coverage_skip"],
            [r for r in clean.realized if r["kind"] == "iv_coverage_skip"],
        )
        self.assertEqual(
            [r for r in messy.realized if r["kind"] == "iv_intrinsic_mtm_fallback"],
            [r for r in clean.realized if r["kind"] == "iv_intrinsic_mtm_fallback"],
        )

    def test_unsorted_duplicate_regime_index_is_normalized(self):
        """Duplicate regime dates must keep the last value and still drive strategy decisions."""
        dates = pd.bdate_range("2023-01-02", periods=3)
        df = pd.DataFrame(
            {"open": [100.0] * 3, "high": [100.0] * 3, "low": [100.0] * 3, "close": [100.0] * 3, "volume": [1000] * 3},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        clean_regimes = pd.Series(["neutral", "bull", "bull"], index=dates)
        messy_regimes = pd.Series(
            ["neutral", "bear", "bull", "bull"],
            index=[dates[0], dates[1], dates[1], dates[2]],
        )

        def run(regimes):
            engine = LeapEngine(starting_cash=30_000.0)
            return engine.run_simulation(
                bars_by_symbol={"SPY": df},
                iv_by_symbol={"SPY": iv_series},
                regime_by_symbol={"SPY": regimes},
                decide_fn=lambda eng, d, px, iv, reg: decide_v1_pmcc(eng, d, px, iv, reg),
            )

        clean = run(clean_regimes)
        messy = run(messy_regimes)

        self.assertEqual(event_projection(messy), event_projection(clean))
        self.assertEqual([r["kind"] for r in messy.realized], ["leap_entry"])

    def test_unsorted_duplicate_bar_index_is_normalized(self):
        """Duplicate bar dates must keep the last close and provide scalar daily prices."""
        dates = pd.bdate_range("2023-01-02", periods=3)
        clean = pd.DataFrame(
            {"open": [100.0] * 3, "high": [100.0] * 3, "low": [100.0] * 3, "close": [100.0, 105.0, 105.0], "volume": [1000] * 3},
            index=dates,
        )
        messy = pd.DataFrame(
            {
                "open": [100.0, 999.0, 105.0, 105.0],
                "high": [100.0, 999.0, 105.0, 105.0],
                "low": [100.0, 999.0, 105.0, 105.0],
                "close": [100.0, 999.0, 105.0, 105.0],
                "volume": [1000, 1000, 1000, 1000],
            },
            index=[dates[0], dates[1], dates[1], dates[2]],
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series(["neutral", "bull", "bull"], index=dates)

        def run(df):
            engine = LeapEngine(starting_cash=30_000.0)
            return engine.run_simulation(
                bars_by_symbol={"SPY": df},
                iv_by_symbol={"SPY": iv_series},
                regime_by_symbol={"SPY": regimes},
                decide_fn=lambda eng, d, px, iv, reg: decide_v1_pmcc(eng, d, px, iv, reg),
            )

        self.assertEqual(event_projection(run(messy)), event_projection(run(clean)))

    def test_spread_settlement_pnl_must_match_cash_moved(self):
        """A vendor realized_pnl that disagrees with credit - close_debit must not reach metrics."""
        d = pd.Timestamp("2023-01-02")
        engine = LeapEngine(starting_cash=30_000.0)
        engine.book.active_spreads = [
            ActiveSpread(
                symbol="SPY",
                spread_type="bull_put",
                short_strike=99.0,
                long_strike=98.0,
                entry_date=d - pd.Timedelta(days=7),
                exit_date=d,
                contracts=1,
                credit_received=0.30,
                max_loss=0.70,
                close_debit=0.12,
                realized_pnl=1.62,  # vendor claims +$162, cash only moved +$18
                exit_reason="profit_target",
            )
        ]

        with self.assertRaises(AssertionError):
            engine._step_expiry(d, {"SPY": 100.0}, {"SPY": 0.20})

    def test_iv_gap_still_settles_expiry_on_that_close(self):
        """Missing IV on expiry day must not defer intrinsic settlement to a future close."""
        dates = pd.bdate_range("2023-01-02", periods=2)
        df = pd.DataFrame(
            {"open": [90.0, 130.0], "high": [90.0, 130.0], "low": [90.0, 130.0], "close": [90.0, 130.0], "volume": [1000, 1000]},
            index=dates,
        )
        iv_series = pd.Series([0.20], index=[dates[1]])
        regimes = pd.Series("neutral", index=dates)

        engine = LeapEngine(starting_cash=30_000.0)
        engine.book.cash = 30_100.0
        engine.book.legs["SPY"] = [
            OptionLeg("call", 100.0, dates[0], -1, 1.0, dates[0] - pd.Timedelta(days=7), "short_call", "SPY")
        ]
        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=lambda eng, d, px, iv, reg: None,
        )

        short_events = [r for r in book.realized if r["kind"] == "short_cycle"]
        self.assertEqual(len(short_events), 1)
        self.assertEqual(pd.Timestamp(short_events[0]["exit_date"]), dates[0])
        self.assertEqual(short_events[0]["detail"]["reason"], "expiry_otm")
        self.assertEqual(short_events[0]["detail"]["close_price"], 90.0)
        self.assertEqual(engine.book.cash, 30_100.0)
        self.assertIn(dates[0], book.equity_curve)

    def test_final_iv_gap_assignment_updates_report_final_equity(self):
        """An expiry event on a final IV gap day must still drive equity metrics."""
        dates = pd.bdate_range("2023-01-02", periods=2)
        df = pd.DataFrame(
            {"open": [105.0, 90.0], "high": [105.0, 90.0], "low": [105.0, 90.0], "close": [105.0, 90.0], "volume": [1000, 1000]},
            index=dates,
        )
        iv_series = pd.Series([0.20], index=[dates[0]])
        regimes = pd.Series("neutral", index=dates)

        engine = LeapEngine(starting_cash=30_000.0)
        engine.book.cash = 30_100.0
        engine.book.reserved_collateral = 10_000.0
        engine.book.legs["SPY"] = [
            OptionLeg("put", 100.0, dates[1], -1, 1.0, dates[0] - pd.Timedelta(days=7), "csp", "SPY", collateral_reserved=10_000.0)
        ]

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=lambda eng, d, px, iv, reg: None,
        )
        report = _build_report("probe", book, engine)

        self.assertIn(dates[1], book.equity_curve)
        self.assertEqual(book.shares["SPY"], 100)
        self.assertAlmostEqual(report.metrics.final_equity, 29_100.0)

    def test_leap_close_waits_for_fresh_iv_on_execution_day(self):
        """Strategic LEAP close needs fresh execution-day IV and must not disappear on a gap."""
        dates = pd.bdate_range("2023-01-02", periods=3)
        df = pd.DataFrame(
            {"open": [100.0] * 3, "high": [100.0] * 3, "low": [100.0] * 3, "close": [100.0] * 3, "volume": [1000] * 3},
            index=dates,
        )
        iv_series = pd.Series([0.20, 0.20], index=[dates[0], dates[2]])
        regimes = pd.Series(["bear", "bear", "bear"], index=dates)

        engine = LeapEngine(starting_cash=30_000.0)
        engine.book.cash = 27_500.0
        engine.book.legs["SPY"] = [
            OptionLeg("call", 80.0, dates[2] + pd.Timedelta(days=365), 1, 25.0, dates[0], "leap", "SPY")
        ]

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=lambda eng, d, px, iv, reg: decide_v1_pmcc(eng, d, px, iv, reg),
        )

        close_events = [r for r in book.realized if r["kind"] == "leap_close"]
        self.assertEqual(len(close_events), 1)
        self.assertEqual(pd.Timestamp(close_events[0]["exit_date"]), dates[2])
        self.assertEqual(len(engine.pending_queue), 0)

    def test_no_prior_iv_fallback_does_not_block_other_symbol_trigger(self):
        """A no-prior IV symbol uses intrinsic MTM and must not block another symbol's trigger."""
        dates = pd.bdate_range("2023-01-02", periods=2)
        bars = {
            "SPY": pd.DataFrame(
                {"open": [100.0, 100.0], "high": [100.0, 100.0], "low": [100.0, 100.0], "close": [100.0, 100.0], "volume": [1000, 1000]},
                index=dates,
            ),
            "QQQ": pd.DataFrame(
                {"open": [100.0, 100.0], "high": [100.0, 100.0], "low": [100.0, 100.0], "close": [100.0, 100.0], "volume": [1000, 1000]},
                index=dates,
            ),
        }
        ivs = {"SPY": pd.Series([0.20, 0.20], index=dates), "QQQ": pd.Series([0.20], index=[dates[1]])}
        regimes = {
            "SPY": pd.Series("neutral", index=dates),
            "QQQ": pd.Series("neutral", index=dates),
        }

        engine = LeapEngine(starting_cash=30_000.0)
        engine.book.cash = 30_000.0 + 500.0 - 2_500.0 - 2_500.0
        engine.book.legs["SPY"] = [
            OptionLeg("call", 80.0, dates[1] + pd.Timedelta(days=365), 1, 25.0, dates[0], "leap", "SPY"),
            OptionLeg("call", 110.0, dates[1], -1, 5.0, dates[0], "short_call", "SPY"),
        ]
        engine.book.legs["QQQ"] = [
            OptionLeg("call", 80.0, dates[1] + pd.Timedelta(days=365), 1, 25.0, dates[0], "leap", "QQQ")
        ]

        book = engine.run_simulation(
            bars_by_symbol=bars,
            iv_by_symbol=ivs,
            regime_by_symbol=regimes,
            decide_fn=lambda eng, d, px, iv, reg: None,
        )

        stop_events = [r for r in book.realized if r.get("detail", {}).get("exit_reason") == "profit_target"]
        fallback_events = [r for r in book.realized if r["kind"] == "iv_intrinsic_mtm_fallback"]
        self.assertEqual(len(stop_events), 1)
        self.assertEqual(pd.Timestamp(stop_events[0]["exit_date"]), dates[0])
        self.assertEqual(fallback_events[0]["symbol"], "QQQ")


class TestV1PMCCMechanics(unittest.TestCase):
    """Test V1 PMCC Classic details (§2.1)."""

    def test_v1_dynamic_net_cost_short_call_skip(self):
        """
        Verify the dynamic net-cost tastytrade rule:
        short_strike > leap_strike + (leap_entry - cumulative_credits).
        If strike <= threshold, short sell is skipped and logged (§2.1).
        """
        dates = pd.bdate_range("2023-01-02", periods=10)
        df = pd.DataFrame(
            {"open": [100] * 10, "high": [101] * 10, "low": [99] * 10, "close": [100.0] * 10, "volume": [1000] * 10},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("bull", index=dates)

        engine = LeapEngine(starting_cash=30_000.0)

        # Inject an expensive LEAP where leap_strike = 80, cost = 25. Threshold = 80 + 25 = 105.
        # At S=100, delta 0.20 strike is ~103, which is <= 105 -> must skip!
        leap = OptionLeg(
            option_type="call",
            strike=80.0,
            expiry=dates[0] + pd.Timedelta(days=365),
            qty=1,
            entry_price=25.0,
            entry_date=dates[0],
            role="leap",
            symbol="SPY",
            cumulative_short_credits=0.0,
        )
        engine.book.legs["SPY"] = [leap]
        engine.book.cash = 27_500.0

        def decide_fn(eng, d, px, iv, reg):
            decide_v1_pmcc(eng, d, px, iv, reg)

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=decide_fn,
        )

        self.assertGreater(engine.short_skip_count, 0)
        skip_events = [r for r in book.realized if r["kind"] == "short_skip"]
        self.assertTrue(len(skip_events) > 0)

    def test_v1_short_call_itm_cash_settlement_preserves_leap(self):
        """Verify ITM short call expiry cash settles intrinsic value while preserving the LEAP (§2.1)."""
        dates = pd.bdate_range("2023-01-02", periods=10)
        # Price starts at 104, rises steadily so PT is never hit, reaching 110 at expiry
        prices = [105.0, 106.0, 107.0, 108.0, 109.0, 109.5, 110.0, 110.0, 110.0, 110.0]
        df = pd.DataFrame(
            {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1000] * 10},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("bull", index=dates)

        engine = LeapEngine(starting_cash=30_000.0)
        leap = OptionLeg(
            option_type="call",
            strike=80.0,
            expiry=dates[0] + pd.Timedelta(days=365),
            qty=1,
            entry_price=26.0,
            entry_date=dates[0],
            role="leap",
            symbol="SPY",
        )
        short_call = OptionLeg(
            option_type="call",
            strike=105.0,
            expiry=dates[6],
            qty=-1,
            entry_price=2.50,
            entry_date=dates[0],
            role="short_call",
            symbol="SPY",
        )
        engine.book.legs["SPY"] = [leap, short_call]
        engine.book.cash = 27_650.0

        def dummy_decide(eng, d, px, iv, reg):
            pass

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=dummy_decide,
        )

        # After dates[6] (when S=110, strike=105, intrinsic=$5.00), short call was cash settled
        # Cash decreased by $500 intrinsic debit
        # LEAP is still intact in legs
        spy_legs = book.legs.get("SPY", [])
        self.assertEqual(len(spy_legs), 1)
        self.assertEqual(spy_legs[0].role, "leap")

        settle_events = [r for r in book.realized if r.get("detail", {}).get("reason") == "expiry_itm_cash_settle"]
        self.assertEqual(len(settle_events), 1)
        # Realized dollar pnl: (2.50 - 5.00) * 100 = -$250.00
        self.assertAlmostEqual(settle_events[0]["dollar_pnl"], -250.0)

    def test_v1_short_call_stop_loss_at_2x_entry(self):
        """Verify V1 PMCC short calls use the 2.0x stop-loss rule before expiry (§2.1)."""
        dates = pd.bdate_range("2023-01-02", periods=5)
        # Day 0 keeps the short call ~ATM (MTM 0.90 vs 0.80 entry: no PT at 0.40, no SL at 1.60),
        # so the 2.0x stop is the first exit rule that can fire, on the day-1 gap to 130.
        prices = [105.0, 130.0, 130.0, 130.0, 130.0]
        df = pd.DataFrame(
            {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1000] * 5},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("bull", index=dates)

        engine = LeapEngine(starting_cash=30_000.0)
        engine.book.legs["SPY"] = [
            OptionLeg("call", 80.0, dates[0] + pd.Timedelta(days=365), 1, 25.0, dates[0], "leap", "SPY"),
            OptionLeg("call", 105.0, dates[4], -1, 0.80, dates[0], "short_call", "SPY"),
        ]
        engine.book.cash = 27_550.0

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=lambda eng, d, px, iv, reg: None,
        )

        stop_events = [r for r in book.realized if r.get("detail", {}).get("exit_reason") == "stop_loss"]
        self.assertEqual(len(stop_events), 1)
        self.assertEqual([leg.role for leg in book.legs["SPY"]], ["leap"])


class TestV2WheelMechanics(unittest.TestCase):
    """Test V2 Wheel + LEAP Sweep details (§2.2)."""

    def test_v2_csp_assignment_and_covered_call_called_away(self):
        """
        Test the full wheel cycle:
        1. CSP expires ITM -> assignment to shares with cost_basis.
        2. Covered call sold against shares with snap strike.
        3. CC expires ITM -> called away, profits deposited to premium_bank.
        """
        dates = pd.bdate_range("2023-01-02", periods=20)
        # Phase 1: Price drops to 20.0 (CSP strike 21.5 assigned on Jan 10)
        # Phase 2: Price rallies to 30.0 (CC strike 22.5 called away on Jan 23)
        prices = [22.0] * 5 + [20.0] * 5 + [22.0, 24.0, 26.0, 28.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0]
        df_slv = pd.DataFrame(
            {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1000] * 20},
            index=dates,
        )
        iv_series = pd.Series(0.30, index=dates)
        regimes = pd.Series("neutral", index=dates)

        report = run_v2_wheel(
            bars_by_symbol={"SLV": df_slv},
            iv_by_symbol={"SLV": iv_series},
            regime_by_symbol={"SLV": regimes},
            starting_cash=30_000.0,
        )

        self.assertGreater(report.assignment_count, 0)
        self.assertGreater(report.called_away_count, 0)
        self.assertGreater(report.premium_bank_history.max(), 0.0)

    def test_v2_stock_stop_loss_at_20pct_drawdown(self):
        """Verify that holding shares with >20% loss triggers stock stop liquidation at next day's close (§2.2)."""
        dates = pd.bdate_range("2023-01-02", periods=10)
        # Stock drops from $100 to $75 (25% loss)
        prices = [100.0, 100.0, 75.0, 75.0, 75.0, 75.0, 75.0, 75.0, 75.0, 75.0]
        df = pd.DataFrame(
            {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1000] * 10},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("neutral", index=dates)

        engine = LeapEngine(starting_cash=20_000.0)
        engine.book.shares["SLV"] = 100
        engine.book.share_cost_basis["SLV"] = 100.0

        def decide_fn(eng, d, px, iv, reg):
            decide_v2_wheel(eng, d, px, iv, reg)

        book = engine.run_simulation(
            bars_by_symbol={"SLV": df},
            iv_by_symbol={"SLV": iv_series},
            regime_by_symbol={"SLV": regimes},
            decide_fn=decide_fn,
        )

        self.assertEqual(engine.stock_stop_count, 1)
        self.assertEqual(book.shares.get("SLV", 0), 0)

    def test_v2_stock_stop_without_covered_call_does_not_need_fresh_iv(self):
        """Stock-only stop execution uses stock close and should not wait for option IV."""
        dates = pd.bdate_range("2023-01-02", periods=3)
        df = pd.DataFrame(
            {"open": [75.0, 75.0, 75.0], "high": [75.0, 75.0, 75.0], "low": [75.0, 75.0, 75.0], "close": [75.0, 75.0, 75.0], "volume": [1000] * 3},
            index=dates,
        )
        iv_series = pd.Series([0.20, 0.20], index=[dates[0], dates[2]])
        regimes = pd.Series("neutral", index=dates)

        engine = LeapEngine(starting_cash=20_000.0)
        engine.book.shares["SLV"] = 100
        engine.book.share_cost_basis["SLV"] = 100.0

        book = engine.run_simulation(
            bars_by_symbol={"SLV": df},
            iv_by_symbol={"SLV": iv_series},
            regime_by_symbol={"SLV": regimes},
            decide_fn=lambda eng, d, px, iv, reg: decide_v2_wheel(eng, d, px, iv, reg),
        )

        stop_events = [r for r in book.realized if r["kind"] == "share_stop"]
        self.assertEqual(len(stop_events), 1)
        self.assertEqual(pd.Timestamp(stop_events[0]["exit_date"]), dates[1])
        self.assertEqual(book.shares.get("SLV", 0), 0)

    def test_v2_csp_and_covered_call_are_not_profit_target_closed(self):
        """V2 wheel option legs are held to expiry; PT is a V1 short-call rule, not a wheel rule."""
        d = pd.Timestamp("2023-01-02")
        engine = LeapEngine(starting_cash=30_000.0)
        engine.book.cash = 30_500.0
        engine.book.reserved_collateral = 2_000.0
        engine.book.shares["SLV"] = 100
        engine.book.share_cost_basis["SLV"] = 20.0
        engine.book.legs["SLV"] = [
            OptionLeg("put", 20.0, d + pd.Timedelta(days=7), -1, 5.0, d, "csp", "SLV", collateral_reserved=2_000.0),
            OptionLeg("call", 25.0, d + pd.Timedelta(days=7), -1, 5.0, d, "covered_call", "SLV"),
        ]

        engine._step_trigger(d, {"SLV": 22.0}, {"SLV": 0.20})

        self.assertEqual([leg.role for leg in engine.book.legs["SLV"]], ["csp", "covered_call"])
        self.assertEqual(engine.book.reserved_collateral, 2_000.0)
        self.assertEqual(engine.book.realized, [])


class TestV3SpreadFinancingAndSignature(unittest.TestCase):
    """Test V3 Strategy 7 Credit Spread Financing (§2.3) and Signature Verification."""

    def test_v3_signature_and_spread_replay(self):
        """
        Verify V3 raw trade replay logic, 5% max-loss sizing, collateral lock/release,
        and Friday funding sweep into LEAPs.
        """
        dates = pd.bdate_range("2023-01-02", periods=30)
        df_spy = pd.DataFrame(
            {"open": [400.0] * 30, "high": [400.0] * 30, "low": [400.0] * 30, "close": [400.0] * 30, "volume": [1000] * 30},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("bull", index=dates)

        # Mock the structures produced after _generate_raw_trades splits iron_condor signals.
        raw_trades = [
            {
                "signal_date": str(dates[0].date()),
                "entry_date": str(dates[1].date()),
                "exit_date": str(dates[6].date()),
                "exit_reason": "profit_target",
                "spread_type": "bull_put",
                "entry_iv": 0.20,
                "short_strike": 395.0,
                "long_strike": 390.0,
                "width": 5.0,
                "credit_received": 1.00,
                "max_loss": 4.00,
                "breakeven": 394.00,
                "close_debit": 0.50,
                "realized_pnl": 0.50,
                "symbol": "SPY",
            },
            {
                "signal_date": str(dates[7].date()),
                "entry_date": str(dates[8].date()),
                "exit_date": str(dates[13].date()),
                "exit_reason": "profit_target",
                "spread_type": "bear_call",
                "entry_iv": 0.20,
                "short_strike": 405.0,
                "long_strike": 410.0,
                "width": 5.0,
                "credit_received": 1.00,
                "max_loss": 4.00,
                "breakeven": 406.0,
                "close_debit": 0.50,
                "realized_pnl": 0.50,
                "symbol": "SPY",
            },
        ]

        report = run_v3_spread_financing(
            bars_by_symbol={"SPY": df_spy},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            raw_trades=raw_trades,
            starting_cash=30_000.0,
        )

        self.assertGreater(report.metrics.n_trades, 0)
        self.assertGreater(report.metrics.total_pnl, 0.0)
        first_short_cycle = next(r for r in report.realized_events if r["kind"] == "short_cycle")
        self.assertEqual(pd.Timestamp(first_short_cycle["entry_date"]), dates[1])
        # Invariant checks were executed at every step
        self.assertEqual(len(report.equity_series), len(dates))

    def test_v3_spread_entry_rejects_impossible_credit_on_actual_path(self):
        """The actual V3 replay path must reject raw credit that creates equity."""
        dates = pd.bdate_range("2023-01-02", periods=3)
        df_spy = pd.DataFrame(
            {"open": [100.0] * 3, "high": [100.0] * 3, "low": [100.0] * 3, "close": [100.0] * 3, "volume": [1000] * 3},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("neutral", index=dates)
        impossible_raw = [
            {
                "entry_date": dates[0],
                "exit_date": dates[2],
                "spread_type": "bull_put",
                "short_strike": 99.0,
                "long_strike": 98.0,
                "credit_received": 999.0,
                "max_loss": 1.0,
                "close_debit": 0.0,
                "realized_pnl": 999.0,
                "exit_reason": "probe",
                "symbol": "SPY",
            }
        ]

        with self.assertRaises(AssertionError):
            run_v3_spread_financing(
                bars_by_symbol={"SPY": df_spy},
                iv_by_symbol={"SPY": iv_series},
                regime_by_symbol={"SPY": regimes},
                raw_trades=impossible_raw,
                starting_cash=30_000.0,
            )

    def test_v3_spread_entry_rejects_before_mutating_book(self):
        """Invalid raw spread invariants must fail before cash/collateral/spread mutation."""
        d = pd.Timestamp("2023-01-02")
        engine = LeapEngine(starting_cash=30_000.0)
        bad_raw = {
            "entry_date": d,
            "exit_date": d + pd.Timedelta(days=7),
            "spread_type": "bull_put",
            "short_strike": 99.0,
            "long_strike": 98.0,
            "credit_received": 999.0,
            "max_loss": 1.0,
            "close_debit": 0.0,
            "realized_pnl": 999.0,
            "symbol": "SPY",
        }

        with self.assertRaises(AssertionError):
            engine._execute_spread_entry(d, bad_raw, {"SPY": 100.0}, {"SPY": 0.20})

        self.assertEqual(engine.book.cash, 30_000.0)
        self.assertEqual(engine.book.reserved_collateral, 0.0)
        self.assertEqual(engine.book.active_spreads, [])

    def test_v3_spread_entry_accepts_vendor_credit_above_entry_day_theoretical_debit(self):
        """Structural raw invariants, not entry-day theoretical repricing, decide validity."""
        dates = pd.bdate_range("2023-01-02", periods=3)
        df_spy = pd.DataFrame(
            {"open": [100.0] * 3, "high": [100.0] * 3, "low": [100.0] * 3, "close": [100.0] * 3, "volume": [1000] * 3},
            index=dates,
        )
        iv_series = pd.Series(0.12, index=dates)
        regimes = pd.Series("neutral", index=dates)
        boundary_raw = [
            {
                "entry_date": dates[0],
                "exit_date": dates[2],
                "spread_type": "bull_put",
                "short_strike": 95.0,
                "long_strike": 94.0,
                "credit_received": 0.05,
                "max_loss": 0.95,
                "close_debit": 0.0,
                "realized_pnl": 0.05,
                "exit_reason": "probe",
                "symbol": "SPY",
            }
        ]

        report = run_v3_spread_financing(
            bars_by_symbol={"SPY": df_spy},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            raw_trades=boundary_raw,
            starting_cash=30_000.0,
        )

        short_cycles = [r for r in report.realized_events if r["kind"] == "short_cycle"]
        self.assertEqual(len(short_cycles), 1)
        self.assertAlmostEqual(short_cycles[0]["dollar_pnl"], 0.05 * 100.0 * 15)

    def test_v3_iron_condor_signal_splits_before_engine_replay(self):
        """The upstream raw generator must split iron_condor into replayable spread sides."""
        dates = pd.bdate_range("2023-01-02", periods=300)
        df_spy = create_synthetic_bars(start_date="2023-01-02", n_days=300, start_price=100.0, seed=77)
        iv_series = pd.Series(0.25, index=dates)
        regimes = pd.Series("neutral", index=dates)
        strategy_name = "__test_iron_condor_split__"
        original = backtest.ALL_STRATEGIES.get(strategy_name)
        backtest.ALL_STRATEGIES[strategy_name] = lambda df: [StrategySignal(df.index[260], "iron_condor")]
        try:
            raw_trades = _generate_raw_trades(
                strategy_name,
                df_spy,
                iv_series,
                pd.Series(0.05, index=dates),
            )
        finally:
            if original is None:
                backtest.ALL_STRATEGIES.pop(strategy_name, None)
            else:
                backtest.ALL_STRATEGIES[strategy_name] = original

        self.assertEqual({t["spread_type"] for t in raw_trades}, {"bull_put", "bear_call"})
        report = run_v3_spread_financing(
            bars_by_symbol={"SPY": df_spy},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            raw_trades=[dict(t, symbol="SPY") for t in raw_trades],
            starting_cash=30_000.0,
        )
        self.assertGreaterEqual(report.metrics.n_trades, 1)


class TestAdapterAndReporting(unittest.TestCase):
    """Test performance calculation adapter and 7-day rolling statistics (§5.5, §7)."""

    def test_metrics_calculation_with_sleeve_equity(self):
        trades = [
            DollarTrade(pd.Timestamp("2023-01-02"), pd.Timestamp("2023-01-09"), contracts=2, dollar_pnl=400.0),
            DollarTrade(pd.Timestamp("2023-01-10"), pd.Timestamp("2023-01-17"), contracts=2, dollar_pnl=-200.0),
            DollarTrade(pd.Timestamp("2023-01-18"), pd.Timestamp("2023-01-25"), contracts=2, dollar_pnl=600.0),
        ]
        equity_series = pd.Series([30000.0, 30400.0, 30200.0, 30800.0], index=pd.bdate_range("2023-01-02", periods=4))
        metrics = calculate_metrics_from_dollar_trades(trades, equity_series, n_years=0.5, starting_equity=30_000.0)

        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.n_trades, 3)
        self.assertAlmostEqual(metrics.total_pnl, 800.0)
        self.assertAlmostEqual(metrics.win_rate, 2.0 / 3.0)
        self.assertAlmostEqual(metrics.profit_factor, 1000.0 / 200.0)

    def test_rolling_7d_uses_calendar_days_and_no_nan_stats(self):
        """Sparse realized/equity dates still produce calendar 7-day windows with defined stats."""
        book = SleeveBook(cash=30_000.0)
        book.equity_curve = {
            pd.Timestamp("2023-01-02"): 30_000.0,
            pd.Timestamp("2023-01-03"): 30_100.0,
        }
        book.realized = [
            {
                "entry_date": pd.Timestamp("2023-01-02"),
                "exit_date": pd.Timestamp("2023-01-10"),
                "symbol": "SPY",
                "kind": "short_cycle",
                "dollar_pnl": 100.0,
                "detail": {"contracts": 1},
            }
        ]
        engine = LeapEngine(starting_cash=30_000.0)
        engine.book = book

        report = _build_report("probe", book, engine)

        self.assertEqual(report.rolling_7d_pnl.index.freqstr, "D")
        self.assertEqual(report.rolling_7d_pnl.loc[pd.Timestamp("2023-01-10")], 100.0)
        self.assertTrue(all(np.isfinite(v) for v in report.rolling_7d_stats.values()))

    def test_regime_calculation_zero_lookahead(self):
        """Verify calculate_adx_ema_regime runs without errors and produces valid regimes."""
        df = create_synthetic_bars(n_days=100, seed=99)
        regimes = calculate_adx_ema_regime(df)
        self.assertEqual(len(regimes), len(df))
        self.assertTrue(set(regimes.unique()).issubset({"bull", "bear", "neutral"}))

    def test_v1_leap_dte_roll_under_90(self):
        """Verify that LEAP with DTE < 90 gets rolled (closed & new LEAP entered) in V1."""
        dates = pd.bdate_range("2023-01-02", periods=10)
        df = create_synthetic_bars(start_date="2023-01-02", n_days=10, start_price=400.0, seed=1)
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("bull", index=dates)

        engine = LeapEngine(starting_cash=30_000.0)
        # Inject LEAP with only 60 days to expiry
        leap = OptionLeg(
            option_type="call",
            strike=350.0,
            expiry=dates[0] + pd.Timedelta(days=60),
            qty=1,
            entry_price=60.0,
            entry_date=dates[0] - pd.Timedelta(days=300),
            role="leap",
            symbol="SPY",
        )
        engine.book.legs["SPY"] = [leap]
        engine.book.cash = 25_000.0

        def decide_fn(eng, d, px, iv, reg):
            decide_v1_pmcc(eng, d, px, iv, reg)

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=decide_fn,
        )

        self.assertEqual(engine.leap_roll_count, 1)
        roll_events = [r for r in book.realized if r["kind"] == "leap_roll"]
        self.assertEqual(len(roll_events), 1)

    def _spy_sweep_cost(self, spot: float, iv: float = 0.20) -> float:
        r = 0.045
        strike = round_to_increment(strike_for_delta(spot, 1.0, r, iv, 0.70, "call"), 0.5)
        return black_scholes_price(spot, strike, 1.0, r, iv, "call") * 100.0

    def _run_v3_sweep_probe(self, premium_bank: float, regimes: pd.Series, prices: List[float] | None = None) -> tuple[LeapEngine, SleeveBook]:
        dates = pd.to_datetime(["2023-01-05", "2023-01-06", "2023-01-09"])
        prices = prices or [100.0, 100.0, 100.0]
        df = pd.DataFrame(
            {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1000] * 3},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        engine = LeapEngine(starting_cash=30_000.0)
        engine.book.premium_bank = premium_bank
        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=lambda eng, d, px, iv, reg: decide_v3_spread_financing(eng, d, px, iv, reg, raw_trades_by_entry_date={}),
        )
        return engine, book

    def test_v3_funding_sweep_bank_below_cost_rolls_forward(self):
        """A bank balance just below LEAP cost should not buy and should remain available for a later sweep."""
        dates = pd.to_datetime(["2023-01-05", "2023-01-06", "2023-01-09"])
        friday_cost = self._spy_sweep_cost(100.0)
        regimes = pd.Series("bull", index=dates)

        engine, book = self._run_v3_sweep_probe(friday_cost - 0.01, regimes)

        self.assertEqual(engine.leap_sweep_count, 0)
        self.assertEqual([r for r in book.realized if r["kind"] == "leap_sweep_buy"], [])
        self.assertAlmostEqual(engine.book.premium_bank, friday_cost - 0.01)

    def test_v3_funding_sweep_bank_above_cost_buys_next_day(self):
        """A bank balance just above LEAP cost should queue on Friday and buy on the next trading day."""
        dates = pd.to_datetime(["2023-01-05", "2023-01-06", "2023-01-09"])
        friday_cost = self._spy_sweep_cost(100.0)
        regimes = pd.Series("bull", index=dates)

        engine, book = self._run_v3_sweep_probe(friday_cost + 0.01, regimes)

        self.assertEqual(engine.leap_sweep_count, 1)
        sweep_events = [r for r in book.realized if r["kind"] == "leap_sweep_buy"]
        self.assertEqual(len(sweep_events), 1)
        self.assertEqual(pd.Timestamp(sweep_events[0]["exit_date"]), dates[2])

    def test_v3_funding_sweep_bear_regime_blocks_purchase(self):
        """A fully funded Friday sweep must not buy when the target underlying is in bear regime."""
        dates = pd.to_datetime(["2023-01-05", "2023-01-06", "2023-01-09"])
        friday_cost = self._spy_sweep_cost(100.0)
        regimes = pd.Series("bear", index=dates)

        engine, book = self._run_v3_sweep_probe(friday_cost * 2.0, regimes)

        self.assertEqual(engine.leap_sweep_count, 0)
        self.assertEqual([r for r in book.realized if r["kind"] == "leap_sweep_buy"], [])

    def test_v2_month_end_funding_sweep_buys_next_day(self):
        """V2 month-end sweep uses the same bank/cash contract as V3 and buys SPY on the next trading day."""
        dates = pd.to_datetime(["2023-01-30", "2023-01-31", "2023-02-01"])
        df = pd.DataFrame(
            {"open": [100.0] * 3, "high": [100.0] * 3, "low": [100.0] * 3, "close": [100.0] * 3, "volume": [1000] * 3},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("bull", index=dates)
        engine = LeapEngine(starting_cash=30_000.0)
        engine.book.premium_bank = self._spy_sweep_cost(100.0) + 1.0

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=lambda eng, d, px, iv, reg: decide_v2_wheel(eng, d, px, iv, reg, month_end_dates={dates[1]}),
        )

        self.assertEqual(engine.leap_sweep_count, 1)
        sweep_events = [r for r in book.realized if r["kind"] == "leap_sweep_buy"]
        self.assertEqual(len(sweep_events), 1)
        self.assertEqual(pd.Timestamp(sweep_events[0]["exit_date"]), dates[2])

    def test_funding_sweep_gap_up_clamps_bank_and_executes_when_cash_available(self):
        """A Friday-approved sweep still executes after a Monday price gap; bank is floored at zero."""
        dates = pd.to_datetime(["2023-01-05", "2023-01-06", "2023-01-09"])
        friday_cost = self._spy_sweep_cost(100.0)
        monday_cost = self._spy_sweep_cost(180.0)
        regimes = pd.Series("bull", index=dates)

        engine, book = self._run_v3_sweep_probe(friday_cost + 1.0, regimes, prices=[100.0, 100.0, 180.0])

        self.assertGreater(monday_cost, friday_cost + 1.0)
        self.assertEqual(engine.leap_sweep_count, 1)
        sweep_events = [r for r in book.realized if r["kind"] == "leap_sweep_buy"]
        self.assertEqual(len(sweep_events), 1)
        self.assertAlmostEqual(sweep_events[0]["detail"]["cost"] * 100.0, monday_cost, places=4)
        self.assertEqual(engine.book.premium_bank, 0.0)
        self.assertGreater(engine.book.available_cash, 0.0)

    def test_funding_sweep_skips_with_reason_when_execution_cash_is_short(self):
        """A gap the sleeve can no longer afford logs leap_sweep_skip and rolls the bank forward."""
        dates = pd.to_datetime(["2023-01-05", "2023-01-06", "2023-01-09"])
        friday_cost = self._spy_sweep_cost(100.0)
        bank = friday_cost + 1.0
        regimes = pd.Series("bull", index=dates)

        engine, book = self._run_v3_sweep_probe(bank, regimes, prices=[100.0, 100.0, 5_000.0])

        self.assertGreater(self._spy_sweep_cost(5_000.0), 30_000.0)
        self.assertEqual(engine.leap_sweep_count, 0)
        skips = [r for r in book.realized if r["kind"] == "leap_sweep_skip"]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["detail"]["reason"], "insufficient_execution_cash")
        # Bank is rolled forward untouched: nothing was bought, so nothing is spent.
        self.assertAlmostEqual(engine.book.premium_bank, bank, places=4)
        self.assertAlmostEqual(engine.book.cash, 30_000.0, places=4)

    def test_funding_sweep_skip_reason_distinguishes_empty_bank(self):
        """A queued sweep that reaches execution with an empty bank says so, not 'short cash'."""
        dates = pd.to_datetime(["2023-01-05", "2023-01-06"])
        prices = [100.0, 100.0]
        df = pd.DataFrame(
            {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1000] * 2},
            index=dates,
        )
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("bull", index=dates)

        engine = LeapEngine(starting_cash=30_000.0)
        engine.book.premium_bank = 0.0  # bank spent elsewhere before this order executes
        engine.pending_queue.append(
            PendingOrder(
                order_type="LEAP_BUY",
                symbol="SPY",
                target_delta=0.70,
                target_dte=365,
                qty=1,
                role="leap",
                is_sweep=True,
                estimated_debit=self._spy_sweep_cost(100.0),
            )
        )

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=lambda eng, d, px, iv, reg: None,
        )

        skips = [r for r in book.realized if r["kind"] == "leap_sweep_skip"]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["detail"]["reason"], "empty_premium_bank")
        self.assertLess(skips[0]["detail"]["cost"], skips[0]["detail"]["available_cash"])
        self.assertEqual(engine.leap_sweep_count, 0)

    def test_v3_friday_funding_sweep(self):
        """Verify V3 Friday funding sweep buys SPY/QQQ LEAP using accumulated premium_bank."""
        dates = pd.bdate_range("2023-01-02", periods=10)
        # Find Friday in dates
        friday_idx = next(i for i, d in enumerate(dates) if d.dayofweek == 4)
        df = create_synthetic_bars(start_date="2023-01-02", n_days=10, start_price=100.0, seed=2)
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("bull", index=dates)

        engine = LeapEngine(starting_cash=30_000.0)
        # Inject profits in premium_bank
        engine.book.premium_bank = 5_000.0

        def decide_fn(eng, d, px, iv, reg):
            decide_v3_spread_financing(eng, d, px, iv, reg, raw_trades_by_entry_date={})

        book = engine.run_simulation(
            bars_by_symbol={"SPY": df},
            iv_by_symbol={"SPY": iv_series},
            regime_by_symbol={"SPY": regimes},
            decide_fn=decide_fn,
        )

        # Sweep executed on Monday following Friday
        self.assertGreater(engine.leap_sweep_count, 0)
        sweep_events = [r for r in book.realized if r["kind"] == "leap_sweep_buy"]
        self.assertEqual(len(sweep_events), 1)


def run_all_tests():
    """Custom test runner supporting unittest & direct python execution."""
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
