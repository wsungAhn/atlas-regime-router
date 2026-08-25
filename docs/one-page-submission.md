# Atlas Options Engine
## Backtest-Selected, Defined-Risk Options Agent for Alpaca Paper Trading

**Competition account:** Dedicated Alpaca Paper account · **Starting balance:** $100,000 · **Execution:** Options only

## Overview

Atlas is a defined-risk options agent whose core strategy was chosen by backtesting, not intuition.
We implemented seven candidate strategies — five from a published ATR-grid options framework, the
original strategy from an earlier related project, and a first-draft regime engine of our own — and
ran all seven through an identical Black-Scholes credit-spread simulator over three years of SPY and
QQQ data, then iterated the winner three more times as new evidence surfaced: weekly-DTE options for
turnover, a wider dynamic risk budget (2–10% of equity) to close a return gap versus prior
competitions, and — critically — an account-level circuit breaker after the wider risk budget
revealed drawdowns the win-rate/Sharpe numbers alone didn't show. Every short option leg is protected
by a long option leg; the system never holds undefined risk.

## AI Logic: Backtest-Selected Regime Router

At each cycle, Atlas computes ADX(14) and EMA(20/50) on daily bars per underlying. Range regime
(ADX < 18) routes to an iron condor; confirmed trend regimes (ADX > 20, EMA alignment) route to a
directional credit spread. Position size is not fixed — it scales with realized volatility, 2–10% of
equity per trade based on the ATR% percentile against its own trailing year. A macro overlay (SPY
Weinstein stage, from an independently-verified daily batch signal) blocks *all* new entries when the
broad market is in a confirmed downtrend, regardless of the local technical read. Options target 5–9
DTE (weekly) rather than monthly — the backtest showed the original 30–45 DTE design was
statistically sound (88% win rate) but far too low-turnover to be competitive on realized dollars.

```
Technical regime (ADX + EMA)  →  Macro gate (Weinstein stage, independent DB)  →  Structure
```

Three account-level circuit breakers run every cycle, independent of the technical/macro read:
daily loss ≥ 3% and weekly loss ≥ 6% both block new entries for the remainder of that window; a
drawdown ≥ 20% from the account's all-time high halts *all* new entries for 45 minutes (the
live loop checks every 15 minutes, so this is a 2-cycle pass before a 3rd-cycle resume). None of the
three ever cancels or reduces an already-open position's exit monitoring — risk-reduction is never
gated, only new risk-taking is.

### The backtest that selected this design

Judging weighs realized P&L, so that's the primary ranking criterion below — not a risk-adjusted
score, though we did use risk metrics (drawdown specifically) to catch a design gap the P&L number
alone would have hidden. Real dollar P&L on a simulated \$100,000 account shared across SPY and QQQ
(the live system's actual universe — one account, not two independent \$100k backtests), 3 years of
data, using the exact constraints the live system runs under: the same volatility-scaled risk budget,
real contract counts at the 100x multiplier, exit monitoring re-checked every 15 minutes against
intraday price (not just once at daily close) to match the live loop's actual cadence, and at most
one open position per symbol at a time:

| Metric | Value |
|---|---:|
| Trades (3 years, SPY+QQQ combined) | 403 |
| Realized P&L | **+\$80,720 (+80.7%, ≈27%/yr)** |
| Win rate | 75.9% |
| Max drawdown | \$8,664 (8.7% of starting equity) |
| Profit factor | 1.85 |

This clears the ≈18–20%/yr bar we benchmarked against a prior competition's winning result, with a
drawdown small enough that the account survives the path that produced it — not just the endpoint.
One honest caveat: the 15-minute exit re-check assumes fills at the theoretical Black-Scholes price
with no bid/ask spread or slippage, so this number is closer to an upper bound than a point estimate
— checking more often than once a day is a real, live-matching improvement, but the exact magnitude
should be read with that in mind.

Strategy 7 (the ADX/EMA regime router above) beat strategy 5 (a narrower ADX+RSI+VWAP condor that
led an earlier pass of this backtest) once the risk budget widened and turnover increased — 5's
extra selectivity stopped paying for itself once frequency became the binding constraint. Strategy 6
(the pullback-trigger design carried over from an earlier related project) placed last on realized
P&L in every backtest pass we ran; we did not ship it on reputation, we tested it against six
alternatives and it consistently lost money.

Five real bugs surfaced and got fixed during this process, not after — each found by refusing to
accept a backtest result that looked plausible without checking it against a stated assumption:
(1) a VWAP proxy using an expanding mean from dataset start, silently zeroing a strategy's trade
count; (2) a credit-spread width inherited from a prior project's leveraged-ETF price level,
producing unrealistically wide spreads on SPY; (3) the backtest allowing 3 concurrent positions per
side while the live re-entry guard caps at 1; (4) the position-sizing function that compounds equity
across trades never actually updated its running balance — every trade in every backtest was sized
off the *starting* \$100k regardless of prior wins or losses, an error only caught because a
4-symbol combined-account run matched the sum of 4 independent single-symbol runs to the dollar,
which is only possible if nothing was compounding; (5) the daily/weekly/portfolio circuit breakers
above were defined as constants from the start of the project but never actually wired into either
the backtest or the live decision loop — a 4-symbol stress test hitting a 63%+ drawdown with no
circuit breaker engaging is what exposed it.

## Risk Gates

Every entry is a defined-max-loss structure; position size is computed from that structure's
maximum theoretical loss, never from premium received:

```
risk_budget = equity × clip(0.10 − 0.08 × atr_percentile, 0.02, 0.10)
contracts   = floor(risk_budget / max_loss_per_contract)
```

- Strategy-bundle risk: 2–10% of equity, scaled down as realized volatility rises.
- Cash reserve floor: 10% of equity at all times.
- Daily loss kill switch: −3% of equity halts new entries for the rest of that day.
- Weekly loss kill switch: −6% halts new entries for the rest of that week.
- Account drawdown circuit breaker: −20% from the all-time equity high halts *all* new entries
  for 45 minutes, live-verified against the same account state the trading loop reads — the halt
  never touches an already-open position's exit monitoring, and never resets the drawdown itself
  (the high-water mark only ever moves up; a halt is a pause on new risk, not an erasure of loss).
- Forced close inside 2 DTE regardless of P&L (gamma-risk avoidance near weekly expiry).
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
backtest harness, one winner chosen on realized P&L rather than familiarity or narrative — and that
process didn't stop once a winner was picked. When the first pass cleared a modest bar but fell well
short of a competitive one, we widened the risk budget and shortened DTE to chase turnover, which is
exactly what surfaced a drawdown the earlier, narrower backtest never exposed: three risk gates that
had been *defined* in the spec since day one but never actually *wired in*, anywhere. The circuit
breaker in this submission exists because a stress test was allowed to fail loudly instead of being
declared done at a headline number. The losing strategies stay in the repo as a record of what didn't
work and why, including a strategy literally inherited from a prior project that we were prepared to
keep — and dropped once the numbers said not to. Each live cycle's decision (regime, macro state,
risk-gate status, chosen structure or skip reason) is logged as a structured record, making every
trade traceable back to the exact technical + macro + risk condition that produced it, and a
scheduled job turns that log into a same-day report the moment the market closes.

*Educational competition submission only; options trading involves substantial risk, including loss
of capital and assignment risk.*
