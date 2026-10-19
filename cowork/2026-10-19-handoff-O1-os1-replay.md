# Codex Handoff — O1 OS-1 cap-applied replay analysis — 2026-10-19

> **상태(Status):** `대기중 / pending`
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-19T19:00-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** 새 `src/os1_replay.py` + `tests/test_os1_replay.py`. 라이브 진입 고정 replay(T8)의 만기 보유 행(X2)에 OS-1 캡(O2 `size_os1`)을 진입 시각 순으로 적용해 스킵·qty 재척도화 후 손익·동시 노출을 재계산하고, ETF 9종·크레딧/폭 부분집합과 X2 대비 A·C 쌍대 차이를 보고한다. 설계: `docs/design-options-only-selling-2026-10-19.md` §3-1, §3-2, §8 O1 행.
> **범위(IN):** 위 파일 2개. **OUT:** 다른 파일 수정(읽기 전용), 네트워크, 라이브 경로, 커밋. 만기 봉 핀 구간 비율(O1b)은 이번 범위 **밖**.

## 0. 규칙
- 하지 말 것: 범위 밖 수정 · 네트워크 · 커밋 · `src.` 임포트(`sys.path.insert(0, str(Path(__file__).resolve().parent))` 후 `from friction_compare import ...`; 테스트는 `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))`) · 미사용 임포트/상수 · 테스트에서 구현 상수 임포트(리터럴 단언) · **데이터 구조 추측 금지**: §2 정본 파일을 읽고 키를 확인하라 · 이미 있는 헬퍼 재구현 금지(`friction_compare._episode_key/_width`, `friction_calibrate.load_inputs/data_period/to_jsonable`, `friction_backtest.load_live_episodes`, `friction_stats.paired_diff/bootstrap_ci`, `os1_sizing.size_os1`).
- 항상: 테스트 실행 → `## 8`에 **직접** 작성(감점 사유 먼저, 변이별 pytest 실패 **원문**, 채팅 출력 금지) → 상태 `검토대기`. 변이는 같은 규칙이 구현된 **모든 위치(형제 분기)** 각각 주입. 변이 실행 시 `python -B`로 pyc 영향을 배제하라. 같은 워크트리에서 다른 Codex 작업이 병렬로 돌고 있지 않다.
- 환경: 워크트리 `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`, 인터프리터 `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`, 테스트 `… -m pytest tests/ -q`, **베이스라인 358 passed**.

## 1. 해석상 제약 (보고서 `## Interpretation limits`에 그대로)
시뮬레이터 보정 등급은 FAIL(T-d). 이 분석은 **탐색용·미보정**이며 절대 달러 수준은 신뢰하지 않는다. 만기 정산 교차 비용을 0으로 둔 모델이므로 X2 우위는 과대평가일 수 있다. 캡 적용은 `e_now = equity`(마크 미반영)로 근사하고, 손익은 qty에 선형(`pnl × qty_new/qty_orig`)이라고 가정한다. 표본: 라이브 진입 119개(2026-08-25~10-06 단일 레짐), 부분집합은 더 작다. 이것은 승격 근거가 아니다.

## 2. 기존 코드 (읽기 전용, 먼저 읽어라)
- `src/friction_compare.py`: `run_variants(episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, scenarios, r, s, equity) -> {scenario: {variant_id: {"rows", "skip_counts"}}}`, `VARIANTS`(X0, X1, X2, A1/A2/A5, C2/C3/C5), `_episode_key(row)` = `(symbol, naive-UTC entry_ts)`, `_width(episode)`, `main`의 인자/출력/원자적 쓰기/덮어쓰기 금지/`__main__` 가드 패턴(**그대로 따른다**).
- 행 키: `symbol, entry_ts, exit_ts, realized_pnl, exit_reason, minutes_held, regime, ...` (`src/friction_calibration.py`의 `replay` 반환 행을 읽어 확인).
- 에피소드(`friction_backtest.load_live_episodes(registry/decisions.jsonl)`): `symbol, entry_ts(UTC tz-aware), regime, legs(OCC 문자열 리스트), n_legs, qty, limit_credit, kind, ...`. 레그 파싱은 `friction.parse_occ`(반환 튜플 형태는 `_width`의 사용을 읽어 확인).
- `src/os1_sizing.py::size_os1(e0, e_now, width, credit, side, open_positions, max_open_positions=5, verified_structure=False) -> (qty, reason)`; reason ∈ `ok, bad_credit, low_credit_ratio, count_cap, risk_cap`. `open_positions`는 `{"L": float, "side": ...}` 리스트. 콘도르 width는 `(put_w, call_w)`.
- `src/friction_stats.py`: `bootstrap_ci`, `paired_diff`(시그니처는 읽어서 확인).
- 입력 파일: `/Users/Mung/dev/atlas-options-hackathon/registry/decisions.jsonl`, `data/friction/half_spread_table.csv`, `data/friction/bars/`(비추적), `reports/friction/calibration-2026-10-19.json`(키 `fit.beta/h_mult`, `fit_pessimistic.beta/h_mult`). 테스트는 합성 데이터만(네트워크·실파일 금지).

