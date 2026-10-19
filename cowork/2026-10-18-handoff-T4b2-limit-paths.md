# Codex Handoff — T4b2 limit close paths: V0 limit-fallback + V1 limit_then_market — 2026-10-18

> **상태(Status):** `대기중 / pending`
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

_(비어 있음 — 감점 사유 먼저.)_

## 9. Review log

_(비어 있음 — reviewer가 작성)_
