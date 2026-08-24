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

| Strategy | SPY Sharpe | SPY Calmar | SPY win rate | Selected? |
|---|---:|---:|---:|:---:|
| **5 — Mean-reversion condor (ADX+RSI+VWAP)** | **2.55** | **2.77** | **85%** | **✅ shipped** |
| 7 — ADX/EMA-only regime (our first draft) | 0.45 | 0.18 | 72% | rejected |
| 1 — Plain range condor (ADX only) | 0.93 | 0.48 | 76% | rejected |
| 4 — Volatility-breakout credit | 0.34 | 0.43 | 78% | rejected (thin sample) |
| 2/3 — Trend-following credit spread | −0.32 | −0.15 | 67% | rejected |
| 6 — Prior project's pullback+ATR trigger | −1.07 | −0.28 | 40% | rejected |

Results held up directionally on QQQ as well. Strategy 6 — the pullback-trigger design carried over
from an earlier related project — placed last on both symbols; we did not ship it on reputation, we
tested it against six alternatives and it lost. One real bug surfaced during this process: our VWAP
proxy used an expanding mean from the start of the dataset, which silently zeroed out strategy 5's
trade count over a 3-year window. Fixing it to a 20-day rolling VWAP is what let the eventual winner
actually fire — a concrete instance of the backtest catching a defect that intuition would have
missed entirely.

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
