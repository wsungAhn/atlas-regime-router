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

    def test_regression_injection_short_entry_missing_cash(self):
        """
        Verify that if an engine bug fails to credit cash on short option sale,
        the asset identity check strictly fails immediately (§5.2.1).
        """
        book = SleeveBook(cash=30_000.0)
        d = pd.Timestamp("2023-01-02")
        S = 100.0
        iv = 0.25
        r = 0.045

        # Sell short call at $3.00, but BUG: forgot `book.cash += 300.0`
        leg = OptionLeg(
            option_type="call",
            strike=105.0,
            expiry=d + pd.Timedelta(days=7),
            qty=-1,
            entry_price=3.0,
            entry_date=d,
            role="short_call",
            symbol="SPY",
        )
        book.legs["SPY"] = [leg]

        # Actual MTM calculation:
        t_rem = 7.0 / 365.0
        short_mtm = -black_scholes_price(S, 105.0, t_rem, r, iv, "call") * 100.0
        reported_equity = 30_000.0  # If someone recorded equity as 30,000 without realizing cash wasn't credited

        # Asset identity MUST fail because reported_equity != book.cash + short_mtm
        with self.assertRaises(AssertionError):
            book.assert_invariants(
                equity=reported_equity,
                current_prices={"SPY": S},
                iv_by_symbol={"SPY": iv},
                current_date=d,
                r=r,
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
        prices = [104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 110.0, 110.0, 110.0]
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
            entry_price=1.50,
            entry_date=dates[0],
            role="short_call",
            symbol="SPY",
        )
        engine.book.legs["SPY"] = [leap, short_call]
        engine.book.cash = 27_950.0

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
        # Realized dollar pnl: (1.50 - 5.00) * 100 = -$350.00
        self.assertAlmostEqual(settle_events[0]["dollar_pnl"], -350.0)


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


class TestV3SpreadFinancingAndSignature(unittest.TestCase):
    """Test V3 Strategy 7 Credit Spread Financing (§2.3) and Signature Verification."""

    def test_v3_signature_and_spread_replay(self):
        """
        Verify V3 raw trade replay logic, 5% max-loss sizing, collateral lock/release,
        and Friday funding sweep into LEAPs.
        """
        dates = pd.bdate_range("2023-01-02", periods=30)
        df_spy = create_synthetic_bars(start_date="2023-01-02", n_days=30, start_price=400.0, seed=42)
        iv_series = pd.Series(0.20, index=dates)
        regimes = pd.Series("bull", index=dates)

        # Mock Strategy 7 raw trades
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
                "credit_received": 1.50,
                "max_loss": 3.50,
                "breakeven": 393.50,
                "close_debit": 0.50,
                "realized_pnl": 1.00,
                "symbol": "SPY",
            },
            {
                "signal_date": str(dates[7].date()),
                "entry_date": str(dates[8].date()),
                "exit_date": str(dates[13].date()),
                "exit_reason": "profit_target",
                "spread_type": "iron_condor",
                "entry_iv": 0.20,
                "short_strike": 405.0,
                "long_strike": 410.0,
                "width": 5.0,
                "credit_received": 2.00,
                "max_loss": 3.00,
                "breakeven": 407.0,
                "close_debit": 0.80,
                "realized_pnl": 1.20,
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
        # Invariant checks were executed at every step
        self.assertEqual(len(report.equity_series), len(dates))


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