## 3. 구현 (`src/os1_replay.py`)
### 3-1. `ETF9 = ("SPY","QQQ","GLD","TLT","SLV","IWM","XLE","XLF","DIA")` (모듈 상수; 테스트는 리터럴 9개 단언).
### 3-2. `episode_structure(episode) -> dict`
`{"side": "bull_put"|"bear_call"|"condor", "widths": (w,) 또는 (put_w, call_w), "credit": limit_credit, "verified": bool}`.
- 레그 전부 put → `bull_put`, 전부 call → `bear_call`, 둘 다 → `condor`. 각 수직 폭 = 해당 타입 행사가 max−min. `verified` = (콘도르일 때만 의미) 4레그 만기가 모두 같고 **모든 put 행사가 < 모든 call 행사가**. 비콘도르는 `verified=False`.
### 3-3. `unit_risk(structure) -> float`
`(W_used − credit) × 100`, `W_used`: 단일 수직은 폭; 콘도르는 verified면 `max(put_w, call_w)`, 아니면 `put_w + call_w`. (O2와 동일 규칙. 테스트 8번이 `size_os1`과 일관성을 확인.)
### 3-4. `cap_replay(rows, episodes, equity, *, max_open_positions=5) -> dict`
- `rows`: 한 시나리오의 X2 행 리스트. `episodes`는 `_episode_key`로 행과 조인(키 없으면 `KeyError` 대신 명시 예외 `ValueError`).
- `entry_ts`(행의 naive UTC) 오름차순, 동률은 입력 순서. 후보마다:
  1. 같은 `symbol`로 아직 열린(수락됐고 `exit_ts > 후보 entry_ts`) 포지션이 있으면 스킵 `symbol_open`.
  2. `open_positions` = 수락됐고 `exit_ts > 후보 entry_ts`인 포지션들(`{"L": 수락 시 L, "side": side}`).
  3. `size_os1(equity, equity, width_arg, credit, side, open_positions, max_open_positions, verified)` (`width_arg`: 단일 수직은 float, 콘도르는 `(put_w, call_w)`). `qty_new == 0`이면 reason을 스킵 사유로 센다. 아니면 수락: `factor = qty_new / episode["qty"]`, `pnl_scaled = realized_pnl × factor`, `L = unit_risk × qty_new`.
- `exit_ts == entry_ts'`는 **이미 닫힌 것**으로 본다(`>` 비교).
- 반환: `{"n_candidates", "n_accepted", "skip_counts"(symbol_open, count_cap, risk_cap, bad_credit, low_credit_ratio 키 항상 존재), "uncapped_total_pnl", "capped_total_pnl", "capped_mean_pnl"(수락 0이면 None), "capped_worst_pnl"(수락 0이면 None), "max_concurrent_L_over_equity", "max_concurrent_count", "median_concurrent_L_over_equity"}`. 동시 노출은 각 **수락 직후** 시점의 열린 포지션 ΣL / equity와 열린 개수(그 시점 이후 청산은 반영 안 함, 이벤트 시점 정의를 보고서에 명시).
### 3-5. `subset_stats(rows_by_variant, episodes, *, alpha, n_resamples, seed) -> dict`
시나리오 하나의 `{variant_id: {"rows": ...}}`를 받아 **공통 에피소드 교집합**(`_episode_key`) 위에서:
- `etf9`: ETF9 종목 부분집합에서 X2−X0 쌍대차이 평균·`bootstrap_ci`·n.
- `ratio_ge_010`: `limit_credit / (두 수직 중 큰 폭) >= 0.10`인 부분집합 n, 그 안의 X2 평균 손익 (비교는 `>=`).
- `x2_vs`: 전체 공통표본에서 X2 대비 각 A1/A2/A5/C2/C3/C5의 쌍대차이 평균과 CI (`paired_diff` 방향: 변형 − X2).
### 3-6. `render_report(result, meta) -> str`와 `main(argv=None) -> int`
- 섹션 고정: `# OS-1 replay <date>`, `## Interpretation limits`(§1 문장 전부), `## Cap-applied X2 (central)`, `## Cap-applied X2 (pessimistic)`(각 `cap_replay` 지표 표 + 스킵 사유 표), `## Subsets`(두 시나리오), `## X2 vs stops`, `## Inputs`. 소수 4자리, None은 `n/a`, 결정적 출력.
- `main` 인자: `--decisions --table --bars-dir --calibration-json`(필수), `--out-dir`(기본 `reports/friction`), `--date --r --equity`(필수), `--s`(0.5), `--seed`(20260930), `--resamples`(2000). 흐름: `friction_compare.main`과 동일하게 로드 → `run_variants` → 시나리오별 `cap_replay`(X2 행)·`subset_stats` → `os1-replay-<date>.md/.json`. 덮어쓰기 금지(**실행 전** 검사), 원자적 쓰기(둘 다 쓰이거나 둘 다 없음), `__main__` 가드.

