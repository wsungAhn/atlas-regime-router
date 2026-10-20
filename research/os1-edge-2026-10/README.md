# Exploratory edge research (2026-10-19/20) — scripts and small outputs

Ad-hoc, **not production code**, not reviewed by the handoff process. Scripts hardcode `/private/tmp/claude-502/bt/` paths and need `ALPACA_API_KEY/SECRET` from `.env.competition` (atlas main). Re-point the paths before re-running.

- `pilot.py`, `btlib.py`: Alpaca historical option contracts/bars helpers, Black-Scholes IV/delta.
- `run_bt.py` -> `samples.jsonl`: Friday-expiry 0.20-delta, 1.5%-wide spreads on 9 ETFs (7-day entry, daily-close prices).
- `run_chain.py` -> `chains.jsonl` (5 MB, not kept; regenerate): full near-the-money daily chain per expiry for delta/width grids.
- `analyze.py`, `filt.py` (IV/RV regime filters, train/test), `grid.py` (delta x width grid).
- `ts_vol.py`, `ts_ivrv.py` -> `ts_vol.json`, `ts_origin.json`: Timestral-123M realized-vol forecasts (needs a separate venv with `pip install git+https://github.com/mistralai/timestral.git`).
- `replay_edge.py` (win/loss structure of the live-entry replay rows), `panel_edge.py` (3-week quote panel).

Conclusions: `docs/report-options-selling-closeout-2026-10-20.md`.
