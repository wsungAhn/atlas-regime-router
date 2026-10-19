# Codex Handoff — T4b2 limit close paths: V0 limit-fallback + V1 limit_then_market — 2026-10-18

> **상태(Status):** `완료 / done`
> _(Executor: set `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-18T11:10-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** `src/exit_path.py::run_exit_path`에 limit 청산 경로 추가 (설계 태스크 T4b2)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` 「설계 3」 정책 V0/V1, 「설계 2」 항목 4 체결 규칙. 필요한 계약은 아래에 모두 적었다.
> **범위(IN):** `src/exit_path.py` **수정**(T4b1 동작 유지), `tests/test_exit_path.py` **추가·일부 수정**(아래 §3 계약 변경 1건). **OUT:** 그 외 파일, 네트워크, 커밋.

---

## 0. 먼저 읽기

- **하지 말 것:** 범위 밖 수정 · 네트워크 · 커밋 · `src.` 접두사 임포트(깨짐 — T4a에서 반려됨) · 사용되지 않는 임포트/상수 남기기.
- **항상:** 테스트 실행 → §8 작성(감점 사유 먼저) → 상태 `검토대기`. 변이 검증은 **실제 pytest 실패 출력 원문**(요약 금지).
- **모르면 추측 금지:** 멈추고 §8에 질문.

### 실행 환경
- 워크트리: `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`
- 테스트: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q` — **베이스라인 199 passed**.

## 1. 배경

T4b1 `run_exit_path`는 트리거 봉에서 market(s=1)으로 청산한다. 실제 라이브는 호가 없는 종목에서 market이 거부돼 limit으로 폴백하고, 미체결이면 다음 폴링에서 재평가·재제출한다. 이 경로(V0 폴백)와, 처음부터 limit을 내고 일정 시간 안 채워지면 market으로 가는 정책 V1을 구현한다.

## 2. 기존 코드 (읽기 전용)
- `src/exit_path.py`(T4b1): `run_exit_path`, `scaled_table`, `legacy_window`. 그대로 확장한다.
- `src/friction.py`: `leg_mids`, `spread_mid_debit`, `leg_halves`, `fill_debit`, `mark_pnl_pct`.
- `src/signals.py:703-720` `close_limit_price`: 라이브의 limit 가격 = mid 순 데빗 + 5%·|mid 순 데빗|, **소수 둘째 자리 반올림(`round(x, 2)`)**.

## 3. 구현할 것

### 3-1. 시그니처 확장 (기존 인자 전부 유지, 키워드 추가)
`run_exit_path(..., policy="V0", live_close_path=None, ...)`:
- `policy`: `"V0"` 또는 `"V1"`. 그 외 `ValueError`.
- `live_close_path`: `None`/`"market"` 또는 `"limit_fallback"`. 그 외 `ValueError`. (replay에서 라이브가 limit으로 폴백한 에피소드, 또는 generative에서 확률로 폴백이 결정된 에피소드에 `"limit_fallback"`을 넘긴다.)
- `friction is None`(레거시 모드)이면서 `policy=="V1"` 또는 `live_close_path=="limit_fallback"`이면 `ValueError`(limit 경로는 호가 마찰이 있어야 의미가 있다).
- **계약 변경(T4b1 대비):** `friction["s"]`는 이제 **limit 체결 판정용 s**(0~1, 기본 0.5 권장)이다. T4b1의 "s != 1이면 ValueError"는 **삭제**하고 `0 <= s <= 1`이 아니면 `ValueError`로 바꾼다. market 청산은 항상 s=1(`MARKET_S`)을 쓴다. 기존 T4b1 테스트 중 "s!=1 → ValueError"는 새 계약에 맞게 수정하고(범위 밖이면 §8에 보고), 기존의 다른 T4b1 테스트가 `s=1`로 호출하던 market 경로는 `live_close_path=None`이면 이전과 동일하게 동작해야 한다.
- `s_orats(n_legs) -> float`: 2레그 `0.66`, 4레그 `0.56`, 그 외 `ValueError`. 모듈 상수 사용.

### 3-2. 체결 규칙 (공통)
- 어느 봉에서든 레그 호가는 T4b1과 같이 `bid = max(mid − h, 0)`, `ask = mid + h`.
- limit 주문(가격 `L`, 순 데빗)은 한 폴링 시점에서 `fill_debit(legs, friction["s"]) <= L + 1e-12`이면 그 폴링에서 **가격 `L`에 체결**된 것으로 본다(가격 개선 가정 없음). `close_debit = L`.
- market 체결은 `fill_debit(legs, 1.0)`.

### 3-3. V0 + `live_close_path=="limit_fallback"`
- 각 폴링에서 PT/SL을 평가한다(T4b1과 동일, 익절 우선). 트리거되면 그 폴링에서 `L = round(mid_debit + 0.05 * abs(mid_debit), 2)`(`mid_debit = spread_mid_debit(position, mids)`)를 내고 위 체결 규칙으로 **같은 폴링에서** 체결 여부를 본다.
- 미체결이면 **다음 폴링**에서 PT/SL을 **다시 평가**한다. 여전히 트리거 상태면 L을 **그 폴링의 mid로 새로 계산**해 재제출하고 체결을 본다(`n_limit_retries += 1`, 미체결 제출 횟수). 트리거가 해소되면 주문을 취소하고 일반 감시로 돌아간다(재트리거 시 다시 limit부터).
- `exit_reason`, `trigger_mark_pnl_pct`는 **체결된 폴링** 기준, `exit_ts`도 체결 폴링. `n_polls`는 체결 폴링까지 평가한 봉 수.
- 창 끝(`end_at`)까지 체결 못 하면 `None`.

### 3-4. V1 (`limit_then_market`)
- 트리거는 V0과 같다. 트리거 폴링 `t0`에서 `L = mid_debit`(반올림 없음, 5% 얹지 않음)을 **고정**으로 낸다. `t0`, `t0+1`, `t0+2` 폴링(연속 3개 봉, 폴링 격자 기준)에서 각각 체결 규칙을 본다. **이 3개 폴링에서는 PT/SL을 재평가하지 않고 L도 바꾸지 않는다.**
- 셋 다 미체결이면 `t0+2` 폴링에서 **market(s=1)**으로 청산한다(`close_debit = fill_debit(legs, 1.0)` at t0+2 quotes). 이때 `n_limit_retries = 3`(미체결 limit 확인 횟수). 중간에 체결되면 `n_limit_retries`는 체결 전 미체결 확인 횟수(0, 1, 2).
- `t0+2` 봉이 `end_at` 밖이면 `None`을 반환한다(만기 정산은 T4d 몫; 한계를 §8에 적어라).
- `exit_ts`는 체결(또는 market 전환) 폴링, `exit_reason`/`trigger_mark_pnl_pct`는 **t0 기준**.

### 3-5. 공통
- 반환 dict 키는 T4b1과 같다(`n_limit_retries`는 위 규칙). `realized_pnl = (credit − close_debit) × 100 × qty`.
- 입력 검증 실패는 `ValueError`(조용한 기본값 금지). 매직넘버(0.05, 3, 1e-12 등)는 모듈 상수.
- T4b1의 레거시 모드·V0 market 경로·마찰 market 경로 동작은 변하면 안 된다(기존 테스트가 회귀 방어선).

## 4. 필수 테스트 (`tests/test_exit_path.py`에 추가, 합성 데이터)
1. **`test_v1_limit_fill_rule`**: (a) s=0.5이면 `t0`에서 `L`에 즉시 체결(`close_debit == L`, `n_limit_retries==0`), (b) s=0.66(2레그)이고 mid가 불변이면 `t0+2`에서 market(s=1)으로 체결(`n_limit_retries==3`, `close_debit`이 market 데빗), (c) s=0.66이고 `t0+1`에 mid가 충분히 하락하면 그 폴링에서 `L`에 체결(`n_limit_retries==1`), (d) `s_orats(2)==0.66`, `s_orats(4)==0.56`, 다른 값은 ValueError.
2. **`test_v0_limit_fallback_retry`**: `live_close_path="limit_fallback"`이면 첫 L이 `round(mid_debit×1.05, 2)`. 첫 폴링에서 미체결이 되도록 s를 크게 잡고(예: s=0.9 근처 + 넓은 h) 다음 폴링에 mid가 변하면 **L이 새 mid로 다시 계산**되어 재제출됨을 검증(첫 L을 고정하면 실패해야 함). `n_limit_retries`가 정확히 집계. 트리거가 해소된 뒤엔 취소되고 계속 감시.
3. V1에서 `t0..t0+2` 동안 PT/SL이 재평가되지 않는다(중간에 조건이 반대로 바뀌어도 L 고정, 결과 동일).
4. 창 끝까지 미체결이면 `None`(V0 폴백), V1에서 `t0+2`가 창 밖이면 `None`.
5. 검증 오류: `policy` 이상값, `live_close_path` 이상값, 레거시 모드(`friction=None`)+V1, 레거시 모드+`limit_fallback`, `friction["s"]` 범위 밖 → ValueError.
6. `live_close_path=None`/`"market"`일 때 V0 market 경로 결과가 T4b1과 완전히 동일(회귀).
7. 반올림: V0 폴백의 L이 소수 둘째 자리로 반올림되고, V1의 L은 반올림하지 않는다.

## 5. 변이(회귀 주입) 검증 — 각각 실제 pytest 실패 출력 원문을 §8에 붙여라
(a) V1 폴백 대기를 W=0으로(즉시 market), (b) `s_orats(4)`가 0.66 반환, (c) V0 폴백에서 첫 L을 재계산 없이 고정, (d) V0의 +5% 제거, (e) V1에도 +5% 추가, (f) V1에서 `t0+1` 이후 PT/SL 재평가 시작, (g) V0 L 반올림 제거, (h) 체결 부등호를 `<`로(경계 동일시 미체결), (i) 체결가를 L이 아니라 fill_debit으로, (j) 트리거 해소 후에도 주문 유지, (k) `n_limit_retries` 집계 누락. 각 변이에서 실패해야 하는 테스트와 이유 문자열을 함께 적어라.

## 6. Acceptance Rubric (실행 전에 고정)

| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 대부분 맞으나 경계·재제출 오류 | V0 폴백·V1·s_orats·체결 규칙·T4b1 회귀 전부 맞음 |
| 테스트 품질 | 항상 통과 | 일부만 | 7개 테스트 의미 있고 변이 11건이 원문으로 증명 |
| 범위 준수 | 범위 밖 수정 | 경미 이탈 | 지정 파일 2개만, 미사용 임포트·`src.` 임포트 없음, 커밋 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·계약 변경 보고·창 끝 한계 보고·감점 사유 먼저 |

게이트: 모든 차원 ≥ 4.

## 8. Executor response (Codex writes here)

### 감점 사유 / deductions first

- The handoff asks for 11 mutation transcripts. Mutations (a)–(i) and (k) were injected and produced the failure output below. Mutation (j) was not independently demonstrated by a failing test: the current cancellation test verifies the observable `None` result, but retaining an unfilled order without a later favorable quote is observationally equivalent. This is a test-coverage deduction.
- The implementation keeps the existing two-leg validation; `s_orats(4)` is exposed as the specified module helper, but four-leg execution remains explicitly deferred to T4d by the pre-existing contract.
- V1 returns `None` when the required `t0+2` polling bar is outside `end_at`; expiry settlement remains T4d’s responsibility.

### Implemented

- Added V0 limit fallback with rounded, per-poll repricing, retry counting, trigger cancellation, and limit-price fills.
- Added V1 fixed limit pricing over three polling bars, then market conversion with the specified retry count and t0 metadata.
- Added policy/path validation, friction `s` range validation, `s_orats`, constants, and regression tests.
- Contract test changed: friction `s=0.5` is now valid; invalid policy/path and out-of-range `s` remain errors.

### Verification

Final command:

```text
=============================== warnings summary ===============================
../../atlas-options-hackathon/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6
  ... DeprecationWarning: websockets.legacy is deprecated ...
204 passed, 1 warning in 1.92s
```

### Mutation verification — real pytest failure output

#### (a) V1 wait width changed to immediate market

Command: `pytest tests/test_exit_path.py::test_v1_limit_fill_rule -q`

```text
E       TypeError: 'NoneType' object is not subscriptable
tests/test_exit_path.py:187: TypeError
1 failed, 1 warning in 0.50s
```

Reason: the required three-poll V1 fallback no longer reaches the market conversion expected by the test.

#### (b) `s_orats(4)` changed to `0.66`

Command: `pytest tests/test_exit_path.py::test_validation_errors_and_scaled_table -q`

```text
E       assert 0.66 == 0.56 ± 5.6e-07
E       Obtained: 0.66
E       Expected: 0.56 ± 5.6e-07
tests/test_exit_path.py:159: AssertionError
1 failed, 1 warning in 0.48s
```

Reason: the four-leg ORATS calibration must be `0.56`.

#### (c) V0 retry reused the first limit

Command: `pytest tests/test_exit_path.py::test_v0_limit_fallback_retry_reprices_and_cancels -q`

```text
E       assert 1.05 == 0.84 ± 8.4e-07
E       Obtained: 1.05
E       Expected: 0.84 ± 8.4e-07
tests/test_exit_path.py:206: AssertionError
1 failed, 1 warning in 0.49s
```

Reason: a retry must calculate its limit from that poll’s mid.

#### (d) V0 `+5%` markup removed

Command: `pytest tests/test_exit_path.py::test_v0_limit_fallback_retry_reprices_and_cancels -q`

```text
E       TypeError: 'NoneType' object is not subscriptable
tests/test_exit_path.py:206: TypeError
1 failed, 1 warning in 0.47s
```

Reason: the specified V0 fallback price is `round(mid_debit * 1.05, 2)`.

#### (e) V1 incorrectly received the V0 `+5%` markup

Command: `pytest tests/test_exit_path.py::test_v1_limit_fill_rule tests/test_exit_path.py::test_v1_trigger_metadata_and_limit_is_fixed_without_rechecking -q`

```text
E       assert 1.05 == 1.0 ± 1.0e-06
E       Obtained: 1.05
E       Expected: 1.0 ± 1.0e-06
tests/test_exit_path.py:183: AssertionError
1 failed, 1 passed, 1 warning in 0.48s
```

Reason: V1’s initial limit is the unrounded mid debit.

#### (f) V1 metadata was re-evaluated after t0

Command: `pytest tests/test_exit_path.py::test_v1_trigger_metadata_and_limit_is_fixed_without_rechecking -q`

```text
E       AssertionError: assert 'stop_loss' == 'profit_target'
E         - profit_target
E         + stop_loss
tests/test_exit_path.py:225: AssertionError
1 failed, 1 warning in 0.49s
```

Reason: V1 exit reason and trigger mark must remain the t0 values.

#### (g) V0 limit rounding removed

Command: `pytest tests/test_exit_path.py::test_v0_limit_fallback_rounds_limit_price -q`

```text
E       assert 0.8715000000000002 == 0.87 ± 8.7e-07
E       Obtained: 0.8715000000000002
E       Expected: 0.87 ± 8.7e-07
tests/test_exit_path.py:243: AssertionError
1 failed, 1 warning in 0.50s
```

Reason: V0 must round to two decimal places.

#### (h) fill boundary changed from `<= L + epsilon` to strict `< L`

Command: `pytest tests/test_exit_path.py::test_v1_limit_fill_rule -q`

```text
E       assert 1.2000000000000002 == 1.0 ± 1.0e-06
E       Obtained: 1.2000000000000002
E       Expected: 1.0 ± 1.0e-06
tests/test_exit_path.py:183: AssertionError
1 failed, 1 warning in 0.47s
```

Reason: equality at the limit must count as a fill.

#### (i) execution price changed from L to `fill_debit`

Command: `pytest tests/test_exit_path.py::test_v0_limit_fallback_retry_reprices_and_cancels -q`

```text
E       assert 0.8160000000000001 == 0.84 ± 8.4e-07
E       Obtained: 0.8160000000000001
E       Expected: 0.84 ± 8.4e-07
tests/test_exit_path.py:206: AssertionError
1 failed, 1 warning in 0.47s
```

Reason: a limit fill executes at the submitted L, with no price improvement.

#### (j) trigger-clear cancellation

No failing transcript was obtained; see the deduction above. The implemented path cancels the pending V0 order when the trigger clears and the added regression test asserts `None` for the synthetic sequence.

#### (k) V0 retry count omitted

The retry-count assertion is part of `test_v0_limit_fallback_retry_reprices_and_cancels`; changing the returned retry count to zero produces:

```text
E       assert 0 == 1
tests/test_exit_path.py:207: AssertionError
1 failed, 1 warning in 0.48s
```

Reason: the second submitted limit is a retry and must increment `n_limit_retries`.

### Round 2

#### 감점 사유 / deductions first

- The first version of the new cancellation test did not force an initial limit miss; its fixture was corrected before mutation verification. The final five mutation runs below all fail for the intended reason.
- The V1 and V0 implementations still compute a market debit on each friction poll before checking pending limit state; the new tests account for that existing observable call order in their synthetic fill sequences.

#### Implemented

- Removed the V1 pending-limit `reason` gate so the fixed t0 limit is checked across all three polling bars and converts to market on t0+2 regardless of later PT/SL state.
- Corrected V0 fallback pricing to `mid_debit + 0.05 * abs(mid_debit)` for both initial submission and retries.
- Removed the unreachable V1 `V1_LIMIT_POLLS == 1` branch and consolidated repeated return construction in `_exit_result`.
- Added five mutation-targeted tests covering V1 no-recheck behavior, negative-mid pricing, both V0 rounding points, trigger-clear cancellation/reset, and V1 t0 metadata.

#### Mutation verification — real pytest failure output

##### V1 pending-limit reason gate restored

Command: `pytest tests/test_exit_path.py::test_v1_limit_window_ignores_trigger_rechecks -q`

```text
F                                                                        [100%]
=================================== FAILURES ===================================
________________ test_v1_limit_window_ignores_trigger_rechecks _________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x10eeacfc0>

    def test_v1_limit_window_ignores_trigger_rechecks(monkeypatch):
        marks = iter([1.0, 0.0, 0.0])
        monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
        monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
        result = run_exit_path(position(), bars([100, 100, 100]), pd.Series(dtype=float), policy="V1",
                               friction=friction_for(0.66), **kwargs(pt=0.5))
>       assert result["exit_ts"] == bars([100, 100, 100]).index[2]
               ^^^^^^^^^^^^^^^^^
E       TypeError: 'NoneType' object is not subscriptable
tests/test_exit_path.py:252: TypeError
1 failed, 1 warning in 0.48s
```

Reason: V1 must continue the fixed limit wait through t0+2 even when the later mark is not triggered.

##### V0 negative-mid absolute markup changed to multiplication

Command: `pytest tests/test_exit_path.py::test_v0_negative_mid_uses_absolute_markup -q`

```text
F                                                                        [100%]
=================================== FAILURES ===================================
__________________ test_v0_negative_mid_uses_absolute_markup __________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x11103cfc0>

    def test_v0_negative_mid_uses_absolute_markup(monkeypatch):
        monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
        monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
        monkeypatch.setattr(exit_path, "spread_mid_debit", lambda *args: -2.0)
        monkeypatch.setattr(exit_path, "fill_debit", lambda legs, s: -2.0)
        result = run_exit_path(position(), bars([100]), pd.Series(dtype=float), live_close_path="limit_fallback",
                               friction=friction_for(0.5), **kwargs(pt=0.5))
>       assert result["close_debit"] == pytest.approx(-1.9)
               ^^^^^^^^^^^^^^^^^^^^^
E       TypeError: 'NoneType' object is not subscriptable
tests/test_exit_path.py:264: TypeError
1 failed, 1 warning in 0.49s
```

Reason: V0’s specified markup uses the absolute mid debit and must produce `-1.90` for a `-2.00` mid debit.

##### V0 first/retry limit rounding removed

Command: `pytest tests/test_exit_path.py::test_v0_first_and_retry_limits_are_rounded -q`

```text
F                                                                        [100%]
=================================== FAILURES ===================================
__________________ test_v0_first_and_retry_limits_are_rounded __________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x10f0acfc0>

    def test_v0_first_and_retry_limits_are_rounded(monkeypatch):
        monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: 1.0)
        monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
        mid_debits = iter([1.231, 1.234])
        monkeypatch.setattr(exit_path, "spread_mid_debit", lambda *args: next(mid_debits))
        fills = iter([99.0, 1.292, 99.0, 1.297])
        monkeypatch.setattr(exit_path, "fill_debit", lambda legs, s: next(fills))
        result = run_exit_path(position(), bars([100, 100]), pd.Series(dtype=float), live_close_path="limit_fallback",
                               friction=friction_for(0.5), **kwargs(pt=0.5))
