# Codex Handoff — T5a2 validation judgement + controls A/C + calibration grade — 2026-10-19

> **상태(Status):** `진행중 / in-progress (round 2 요청)`
> _(Executor: set `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-19T12:10-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** validation 구간 판정, 대조군 A·C, 보정 등급 — 새 모듈 `src/friction_validation.py` (설계 태스크 T5a2)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` 「설계 4. 보정 절차」 4~6항(+ 4-2 SE 정의 재사용), 테스트 #5/#29. 필요한 계약은 아래에 모두 적었다.
> **범위(IN):** 새 파일 `src/friction_validation.py`, 새 테스트 `tests/test_friction_validation.py`. **OUT:** 다른 모든 파일 수정(읽기 전용), 네트워크, 커밋. (CLI·보고서는 후속 T5c.)

---

## 0. 먼저 읽기

- **하지 말 것:** 범위 밖 수정 · 네트워크(봉 데이터는 인자로 받는다 — 테스트는 합성 데이터) · 커밋 · `src.` 접두사 임포트(`sys.path.insert(0, str(Path(__file__).resolve().parent))` 후 `from friction_calibration import ...`) · 미사용 임포트/상수 · 테스트에서 구현 상수를 임포트해 자기 자신과 비교(**리터럴 값으로 단언**: 이전 태스크에서 반복 반려됨).
- **항상:** 테스트 실행 → §8(감점 사유 먼저) → 상태 `검토대기`. 변이 검증은 **실제 pytest 실패 출력 원문**(요약 금지). 변이는 **그 규칙이 구현된 모든 코드 위치(형제 분기: A/C 대조군, t_a/t_c_i/t_c_ii/t_d, 중앙/민감도, PASS/PASS_CONDITIONAL 등)에 각각 주입**하라. 이전 태스크에서 형제 분기 변이가 반복해서 살아남았다.
- **모르면 추측 금지:** 멈추고 §8에 질문.

### 실행 환경
- 워크트리: `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`
- 테스트: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q` — **베이스라인 278 passed**.

## 1. 배경

`src/friction_calibration.py`(T5a1)가 라이브 에피소드 재생(`replay`), 목표 통계(`target_stats`), β 적합(`fit_beta`)을 제공한다. 이 태스크는 **적합된 β를 validation 구간에서 판정**하고, 음성 대조군 A·C로 "목표 지표가 마크 편향을 판별하는가"를 검증하며, 최종 등급(PASS / PASS_CONDITIONAL / FAIL)을 산출한다. 통과 못 해도 정상 결과다(FAIL은 유효한 결론).

## 2. 기존 코드 (읽기 전용 — 먼저 읽어라)

`src/friction_calibration.py`:
- 상수 `BETA_GRID, BETA_EXTENDED, EARLY_MINUTES(=60.0), SE_FLOOR_*`, `DEFAULT_SEED/DEFAULT_RESAMPLES`(friction_stats에서 임포트되어 있음).
- `split_train_validation(clean) -> (train, validation, boundary_ts)`, `prepare_bars`, `replay(episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, beta, r, **kwargs) -> (rows, skip_counts)` (kwargs로 `h_mult, s, k, rng, policy ...` 전달), `target_stats(rows)`(반환: `t_a, t_b, t_c_i, t_c_ii, t_d(regime→PT비율), undefined`), `live_rows(episodes)`, `sim_rows(results)`, `fit_beta(train_episodes, ..., boundary_ts, r, h_mult=1.0, s=0.5, seed, n_resamples)`(반환: `beta, loss_by_beta, extended, model_insufficient, skip_counts, live_stats, se, h_mult, sim_stats, residuals_by_regime`).
- `src/friction.py::episode_rng(episode_index, seed)`.
- 에피소드 dict: `kind` ("clean"|"single_limit"|"retry"), `entry_ts`(UTC tz-aware), `regime`, `limit_credit`, `exit_reason`(PT/SL/`max_hold_days`), `minutes_held`, `trigger_profit_pct` 등.

## 3. 구현할 것 (`src/friction_validation.py`)

모듈 상수: `CI_LEVEL = 0.90`, `K_GRID = tuple(0.25 * i for i in range(13))`(0~3.0), `TREND_REGIME = "trend_up"`, `LOW_CREDIT = 0.02`, `PROBATION_STOP = -0.10`, `PROBATION_STOP_CONDITIONAL = -0.05`.

### 3-1. `bootstrap_ci(rows, *, level=CI_LEVEL, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED) -> dict`
`rows`는 `live_rows(...)` 형식(validation 라이브). 퍼센타일 방식, 양끝 `(1−level)/2`. 키별 `(lo, hi)`:
- `t_a`, `t_c_i`: 전체 행을 에피소드 단위 리샘플해 `target_stats` 값의 분포.
- `t_b`: 같은 리샘플에서 `t_b is None`인 리샘플은 버리고(`nan` 제외) 계산. 전부 `None`이면 `(None, None)`.
- `t_c_ii`: **SL 행과 PT 행을 따로 리샘플**(각각 자기 개수만큼)한 뒤 `Δ = (SL 중 60분 이내 비율) − (PT 중 60분 이내 비율)`. PT 행이 0개면 PT 쪽 비율은 0.0. SL 행이 0개면 SL 쪽 비율 0.0. (`target_stats`의 규칙과 동일하게 정의.)
- `t_d_trend_up`: `regime == TREND_REGIME`인 행만 리샘플해 PT 비율의 분포. 그 regime 행이 0개면 `ValueError`.
- 시드 고정이면 재현. `rows` 비면 `ValueError`.
- 내부에 `in_ci(value, ci) -> bool`(**닫힌 구간**, `ci`가 `(None, None)`이거나 value가 None이면 False).

### 3-2. `judge_validation(sim_stats, validation_live_rows, *, n_resamples, seed) -> dict`
설계 4-4. `ci = bootstrap_ci(validation_live_rows, ...)`. 반환:
```
{"targets": {"t_a": {"sim", "ci", "passed"}, "t_b": {...}, "t_c_i": {...}, "t_c_ii": {...},
             "t_d": {"sim_trend_up", "ci", "in_ci", "lowest", "passed"}},
 "undefined": sorted list of sim_stats["undefined"], "passed": bool}
```
- `t_a, t_b, t_c_i, t_c_ii`: `in_ci(sim, ci)`. **시뮬 `t_b is None`이면 passed=False**(분모 0 규칙: T-b 정의 불가 = 실패). `t_c_i`가 시뮬에서 SL 0건이면 값 0.0으로 CI와 비교(라이브 CI 하한이 0보다 크면 자연히 실패).
- `t_d`: (i) 시뮬 `t_d[trend_up]`이 `in_ci`, **그리고** (ii) 시뮬 `t_d`에서 trend_up 승률이 **가장 낮다** = `min(t_d.values())`와 같다(동률 허용). trend_up이 시뮬 `t_d`에 없으면 passed=False. 둘 다여야 passed.
- 전체 `passed` = 위 5개 모두 passed.
- 레짐 차이로 실패해도 구제 로직 없음. 이 함수는 판정만 한다.

### 3-3. `fit_symmetric_k(train_episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, r, h_mult=1.0, s=0.5, seed=DEFAULT_SEED) -> dict`
대조군 C용. `beta=0`, `h_mult`, `s` 고정으로 `k in K_GRID`마다 `replay(train_episodes, ..., beta=0.0, k=k, rng=np.random.default_rng(seed))`(**k마다 새 rng**, k=0이면 rng 없이) 후 `target_stats(sim_rows(rows))["t_c_i"]`가 라이브 train `t_c_i`에 가장 가까운(절대차 최소) k를 고른다. **동점이면 작은 k**. 반환 `{"k", "diff_by_k": {k: abs diff}}`. 라이브 train 통계는 `target_stats(live_rows(train_episodes))`.

### 3-4. `run_controls(train_episodes, validation_episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, r, s=0.5, seed, n_resamples) -> dict`
- 같은 validation 라이브 CI(`bootstrap_ci(live_rows(validation_episodes))`)를 사용.
- **A 무마찰:** `replay(validation, ..., beta=0.0, h_mult=0.0, s=s)` → `target_stats`. **통과 조건: `t_a` 또는 `t_c_i`가 CI 밖.** (CI 안이면 A 실패 = 목표가 마찰을 판별 못 함.)
- **C 대칭 노이즈:** `fit_symmetric_k`(train)로 k를 고르고 `replay(validation, ..., beta=0.0, h_mult=1.0, k=k, rng=np.random.default_rng(seed), s=s)` → `target_stats`. **통과 조건: `t_c_ii`가 CI 밖.** (`t_c_ii`가 None-like여서 비교 불가하면 통과로 치지 않는다: `in_ci`는 value가 None이면 False이므로 "CI 밖"이 아니라 "비교 불가". 비교 불가는 **실패**로 처리. `t_c_ii`는 항상 숫자이므로 실제로는 거의 안 생기지만 코드 경로는 구분해 둘 것.)
- 반환 `{"A": {"sim_stats", "outside_t_a", "outside_t_c_i", "passed"}, "C": {"sim_stats", "k", "outside_t_c_ii", "passed"}, "ci": ci, "passed": A.passed and C.passed}`.
- "CI 밖" = `not in_ci(...)`이되 value가 숫자일 때만 True. 한쪽 경계를 정확히 맞추는 값은 CI 안(닫힌 구간).

### 3-5. `grade_calibration(fit, validation_judgement, controls, sensitivity_in_ci) -> dict`
설계 4-6.
- `FAIL`: `fit["model_insufficient"]` 이거나 `validation_judgement["passed"]`가 False 이거나 `controls["passed"]`가 False.
- 그 외 `PASS_CONDITIONAL`: `sensitivity_in_ci`(dict, 키 `t_a`, `t_c_i` → bool) 중 하나라도 False.
- 그 외 `PASS`.
- 반환 `{"grade", "probation_stop_pct", "reasons": [...]}`; `probation_stop_pct`: PASS → `PROBATION_STOP`(−0.10), PASS_CONDITIONAL → `PROBATION_STOP_CONDITIONAL`(−0.05), FAIL → `None`. `reasons`는 실패/조건부 사유 문자열 목록(예: `"model_insufficient"`, `"validation:t_b"`, `"control_A"`, `"control_C"`, `"sensitivity:t_a"`).

### 3-6. `sensitivity_all_kinds(all_episodes, boundary_ts, ..., fit_beta_value, r, s, seed, n_resamples) -> dict`
설계 4-6의 112개 민감도. `entry_ts >= boundary_ts`인 **모든 kind**(clean·single_limit·retry) 에피소드를 `replay(..., beta=fit_beta_value, h_mult=1.0, s=s)`로 재생해 `t_a`, `t_c_i`를 구하고, 같은 에피소드 집합의 라이브(`live_rows`, 첫 청산 기준)에서 `bootstrap_ci`를 만들어 비교. 반환 `{"t_a": {"sim", "ci", "in_ci"}, "t_c_i": {...}, "n": int}`. 해당 집합이 비면 `ValueError`. *(해석 주의: 설계 문구가 "112개 에피소드로 계산한 민감도가 validation CI 안"이라 모호하다. 위 정의로 구현하고 §8에 이 해석을 명시하라.)*

### 3-7. `calibrate(episodes, bars_by_symbol, mtm_iv_by_symbol, table, *, r, s=0.5, seed=DEFAULT_SEED, n_resamples=DEFAULT_RESAMPLES) -> dict`
오케스트레이션(얇게):
1. `clean = [e for e in episodes if e["kind"] == "clean"]` → `split_train_validation` → `(train, validation, boundary_ts)`.
2. 중앙 적합 `fit = fit_beta(train, ..., boundary_ts=boundary_ts, r=r, h_mult=1.0, s=s, ...)`; 비관 `fit_pess = fit_beta(..., h_mult=1.5, ...)`(보고용, 등급에는 불사용; `beta_h_product`: 중앙 `β·1.0`, 비관 `β·1.5` 둘 다 반환).
3. validation 재생 `rows = replay(validation, ..., beta=fit["beta"], h_mult=1.0, s=s)`; `sim_stats = target_stats(sim_rows(rows))`(빈 rows면 `ValueError`, 스킵 수 `skip_counts` 포함해 반환); `judge_validation(sim_stats, live_rows(validation))`.
4. `run_controls`, `sensitivity_all_kinds`(→ `sensitivity_in_ci`), 그리고 **크레딧 하한 민감도**: validation에서 `limit_credit <= LOW_CREDIT`인 에피소드를 뺀 `t_b`의 라이브 중앙값과 시뮬 중앙값(`{"sim_t_b", "live_t_b", "n_excluded"}`; SL 없으면 해당 값 `None`).
5. `grade_calibration`. 반환: `{"boundary_ts", "n_train", "n_validation", "fit", "fit_pessimistic", "validation", "controls", "sensitivity", "low_credit_sensitivity", "grade", "skip_counts_validation"}`.
`validation` 단계에서 train 행이 섞이지 않았음을 보장(`split`이 보장; 별도 로직 불필요).

## 4. 필수 테스트 `tests/test_friction_validation.py` (합성 데이터, 네트워크 금지)

판정 로직은 `target_stats` 형식의 손으로 만든 `sim_stats` dict로 직접 단언한다(리플레이 불필요). 대조군/오케스트레이션은 `monkeypatch`로 `replay`/`fit_beta`를 대체하거나, T5a1 테스트(`tests/test_friction_calibration.py`)의 합성 봉·포지션 헬퍼를 참고해 소형 실경로 통합 1개.

1. `bootstrap_ci`: 시드 고정 재현, 닫힌 구간(`in_ci` 경계값 포함), `(None, None)`/None 값은 False, 분산 0 입력에서 CI가 점(lo==hi), trend_up 행 없으면 ValueError, 빈 입력 ValueError. **`t_c_ii`가 SL·PT를 따로 리샘플**함을 증명: PT 행이 모두 60분 초과이고 SL 행이 모두 60분 이내인 입력 → CI가 정확히 `(1.0, 1.0)`.
2. `judge_validation` 항목별: 각 목표(`t_a`,`t_b`,`t_c_i`,`t_c_ii`)가 CI 안/밖일 때 passed가 맞게 나오고 **다른 하나만 밖이어도 전체 passed=False**(5개 목표 각각에 대해 파라미터화). 시뮬 `t_b is None`이면 실패. 시뮬 SL 0건(`t_c_i=0.0`)이 라이브 CI 하한 > 0이면 실패, 하한이 0이면 통과.
3. `t_d`: (i)만 만족/(ii)만 만족/둘 다 — 셋 모두에 대해 passed 값이 맞고, trend_up이 최저가 아니면 실패, 최저와 동률이면 통과, trend_up 키 없으면 실패.
4. `fit_symmetric_k`: 동점이면 작은 k, 정확히 최소 차이 k를 고름, k마다 새 rng 사용(같은 입력 두 번 호출하면 같은 결과), k=0은 rng 없이 호출(스파이로 `rng is None` 확인).
5. `run_controls`: A — `t_a`만 밖/`t_c_i`만 밖/둘 다 안 → passed 각각 True/True/False; A는 `beta=0.0`, `h_mult=0.0`으로 replay를 호출(스파이로 인자 확인); C — `t_c_ii` 밖/안 → passed True/False; C는 `beta=0.0`, `h_mult=1.0`, 고른 `k`를 전달; 전체 passed는 A and C. 경계값이 CI 경계 정확히 같으면 "밖" 아님(닫힌 구간).
6. `grade_calibration` 진리표: model_insufficient → FAIL; validation 실패 → FAIL; control 실패 → FAIL; 셋 다 통과 + 민감도 둘 다 True → PASS(`-0.10`); 민감도 하나만 False(각각 `t_a`, `t_c_i`) → PASS_CONDITIONAL(`-0.05`, 리터럴 값 단언); FAIL이면 `probation_stop_pct is None`; `reasons` 문자열 포함 확인.
7. `sensitivity_all_kinds`: `entry_ts >= boundary_ts`인 모든 kind만 쓰고(경계 정확히 같은 행 포함, 이전 행 제외), clean만 남기면 결과가 달라짐을 확인하는 스파이/합성; 빈 집합 ValueError.
8. `calibrate` 통합(합성): 최소 에피소드 ~10개(clean 8 + single_limit 2) 합성 봉/IV/표로 끝까지 실행해 반환 키가 모두 있고 `grade ∈ {PASS, PASS_CONDITIONAL, FAIL}`, `n_train + n_validation == clean 수`, `fit_pessimistic["h_mult"] == 1.5`, 크레딧 하한 민감도의 `n_excluded`가 손으로 센 값과 일치. 등급 자체가 어떻게 나오든 단언하지 말 것(합성 한계).

## 5. 변이(회귀 주입) 검증 — 각각 실제 pytest 실패 출력 원문을 §8에 붙여라 (형제 분기마다 주입)
(a) CI 레벨 0.90→0.95 / 0.80 (b) `in_ci` 닫힌 구간→열린 구간 (c) `t_c_ii` CI에서 SL·PT 합쳐서 리샘플 (d) `t_b is None`을 통과로 처리 (e) `t_d` (i)/(ii) 조건 중 하나 제거(각각) (f) trend_up이 "최저"인데 엄격히 낮아야 통과로 바꿈 (g) `judge_validation` 전체 passed에서 5개 목표 중 하나씩 제외(5회) (h) 대조군 A: `h_mult=0.0` → `1.0` / `beta` 변경 (i) A 통과 조건 `or`→`and` (j) C: `t_c_ii` 대신 `t_c_i`로 판정 (k) C 통과 조건 반전 (l) `fit_symmetric_k` 동점 시 큰 k / k=0에 rng 전달 / 공유 rng (m) `grade_calibration`: model_insufficient 무시 / 민감도 `t_a`·`t_c_i` 각각 무시 / probation 값 뒤바뀜 (n) `sensitivity_all_kinds`가 clean만 사용 / 경계 `>=`→`>` (o) `calibrate`가 train으로 판정 / 비관 `h_mult` 무시 / 크레딧 하한 비교 `<=`→`<`. 각 변이에서 실패해야 하는 테스트와 이유 문자열을 함께 적어라.

## 6. Acceptance Rubric (실행 전에 고정)

| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 대부분 맞으나 경계(닫힌 구간)·분모 0·동률 오류 | CI·판정 5항·대조군 A/C·등급 규칙·민감도 정의가 설계와 전부 일치 |
| 테스트 품질 | 항상 통과/상수 임포트 자기비교 | 일부만 | 리터럴 값 단언, 형제 분기(목표별·대조군별) 전부 커버, 변이가 형제마다 원문으로 증명 |
| 범위 준수 | 범위 밖 수정 | 경미 이탈 | 지정 파일 2개만, 미사용 임포트·`src.` 임포트 없음, 커밋 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·감점 사유 먼저·모호 해석(§3-6) 명시 |

게이트: 모든 차원 ≥ 4.

## 8. Executor response (Codex writes here)

_(비어 있음 — 수행자가 작성)_

## 9. Reviewer log (Claude writes here)

### Round 1
**Verdict:** changes requested

- 읽기: `src/friction_validation.py`는 설계와 일치(CI 닫힌 구간, t_c_ii 분리 리샘플, A=`or`, C=t_c_ii, 등급 진리표, 민감도 정의 §3-6). 구현 수정은 요구하지 않는다.
- 스위트 300 passed 재실행 확인. 그러나 (1) Codex는 §8을 핸드오프에 쓰지 않고 채팅 출력만 남겼고, 변이 5종만 시도했다(요구 §5 (a)~(o) 대비 부족). (2) 테스트 파일에 `if False else` 잔재 한 줄.
- 내가 주입한 변이 중 **테스트가 못 잡은 생존 10건**(각각 `friction_validation.py`에서 해당 줄만 바꿈):
  1. A 통과 조건을 `t_a`만으로 (`outside_a`) — A 테스트는 `t_a`가 밖인 경우만 있어 `t_c_i`만 밖인 케이스 부재(반대로 `t_c_i`만 쓰는 변이는 죽음).
  2. C 통과를 항상 True — C가 실패하는 케이스(t_c_ii 안) 부재.
  3. 전체 controls `and`→`or` — A 실패+C 통과, A 통과+C 실패 케이스 부재(`run_controls` 수준).
  4. C replay에서 `rng` 인자 제거 — 전달 단언 없음.
  5. `sensitivity_all_kinds`가 `beta=fit_beta_value` 대신 0.0 — beta 전달 단언 없음(`h_mult=1.0`도).
  6. 크레딧 하한 비교 `>`→`>=` (경계 정확히 0.02인 에피소드) — 경계 케이스 부재.
  7. `calibrate`가 validation 재생에 train을 사용 — 호출 인자(어떤 에피소드 집합인지) 단언 없음.
  8. `calibrate`가 `judge_validation`에 train 라이브 행을 전달 — 단언 없음.
  9. `calibrate`의 validation 재생이 적합 β 대신 0.0 — β 전달 단언 없음.
  10. `t_d` 판정에서 CI 조건(i) 제거 — 최저(ii)만 만족·CI 밖인 케이스 부재(반대 변이는 죽음).
- failure_log: a2ddc040

### 수정 요청 (Round 2)
1. **§8을 이 핸드오프 파일의 `## 8. Executor response` 아래에 직접 쓴다**(채팅 출력 금지). 감점 사유 먼저, 변이 원문 순서대로.
2. 위 생존 10건을 죽이는 테스트를 추가(리터럴 값, 스파이로 호출 인자 확인):
   - `run_controls`: (A만 밖, C 안) → A True/C False/전체 False; (A 안, C 밖) → A False/C True/전체 False; (A는 `t_c_i`만 밖) → A True; (A는 `t_a`만 밖) → A True; C replay 호출에 `rng`가 `None`이 아님을 확인(`k`>0일 때).
   - `sensitivity_all_kinds`: replay 호출 kwargs가 `beta == fit_beta_value`, `h_mult == 1.0`.
   - `calibrate`: validation replay 첫 호출(`beta`=적합 β, 에피소드 = validation 집합), `judge_validation`에 전달된 라이브 행이 validation 에피소드에서 나온 것, 크레딧이 정확히 0.02인 validation 에피소드가 제외됨(`n_excluded`에 포함)과 0.0200001인 것은 유지됨.
   - `judge_validation` t_d: (ii) 최저이지만 (i) CI 밖 → passed False.
3. 변이는 §5의 (a)~(o) 전체를 실제로 주입해 원문을 §8에 붙인다(생존 10건 포함, 형제 분기 각각). 못 하는 변이는 사유를 쓴다.
4. 테스트 파일의 `if False else` 잔재 제거. 미사용 임포트 없음 확인.
