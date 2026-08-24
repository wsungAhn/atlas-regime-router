# Atlas Options Engine
## Backtest-Selected, Defined-Risk Options Agent for Alpaca Paper Trading

**Competition account:** Dedicated Alpaca Paper account · **Starting balance:** $100,000 · **Execution:** Options only

## Overview

Atlas is a defined-risk options agent whose core strategy was chosen by backtesting, not intuition.
Before writing the live trading loop, we implemented seven candidate strategies — five from a
published ATR-grid options framework, the original strategy from an earlier related project, and
a first-draft regime engine of our own — and ran all seven through an identical Black-Scholes
credit-spread simulator over three years of SPY and QQQ daily data. The winner, a mean-reversion
iron condor gated by ADX, RSI, and rolling VWAP, is what ships. Every short option leg is protected
by a long option leg; the system never holds undefined risk.

## AI Logic: Backtest-Selected Regime Router

At each cycle, Atlas computes ADX(14), RSI(14), and a 20-day rolling VWAP on daily bars per
underlying. The primary trigger — range regime → iron condor — fires only when ADX < 18 **and**
RSI sits in 40–60 **and** price is within 1 ATR of the rolling VWAP; a directional credit-spread
fallback covers confirmed trend regimes (ADX > 20 with EMA(20)/EMA(50) alignment). A macro overlay
(SPY Weinstein stage, from an independently-verified daily batch signal) blocks *all* new entries
when the broad market is in a confirmed downtrend, regardless of the local technical read.

```
Technical regime (ADX + RSI + rolling VWAP)  →  Macro gate (Weinstein stage, independent DB)  →  Structure
```

### The backtest that selected this design

Judging weighs realized P&L, so that's the primary ranking criterion below — not a risk-adjusted
score. Real dollar P&L on a simulated \$100,000 account, 3 years of SPY/QQQ daily data, using the
exact constraints the live system runs under: the same volatility-scaled risk-budget formula (2–5%
of equity per trade, real contract counts at the 100x multiplier), and at most one open position per
symbol at a time — matching the live re-entry guard exactly, not a looser backtest-only assumption:

| Strategy | SPY realized P&L | SPY win rate | Max drawdown | Selected? |
|---|---:|---:|---:|:---:|
| **5 — Mean-reversion condor (ADX+RSI+VWAP)** | **+\$4,436 (+4.4%)** | **88%** | \$1,021 | **✅ shipped** |
| 1 — Plain range condor (ADX only) | +\$3,532 (+3.5%) | 82% | \$1,313 | rejected |
| 7 — ADX/EMA-only regime (our first draft) | +\$2,306 (+2.3%) | 72% | \$4,017 | rejected |
| 4 — Volatility-breakout credit | +\$458 (+0.5%) | 80% | \$1,782 | rejected (5 trades — too thin to trust) |
| 2/3 — Trend-following credit spread (3-tier ladder) | −\$55 (−0.1%) | 65% | \$916 | rejected |
| 6 — Prior project's pullback+ATR trigger | **−\$526 (−0.5%)** | 57% | \$1,116 | rejected |

Strategy 5 wins on realized dollars outright — highest P&L of all seven, not a runner-up propped up
by a risk-adjusted score. On QQQ it's effectively tied with our own rejected first draft on raw
return (+\$2,681 vs +\$2,707), and there we do lean on the tiebreak evidence (win rate, drawdown) to
choose 5 over 7 — that's the one place risk metrics did the deciding, everywhere else P&L alone
already picked the winner. Strategy 6 — the pullback-trigger design carried over from an earlier
related project — placed last on both symbols on realized P&L; we did not ship it on reputation, we
tested it against six alternatives and it lost money.

Three real bugs surfaced and got fixed during this process, not after — each one found by refusing
to accept a backtest result that looked plausible without checking it against a stated assumption:
(1) our VWAP proxy used an expanding mean from the start of the dataset, silently zeroing out
strategy 5's trade count over the 3-year window — fixed to a 20-day rolling VWAP, which is what let
the eventual winner actually fire; (2) our credit-spread width was inherited unchanged from a prior
project tuned for \$10–30 leveraged ETFs, producing unrealistic \$30-wide spreads (~\$3,000 max loss
per contract) on SPY's ~\$630 price level — recalibrated to the \$1–10 width the SPY/QQQ options
market actually trades; (3) the backtest allowed up to 3 simultaneous open positions per side while
the live system's re-entry guard caps at 1 — capped it to match, which cut trade counts by roughly
two-thirds and is the reason these dollar figures are smaller (and more honest) than an earlier pass.

## Risk Gates

Every entry is a defined-max-loss structure; position size is computed from that structure's
maximum theoretical loss, never from premium received:

```
risk_budget = equity × clip(0.05 − 0.03 × atr_percentile, 0.02, 0.05)
contracts   = floor(risk_budget / max_loss_per_contract)
```

- Strategy-bundle risk: 2–5% of equity, scaled down as realized volatility rises.
- Same-underlying risk cap: 3% of equity. Portfolio risk cap: 6% of equity.
- Cash reserve floor: 10% of equity at all times.
- Daily loss kill switch: −3% of equity halts new entries for the day.
- Weekly loss kill switch: −6% halts the system pending review.
- No new short-premium legs inside 14 DTE.
- Macro-blocked cycles produce zero new entries regardless of technical signal strength.

## Alpaca Infrastructure Implementation

Atlas separates signal computation, backtesting, and order execution into independently-verifiable
layers:

```
Market/option-chain data (alpaca-py, read-only)
        → regime classification + macro gate (pure Python, unit-tested)
        → option leg selection by target delta
        → risk-gated position sizing
        → order-intent dict matching the MCP place_option_order schema
        → Alpaca MCP Server (stdio protocol, no LLM in the execution loop): place_option_order
        → decision + audit log
```

The signal layer has zero broker dependency and is fully unit-tested against synthetic data before
ever touching the account. Order submission runs unattended on a scheduler and talks to the Alpaca
MCP Server as a deterministic MCP client — the same server an operator would use interactively to
inspect account state, verifying the automated agent's trading actions are auditable at that exact
layer, with no LLM call (and its associated latency/cost/failure surface) in the critical execution
path. The backtest layer reuses a previously-built and reviewed Black-Scholes credit-spread
simulator rather than a from-scratch pricer, keeping the evaluation methodology itself independently
checkable.

## Creativity & Engagement

Atlas's headline claim isn't a strategy — it's a selection process: seven strategies, one shared
backtest harness, one winner chosen on realized P&L rather than familiarity or narrative.
The losing strategies stay in the repo as a record of what didn't work and why, including a strategy
literally inherited from a prior project that we were prepared to keep — and dropped once the numbers
said not to. Each live cycle's decision (regime, macro state, chosen structure or skip reason) is
logged as a structured record, making every trade traceable back to the exact technical + macro
condition that produced it, and back to the backtest evidence that justified the structure in the
first place.

*Educational competition submission only; options trading involves substantial risk, including loss
of capital and assignment risk.*