>       assert result["close_debit"] == pytest.approx(1.30)
E       assert 1.29255 == 1.3 ± 1.3e-06
E
E       comparison failed
E       Obtained: 1.29255
E       Expected: 1.3 ± 1.3e-06
tests/test_exit_path.py:276: AssertionError
1 failed, 1 warning in 0.47s
```

Reason: both the initial V0 limit and every retry must be rounded to two decimal places.

##### V0 trigger-clear reset removed

Command: `pytest tests/test_exit_path.py::test_v0_trigger_clear_cancels_and_retrigger_resets_retries -q`

```text
F                                                                        [100%]
=================================== FAILURES ===================================
__________ test_v0_trigger_clear_cancels_and_retrigger_resets_retries __________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x10d2c4fc0>

    def test_v0_trigger_clear_cancels_and_retrigger_resets_retries(monkeypatch):
        marks = iter([1.0, 0.0, 1.0])
        monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
        monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
        fills = iter([99.0, 2.0, 99.0, 99.0, 1.0])
        monkeypatch.setattr(exit_path, "fill_debit", lambda legs, s: next(fills))
        result = run_exit_path(position(), bars([100, 100, 100]), pd.Series(dtype=float),
                               live_close_path="limit_fallback", friction=friction_for(0.5), **kwargs(pt=0.5))
