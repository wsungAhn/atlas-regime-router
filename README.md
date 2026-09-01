# Atlas Regime Router

**Backtest-selected, defined-risk options agent for Alpaca (with a crypto sleeve)** — built for the [lablab.ai x Alpaca "AI Trading Agents" Hackathon](https://lablab.ai) (Aug 28 – Sep 4, 2026).

**Competition Alpaca paper account:** `PA3Q19GCQ076` (created fresh for this hackathon on 2026-08-28, starting balance $100,000).

## What it does

Atlas is an options + crypto trading agent whose core strategy was picked by backtesting seven candidates against an identical simulator, not by intuition. At each 15-minute cycle it:

1. Computes ADX(14)/EMA(20/50) regime per underlying (SPY, QQQ, GLD, TLT, SLV, IWM) plus a spot crypto sleeve (BTC/USD, ETH/USD).
2. Routes range regimes to an iron condor, confirmed trends to a directional credit spread — every short leg is protected by a long leg, no naked risk.
3. Checks an independent macro gate (Weinstein stage) that blocks all new entries when the broad market is in a confirmed downtrend.
4. Sizes positions from realized volatility (2–10% of equity) against a defined maximum loss, never against premium received.
5. Submits the order intent to the [Alpaca MCP Server](https://github.com/alpacahq/alpaca-mcp-server) — no LLM call sits in the execution path; the agent is a deterministic MCP client.

Three account-level circuit breakers (daily -20%, weekly -50%, portfolio drawdown -20% from all-time-high) run every cycle independent of the technical signal, and only ever gate new risk-taking — exit monitoring on open positions is never blocked.

See [`docs/one-page-submission.md`](docs/one-page-submission.md) for the full writeup: strategy selection process, backtest methodology and results (+93.5% over 3 years, 1,330 trades, 68.1% win rate — see the caveats in that doc), six real bugs found and fixed during development, and the crypto sleeve.

## Repo layout

- `src/signals.py` — pure signal computation (regime classification, macro gate, order-intent generation). No broker dependency, fully unit-tested against synthetic data.
- `src/mcp_runner.py` — the live 15-minute cycle: fetches account/position state via the Alpaca MCP Server, evaluates exits first, then new entries, with stale-order cancellation and a market-reject → limit-price fallback for illiquid names.
- `src/backtest.py` — the Black-Scholes credit-spread simulator used to select and validate the strategy, sharing position-sizing and circuit-breaker code with the live loop.
- `src/health_check.py` — a 15-minute watchdog that reconciles broker state against local state and raises a native macOS alert if they drift.
- `docs/` — design docs, the submission one-pager, and strategy-variant writeups (including the losing candidates, kept as a record).
- `reports/` — daily auto-generated P&L reports, one per trading day since 2026-08-25.
- `registry/decisions.jsonl` (gitignored, local only) — a structured log of every cycle's decision (regime, macro state, risk-gate status, chosen structure or skip reason).

## Running it

```bash
.venv/bin/python -m pytest tests/ -q   # pure-logic test suite, no broker keys needed
```

Live cycles require a `.env.competition` file with Alpaca paper-trading API keys (see `.env.competition.example`) and run via two independent `launchd` schedulers: one for options (market hours only), one for crypto (24/7). Neither schedule commits real money — this is a paper-trading account for the competition.

## Risk gates (full detail in the one-pager)

- Per-trade risk: 2–10% of equity, scaled down as realized volatility rises.
- Cash reserve floor: 10% of equity at all times.
- Daily/weekly kill switches: -20% / -50% halt new entries for the rest of that window.
- Portfolio drawdown circuit breaker: -20% from the account's all-time equity high halts new entries for 45 minutes, edge-triggered (fires once per drawdown event, only re-arms after a genuine recovery).
- Forced close inside 2 DTE regardless of P&L.
- Crypto sleeve shares the same account-level breakers, additionally capped by real non-marginable buying power.

---

*Educational competition submission only; options and crypto trading involve substantial risk, including loss of capital and, for options, assignment risk.*
