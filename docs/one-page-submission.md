# Atlas Options Engine
## Regime-Adaptive Defined-Risk Options Agent for Alpaca Paper Trading

**Competition account:** Dedicated Alpaca Paper account · **Starting balance:** $100,000 · **Execution:** Options only

## Overview

Atlas is a two-regime, defined-risk options agent built on Alpaca's Trading API and MCP Server.
Instead of a single static strategy, Atlas classifies each cycle into range or trend, routes range
conditions into an iron condor and trend conditions into a directional credit spread, and gates all
new entries behind a macro-regime signal derived from an independently-verified Weinstein-stage
classifier. Every short option leg is protected by a long option leg — the system never holds
undefined risk.

## AI Logic

At each cycle, Atlas computes ADX(14) and EMA(20/50) on daily bars per underlying to classify the
technical regime, then reads a macro overlay signal (SPY Weinstein stage, computed by an independent
daily batch process) that blocks *all* new entries when the broad market is in a confirmed downtrend
— not because the technical setup is wrong, but because a system should not add new short-premium
risk into a deteriorating macro tape. This two-layer design (local technical signal + independent
macro signal) means Atlas's entry decisions are never based on a single indicator family.

```
Technical regime (ADX/EMA)  →  Macro gate (Weinstein stage, independent DB)  →  Option structure
```

| Regime | Structure | Legs |
|---|---|---|
| Range (`ADX < 18`) | Iron condor | Short put + long put, short call + long call |
| Trend (`ADX > 20`) | Directional credit spread | Short leg + protective long leg |
| Macro-blocked | Cash | No new entries |

## Risk Gates

Atlas never holds a naked short option — every entry is a defined-max-loss structure. Position size
is computed from that structure's maximum theoretical loss, not from premium received:

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

Atlas separates signal computation from order execution by design:

```
Market/option-chain data (alpaca-py, read-only)
        → regime classification + macro gate (pure Python, unit-tested)
        → option leg selection by target delta
        → risk-gated position sizing
        → order-intent dict matching the MCP place_option_order schema
        → Alpaca MCP Server: place_option_order (multi-leg, order_class="mleg")
        → decision + audit log
```

The signal layer has zero broker dependency and is fully covered by unit tests that run without any
API credentials — regime classification, risk sizing, and order-intent construction are all verified
against synthetic data before ever touching the account. Actual order submission is deliberately
routed through the Alpaca MCP Server's `place_option_order` tool rather than a raw SDK call, so the
agent's trading actions are auditable at the same layer an operator would use to inspect or replay
them.

## Creativity & Engagement

Atlas's macro gate reuses an independently-built, separately-verified regime signal rather than
re-deriving market state inside the trading loop — a deliberate choice to avoid a single point of
signal failure. Each cycle's decision (regime, macro state, chosen structure or skip reason) is
logged as a structured record, making every trade traceable back to the exact technical + macro
condition that produced it.

*Educational competition submission only; options trading involves substantial risk, including loss
of capital and assignment risk.*