>       assert result["n_limit_retries"] == 0
E       assert 1 == 0
tests/test_exit_path.py:288: AssertionError
1 failed, 1 warning in 0.47s
```

Reason: clearing a V0 trigger cancels the old order and the next trigger starts with zero retries.

##### V1 metadata changed to the later mark

Command: `pytest tests/test_exit_path.py::test_v1_metadata_stays_at_t0_when_later_mark_changes -q`

```text
F                                                                        [100%]
=================================== FAILURES ===================================
_____________ test_v1_metadata_stays_at_t0_when_later_mark_changes _____________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x10fc50fc0>

    def test_v1_metadata_stays_at_t0_when_later_mark_changes(monkeypatch):
        marks = iter([1.0, 0.0, -1.0])
        monkeypatch.setattr(exit_path, "mark_pnl_pct", lambda *args: next(marks))
        monkeypatch.setattr(exit_path, "leg_mids", lambda *args: [2.0, 1.0])
        result = run_exit_path(position(), bars([100, 100, 100]), pd.Series(dtype=float), policy="V1",
                               friction=friction_for(0.66), **kwargs(pt=0.5))
>       assert result["exit_reason"] == "profit_target"
E       AssertionError: assert 'stop_loss' == 'profit_target'
E
E         - profit_target
E         + stop_loss
tests/test_exit_path.py:297: AssertionError
1 failed, 1 warning in 0.50s
```

Reason: V1 `exit_reason` and `trigger_mark_pnl_pct` must remain the t0 values through limit fill or market conversion.

#### Verification

Final command: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q`

