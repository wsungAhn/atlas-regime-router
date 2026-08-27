# Implementation Notes: Stateful Multi-Leg LEAP / PMCC / Wheel Backtest Engine

## 1. Overview & Architectural Alignment

This implementation delivers the standalone, stateful multi-leg backtest engine designed in `design-wheel-pmcc-leap-strategy` (Clean Round 4 Audit) for the `atlas-options-hackathon` options trading platform.

### Zero Modification Invariant
- **Rule Compliance**: Existing repository source files (`src/vendor/*`, `src/backtest.py`, `src/portfolio.py`, `src/strategies.py`) remain **100% untouched (0 lines modified)**.
- **New Deliverables**:
  - `leap_engine.py`: Single ledger multi-leg backtest engine, Black-Scholes primitives, daily 5-step event loop, V1/V2/V3 strategy decision logic, and reporting adapter.
  - `test_leap_engine.py`: Comprehensive test suite (16 test suites) covering the P0 Asset Identity Gate, invariant checks, bug regression tests, and lookahead timing verification.
  - `IMPLEMENTATION_NOTES.md`: Technical documentation and audit rationale.

---

## 2. SleeveBook Single Ledger Accounting (§5.2, §5.2.1)

All capital allocation, collateral locking, premium tracking, and order debits across all symbols in a sleeve are managed via a single `SleeveBook` instance.

### 2.1 Cash Equations & Invariant Formulations
```python
available_cash = cash - reserved_collateral - pending_debits
```
1. **Total Cash (`cash`)**: Total physical cash held in the sleeve. Includes cash that is locked as collateral.
2. **Reserved Collateral (`reserved_collateral`)**: Cash committed to cash-secured puts (V2: `strike * 100 * contracts`) or credit spread max loss (V3: `max_loss * 100 * contracts`).
3. **Pending Debits (`pending_debits`)**: Estimated capital committed to orders queued during today's `DECIDE` step to execute on tomorrow's `EXECUTE` step.
4. **Premium Bank (`premium_bank`)**: A sub-ledger tracking realized profits from credit operations (CSPs, CCs, credit spreads) earmarked for LEAP funding sweeps.
   - **Clamped Deduction Rule**: `premium_bank = max(0.0, premium_bank - sweep_cost)` (§5.2.1, 3R P3-1).
   - **Upper Bound Invariant**: `premium_bank <= cash` at all times.
5. **Asset Identity Invariant (P0 Gate)**:
   ```python
   equity == cash + sum(signed_leg_mtm) + sum(active_spread_mtm) + sum(shares * close)
   ```
   Validated on every trading day during `_step_mtm`.

---

## 3. Event Loop & Timing Contract (§5.3)

The daily execution follows the strictly ordered 5-step lifecycle to eliminate lookahead bias:

```
[EXECUTE] (Day T Close)  -> Fills orders queued from Day T-1 DECIDE at Day T Close prices.
   ↓
[EXPIRY]  (Day T Close)  -> Settle contracts expiring on Day T (OTM, ITM cash-settle, assignment, called-away).
   ↓
[TRIGGER] (Day T Close)  -> Price-touch 50% Profit Target exits filled at Day T Close prices.
   ↓
[DECIDE]  (Day T Close)  -> Evaluates day T market data / regime -> Queues PendingOrders for Day T+1.
   ↓
[MTM]     (Day T Close)  -> Computes daily sleeve equity curve, asserts P0 invariants.
```

### Strategic Exit vs Price-Touch Exit Timing
- **Strategic Exits** (Regime flip to Bear, LEAP -50% Hard Stop, V2 Stock -20% Stop Loss, LEAP DTE < 90 Roll): Evaluated in `DECIDE` on Day T, queued to `pending_queue`, and executed on **Day T+1** in `EXECUTE` at Day T+1 prices. Zero lookahead.
- **Price-Touch Exits** (50% Profit Target on short legs): Evaluated in `TRIGGER` on Day T and closed at Day T MTM prices (same-day close-to-close touch).

---

## 4. Strategy Variants Implementation Details (§2)

### 4.1 V1: PMCC Classic (§2.1)
- **Underlyings**: SPY, QQQ.
- **LEAP Entry**: Delta 0.80 Call, DTE 365 days when regime is `bull`. Sized to <= 40% sleeve equity per symbol, total LEAP commitment <= 80%.
- **Short Call**: Weekly (DTE 7), delta 0.20 Call sold against LEAP.
- **Dynamic Net-Cost Rule** (§2.1, 3R P2-2):
  ```python
  net_cost = leap.entry_price - (leap.cumulative_short_credits / leap.qty)
  strike_condition = short_strike > (leap.strike + net_cost)
  ```
  If not met, short call is skipped and incremented in `short_skip_count`.
- **ITM Cash Settlement**: When short call expires ITM, intrinsic difference `(S - K) * 100 * q` is debited from cash without exercising or dismantling the underlying LEAP.
- **Roll & Stop**: LEAP rolled when DTE < 90; hard stop at -50% loss; bear regime closes LEAP and attached short call.

