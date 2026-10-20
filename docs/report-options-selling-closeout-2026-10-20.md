# Options-selling strategy track — closeout report (2026-10-20)

Session: `a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d` · machine: mac-studio · worktree: `friction-backtest` (not merged into atlas main, not pushed).
Scope: Alpaca hackathon paper account, options-only credit-spread selling (strategy 7 entries, hold-to-expiry, stop-rule A vs C comparison).

## 1. Decision

**Track closed on 2026-10-20 (user decision).** Reason: after building a friction-calibrated simulator and an extensive edge search, no evidence was found that this structure earns a profit net of friction. No live or probation work (O3–O6) was started. Live order paths remain closed: `registry/OPTIONS_CLOSE_ONLY` stays in place (atlas main, `5228d90`); no Task Contract was approved.

## 2. What was built (kept, on branch `friction-backtest`)

| Area | Artifact | Verification |
|---|---|---|
| Friction simulator | half-spread table, mids/IV anchor, exit paths V0–V6, expiry settlement, iron condor, replay, beta fit, validation/controls/grade, calibration CLI | 376 tests passing; each task reviewed by direct code reading plus own mutation injection at sibling sites (2–3 rounds each) |
| Policy comparison | `src/friction_compare.py` (X0–X2, A1–A5, C2–C5) | reports `reports/friction/compare-2026-10-19.*` |
| OS-1 design | `docs/design-options-only-selling-2026-10-19.md` (+ rubric) | independent grading 4.4; adversarial audit rounds 1–15, two consecutive CLEAN (r14, r15) under the narrowed scope (implementation-level details excluded by user decision; to be checked at implementation time) |
| O0 facts | `docs/o0-facts-2026-10-19.md` | measured against the paper account (read-only) |
| O1 | `src/os1_replay.py` + report `reports/friction/os1-replay-2026-10-19.*` | approved round 3; 24/25 injected mutations killed (1 equivalent) |
| O2, O2k | `src/os1_sizing.py`, `src/os1_kill.py` | approved round 3; all injected mutations killed |
| Exploratory research | `research/os1-edge-2026-10/` | ad hoc, not handoff-reviewed |

Not built: O3 (hold-to-expiry exit parity), O4 (assignment/ex-div), O5/O5b (probation evaluator/shadow), O6 (live wiring), O1b (pin-zone ratio).

## 3. Results

### 3.1 Simulator calibration (2026-10-19)
119 live episodes (103 clean), train 61 / validation 42, beta = 0.625 (pessimistic 0.375). 4 of 5 validation targets passed; T-d (trend_up win rate) failed (sim 0.238 vs CI 0–0.19), so the grade is **FAIL**. Controls A and C were both outside the CI, so the targets do discriminate friction bias. The simulator was used for relative comparisons only.

### 3.2 Policy comparison (replay of the live entries)
Hold-to-expiry without a stop (X2) beat the current exit (X0) overall (+275 central, +464 pessimistic, wide CI), but the pessimistic advantage is partly an artifact of zero cross cost at expiry settlement. Stop A (account-equity based) is effectively non-binding (median max loss / equity 0.091), stop C (underlying move) was worse. On the 9-ETF subset X2 − X0 was **−379** (central; 90% CI −810…+11), i.e. the overall advantage came from non-ETF names.

### 3.3 Caps applied (O1)
Of 119 candidate entries only 15 survive the OS-1 caps (count cap 38, same-symbol 20, credit/width < 0.10: 32, risk cap 14); capped P&L −$320 (−0.6% of equity), worst −$688, peak concurrent exposure 7.7%. Sample too small to conclude anything about profitability.

### 3.4 Payoff structure
Hold-to-expiry rows (n = 119): win rate 0.79, average win +$534, average loss −$2,881 (payoff 0.19), break-even win rate ≈ 0.87, mean −$183 per trade (9 ETFs: −$515). With a short delta of 0.20 the win rate is ≈ 0.80 by construction; credit is ≈ 12% of width, so break-even needs ≈ 87–89% wins. A credit/width ≥ 0.10 floor is arithmetic, not edge.