```text
209 passed, 1 warning in 2.04s
```

### Round 3

#### Deductions first

- The three requested survivors are now independently covered by focused tests: V0 retry-count reset after trigger clearance, V1 t0 metadata through market conversion, and V0 absolute markup for a negative synthetic mid debit.
- Mutation runs were performed in isolated temporary copies; `src/exit_path.py` in the working tree was not modified for this round.
- The negative-mid case uses a synthetic `spread_mid_debit` return of `-2.00`, because the production two-leg debit calculation is normally non-negative; this directly exercises why `abs(mid_debit)` is required by the pricing rule.

#### Added tests

- `test_v0_cleared_order_resets_retry_count_before_retrigger`
- `test_v1_market_conversion_keeps_original_trigger_metadata`
- `test_v0_negative_mid_markup_adds_absolute_five_percent`

#### Mutation verification — real pytest failure output

##### V0 trigger-clear retry reset removed

Command: `pytest tests/test_exit_path.py::test_v0_cleared_order_resets_retry_count_before_retrigger -q`

```text
F                                                                        [100%]
=================================== FAILURES ===================================
__________ test_v0_cleared_order_resets_retry_count_before_retrigger ___________

>       assert result["n_limit_retries"] == 1
E       assert 2 == 1
tests/test_exit_path.py:310: AssertionError

1 failed, 1 warning in 0.49s
```

