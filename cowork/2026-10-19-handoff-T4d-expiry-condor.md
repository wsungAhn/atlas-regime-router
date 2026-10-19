# Codex Handoff — T4d expiry settlement + iron condor (4-leg) — 2026-10-19

> **상태(Status):** `대기중 / pending`
> _(Executor: set `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-19T09:30-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** `src/exit_path.py`에 만기 정산과 콘도르(4레그) 경로 추가 (설계 태스크 T4d)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` 「설계 3」 콘도르·만기 문단, 테스트 #17/#18/#20. 필요한 계약은 아래에 모두 적었다.
> **범위(IN):** `src/exit_path.py` 수정, `tests/test_exit_path.py` 추가·일부 수정(아래 §3-4 파리티 테스트 1건). **OUT:** 그 외 파일, 네트워크, 커밋.

---

## 0. 먼저 읽기

- **하지 말 것:** 범위 밖 수정 · 네트워크 · 커밋 · `src.` 접두사 임포트 · 미사용 임포트/상수.
- **항상:** 테스트 실행 → §8(감점 사유 먼저) → 상태 `검토대기`. 변이 검증은 **실제 pytest 실패 출력 원문**(요약 금지). **변이는 그 규칙이 구현된 모든 코드 위치(형제 분기 포함)에 각각 주입**하라 — 이전 태스크 4건 연속으로 한쪽 분기의 변이만 걸리고 형제 분기 변이가 살아남았다(2레그/4레그, 풋/콜, 정책별 분기 각각).
- **모르면 추측 금지:** 멈추고 §8에 질문. 기존 테스트를 바꿔야 하면 바꾼 이유를 §8에 보고.

### 실행 환경
- 워크트리: `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`
- 테스트: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q` — **베이스라인 241 passed**.

## 1. 배경

`run_exit_path`는 지금 2레그 수직 스프레드만 받고, 만기까지 트리거가 없으면 `None`을 돌려준다. 옵션 전용 셀링 전략은 "익절 없이 만기까지 보유"(`pt=None`)와 아이언 콘도르(풋 수직 + 콜 수직)를 쓰므로, 이 태스크에서 ① 4레그 포지션과 ② 만기 정산을 추가한다.

## 2. 기존 코드 (확장 대상)

- `src/exit_path.py`: `run_exit_path`, `_trigger_reason`, `_spread_type`, `_validate_position`, `_exit_result`, `s_orats`(4레그 0.56 이미 있음).
- `src/friction.py`: `leg_mids`, `spread_mid_debit`, `leg_halves`, `mark_pnl_pct`(4레그 지원), `fill_debit`(4레그 지원), `parse_occ`.
- `src/vendor/credit_spread_simulator.py::price_spread_to_close(S_t, short_strike, long_strike, T_remaining, r, iv_t, spread_type)`: `T_remaining <= 0`이면 내재가치 분기. 콘도르의 레거시 모드 데빗은 **두 수직의 `price_spread_to_close` 합**.

## 3. 구현할 것

### 3-1. 4레그 포지션 (콘도르)
- `position`은 2레그(수직 1개) 또는 4레그(풋 수직 1개 + 콜 수직 1개)를 받는다. 4레그는 풋 2개(숏 1·롱 1, 숏 행사가 > 롱 행사가) + 콜 2개(숏 1·롱 1, 숏 행사가 < 롱 행사가)이고, **풋 숏 행사가 < 콜 숏 행사가**여야 한다. 위반(숏 2개가 같은 타입, 방향 틀림, 풋 숏 ≥ 콜 숏, 레그 수 3 등)은 `ValueError`.
- `_spread_type`을 일반화: 수직 목록 `[(spread_type, short_strike, long_strike), ...]`을 돌려주는 헬퍼로 바꾸고 2레그 동작은 그대로 유지.
- **레거시 모드(`friction is None`)** 마크: 각 수직에 `price_spread_to_close`를 호출해 데빗을 **합산**, `mark_pnl = (credit − 합산 데빗)/credit`. (credit은 4레그 합산 크레딧.) 트리거는 **4레그 합산 마크**로 판정한다. 수직별로 따로 판정하지 않는다.
- **마찰 모드**: 이미 4레그를 지원하는 `mark_pnl_pct`/`leg_mids`/`leg_halves`/`fill_debit`를 그대로 쓴다(교차비용은 4레그 `Σh`).
- **V4 `underlying_breach` 콘도르**: 풋 숏 행사가 아래로 내려가거나(`S < put_short`) 콜 숏 행사가 위로 올라가면(`S > call_short`) 어느 쪽이든 `stop_loss`(엄격 부등식). 2레그는 기존 규칙 유지.
- **V6 `underlying_move` 콘도르**: 어느 방향이든 `abs(S − S0)/S0 >= stop_underlying_pct`이면 `stop_loss`. 2레그는 기존(불리한 방향만) 유지.
- V5·V0·V1·V2·V3, `live_close_path` 실행 로직은 4레그에서도 동일하게 동작해야 한다.

### 3-2. 만기 정산
- 폴링이 트리거 없이 끝났고(위 로직에서 `return`되지 않음), **창의 마지막 봉의 날짜(`bar_time.normalize()`)가 `expiry` 날짜 이상**이면 그 마지막 봉에서 **내재가치로 정산**한다: `exit_reason="expiry"`, `exit_ts` = 그 마지막 봉 시각, `close_debit` = 만기 내재가치 순 데빗(레거시 모드: 수직별 `price_spread_to_close`의 `T=0` 분기 합; 마찰 모드: `T=0`일 때의 `spread_mid_debit(position, leg_mids(...))` — **β·h·노이즈·교차비용 모두 0**), `realized_pnl = (credit − close_debit) × 100 × qty`, `trigger_mark_pnl_pct = (credit − close_debit)/credit`, `n_polls` = 평가한 봉 수, `n_limit_retries` = 대기 중이던 limit 주문의 `limit_misses`(없으면 0).
- 마지막 봉이 만기일 이전이면 지금처럼 `None`(창이 만기에 못 미침).
- **만기일 봉에서도 트리거 평가는 그대로 한다**(레거시 `reprice_exits_intraday`와 같이 `days_to_expiry <= 0`이면 `T=0` 내재가치로 마크). 정산은 그 봉들에서 트리거가 없을 때만.
- 만기 시점에 대기 중이던 limit 주문(V0 폴백/V1)은 취소하고 정산한다(`pending_limit`와 무관하게 정산).
- 모듈 상수: `EXIT_REASON_EXPIRY = "expiry"`.

### 3-3. 반환값
키는 기존과 같다. `exit_reason`에 `"expiry"`가 추가된다.

### 3-4. 기존 테스트 영향
레거시 파리티 테스트(`test_legacy_mode_parity_with_reprice_exits_intraday`)에서 "트리거 없는 거래 → `None`" 케이스의 창이 `expiry` 날짜에 닿으면 이제 `"expiry"` 결과가 나온다. 그 케이스는 창이 만기에 못 미치게 조정하거나(권장), 만기에 닿는 경우를 `"expiry"` 단언으로 바꾸고 이유를 §8에 보고하라. 다른 기존 테스트는 그대로 통과해야 한다.

## 4. 필수 테스트 (`tests/test_exit_path.py`에 추가, 합성 데이터)
1. **`test_v3_no_stop_and_expiry_settlement`**: −300% 마크 경로에서도 V3는 청산하지 않고 만기에 정산: `exit_reason=="expiry"`, `close_debit`이 `T=0` 내재가치와 일치(레거시 모드와 마찰 모드 모두), **마찰 모드에서도 교차비용 0**(`β`, `h`를 크게 줘도 `close_debit`·`realized_pnl` 불변). `pt=None`+V0, V4, V5, V6에서도 만기 정산 동작.
2. 만기 미도달 창은 `None`; 만기일 봉에서 트리거가 나면 정산이 아니라 그 트리거 결과; 대기 중 limit 주문은 만기에 취소·정산(`n_limit_retries`).
3. **`test_v4_underlying_breach` 콘도르**: 마크가 −50%여도 풋 쪽 돌파 / 콜 쪽 돌파 각각 청산, 정확히 행사가와 같은 종가는 돌파 아님, 두 숏 행사가 사이에 있으면 청산 안 함.
4. **`test_condor_combined_trigger`**: 두 수직 크레딧이 같고 풋 쪽 −150%, 콜 쪽 +80%(합산 −35%)이면 SL이 나지 않는다(수직별 판정이었다면 SL이 났을 입력). 합산이 임계를 넘는 입력에서는 SL이 난다.
5. 콘도르 레거시 데빗 = 두 수직 `price_spread_to_close` 합(풋 쪽만 또는 콜 쪽만 쓰면 실패해야 함).
6. 콘도르 마찰 모드: β=0, h>0에서 트리거는 레거시와 같고 `realized`가 `Σh(4레그) × 100 × qty`만큼 작다; `h_mult`가 비례한다; `live_close_path="limit_fallback"`와 V1이 4레그에서도 동작(`s_orats(4)==0.56` 사용 케이스 포함).
7. 콘도르 V6: 상방/하방 어느 쪽이든 `abs` 이동이 임계 이상이면 청산(경계 `>=`), 임계 미만이면 안 함.
8. 검증 오류: 콘도르 구성 위반(숏 2개 같은 타입, 풋 숏 ≥ 콜 숏, 방향 틀림, 레그 3개) → ValueError; 2레그 기존 동작 불변.

## 5. 변이(회귀 주입) 검증 — 각각 실제 pytest 실패 출력 원문을 §8에 붙여라 (형제 분기마다 주입)
(a) 만기 정산에 교차비용(h) 가산 — **레거시/마찰 두 분기 각각** (b) 정산 생략(`None`) (c) 창이 만기에 못 미쳐도 정산 (d) 만기일 봉 트리거 평가 생략 (e) 대기 limit이 있어도 정산 안 함 (f) V4 콘도르 콜 쪽 검사 누락 (g) V4 콘도르 풋 쪽 검사 누락 (h) V4 콘도르 `<`→`<=` 양쪽 각각 (i) 수직별 판정으로 변경 (j) 레거시 4레그 데빗이 풋 수직만/콜 수직만 (k) V6 콘도르가 한쪽 방향만 (l) V6 콘도르 `>=`→`>` (m) 콘도르 검증 완화(풋 숏 ≥ 콜 숏 허용) (n) `exit_reason`/`trigger_mark_pnl_pct` 값 변경 (o) `qty` 또는 `100` 누락. 각 변이에서 실패해야 하는 테스트와 이유 문자열을 함께 적어라.

## 6. Acceptance Rubric (실행 전에 고정)

| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 대부분 맞으나 경계·분기 오류 | 콘도르 합산 판정·V4/V6 양쪽·만기 정산(교차비용 0)·T4b/T4c 회귀 전부 맞음 |
| 테스트 품질 | 항상 통과 | 일부만 | 8개 테스트가 형제 분기(풋/콜, 레거시/마찰)를 모두 단언하고 변이 15건이 원문으로 증명 |
| 범위 준수 | 범위 밖 수정 | 경미 이탈 | 지정 파일 2개만, 미사용 임포트·`src.` 임포트 없음, 커밋 없음, 기존 테스트 변경은 보고 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·감점 사유 먼저·기존 테스트 변경 사유 |

게이트: 모든 차원 ≥ 4.

## 8. Executor response (Codex writes here)

_(비어 있음 — 감점 사유 먼저.)_

## 9. Review log

_(비어 있음 — reviewer가 작성)_
