# Codex Handoff — T5b statistics utilities — 2026-09-30

> **상태(Status):** `검토대기 / review-pending`
> _(Executor: `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** `session=codex machine=friction-backtest started=2026-09-30T00:00:00-07:00`
>
> **작성자:** Claude Sonnet 5.5 (reviewer) → **수행자:** Codex gpt-reserve (effort low)
> **작업명:** 마찰 백테스트용 통계 유틸 `src/friction_stats.py` (설계문서 태스크 T5b)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` 설계 4-2(L227), 4-4(L238), 6(L312-326), 테스트 #14/#23/#26(L357,366,369). 필요한 사양은 아래에 옮겨 놓았다.
> **범위(IN):** 새 파일 `src/friction_stats.py`, 새 테스트 `tests/test_friction_stats.py`. **OUT:** 다른 `src/` 수정, 커밋, 새 의존성 추가.

## 0. 먼저 읽기 (Executor)
- **하지 말 것:** 기존 파일 수정 · 새 패키지 설치(이미 설치된 `numpy`만 사용; `scipy`가 필요해 보이면 쓰지 말고 §8에 질문) · 커밋.
- **항상:** 작업 후 테스트 → 통과 확인 → §8 작성 → 상태줄 `검토대기`. 모르면 추측 말고 §8에 질문.
### 실행 환경
- 워크트리: `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`
- 테스트: 워크트리에서 `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q`
- **현재 베이스라인: 159 passed.** 그 아래로 떨어지면 회귀.

## 1. 배경
라이브와 백테스트의 손실 격차를 설명하는 마찰 모델을 라이브 거래 통계에 **보정**하고, 수정안을 **쌍대 비교**하려 한다. 표본이 작아(clean 96개) 모든 판정은 부트스트랩 신뢰구간과 다중비교 보정에 의존한다. 이 태스크는 그 통계 함수 3+1개를 만든다. 전부 순수 함수, 네트워크·파일 I/O 없음.

## 2. Task T5b — `src/friction_stats.py`

### 함수 계약 (이 시그니처로)
```python
DEFAULT_SEED = 20260930
DEFAULT_RESAMPLES = 2000

def bootstrap_ci(values, stat=np.mean, alpha=0.10, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED) -> tuple[float, float]
    # (1-alpha) 양측 percentile CI. 표본에서 **복원추출**로 len(values)개를 n_resamples번 뽑아
    # stat을 계산하고 alpha/2, 1-alpha/2 분위수를 돌려준다. rng = numpy.random.default_rng(seed).
    # values가 비어 있으면 ValueError.

def bootstrap_se(values, stat=np.mean, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED) -> float
    # 같은 부트스트랩 분포의 표준편차(ddof=1). 설계 4-2에서 SE_j로 쓴다. 같은 seed·입력이면 bootstrap_ci와 같은 리샘플 분포를 써야 한다(공통 private 헬퍼 하나).

def bootstrap_p_one_sided(values, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED) -> float
    # H0: 평균 <= 0 대 H1: 평균 > 0 의 단측 부트스트랩 p값 = (리샘플 평균 <= 0 인 횟수 + 1) / (n_resamples + 1).
    # (+1은 p=0을 피하는 보수적 보정. 그 이유를 주석 한 줄로.)

def paired_diff(pnl_f, pnl_c00) -> np.ndarray
    # 같은 길이의 두 시퀀스. pnl_f[i]가 None이면 "필터로 탈락 = 미거래"이며 0으로 계산한다.
    # 반환 d_i = (pnl_f[i] or 0 if None) - pnl_c00[i]. 길이가 다르면 ValueError.
    # 평균은 호출부가 np.mean(d)로 구하므로 분모는 **항상 전체 N**이다(탈락 포함).

def holm(pvalues, alpha=0.10) -> list[bool]
    # Holm step-down. p를 오름차순 정렬, k번째(1부터) p_(k)가 alpha/(m-k+1) 이하이면 기각하고 다음으로,
    # 처음으로 넘으면 그 이후는 전부 기각 안 함. 반환은 **입력 순서**의 bool 리스트.
```
`stat` 인자는 `values`(1-D numpy array)를 받아 스칼라를 돌려주는 호출 가능 객체. 성능을 위해 `stat is np.mean`이면 벡터화해도 되지만 **결과는 같아야** 한다.

### 주의
- 난수는 반드시 `numpy.random.default_rng(seed)`만 사용(전역 `np.random.seed` 금지) — 같은 시드면 같은 결과.
- 마법 숫자 금지(상수 2개만), 새 추상화 금지(Simplicity First), 모듈 docstring 영어.

### 필수 테스트 `tests/test_friction_stats.py`
1. `bootstrap_ci`: 상수 입력 `[2.0]*50` → `(2.0, 2.0)`. 30개 1, 70개 0 → CI가 정규근사 `0.3 ± 1.645*sqrt(0.3*0.7/100)` = `[0.2246, 0.3754]`의 **±0.03 이내**. 같은 seed면 같은 결과, 다른 seed면 (거의 확실히) 다른 결과. 빈 입력은 `ValueError`.
2. `bootstrap_se`: 정규근사 `sqrt(0.3*0.7/100)=0.0458` 근처(±0.01). `bootstrap_ci`와 같은 seed로 호출했을 때 같은 리샘플 분포를 쓰는지 간접 확인: `stat=np.mean`에서 `se`가 일관.
3. `bootstrap_p_one_sided`: 모두 양수인 큰 표본 → p가 `1/(n_resamples+1)` (즉 매우 작음); 평균이 0인 대칭 표본 → p가 0.5 근처(±0.1); 모두 음수 → p가 1.0.
4. `paired_diff`: `pnl_f=[1.0, None, 3.0, None, 5.0, None, None, 2.0, 4.0, 6.0]`(10개 중 4개 탈락), `pnl_c00=[...10개...]` → `len(d)==10`, 탈락 위치의 d는 `-pnl_c00[i]`, `np.mean(d)`의 분모는 10(남은 6개가 아님). 길이 불일치는 `ValueError`.
5. `holm` 해석값: α=0.10에서 `[0.01, 0.04, 0.03]` → `[True, True, True]`; `[0.03, 0.04, 0.2]` → `[True, True, False]`; `[0.05, 0.06, 0.07]` → `[False, False, False]`; 입력 순서가 섞여도(`[0.2, 0.03, 0.04]`) 기각 여부가 **입력 위치**로 돌려진다 → `[False, True, True]`; 빈 리스트 → `[]`; **step-down 중단 규칙**: `[0.01, 0.5, 0.011]`에서 `0.5`가 실패하면 그 뒤(정렬상 더 큰 값)는 검사 없이 기각 안 함 → 정렬상 `[0.01,0.011,0.5]`이므로 `[True, False, True]`.
각 테스트는 **구현을 의도적으로 깨뜨리면 실패해야 한다**: 리뷰어가 scratch 사본에 주입한다(복원→비복원 추출, `paired_diff`의 분모를 남은 개수로, Holm에서 보정 없이 `p<=alpha` 비교, Holm 중단 규칙 제거, p값의 `+1` 제거, 전역 시드 사용 등).

## 5. 코딩 원칙
식별자·주석·문서는 영어, type hints, 완료 선언 전 실제 테스트 실행.

## 6. 완료 기준
- [ ] `src/friction_stats.py` 5개 함수
- [ ] `tests/test_friction_stats.py` 위 5그룹 통과
- [ ] 전체 스위트 ≥159 passed
- [ ] 자기채점 §8-6 (감점 사유 먼저)
- [ ] 커밋 안 함

### Acceptance rubric (고정)
| 차원 | 5점 기준 | 게이트 |
|---|---|---|
| 정확성 | 함수 계약·해석값 일치, 회귀 주입 시 테스트 실패 | 4+ |
| 재현성 | 같은 seed·입력이면 항상 같은 출력, 전역 난수 상태 무사용 | 4+ |
| 범위 준수 | 새 의존성 없음, 기존 파일 무수정, 커밋 없음 | 5 |

## 7. 보고 항목 (§8)
1. 변경 파일 2. 새 테스트 수 3. 전체 테스트 결과 4. 판단한 점/막힌 점 5. 루브릭 자기채점(감점 사유 먼저)

## 8. Executor response (Codex writes here)
1. 변경 파일: `src/friction_stats.py`, `tests/test_friction_stats.py`
2. 새 테스트 수: 8개 (라운드 2에서 3개 추가)
3. 전체 테스트 결과: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q` → `166 passed, 1 warning`
4. 판단한 점/막힌 점: reviewer 요청대로 정확히 0인 입력의 `<=` 경계, 동일 리샘플 기반 `ddof=1`, 전역 RNG 상태 불변을 검증했다. 구현 변경 없음. scipy·추가 의존성은 사용하지 않았다. 막힌 점 없음.
5. 루브릭 자기채점(감점 사유 먼저): 감점 사유 없음. 정확성 5/5, 재현성 5/5, 범위 준수 5/5.

## 9. Reviewer log (reviewer writes here)

### 라운드 1
**Verdict:** changes requested
검증: 전체 164 passed 직접 재실행, 코드 전문 읽음 — **구현은 명세·해석값과 일치**(Holm 케이스 6종, 정규근사 CI/SE, 재현성 모두 직접 확인). scratch 회귀 주입 9종 중 6종은 테스트가 포착(비복원, Holm 보정 제거, 중단 규칙 제거, p값 +1 제거, None을 0으로 안 침, CI 상한 오류). **3종은 테스트가 잡지 못함(테스트 약점):**
1. `bootstrap_p_one_sided`의 `statistics <= 0`을 `< 0`으로 바꿔도 통과 → **평균이 정확히 0인 경우**를 검증하는 테스트 추가: 전부 0인 입력 `[0.0]*50`에서 p가 정확히 `1.0`이어야 한다(모든 리샘플 평균이 0 → `<=`라서 전부 카운트).
2. `bootstrap_se`의 `ddof=1`→`0` 변경을 못 잡음 → 작은 입력에서 구분되는 테스트 추가: `values=[0.0, 1.0]`, `n_resamples`를 크게(예: 20000)로 두고 `bootstrap_se`가 이론값 `0.3536`(= sqrt(0.25/2)) 근처(±0.01)인지는 ddof로 구분이 안 되므로, **대신** 같은 입력을 직접 재현해 계산: 테스트 안에서 `np.random.default_rng(seed).choice(..., replace=True)`로 동일 리샘플 행렬을 만들어 `np.std(means, ddof=1)`과 `bootstrap_se` 결과가 **정확히 같음**(`==` 또는 `rel=1e-12`)을 단언한다.
3. 전역 난수 상태 사용(`np.random.seed`)을 못 잡음 → 테스트 추가: `np.random.seed(123); before = np.random.get_state()[1].copy()`; `bootstrap_ci`/`bootstrap_se`/`bootstrap_p_one_sided`를 호출; 호출 후 `np.random.get_state()[1]`이 `before`와 **같음**(전역 상태 불변)을 단언한다.
**잘한 점:** 공통 private 헬퍼로 CI/SE가 같은 분포를 공유, 빈 입력 예외, 새 의존성 없음.
**요청:** 위 3개 테스트를 `tests/test_friction_stats.py`에 추가(기존 테스트는 유지). 구현 변경 불필요(구현 변경이 필요해 보이면 §8에 이유를 적고 멈춰라). 전체 스위트 실행 결과를 §8에 갱신. 커밋 금지.

### 라운드 2
**Verdict:** approved
검증: 전체 166 passed 직접 재실행, 구현 파일 무변경(diff 없음), 추가 테스트 3개 확인(정확히 0인 평균 p=1.0, ddof=1을 동일 리샘플 재현으로 `rel=1e-12` 대조, 전역 난수 상태 불변). **회귀 주입 9종을 같은 방식으로 재실행 → 전부 해당 테스트가 실패로 포착**(이전 escape 3종 포함). failure_log: round-1 기록됨(TEST_FAILING).