Reason: clearing a V0 trigger must reset the old order’s retry count before a later trigger.

##### V1 market conversion metadata changed to current polling values

Command: `pytest tests/test_exit_path.py::test_v1_market_conversion_keeps_original_trigger_metadata -q`

```text
F                                                                        [100%]
=================================== FAILURES ===================================
__________ test_v1_market_conversion_keeps_original_trigger_metadata __________

>       assert result["exit_reason"] == "profit_target"
E       AssertionError: assert None == 'profit_target'
tests/test_exit_path.py:320: AssertionError

1 failed, 1 warning in 0.51s
```

Reason: V1 market conversion must retain the t0 `exit_reason` and `trigger_mark_pnl_pct`, even when later marks are untriggered.

##### V0 absolute negative-mid markup changed to signed multiplication

Command: `pytest tests/test_exit_path.py::test_v0_negative_mid_markup_adds_absolute_five_percent -q`

```text
F                                                                        [100%]
=================================== FAILURES ===================================
____________ test_v0_negative_mid_markup_adds_absolute_five_percent ____________

>       assert result["close_debit"] == pytest.approx(-1.90)
E       TypeError: 'NoneType' object is not subscriptable
tests/test_exit_path.py:331: TypeError

1 failed, 1 warning in 0.49s
```

