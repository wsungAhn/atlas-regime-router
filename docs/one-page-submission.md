# Atlas Options Engine
## Backtest-Selected, Defined-Risk Options Agent for Alpaca Paper Trading (with a Crypto Sleeve)

**Competition account:** Dedicated Alpaca Paper account. **Starting balance:** $100,000. **Execution:** Options (6 underlyings) plus a crypto spot sleeve.

## Overview

Atlas is a defined-risk options agent whose core strategy was chosen by backtesting, not intuition. We implemented seven candidate strategies (five from a published ATR-grid options framework, the original strategy from an earlier related project, and a first-draft regime engine of our own), ran all seven through an identical Black-Scholes credit-spread simulator, then kept iterating the winner as new evidence surfaced: weekly-DTE options for turnover, a wider dynamic risk budget (2-10% of equity), an account-level circuit breaker after the wider risk budget revealed drawdowns the win-rate and Sharpe numbers alone didn't show, and, one day before submission, a bug in that same circuit breaker that had been silently freezing the 3-year backtest after its first drawdown and reporting a partial-period result as the full one. Every short option leg is protected by a long option leg; the system never holds undefined risk.

## AI Logic: Backtest-Selected Regime Router

At each cycle, Atlas computes ADX(14) and EMA(20/50) on daily bars per underlying. Range regime (ADX < 18) routes to an iron condor; confirmed trend regimes (ADX > 20, EMA alignment) route to a directional credit spread. Position size scales with realized volatility, 2-10% of equity per trade based on the ATR% percentile against its own trailing year. A macro overlay (SPY Weinstein stage, from an independently verified daily batch signal) blocks all new entries when the broad market is in a confirmed downtrend, regardless of the local technical read. Options target 5-9 DTE (weekly) rather than monthly; the original 30-45 DTE design had a strong win rate but too little turnover to be competitive on realized dollars.

```
Technical regime (ADX + EMA)  ->  Macro gate (Weinstein stage, independent DB)  ->  Structure
```

Three account-level circuit breakers run every cycle, independent of the technical and macro read: daily loss >= 20% and weekly loss >= 50% both block new entries for the rest of that window; a drawdown >= 20% from the account's all-time high halts all new entries for 45 minutes (the live loop checks every 15 minutes, so this is a 2-cycle pass before a 3rd-cycle resume). After that halt, trading resumes even if the account is still below the 20% line; the breaker only re-arms once equity recovers above it, so a single sustained drawdown pauses the system once rather than freezing it. None of the three ever cancels an already-open position's exit monitoring; risk reduction is never gated, only new risk-taking is.

### The backtest that selected this design

Judging weighs realized P&L, so that's the primary number below, though drawdown mattered too: it's what caught the design gap the P&L number alone would have hidden twice (see the bug list). Real dollar P&L on a simulated $100,000 account shared across all six live underlyings (SPY, QQQ, GLD, TLT, SLV, IWM), one account, not six independent backtests, 3 years of data, using the exact constraints the live system runs under: the same volatility-scaled risk budget, real contract counts at the 100x multiplier, exit monitoring re-checked every 15 minutes against intraday price rather than once at daily close, and at most one open position per symbol at a time:

| Metric | Value |
|---|---:|
| Trades (3 years, 6 underlyings combined) | 1,330 |
| Realized P&L | **+$93,518 (+93.5%, roughly 25%/yr)** |
| Win rate | 68.1% |
| Max drawdown | $38,365 (38.4% of starting equity) |
| Profit factor | 1.23 |

One honest caveat: the 15-minute exit re-check assumes fills at the theoretical Black-Scholes price with no bid/ask spread or slippage, so this number is closer to an upper bound than a point estimate.

Strategy 7 (the ADX/EMA regime router above) beat strategy 5 (a narrower ADX+RSI+VWAP condor that led an earlier pass of this backtest) once the risk budget widened and turnover increased; strategy 5's extra selectivity stopped paying for itself once frequency became the binding constraint. Strategy 6 (the pullback-trigger design carried over from an earlier related project) placed last on realized P&L in every backtest pass we ran; we did not ship it on reputation, we tested it against six alternatives and it consistently lost money.

Six real bugs surfaced and got fixed during this process, not after, each found by refusing to accept a backtest result that looked plausible without checking it against a stated assumption:

1. A VWAP proxy using an expanding mean from dataset start, silently zeroing a strategy's trade count.
2. A credit-spread width inherited from a prior project's leveraged-ETF price level, producing unrealistically wide spreads on SPY.
3. The backtest allowing 3 concurrent positions per side while the live re-entry guard caps at 1.
4. The position-sizing function that compounds equity across trades never actually updated its running balance; every trade in every backtest was sized off the starting $100k regardless of prior wins or losses, caught because a 4-symbol combined-account run matched the sum of 4 independent single-symbol runs to the dollar, which is only possible if nothing was compounding.
5. The daily, weekly, and portfolio circuit breakers had been defined as constants since day one but never actually wired into either the backtest or the live decision loop; a 4-symbol stress test hitting a 63%+ drawdown with no circuit breaker engaging is what exposed it.
6. Found one day before submission: the portfolio circuit breaker re-evaluated the same 20%-drawdown condition every cycle after its 45-minute halt expired, so if the account was still below the line, it re-triggered indefinitely. In the closed-loop backtest this meant the account froze after its first breach and never traded again; a 3-year run had effectively become a 4.5-month run reported as a 3-year one, turning a strategy that returns +93.5% into one that looked like it lost 20.6%. Fixed by making the breaker edge-triggered: it fires once per drawdown event, resumes trading after 45 minutes regardless of whether equity has recovered, and only re-arms after a genuine recovery above the 20% line.

