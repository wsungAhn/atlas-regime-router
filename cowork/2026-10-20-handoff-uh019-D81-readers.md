# Codex Handoff — 2026-10-20 — atlas reader repoint to the Universe Hub quote store (UH-019 D8.1)

> **상태(Status):** `검토대기 / review-pending`
> **시작 기록(Started by):** Codex (`gpt-5.6-luna`, medium) — 2026-10-20
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
#### 8-1 Files changed
- `src/friction.py`
- `tests/test_friction.py`
- `research/os1-edge-2026-10/panel_edge.py`
- This handoff

No commit created.

#### 8-2 Tests
Final command:
`cd /Users/Mung/dev/atlas-options-hackathon && .venv/bin/python -m pytest tests/ -q -p no:cacheprovider`

Result: `376 passed, 1 warning in 4.28s`.

#### 8-3 raw pytest tail
```text
...................................................................... [ 19%]
........................................................................ [ 38%]
........................................................................ [ 57%]
........................................................................ [ 76%]
........................................................................ [ 95%]
................                                                         [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6
  /Users/Mung/dev/atlas-options-hackathon/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html
    warnings.warn(  # deprecated in 14.0

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
376 passed, 1 warning in 4.28s
```

#### 8-4 real-store run
Command:
`cd /Users/Mung/dev/atlas-options-hackathon && .venv/bin/python -m src.friction --out /tmp/hs.csv`

Exit code: `0`

First lines of `/tmp/hs.csv`:
```text
class,bucket,n,h,fallback
index_etf,<0.10,27074,0.010000000000000002,False
index_etf,0.10-0.50,17135,0.009999999999999981,False
index_etf,0.50-2.00,13126,0.011823529411764715,False
index_etf,>2.00,98230,0.48499999999999943,False
```

At least one cell has `n>0` (all 12 cells do).

#### 8-6 judgment calls
- Added `main(argv=None)` and kept the default as a runtime `None` resolution to `HUB_QUOTE_ROOT`, so the integration test can monkeypatch `friction.HUB_QUOTE_ROOT` and invoke the real CLI entry in-process. No environment-variable override was added.
- Read each parquet file without a column subset, then selected only `underlying`, `bid`, `ask`, `mid`, and `dte` after concatenation, as required for the hub `date=*` layout.
- Added `timeout=60` to the subprocess-based CLI test.

#### 8-7 self-score
Deductions: none.

- Reader correctness on hub layout: `5/5`
- Fail-loud on empty/stale path: `5/5`
- Test strength: `5/5`
- Scope: `5/5`

### Round 2
**Executed:** 2026-10-20 | **Result:** two reviewer-requested test gaps added; no `src/` change required

#### 8-1 Files changed
- `tests/test_friction.py`
- This handoff

No commit created.

#### 8-2 Tests
Final command:
`cd /Users/Mung/dev/atlas-options-hackathon && .venv/bin/python -m pytest tests/ -q -p no:cacheprovider`

Result: `378 passed, 1 warning in 8.77s`.

#### 8-3 raw pytest tail
```text
........................................................................ [ 19%]
........................................................................ [ 38%]
........................................................................ [ 57%]
........................................................................ [ 76%]
......
.................................................................. [ 95%]
..................                                                       [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6
  /Users/Mung/dev/atlas-options-hackathon/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
378 passed, 1 warning in 8.77s
```

#### 8-6 judgment calls
- Added exactly the two reviewer-requested tests: all-unreadable parquet files raise `ValueError`; the real hub default constant and no-argument default reader path are asserted.
- No `src/` change was needed because both tests pass against the existing implementation.

#### 8-7 self-score
Deductions: none.

- Reader correctness on hub layout: `5/5`
- Fail-loud on empty/stale path: `5/5`
- Test strength: `5/5`
- Scope: `5/5`

## 9. Review log

### Round 1
**Reviewed:** 2026-10-20 | **Verdict:** changes requested (two small test gaps)

Verified directly: diff read (friction.py, test_friction.py, panel_edge.py); suite 376 passed; real-store CLI `python -B -m src.friction --out ...` against `~/data/universe-hub/options/quotes` exits 0 with real n values (index_etf 27,074 / 17,135 / 13,126 ...). Mutation sweep on `src/friction.py` (7 mutations, `tests/test_friction.py` only): 4 KILLED, 3 SURVIVED. `empty_paths_no_raise` is equivalent (the second `if not frames` raise covers it) — no action. Real gaps:
1. `all_unreadable_no_raise`: when files exist but every one is unreadable, `build_half_spread_table` must raise the ValueError — add a test with a corrupt `date=*/x.parquet` (garbage bytes) as the only file.
2. `default_root_changed`: nothing asserts the real default; add a test `friction.HUB_QUOTE_ROOT == Path.home() / "data/universe-hub/options/quotes"` and that `build_half_spread_table()` with `panel_dir=None` globs `HUB_QUOTE_ROOT` (monkeypatch it to a tmp hub-layout dir and call without arguments).
Change `src/` only if a test reveals a real defect (say so in §8-6). Paste the raw pytest tail in §8-3. Do not touch other files.


### Round 2
**Reviewed:** 2026-10-20 | **Verdict:** approved

Verified directly: suite 378 passed (376 + 2); my 5 reader mutations all KILLED (glob pattern, all-unreadable raise, default root constant, CLI default, default ignored); real-store CLI run exit 0 (round 1). `src/` unchanged since round 1 review (tests only).
