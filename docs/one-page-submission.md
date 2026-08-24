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

Real dollar P&L on a simulated \$100,000 account, 3 years of SPY/QQQ daily data, position sizing
from the same risk-budget formula the live system uses (2–5% of equity per trade, actual contract
counts at the 100x multiplier — not per-share units):

| Strategy | SPY return | SPY Sharpe | SPY Calmar | SPY win rate | Selected? |
|---|---:|---:|---:|---:|:---:|
| **5 — Mean-reversion condor (ADX+RSI+VWAP)** | **+19.6%** | **2.66** | **3.20** | **88%** | **✅ shipped** |
| 7 — ADX/EMA-only regime (our first draft) | +12.0% | 0.53 | 0.20 | 74% | rejected |
| 1 — Plain range condor (ADX only) | +8.8% | 1.14 | 0.99 | 79% | rejected |
| 4 — Volatility-breakout credit | +2.9% | 0.75 | 0.53 | 89% | rejected (9 trades — too thin to trust) |
| 2/3 — Trend-following credit spread (3-tier ladder) | +1.8% | 0.39 | 0.21 | 72% | rejected |
| 6 — Prior project's pullback+ATR trigger | **−1.9%** | −0.30 | −0.15 | 57% | rejected |

On QQQ the condor family (1 and 5) again leads at +13.7%/+12.6%, well ahead of everything else.
Strategy 6 — the pullback-trigger design carried over from an earlier related project — placed
last or near-last on both symbols; we did not ship it on reputation, we tested it against six
alternatives and it lost.

Two real bugs surfaced and got fixed during this process, not after: (1) our VWAP proxy used an
expanding mean from the start of the dataset, which silently zeroed out strategy 5's trade count
over the 3-year window — fixed to a 20-day rolling VWAP, which is what let the eventual winner
actually fire; (2) our credit-spread width was inherited unchanged from a prior project tuned for
\$10–30 leveraged ETFs, which produced unrealistic \$30-wide spreads (and ~\$3,000 max loss per
contract) on SPY's ~\$630 price level — recalibrated to the \$1–10 width actually traded in the SPY/
QQQ options market. Both were caught by demanding the backtest produce real dollar figures instead
of accepting relative per-share units at face value.

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
backtest harness, one winner chosen on Sharpe/Calmar/win-rate rather than familiarity or narrative.
The losing strategies stay in the repo as a record of what didn't work and why, including a strategy
literally inherited from a prior project that we were prepared to keep — and dropped once the numbers
said not to. Each live cycle's decision (regime, macro state, chosen structure or skip reason) is
logged as a structured record, making every trade traceable back to the exact technical + macro
condition that produced it, and back to the backtest evidence that justified the structure in the
first place.

*Educational competition submission only; options trading involves substantial risk, including loss
of capital and assignment risk.*