## 4. 필수 테스트 (합성, 네트워크 금지)
1. `ETF9` 리터럴 9개.
2. `episode_structure`: put 수직 → bull_put·폭, call 수직 → bear_call, 콘도르 verified True(같은 만기, put<call), 콘도르 만기 불일치 → verified False, 콘도르 행사가 겹침 → False. (OCC 문자열은 `friction.parse_occ`가 읽는 형식, 합성 리터럴로.)
3. `unit_risk`: 수직 `(1.5−0.30)·100=120`; 콘도르 verified `(2.0−0.5)·100=150`, unverified `(3.0−0.5)·100=250`.
4. `cap_replay` 손계산 케이스(equity=50000): (a) 겹치지 않는 3건 전부 수락, 손익 합 = 리터럴. (b) 같은 symbol 열린 상태의 후보 → `symbol_open`. (c) `exit_ts == 다음 entry_ts`면 닫힌 것으로 보아 `count_cap`/`risk_cap` 비해당(경계 단언). (d) 6번째 동시 포지션 → `count_cap`. (e) 총/방향 캡으로 qty 재척도화(factor≠1, `pnl_scaled` 리터럴). (f) `bad_credit`(credit≥width)·`low_credit_ratio` 스킵 집계. (g) 입력 순서가 섞여도 entry_ts 순으로 처리. (h) 수락 0건이면 mean/worst가 None. (i) 조인 실패 → `ValueError`.
5. `max_concurrent_*` 지표 리터럴.
6. `subset_stats`: ETF 부분집합 n과 쌍대차이 평균 부호, 비ETF가 제외됨; `ratio_ge_010` 경계(정확히 0.10 포함); `x2_vs` 방향(변형−X2).
7. `render_report`: 섹션 제목 순서, Interpretation limits 문구, `None→n/a`, 동일 입력 두 번 동일 문자열.
8. `size_os1` 일관성: 임의 구조에서 수락된 `qty_new × unit_risk ≤ 0.02 × equity`.
9. `main`: 모킹한 `run_variants`/`load_inputs`로 md+json 생성, 재실행 `FileExistsError`(기존 불변, `run_variants` 미호출), 필수 인자 누락 `SystemExit`, 보정 JSON 키 누락 예외+파일 없음, 직렬화 실패 주입 시 두 파일 모두 없음, `--help` 서브프로세스.

## 5. 변이 검증 (각각 pytest 실패 원문을 §8에, `python -B`)
(a) `exit_ts > t`→`>=` (b) symbol_open 검사 제거 (c) entry_ts 정렬 제거 (d) `e_now`에 다른 값 전달 (e) factor 계산 분모/분자 반전 (f) 콘도르 verified 판정에서 만기 일치 검사 제거 / 행사가 비중첩 검사 제거(각각) (g) `unit_risk` 콘도르 분기 swap (h) ETF 부분집합 필터 제거 (i) 비율 `>=`→`>` (j) 쌍대 방향 반전 (k) 보고서 섹션 삭제 (l) 덮어쓰기 검사를 실행 후로 이동 (m) 원자적 쓰기 제거 (n) skip_counts 키 일부 누락.

## 6. Rubric (게이트: 모두 ≥ 4)
| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 대부분 맞음 | 조인·정렬·캡 적용·재척도화·부분집합·CLI 계약 전부 정확 |
| 테스트 품질 | 항상 통과 | 일부 | 리터럴 단언, 형제 분기 전부, 변이 원문 증명 |
| 범위 준수 | 범위 밖 수정 | 경미 | 파일 2개만, 재구현 없음, 미사용 코드 없음, 커밋 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·감점 사유 먼저·모호점 명시 |

## 8. Executor response (Codex writes here)

(비어 있음)

## 9. Reviewer log (Claude writes here)

(비어 있음)
