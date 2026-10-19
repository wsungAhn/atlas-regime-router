# Codex Handoff — T5c calibration CLI + markdown report — 2026-10-19

> **상태(Status):** `진행중 / in-progress (dispatched)`
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-19T13:00-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** 보정 실행 CLI와 보고서 렌더러 — 새 모듈 `src/friction_calibrate.py` (설계 태스크 T5c)
> **범위(IN):** 새 파일 `src/friction_calibrate.py`, 새 테스트 `tests/test_friction_calibrate.py`. **OUT:** 다른 파일 수정(읽기 전용), 네트워크, 커밋.

## 0. 규칙
- 하지 말 것: 범위 밖 수정 · 네트워크 · 커밋 · `src.` 임포트(`sys.path.insert(0, str(Path(__file__).resolve().parent))` 후 `from friction_validation import ...`) · 미사용 임포트 · 테스트에서 구현 상수 임포트 자기비교(리터럴로 단언).
- 항상: 테스트 실행 → 이 파일 `## 8`에 **직접** 작성(감점 사유 먼저, 변이 pytest 실패 원문, 채팅 출력 금지) → 상태 `검토대기`.
- 변이는 같은 규칙이 구현된 모든 위치(형제 분기)에 각각 주입.
- 환경: 워크트리 `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`, 인터프리터 `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`, 테스트 `… -m pytest tests/ -q`, **베이스라인 307 passed**.

## 1. 배경
`friction_validation.calibrate(episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, r, s, seed, n_resamples)`가 전체 보정 결과 dict를 돌려준다(키: `boundary_ts, n_train, n_validation, fit, fit_pessimistic, validation{sim_stats, judgement}, controls, sensitivity, low_credit_sensitivity, grade{grade, probation_stop_pct, reasons}, skip_counts_validation`). 이 태스크는 입력 로딩과 보고서 출력만 만든다. 봉 데이터는 리뷰어가 사전에 `--bars-dir`에 저장한다(Codex는 네트워크 없음).

## 2. 기존 코드 (읽기 전용)
- `src/friction_backtest.py::load_live_episodes(path) -> (episodes, exclusion_counts)`.
- `src/friction.py`: half-spread 표 CSV 로더가 있는지 확인하라(`write_half_spread_table`이 쓴 CSV를 읽는 함수가 없으면 `pd.read_csv`로 읽되, `half_spread`가 요구하는 컬럼 형식에 맞는지 코드를 읽고 확인; 인덱스/컬럼 형태가 불명이면 §8에 질문).
- `src/friction_calibration.py::prepare_bars`; `src/backtest.py::_rolling_iv_series(daily_df)`(일봉 DataFrame → 날짜 인덱스 IV Series; `from backtest import _rolling_iv_series`가 임포트 가능한지 확인하고, 안 되면 §8에 사유).
- 봉 파일 규약: `<bars_dir>/<SYM>_15min.parquet`(인덱스 tz 없는 UTC DatetimeIndex = 봉 시작, 컬럼 `close`), `<bars_dir>/<SYM>_daily.parquet`(날짜 인덱스, 컬럼 open/high/low/close/volume).

## 3. 구현
### 3-1. `load_inputs(episodes, bars_dir) -> (bars_by_symbol, mtm_iv_by_symbol, missing)`
에피소드에 나온 심볼마다 두 파일을 읽어 `bars_by_symbol[sym] = prepare_bars({sym: df})[sym]`, `mtm_iv_by_symbol[sym] = _rolling_iv_series(daily)`. 파일이 없는 심볼은 두 dict에서 모두 빼고 `missing`(정렬된 리스트)에 넣는다(예외 아님 — 이후 `replay`가 `no_bars`로 집계). 데이터 기간 `data_period`(각 심볼 15분봉 첫/마지막 인덱스의 전체 min/max)도 별도 함수 `data_period(bars_by_symbol) -> (min_ts, max_ts)`로 제공(빈 입력 ValueError).
### 3-2. `render_report(result, meta) -> str`
`meta` 키: `date, n_episodes_total, exclusion_counts, missing_symbols, data_period, r, s, seed, n_resamples`. 마크다운 섹션(제목 고정): `# Friction calibration <date>`, `## Summary`(등급, probation stop %, 사유 목록, n_train/n_validation, 경계 시각, 데이터 기간), `## Fit`(선택 β, 확장·model_insufficient 여부, β·h 곱 중앙/비관, `loss_by_beta` 표, train 스킵 수, 라이브 vs 시뮬 train 통계, regime별 T-a 잔차 표), `## Validation`(목표 4+T-d 각각 sim·CI·통과 표, 전체 통과), `## Controls`(A/C 각각 sim 통계·CI 밖 여부·통과, C의 k), `## Sensitivity`(112 민감도 t_a/t_c_i와 CI, 크레딧 하한 제외 t_b), `## Limitations`(고정 문구 목록: validation n 작음/검정력 약함, 단일 레짐·train/validation 레짐 차이, limit 체결 가정 s 밴드, 페이퍼 체결, clean이 최악 마찰을 과소대표, 호가 피드 indicative 여부 미검증, 이 보고서의 달러 손익은 미보정), `## Inputs`(meta 전부). 값은 소수 4자리, `None`은 `n/a`. 결정적 출력(같은 입력 → 같은 문자열; 딕셔너리 순회 순서 고정).
### 3-3. `main(argv=None) -> int`
인자: `--decisions`(필수), `--table`(필수, half-spread CSV), `--bars-dir`(필수), `--out-dir`(기본 `reports/friction`), `--date`(필수, YYYY-MM-DD), `--r`(**필수**, 기본값 없음), `--s`(0.5), `--seed`(DEFAULT_SEED), `--resamples`(DEFAULT_RESAMPLES). 흐름: `load_live_episodes` → `load_inputs` → `calibrate` → `render_report`. `<out-dir>/calibration-<date>.md`와 같은 이름 `.json`(결과 dict를 JSON 직렬화: Timestamp→ISO 문자열, set→정렬 리스트, numpy 스칼라→float, 튜플→리스트, `in_ci` 같은 callable은 제외; 직렬화 헬퍼 `to_jsonable(obj)`)을 쓴다. 같은 날짜 파일이 이미 있으면 덮어쓰지 말고 `FileExistsError`(`--force` 없음). 반환 0. `calibrate`가 `ValueError`를 던지면 로깅 후 1 반환(파일 미작성). 에피소드/표 로드 실패는 예외 그대로.
### 3-4. 요구 로깅
외부 파일 읽기/쓰기에 `logging`(try/except로 원인을 로그하고 재던짐).