### 3.5 Independent out-of-sample checks
- 3-week quote panel (11 expiries, 242 spreads): mean P&L/width −1.2% (±1.9%); ETFs +0.4% (±2.1%).
- **Alpaca historical option bars, 9 ETFs, 125 Fridays (2024-03…2026-10), 2,200 spreads** (0.20 delta, 1.5% wide, 7 days, daily-close prices, 5% credit haircut): win 0.81, break-even 0.89, **mean −0.6% of width (±0.9%, week-clustered)**; puts +0.9% (±1.4%), calls −2.1% (±1.4%). By year: 2024 −1.5%, 2025 −0.7%, 2026 +0.6%. Loss size when ITM: mean 60% of width, 34% full width.
- Pre-registered grid (delta 0.10/0.20/0.30 × width 1.5%/3%): all six cells between −1.4% and −0.3% of width overall (all within noise); every cell negative in the 2024-03…2025-06 train half and ≥ 0 in the 2025-07… test half, so the period dominates the parameters.
- IV/RV20 regime filters: P&L rises monotonically with IV/RV quartile (−1.5, −1.2, −0.5, +0.8%), but filtered subsets flip sign between halves (e.g. IV/RV ≥ 1.5: −2.7% train, +4.8% test). Not robust.
- Timestral-123M (Mistral, released 2026-10-15), zero-shot 5-day realized-volatility forecast, ETF weekly origins n = 747: log error 0.322 vs RV20 0.374 (difference −0.052, 90% CI −0.077…−0.028), correlation 0.78 vs 0.70. But using IV/Timestral instead of IV/RV20 as the spread filter did **not** improve P&L (top quartile +0.6% ± 2.2% vs +2.7% ± 1.6%). Better volatility forecasts beat simple baselines; whether they beat implied volatility (the quantity that matters) was not shown.

### 3.6 Who profits from options (search, 2026-10-20)
Retail traders lose in aggregate in the cited studies; wholesalers/market makers capture most of the gain; institutions lose less than retail (ASX). Not verified in this session: net-of-cost variance-risk-premium returns and Asian index-option evidence.

## 4. Caveats on this report's own evidence
- Simulator grade is FAIL; absolute dollar levels are not trustworthy.
- Option prices in the 2-year backtest are daily closes (possibly stale legs); delta is back-solved from IV; historical quotes are not available from Alpaca, so friction is a 5%-of-credit assumption plus the half-spread table.
- Same-week trades across 9 ETFs are correlated; standard errors are week-clustered but still rough. Two years is a short window for a tail-risk strategy.
- The exploratory scripts were not code-reviewed; selection of tested hypotheses was fixed beforehand (6-cell grid, 4 IV/RV filters) to limit data-dredging.
- "No evidence of edge" ≠ "proof of negative edge": the 2,200-trade mean is −0.7σ from zero.

## 5. Facts worth keeping
- Paper-account Activities API works (earlier 401 gone). Option lifecycle types: `OPEXP`, `OPASN`, `OPTRD`, all dated on the expiry day. The 300 TLT shares in the account came from the 2026-09-23 assignment of 3 short TLT 81 puts; they were never unwound (`docs/o0-facts-2026-10-19.md`).
- `PORTFOLIO_RISK_CAP_PCT` (atlas `src/signals.py:46`) is defined but unwired.
- Alpaca provides expired option contracts, daily bars and trades historically (quotes 404).
- The `com.atlas.quote-panel` launchd job still runs from the worktree path (`friction-backtest`); do not move or delete the worktree without stopping it first.

## 6. Reuse
`src/os1_sizing.py` (portfolio-cap sizing) and `src/os1_kill.py` (account kill switch, enable-flag default off, no flag-delete API) are generic, tested pure functions; the simulator can price other exit-policy questions. If the track is ever reopened, the entry condition to test first is a structure with materially lower friction or a relative-value (not directional) edge, not another parameter sweep of the same spread.
