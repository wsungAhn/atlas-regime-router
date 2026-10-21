# Codex Handoff — 2026-10-20 — atlas reader repoint to the Universe Hub quote store (UH-019 D8.1)

> **상태(Status):** `대기중 / pending`
> **시작 기록(Started by):** _(비워둠)_
> **작성자:** Claude (reviewer) → **수행자:** Codex (`gpt-5.6-luna`, medium)
> **설계 근거:** `/Users/Mung/dev/universe-hub/docs/design-uh019-option-quote-panel.md` section D8.1 (read it) and D2 (hub layout). The hub already holds the migrated data: `~/data/universe-hub/options/quotes/date=YYYY-MM-DD/<YYYYMMDDTHHMMSSffffffZ>.parquet` (58 snapshots, 331,206 rows; one file = one snapshot; columns ts[timestamp us UTC], underlying, occ, bid, ask, mid, delta, dte, feed).
> **범위:** IN: `src/friction.py`, `tests/test_friction.py`, `research/os1-edge-2026-10/panel_edge.py`. OUT: everything else (do not touch `scripts/collect_option_quotes.py`, plists, `~/data`, the hub repo, `~/Library/LaunchAgents`). Do not commit. Atlas main has unrelated untracked `reports/*` — ignore them.

## 0. Environment
- Interpreter/tests: `cd /Users/Mung/dev/atlas-options-hackathon && .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` — baseline **376 passed**.
- Stamp "Started by" first; status `진행중` → `검토대기`; unsure → stop and write in §8-6.
- IMPORTANT (measured): in the hub venv, `pd.read_parquet(f, columns=[...])` over several files hangs at interpreter exit. This atlas venv is different, but to be safe read each file with `pyarrow.parquet.read_table(path).to_pandas()` (no column subset) or `pd.read_parquet(path)` without `columns`; every test subprocess needs a `timeout=`.
- A path inside a `date=YYYY-MM-DD/` dir may make pyarrow add a `date` column on dataset-style reads; build the panel using only the needed columns (`underlying, bid, ask, mid, dte`) after reading.

## 1. Changes (D8.1)
1. `src/friction.py`: add `HUB_QUOTE_ROOT = Path.home() / "data/universe-hub/options/quotes"`. `build_half_spread_table(panel_dir=HUB_QUOTE_ROOT)` globs `Path(panel_dir).glob("date=*/*.parquet")` (sorted). **When the glob finds no file, raise `ValueError(f"no quote panel files under {panel_dir}")`** instead of returning an empty table (a stale path like `data/quote_panel` must therefore error out). Keep the per-file read-error logging/skipping behaviour for unreadable files, but if every file fails to read the same ValueError must be raised. CLI `--panel-dir` (currently `required=True`, ~line 309) becomes optional with default `HUB_QUOTE_ROOT`; `write_half_spread_table(panel_dir, out_path)` passes the value through (give it the same default).
2. `tests/test_friction.py`: fixtures write the hub layout (`tmp/date=2026-10-01/<ts>.parquet`, with `ts` as ISO string or timestamp — either is fine for the reader); the empty-dir call around line 44 becomes `pytest.raises(ValueError)`; delete `test_positions_files_are_ignored` (hub layout has no positions files); add an **integration test** that monkeypatches `friction.HUB_QUOTE_ROOT` to a tmp dir holding a hub-layout file and runs the real CLI entry (`subprocess.run([sys.executable, "-m", "src.friction"/script form used by existing CLI tests, "--out", csv], timeout=60)` — follow how `src/friction.py` is already invoked in tests) **without `--panel-dir`**: it must exit 0 and the CSV must have `n>0` in at least one cell; and the same CLI with an empty or old-layout dir (`--panel-dir <dir with 20*.parquet files directly inside>`) must exit non-zero. Because a subprocess cannot see a monkeypatch, make the default overridable through an environment variable ONLY IF that is the simplest way; otherwise call `main`/`__main__` logic through a small `main(argv=None)` function you add and test it in-process with `monkeypatch.setattr(friction, "HUB_QUOTE_ROOT", tmp)`. State your choice in §8-6.
3. `research/os1-edge-2026-10/panel_edge.py` line 4: glob `os.path.expanduser('~/data/universe-hub/options/quotes/date=*/*.parquet')`; drop the `positions`/`r1` filters (the r1 snapshot is now included by user decision); `raise SystemExit("no quote panel files")` if the list is empty. Read files without a column subset.
4. After the change, run the real default CLI once against the live hub store to prove it works (read only): `cd /Users/Mung/dev/atlas-options-hackathon && .venv/bin/python -m src.friction --out /tmp/hs.csv` (or the invocation the repo uses) and paste the first lines of the CSV and exit code in §8-4. It must exit 0 with `n>0` in at least one row.

## 6. Done criteria
- [ ] 3 files changed (+ this handoff), no commit; full suite ≥ 376 (count may differ by the deleted/added tests — state the exact numbers), raw pytest tail in §8-3
- [ ] real-store CLI run output pasted (§8-4)
- [ ] every new assertion fails if its behaviour is removed (reviewer injects mutations: glob pattern reverted, ValueError removed, CLI default removed, positions filter re-added)
Rubric: Reader correctness on hub layout gate 5 · Fail-loud on empty/stale path gate 5 · Test strength 4+ · Scope 4+.

## 8. Executor response
### 8-1 Files changed / 8-2 Tests / 8-3 raw pytest tail / 8-4 real-store run / 8-6 judgment calls / 8-7 self-score
_(write here)_

## 9. Review log
_(empty)_