## Crypto Sleeve

Added the day before submission after live options trading confirmed the shared-account, shared-risk-gate architecture works as designed. BTC/USD and ETH/USD, spot only (Alpaca crypto has no margin, so short positions aren't possible), same regime router as the options engine (long when the underlying is in a confirmed ADX/EMA uptrend, flat otherwise), same $100,000 account and the same daily, weekly, and portfolio drawdown circuit breakers. Entry sizing targets a fixed dollar risk on a 2x-ATR stop with a 2R profit target and a 10-day max hold, capped by the account's actual non-marginable buying power (crypto is cash-secured; a naive equity-based sizing formula asked for more than the account could pay for the first time it ran live, since the options positions already had cash committed as margin). Runs on its own 24/7, 15-minute-interval schedule, independent of the options engine's market-hours schedule, so it isn't idle outside 9:30-16:00 ET. First live fills: BTC/USD filled the first cycle it ran; ETH/USD filled once sizing was corrected to respect actual available cash.

## Risk Gates

Every option entry is a defined-max-loss structure; position size is computed from that structure's maximum theoretical loss, never from premium received:

```
risk_budget = equity * clip(0.10 - 0.08 * atr_percentile, 0.02, 0.10)
contracts   = floor(risk_budget / max_loss_per_contract)
```

- Strategy-bundle risk: 2-10% of equity, scaled down as realized volatility rises.
- Cash reserve floor: 10% of equity at all times.
- Daily loss kill switch: -20% of equity halts new entries for the rest of that day.
- Weekly loss kill switch: -50% halts new entries for the rest of that week.
- Account drawdown circuit breaker: -20% from the all-time equity high halts all new entries for 45 minutes, then resumes; it only re-arms after equity recovers above that line, so a sustained drawdown pauses the system once rather than freezing it permanently. The halt never touches an already-open position's exit monitoring, and never resets the drawdown itself; the high-water mark only ever moves up.
- Forced close inside 2 DTE regardless of P&L, to avoid gamma risk near weekly expiry.
- Macro-blocked cycles produce zero new entries regardless of technical signal strength.
- Crypto sleeve shares the same account-level circuit breakers; entries are additionally capped by real non-marginable buying power so a sizing formula can't ask for more cash than the account actually has free.

## Alpaca Infrastructure Implementation

Atlas separates signal computation, backtesting, and order execution into independently verifiable layers:

```
Market/option-chain data (alpaca-py, read-only)
        -> regime classification + macro gate (pure Python, unit-tested)
        -> option leg selection by target delta / crypto direction check
        -> risk-gated position sizing
        -> order-intent dict matching the MCP place_option_order / place_crypto_order schema
        -> Alpaca MCP Server (stdio protocol, no LLM in the execution loop)
        -> decision + audit log
```

The signal layer has zero broker dependency and is fully unit-tested against synthetic data before ever touching the account. Order submission runs unattended on two independent schedulers (options: market hours; crypto: 24/7) and talks to the Alpaca MCP Server as a deterministic MCP client, the same server an operator would use interactively to inspect account state, so the automated agent's trading actions are auditable at that exact layer, with no LLM call in the critical execution path. The backtest layer reuses a previously built and reviewed Black-Scholes credit-spread simulator rather than a from-scratch pricer, keeping the evaluation methodology itself checkable, and now shares its position-sizing and circuit-breaker code with the live loop rather than duplicating the logic in two places.

## Creativity & Engagement

Atlas's headline claim isn't a strategy, it's a selection and verification process: seven strategies, one shared backtest harness, one winner chosen on realized P&L rather than familiarity or narrative, and a process that kept finding and fixing its own mistakes right up to the day before submission. When the first backtest pass cleared a modest bar but fell short of a competitive one, we widened the risk budget and shortened DTE to chase turnover, which is exactly what surfaced a drawdown the earlier, narrower backtest never exposed: risk gates that had been defined in the spec since day one but never actually wired in anywhere. The day before submission, a sharp question about why a supposedly strong backtest suddenly looked negative led to finding that the circuit breaker meant to protect the account from a 45-minute halt was instead freezing the entire 3-year simulation after its first drawdown; fixing it turned a -20.6% 3-year result into +93.5%. The losing strategies stay in the repo as a record of what didn't work and why, including a strategy inherited from a prior project that we were prepared to keep and dropped once the numbers said not to. The crypto sleeve exists because the same account and risk-gate architecture, once trusted in production for options, was cheap to extend to a second asset class rather than a reason to rebuild from scratch. Each live cycle's decision (regime, macro state, risk-gate status, chosen structure or skip reason) is logged as a structured record, making every trade traceable back to the exact technical, macro, and risk condition that produced it, and a scheduled job turns that log into a same-day report the moment the market closes.

*Educational competition submission only; options and crypto trading involve substantial risk, including loss of capital and, for options, assignment risk.*
