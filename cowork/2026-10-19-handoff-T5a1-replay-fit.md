# Codex Handoff — T5a1 replay + target stats + β fit — 2026-10-19

> **상태(Status):** `대기중 / pending`
> _(Executor: set `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-19T11:00-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** 라이브 에피소드 재생(replay), 보정 목표 통계, β 적합 — 새 모듈 `src/friction_calibration.py` (설계 태스크 T5a1)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` 「설계 4. 보정 절차」 1~3항, 테스트 #4/#28. 필요한 계약은 아래에 모두 적었다.
> **범위(IN):** 새 파일 `src/friction_calibration.py`, 새 테스트 `tests/test_friction_calibration.py`. **OUT:** 다른 모든 파일 수정(읽기 전용), 네트워크, 커밋. (검증 구간 판정·대조군·등급은 후속 T5a2.)

---

## 0. 먼저 읽기

- **하지 말 것:** 범위 밖 수정 · 네트워크(봉 데이터는 인자로 받는다 — 테스트는 합성 데이터) · 커밋 · `src.` 접두사 임포트(기존 모듈처럼 `sys.path.insert(0, str(Path(__file__).resolve().parent))` 후 `from friction import ...`) · 미사용 임포트/상수.
- **항상:** 테스트 실행 → §8(감점 사유 먼저) → 상태 `검토대기`. 변이 검증은 **실제 pytest 실패 출력 원문**(요약 금지). 변이는 **그 규칙이 구현된 모든 코드 위치(형제 분기 포함: 2레그/4레그, 풋/콜, train/validation, 중앙/비관)에 각각 주입**하라 — 이전 4개 태스크에서 형제 분기 변이가 반복해서 살아남았다.
- **모르면 추측 금지:** 멈추고 §8에 질문.

### 실행 환경
- 워크트리: `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`
- 테스트: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q` — **베이스라인 253 passed**.

## 1. 배경

마찰 시뮬레이터(`src/exit_path.py::run_exit_path`, `src/friction.py`)가 갖춰졌다. 이제 **실제 라이브 진입 에피소드를 그대로 재생**(같은 진입, 청산만 시뮬레이션)해서 라이브 청산 통계와 비교하고, 마크 편향 파라미터 β를 train 구간에서 적합한다. 이 태스크는 재생·목표 통계·β 적합까지(판정·대조군·등급은 T5a2).

## 2. 기존 코드 (읽기 전용)

- `src/exit_path.py`: `run_exit_path(position, bars, mtm_iv, *, expiry, entry_iv, r, qty, policy, live_close_path, start_after, end_at, pt, sl, friction, iv_scale, entry_ts, ...)` → dict(`exit_ts, exit_reason ("profit_target"|"stop_loss"|"expiry"), trigger_mark_pnl_pct, close_debit, realized_pnl, n_polls, n_limit_retries`) 또는 `None`. `friction` dict 키는 정확히 `{"table","beta","h_mult","s","k","rng"}`. `legacy_window`, `s_orats`, `scaled_table`.
- `src/friction.py`: `implied_iv_scale(position, S, T, r, base_iv, haircut=0.05) -> float|None`, `parse_occ(occ) -> (underlying, expiry: date, "call"|"put", strike)`, `half_spread`, `leg_mids` 등.
- `src/friction_backtest.py::load_live_episodes(path) -> (episodes, exclusion_counts)`. 에피소드 dict 키: `symbol, entry_ts (tz-aware UTC Timestamp), regime, legs (OCC 문자열 리스트), n_legs, qty, limit_credit (주당 순크레딧), kind ("clean"|"single_limit"|"retry"), exit_reason ("profit_target"|"stop_loss"|"max_hold_days"), exit_ts, minutes_held, trigger_profit_pct, live_close_path ("market"|"limit_fallback"), ...`.
- `src/friction_stats.py`: `DEFAULT_SEED`(=20260930), `DEFAULT_RESAMPLES`(=2000), `bootstrap_se`, `_bootstrap_statistics(values, stat, n_resamples, seed)`(비공개이지만 같은 패키지 모듈이므로 임포트해서 써도 된다 — NaN을 `np.nanstd(..., ddof=1)`로 처리하려는 목적).

## 3. 구현할 것 (`src/friction_calibration.py`)

모듈 상수: `BETA_GRID = tuple(0.125 * i for i in range(13))`(0~1.5), `BETA_EXTENDED = (2.0, 3.0, 4.0)`, `TRAIN_FRACTION = 0.6`, `EARLY_MINUTES = 60.0`, `BAR_MINUTES = 15`, `SE_FLOOR_RATE = 0.01`(T-a, T-c), `SE_FLOOR_PNL = 0.05`(T-b), `UNDEFINED_LOSS = 1e6`, `RISK_FREE_RATE_ARG`는 만들지 말고 `r`은 인자로 받는다.

### 3-1. `split_train_validation(clean_episodes) -> (train, validation, boundary_ts)`
`kind != "clean"`인 에피소드가 섞여 있으면 `ValueError`. `entry_ts` 오름차순 정렬(안정 정렬). `n_train = floor(TRAIN_FRACTION × N)`. `train = 앞 n_train개`, `validation = 나머지`, `boundary_ts = validation[0]["entry_ts"]`(비어 있으면 `ValueError`). N이 너무 작아 train/validation이 비면 `ValueError`.

### 3-2. 봉 데이터 정렬 `prepare_bars(bars_by_symbol) -> dict`
입력은 `symbol → DataFrame(index: tz 없는 UTC DatetimeIndex = **봉 시작 시각**, 컬럼 close)`. 라이브 폴링은 봉이 닫힌 뒤의 가격을 보므로 **인덱스를 `BAR_MINUTES`분 뒤로 민 복사본**(= 봉 종료 시각)을 반환한다(원본 불변). tz가 있으면 `ValueError`.

### 3-3. `build_replay_position(episode) -> dict`
`{"legs": [{"occ", "side"}...], "credit": episode["limit_credit"]}`. **side는 행사가로 정한다**: 같은 타입(풋/콜) 레그 쌍에서 풋은 높은 행사가가 `short`·낮은 쪽이 `long`, 콜은 낮은 행사가가 `short`·높은 쪽이 `long`. `len(legs) != n_legs`, 쌍이 안 맞음 등은 `ValueError`.

### 3-4. `replay_episode(episode, bars, mtm_iv, table, *, beta, r, policy="V0", s=0.5, k=0.0, rng=None, h_mult=1.0, pt=0.5, sl=-1.0, **policy_kwargs) -> dict | None`
- `bars`는 `prepare_bars`를 거친 이 종목의 DataFrame, `mtm_iv`는 이 종목의 날짜 인덱스 Series.
- `entry_naive = episode["entry_ts"]`를 **tz 없는 UTC**로 변환.
- **진입 시 기초자산 가격** `S_entry`: `bars`에서 `index <= entry_naive`인 마지막 봉(종료 시각이 진입 이전인 **완결된 봉**)의 `close`. 없으면 `None` 반환(카운트용 사유 `no_entry_bar`). (미래 봉을 쓰면 안 된다 — 룩어헤드.)
- `entry_iv`: `mtm_iv`에서 진입일(`entry_naive.normalize()`) 이하의 마지막 값. 없으면 `None`(사유 `no_iv`).
- `T_entry = (만기일 − 진입일).days / 365` (만기일은 `parse_occ`로 첫 레그에서).
- `iv_scale = implied_iv_scale(position, S_entry, T_entry, r, entry_iv)`. `None`이면 `None` 반환(사유 `unanchorable`).
- `run_exit_path(...)` 호출: `start_after=entry_naive`, `end_at = 만기일 정규화 + 1일 − 1ns`, `expiry=만기일`, `friction={"table": table, "beta": beta, "h_mult": h_mult, "s": s, "k": k, "rng": rng}`, `live_close_path=episode["live_close_path"]`, `iv_scale=iv_scale`, `entry_ts=entry_naive`(V2용), `entry_underlying=S_entry`(V6용), 그 외 `policy_kwargs`를 그대로 전달. `entry_iv`·`qty=episode["qty"]` 전달.
- 결과가 `None`이면 `None`(사유 `no_exit`). 아니면 결과 dict에 `regime`, `symbol`, `entry_ts`(naive), `minutes_held = (exit_ts − entry_naive).total_seconds()/60`, `live_exit_reason`, `live_trigger_profit_pct`, `live_minutes_held`를 덧붙여 반환.
- **반환이 None인 이유를 구분해서 집계**하기 위해 내부 헬퍼 `_replay_one(...) -> (row | None, reason | None)`를 두고 `replay_episode`는 row만 돌려줘도 된다.

### 3-5. `replay(episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, beta, r, **kwargs) -> (rows, skip_counts)`
`bars_by_symbol`은 이미 `prepare_bars`를 거친 것. 종목 봉/IV 누락은 `no_bars`로 집계. 반환 `skip_counts = {"no_bars": n, "no_entry_bar": n, "no_iv": n, "unanchorable": n, "no_exit": n}`(0 포함 키 고정).

### 3-6. 목표 통계 `target_stats(rows) -> dict`
`rows`는 `{"exit_reason","trigger_pnl","minutes_held","regime"}`을 가진 dict 리스트(라이브용 변환 `live_rows(episodes)`: `exit_reason`, `trigger_pnl=trigger_profit_pct`, `minutes_held`, `regime`; 시뮬용 변환 `sim_rows(results)`: `exit_reason`, `trigger_pnl=trigger_mark_pnl_pct`, `minutes_held`, `regime`). 반환 키:
- `t_a`: `exit_reason=="profit_target"`인 행 비율(분모 = 전체 행; 만기·max_hold 등은 PT가 아님).
- `t_b`: `exit_reason=="stop_loss"`인 행의 `trigger_pnl` **중앙값**(`trigger_pnl`이 None인 행 제외). SL 행이 없거나 모두 None이면 `None`.
- `t_c_i`: SL 행 중 `minutes_held <= EARLY_MINUTES` 비율. SL 행이 0이면 `0.0`.
- `t_c_ii`: `t_c_i − (PT 행 중 minutes_held <= EARLY_MINUTES 비율)`. PT 행이 0이면 그 비율은 `0.0`.
- `t_d`: `regime → PT 비율`(그 regime 행 중).
- `undefined`: 정의되지 않은 항목 이름의 `set`: SL 행 0이면 `{"t_b", "t_c_i"}`에 `t_b`와 `t_c_i`, `t_c_ii`도 SL 쪽이 0건이면 포함; PT 행 0이면 `t_c_ii`에 포함(분모 0 규칙: 값은 0으로 기록하고 이름만 `undefined`에 넣는다).
- 행이 0개면 `ValueError`.

### 3-7. 손실 `calibration_loss(sim_stats, live_stats, se) -> float`
`j ∈ {t_a, t_b, t_c_i, t_c_ii}`에 대해 `Σ ((sim_j − live_j)/se_j)²`. `se_j`는 호출자가 하한 적용 후 넘긴다. **시뮬 통계가 정의되지 않으면(`t_b is None`)** 그 항은 `UNDEFINED_LOSS`로 계산(나머지 항은 정상). `live` 쪽 `t_b`가 `None`이면 `ValueError`.

### 3-8. 부트스트랩 SE `bootstrap_ses(train_live_rows, *, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED) -> dict`
train 라이브 행을 에피소드 단위로 리샘플해 각 목표의 표준편차(`np.nanstd(..., ddof=1)`, 리샘플에서 정의되지 않은 값 NaN은 무시)를 구하고 **하한 적용**: `t_a`, `t_c_i`, `t_c_ii`는 `max(se, SE_FLOOR_RATE)`, `t_b`는 `max(se, SE_FLOOR_PNL)`. 시드 고정이면 재현. 리샘플에서 `t_b`가 정의되지 않으면 NaN, 전부 NaN이면 하한값 사용.

### 3-9. β 선택 `select_beta(loss_fn, *, grid=BETA_GRID, extended=BETA_EXTENDED) -> dict`
순수 함수(테스트가 합성 `loss_fn`을 넣는다): `loss_by_beta = {β: loss_fn(β) for β in grid}`; 최솟값 β(동점이면 **작은 β**). 최적이 `grid`의 최댓값(1.5)이면 `extended`를 **한 번만** 추가 평가해 다시 고른다(`extended=True`). 그래도 최적이 `extended` 최댓값(4.0)이면 `model_insufficient=True`. 반환 `{"beta", "loss_by_beta", "extended": bool, "model_insufficient": bool}`.

### 3-10. `fit_beta(train_episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, boundary_ts, r, h_mult=1.0, s=0.5, seed=DEFAULT_SEED, n_resamples=DEFAULT_RESAMPLES) -> dict`
- **train 에피소드 중 `entry_ts >= boundary_ts`가 하나라도 있으면 `ValueError`**(날짜 경계 검사).
- 라이브 통계 = `target_stats(live_rows(train))`, SE = `bootstrap_ses(live_rows(train))`. `loss_fn(β) = calibration_loss(target_stats(sim_rows(replay(train, ..., beta=β, h_mult=h_mult, s=s)[0])), live_stats, se)`. replay가 건너뛴 에피소드 수(`skip_counts`)는 β마다 동일해야 하며 결과에 `skip_counts`로 포함.
- 반환 `select_beta` 결과 + `skip_counts`, `live_stats`, `se`, `h_mult`, 그리고 선택된 β로 다시 돌린 `sim_stats`와 **regime별 잔차**(`regime → {"sim_t_a", "live_t_a", "n"}`, T-a만).
- **비관 시나리오**는 `h_mult=1.5`로 같은 함수를 다시 호출하는 것으로 구현(β 재적합). 곱 `β·h_mult`가 보존됨은 테스트로 검증.

## 4. 필수 테스트 `tests/test_friction_calibration.py` (합성 데이터, 네트워크 금지)
1. `split_train_validation`: N=10 → train 6/validation 4, N=7 → train 4(`floor(4.2)`), 경계 시각이 validation 첫 에피소드의 `entry_ts`, 입력이 순서가 뒤섞여 있어도 시간순, `kind != clean` 섞이면 ValueError, 빈 구간이면 ValueError.
2. `prepare_bars`: 인덱스가 15분 밀리고 원본 불변, tz 있는 입력 ValueError.
3. `build_replay_position`: 풋 수직·콜 수직·콘도르에서 side가 행사가 규칙대로(레그 순서를 뒤섞어도 동일), 레그 수 불일치 ValueError.
4. `replay_episode`: **룩어헤드 방지** — 진입 직후(아직 닫히지 않은) 봉의 가격이 `S_entry`로 쓰이지 않는다(그 봉의 close를 크게 바꿔도 `iv_scale`/결과의 진입 의존 부분이 불변, 완결된 직전 봉을 바꾸면 변한다). `unanchorable`(크레딧 ≥ 폭)은 `None`이고 `replay`가 해당 카운트를 올린다. 종목 봉 누락은 `no_bars`. `skip_counts`의 키는 항상 5개.
5. **`target_stats`**: 손으로 계산한 소형 행 집합으로 `t_a`, `t_b`(중앙값, 짝수 개 케이스 포함), `t_c_i`(경계 정확히 60분 포함), `t_c_ii`, `t_d` 검증; SL 행이 없을 때 `t_b is None`, `t_c_i == 0.0`, `undefined`에 해당 이름; PT 행이 없을 때 `t_c_ii`가 `undefined`; 빈 입력 ValueError. 만기·max_hold 행은 PT가 아니지만 분모에는 포함됨을 검증.
6. `calibration_loss`: 손으로 계산한 값 일치, 시뮬 `t_b`가 `None`이면 해당 항이 `UNDEFINED_LOSS`, live `t_b`가 `None`이면 ValueError.
7. `bootstrap_ses`: 시드 고정 재현(같은 입력·시드 → 같은 값), 하한 적용(분산 0인 입력에서 `t_a`는 0.01, `t_b`는 0.05), 시드가 다르면 값이 달라질 수 있음(적어도 함수가 시드를 사용함: 시드 인자 무시 변이가 걸리게).
8. **`test_fit_rejects_validation_rows`**(설계 #4): `entry_ts >= boundary_ts`인 에피소드가 train에 섞이면 ValueError, 경계와 정확히 같은 `entry_ts`도 ValueError.
9. **`test_beta_grid_edge_and_pessimistic_refit`**(설계 #28): 합성 `loss_fn`으로 `select_beta` — (a) 최적이 격자 내부면 확장 없음 (b) 최적이 1.5면 `{2,3,4}`로 한 번 확장해 재선택(`extended=True`) (c) 확장 후에도 4.0이면 `model_insufficient=True` (d) 동점이면 작은 β (e) 최솟값이 0.0이면 0.0. 그리고 `fit_beta`의 곱 보존: 합성 시나리오에서 중앙(`h_mult=1`)의 최적 β와 비관(`h_mult=1.5`)의 최적 β가 `β_center ≈ 1.5 × β_pess`(±격자 한 칸)가 되도록 구성해 단언 — 실제 replay를 쓰기 어려우면 `fit_beta` 내부가 `loss_fn`을 주입받을 수 있게 하지 말고, 대신 합성 시장(고정 경로, 단순 포지션)에서 β·h 곱만 마크 편향에 들어간다는 `run_exit_path` 속성에 기대 합성 에피소드 몇 개로 구성하라(불가능하면 이유를 §8에 적어라).
10. `fit_beta` 통합(합성): 에피소드 5~8개 + 합성 봉으로 β=0에서는 SL이 거의 없고 β가 클수록 조기 SL이 늘어나는 구조를 만들어, 적합 결과 `loss_by_beta`가 13개(또는 확장 시 16개)이고 `skip_counts`/`live_stats`/`sim_stats`/`residuals_by_regime`이 채워진다.

## 5. 변이(회귀 주입) 검증 — 각각 실제 pytest 실패 출력 원문을 §8에 붙여라 (형제 분기마다 주입)
(a) 학습/검증 분할 `floor`→`round`/`ceil` (b) 분할 후 정렬 생략 (c) `prepare_bars`가 인덱스를 밀지 않음 / 부호 반전 (d) `build_replay_position`에서 풋·콜 side 규칙 한쪽 뒤집기(둘 다 각각) (e) `S_entry`가 진입 직후 봉의 close를 사용(룩어헤드) (f) `unanchorable`을 `iv_scale=1.0`으로 폴백 (g) 만기일 계산 오류 (h) `t_a` 분모를 PT+SL로 (i) `t_b` 중앙값→평균 (j) `t_c_i` 경계 `<=`→`<` (k) `t_c_ii`에서 PT 비율 빼기 누락 (l) `calibration_loss`에서 제곱 누락 / `se` 하한 제거 (m) `UNDEFINED_LOSS` 처리 누락 (n) `bootstrap_ses` 시드 무시 (o) 경계 검사 `>=`→`>` / 검사 제거 (p) `select_beta` 동점 시 큰 β 선택 / 확장 안 함 / 확장을 두 번 / `model_insufficient` 누락 (q) 비관 재적합에서 `h_mult` 무시. 각 변이에서 실패해야 하는 테스트와 이유 문자열을 함께 적어라.

## 6. Acceptance Rubric (실행 전에 고정)

| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 대부분 맞으나 경계·룩어헤드·분모 오류 | 분할·정렬·룩어헤드·목표 통계·손실·SE 하한·β 격자/확장 전부 맞음 |
| 테스트 품질 | 항상 통과 | 일부만 | 10개 테스트가 손계산 값과 경계를 의미 있게 단언하고, 변이가 형제 분기마다 원문으로 증명 |
| 범위 준수 | 범위 밖 수정 | 경미 이탈 | 지정 파일 2개만, 미사용 임포트·`src.` 임포트 없음, 커밋 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·감점 사유 먼저·구성 불가 케이스 사유 |

게이트: 모든 차원 ≥ 4.

## 8. Executor response (Codex writes here)

_(비어 있음 — 감점 사유 먼저.)_

## 9. Review log

_(비어 있음 — reviewer가 작성)_