## 4. 필수 테스트 (합성, 네트워크 금지; tmp_path 사용)
1. `load_inputs`: 두 심볼 중 하나만 파일이 있으면 `missing`에 정렬되어 들어가고 dict에서 빠짐; 로드된 봉 인덱스가 15분 밀림(`prepare_bars` 적용); `mtm_iv`는 `_rolling_iv_series` 결과(스파이/모킹으로 일봉이 전달됨 확인).
2. `data_period`: 손계산 min/max, 빈 입력 ValueError.
3. `render_report`: 합성 result로 모든 섹션 제목이 순서대로 존재, 등급·β·probation(−0.1/−0.05/n/a) 문자열이 리터럴로 나타남, `None`→`n/a`, 두 번 호출한 문자열이 동일, `loss_by_beta` 모든 β 행 포함, 실패 사유 목록 출력, Limitations 항목 7개 모두 존재.
4. `to_jsonable`: Timestamp/set/numpy/튜플/callable 처리, `json.dumps` 가능.
5. `main`: `calibrate`를 모킹해 파일 두 개가 `calibration-<date>.md/.json`로 생성됨, 같은 날짜 재실행은 `FileExistsError`(기존 파일 불변), `--r` 누락은 `SystemExit`, `calibrate`가 ValueError면 반환 1이고 파일 없음, 인자(`r`, `s`, `seed`, `resamples`)가 `calibrate`에 정확히 전달됨(스파이).
6. 통합 스모크 1개: 합성 decisions.jsonl 대신 `load_live_episodes`를 모킹하되 실제 `render_report` + 실제 `to_jsonable`로 끝까지.

## 5. 변이 검증 (각각 pytest 실패 원문을 §8에)
(a) `load_inputs`가 `prepare_bars` 생략 (b) `missing` 정렬 제거 / 누락 심볼을 dict에 남김 (c) `data_period` min↔max 뒤바뀜 (d) 보고서 섹션 하나 삭제 / 순서 변경 (e) probation `-0.1`↔`-0.05` 표기 뒤바뀜 (f) `None`→`n/a` 누락 (g) `to_jsonable`의 Timestamp·set·numpy 각각 미처리 (h) 덮어쓰기 허용 (i) `--r` 기본값 추가 (j) `calibrate` 인자 `s`/`seed`/`resamples` 각각 미전달 (k) ValueError 시 파일 작성 / 반환 0 (l) loss 표에서 β 행 누락 (m) Limitations 항목 하나 삭제.

## 6. Rubric (게이트: 모두 ≥ 4)
| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 대부분 맞음 | 로딩·보고서·직렬화·덮어쓰기 방지·인자 전달 전부 계약대로 |
| 테스트 품질 | 항상 통과 | 일부 | 리터럴 단언, 형제 분기 전부, 변이 원문 증명 |
| 범위 준수 | 범위 밖 수정 | 경미 | 파일 2개만, 미사용 임포트 없음, 커밋 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·감점 사유 먼저, 모호점 명시 |

## 8. Executor response (Codex writes here)

_(비어 있음)_

## 9. Reviewer log (Claude writes here)

_(비어 있음)_