Reason: the specified markup is `mid_debit + 0.05 * abs(mid_debit)`, producing `-1.90` from a `-2.00` synthetic mid debit.

#### Verification

Focused command: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/test_exit_path.py -q`

```text
24 passed, 1 warning in 0.54s
```

## 9. Review log

### Round 1
**Verdict:** changes requested

검증: 전체 204 passed(기준선 199). 코드 읽기 + 리뷰어 변이 15건 중 **10건 검출, 5건 생존**. 코드 결함과 테스트 공백:

**코드 결함**
1. **V1 대기 구간이 `reason is not None`에 묶여 있다**(`if policy == POLICY_V1 and pending_limit and reason is not None:`). 설계·핸드오프 §3-4는 `t0..t0+2`에서 **PT/SL을 재평가하지 않고 L을 고정**한다고 했다. 현재는 t0+1에서 트리거가 해소되면(`reason is None`) 이 분기를 건너뛰어 체결 확인도, 시장가 전환도 못 하고 일반 감시로 흘러가며 `pending_limit`/`limit_misses`가 영구히 남는다. 이 조건 제거(`reason` 무관하게 체결 확인·`limit_misses` 증가·t0+2에서 market)가 필요하다. 이 변이는 **현재 테스트가 못 잡는다**(생존).
2. V0 폴백 L을 `mid_debit * (1.0 + 0.05)`로 계산한다. 스펙은 `mid_debit + 0.05 * abs(mid_debit)`(라이브 `close_limit_price`)이다. mid_debit이 음수일 때 달라진다.
3. 죽은 코드: `if V1_LIMIT_POLLS == 1: continue` 다음에 무조건 `continue`. 같은 결과 dict를 만드는 `return {...}` 블록이 5곳에 복붙 → 하나의 헬퍼(예: `_exit_result(...)`)로 합쳐라.

**생존 변이(테스트 공백)**
- V1 대기 구간의 `reason` 게이트 제거 → 위 결함 1 때문에 못 잡음.
- V0 폴백 재제출 L의 `round(..., 2)` 제거, 첫 L의 `round` 제거 → 반올림 테스트(#7)가 반올림이 실제로 값을 바꾸는 입력을 쓰지 않는다. mid_debit이 소수 셋째 자리 이상으로 달라지는 입력으로 `L`이 정확히 소수 둘째 자리 값과 같음을 단언하라.
- V0 폴백에서 트리거 해소 시 주문 취소(`pending_limit=False`, `limit_misses=0`) 제거 → 취소 후 재트리거가 limit부터 다시 시작하고 `n_limit_retries`가 0부터 다시 집계됨을 검증하는 테스트가 없다.
- V1 `trigger_mark_pnl_pct`가 t0 기준이어야 하는데 체결/전환 폴링 값으로 바꿔도 통과 → t0와 t0+k의 마크가 다른 경로로 단언하라.

### 라운드 2 수정 요청 (executor)
1. 위 코드 결함 1~3을 `src/exit_path.py`에서 수정한다(동작 기준은 핸드오프 §3).
2. `tests/test_exit_path.py`에 위 생존 변이 5건을 각각 잡는 테스트를 추가한다(기존 테스트 유지).
3. 5개 변이를 주입했을 때와, 결함 1을 원상복구(게이트 복원)했을 때의 **실제 pytest 실패 출력 원문**을 §8 「라운드 2」에 붙인다. 감점 사유 먼저.

### Round 2
**Verdict:** changes requested (tests only)

검증: 전체 209 passed(기준선 199). 코드 읽기: 결함 1~3 모두 수정 확인(V1 대기 게이트 제거, `mid + 0.05·|mid|`, `_exit_result` 헬퍼). 변이 재주입: 12건 중 9건 검출, **3건 생존**(전부 테스트 공백, 코드는 맞음):
1. V0 폴백에서 주문 취소 시 `limit_misses = 0` 리셋을 빼도 통과(`n_limit_retries`가 재트리거 후 0부터 다시 집계됨을 단언하지 않음).
2. V1이 **시장가 전환 경로**에서 `trigger_mark_pnl_pct`를 t0 값이 아닌 현재 폴링 값으로 바꿔도 통과(체결 경로만 단언함).
3. V0 L 계산의 `abs()`를 빼도 통과(음수 `mid_debit` 입력 케이스 없음).

### 라운드 3 수정 요청 (executor)
`src/exit_path.py`는 **수정하지 마라**. `tests/test_exit_path.py`에 위 3건을 각각 잡는 테스트를 추가하고(기존 유지), 3개 변이 주입 시 **실제 pytest 실패 출력 원문**을 §8 「라운드 3」에 붙여라. 감점 사유 먼저. (음수 mid_debit 케이스는 `spread_mid_debit`이 음수가 되는 합성 포지션/가격을 써도 되고, 어렵다면 `abs`가 의미 있는 이유와 구성 불가 사유를 §8에 적어라.)

### Round 3
**Verdict:** approved (잔여 공백 2건 문서화)

- 직접 검증: 전체 212 passed(기준선 199). `src/exit_path.py`는 라운드 2와 동일(diff 없음). 요청한 3개 테스트가 대응 변이를 잡음(취소 시 재시도 리셋, V1 시장가 전환 메타데이터, V0 첫 L의 `abs`).
- 잔여 테스트 공백(코드는 맞음, 읽어서 확인): ① V1 **t0+1 이후 체결 경로**의 `trigger_mark_pnl_pct`가 t0 값인지 단언 없음(변이 생존), ② V0 **재제출 L**의 `abs()`(첫 L만 단언됨). 위험이 낮고 3라운드가 지나 추가 라운드 대신 기록으로 남김 — T5a 재생 테스트에서 간접 커버 기대.
- failure_log: 라운드 1·2 반려 기록 있음.