### 4.2 V2: Wheel + LEAP Sweep (§2.2)
- **Wheel Underlyings**: SLV, TLT (high IV / non-equity macro assets).
- **LEAP Sweep Underlying**: SPY.
- **CSP**: Sell delta 0.25 Put (DTE 7) when regime is not `bear`. Full collateral reserved (`strike * 100 * q`).
  - OTM Expiry: Collateral released, 100% premium realized -> deposited to `premium_bank`.
  - ITM Assignment: `cash -= strike * 100 * q`, `shares += 100 * q`, `share_cost_basis = strike - entry_credit`.
- **Covered Call**: Sell delta 0.25 Call (DTE 7) against assigned shares.
  - Snap Rule: Strike must be `>= share_cost_basis`. If Black-Scholes strike is lower, snapped to `max(cost_basis, ATM)`.
  - ITM Called Away: Shares sold at strike, total realized gain deposited to `premium_bank`.
- **Stock Stop Loss**: If stock price drops >= 20% below `share_cost_basis`, liquidated at next day's close.
- **Month-End LEAP Sweep**: On month-end trading day, buy 1 contract delta 0.70 DTE 365 SPY Call if `cost <= min(premium_bank, available_cash)` and total LEAP cost <= 50% sleeve equity.

### 4.3 V3: Strategy 7 Credit Spread Financing LEAP Ladder (§2.3)
- **Spread Underlyings**: SPY, QQQ, IWM. Replays the raw Strategy 7 side trades emitted by `_generate_raw_trades`; `iron_condor` signals are split upstream into `bull_put` and `bear_call` raw trades before engine replay.
- **Sizing**: 5% available cash risk rule:
  ```python
  contracts = floor(available_cash * 0.05 / (max_loss * 100))
  ```
- **Collateral & MTM**: `max_loss * 100 * contracts` locked. Daily spread liability marked to market.
- **Entry Validation**: Spread replay validates raw structural invariants (`credit_received <= width`, `max_loss == width - credit_received`, positive `max_loss`, and collateral/contract consistency) before mutating the sleeve. It does not reprice signal-day vendor credit against entry-day theoretical debit.
- **Friday Funding Sweep**: Every Friday, if SPY (priority 1) or QQQ (priority 2) is in `bull` regime, buy 1 contract delta 0.70 DTE 365 Call funded from `premium_bank`.

---

## 5. Performance Metrics Adapter & Diagnostics (§5.5, §7)

- **`calculate_metrics_from_dollar_trades`**: Correctly receives the actual sleeve equity series (based on `$30,000` sleeve capital) rather than a fallback `$100,000` capital, preventing CAGR / Sharpe / Calmar distortions.
- **7-Day Rolling Realized P&L Statistics**:
  - `rolling_7d_pnl`: Daily sum of realized P&L over trailing 7 calendar days.
  - Distribution metrics: Mean, Median, 10th percentile (P10), 90th percentile (P90), Min, Max, Positive Rate.
- **Detailed Event Counters**: Tracks `assignment_count`, `called_away_count`, `stock_stop_count`, `short_skip_count`, `leap_roll_count`, and `leap_sweep_count`.

---

## 6. Verification & Test Suite Summary

The unit test suite in `test_leap_engine.py` was executed with Python 3.12 (`16/16 tests passing`):

| Test Case | Description | Result |
|---|---|---|
| `test_bs_call_put_parity` | Verifies Black-Scholes call-put parity | PASS |
| `test_strike_for_delta_inversion` | Verifies root-finding strike for delta (0.80, 0.70, 0.25, 0.20) | PASS |
| `test_asset_identity_full_lifecycle` | P0 Gate: Verifies `equity == cash + legs + stock + spreads` every day | PASS |
| `test_regression_injection_short_entry_missing_cash` | Injected Bug Test: Missing cash inflow on short sale causes immediate failure | PASS |
| `test_regression_injection_bank_exceeds_cash` | Injected Bug Test: `premium_bank > cash` triggers assertion error | PASS |
| `test_available_cash_non_negativity` | Verifies `available_cash >= 0` invariant enforcement | PASS |
| `test_exit_timing_strategic_vs_touch` | Verifies Day T DECIDE -> Day T+1 EXECUTE zero-lookahead timing | PASS |
| `test_v1_dynamic_net_cost_short_call_skip` | Verifies tastytrade net-cost threshold skip logic | PASS |
| `test_v1_short_call_itm_cash_settlement_preserves_leap` | Verifies ITM cash settlement without LEAP destruction | PASS |
| `test_v1_leap_dte_roll_under_90` | Verifies DTE < 90 roll trigger and execution | PASS |
| `test_v2_csp_assignment_and_covered_call_called_away` | Verifies complete Wheel lifecycle (CSP -> Stock -> CC -> Called Away) | PASS |
| `test_v2_stock_stop_loss_at_20pct_drawdown` | Verifies -20% stock stop liquidation | PASS |
| `test_v3_signature_and_spread_replay` | Verifies Strategy 7 replay & 5% sizing | PASS |
| `test_v3_friday_funding_sweep` | Verifies Friday funding sweep into LEAPs from premium bank | PASS |
| `test_regime_calculation_zero_lookahead` | Verifies ADX/EMA regime calculation zero lookahead | PASS |
| `test_metrics_calculation_with_sleeve_equity` | Verifies $30k sleeve equity adapter metrics calculations | PASS |
