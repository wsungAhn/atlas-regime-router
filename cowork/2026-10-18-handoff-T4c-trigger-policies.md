# Codex Handoff — T4c trigger policies V2/V3/V4 + account stop (V5) + underlying-move stop (V6) + hold-to-expiry — 2026-10-18

> **상태(Status):** `대기중 / pending`
> _(Executor: set `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-18T12:05-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** `src/exit_path.py`에 트리거 정책 추가 (설계 태스크 T4c + 옵션 전용 전략 요구)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` 「설계 3」 정책 V2/V3/V4. **V5/V6/PT 끄기는 설계 문서에 없는 신규 요구**다(사용자 2026-10-18: "이익이 나면 만기까지 끌고 가고, 손절은 -5%": 기준 후보 A=계좌 대비, C=기초자산 가격 — 같은 시뮬레이터에서 쌍대비교). 신규 정책은 기존 사전등록 5개(V0~V4)의 통계 가족에 들어가지 않는 **탐색용**이다.
> **범위(IN):** `src/exit_path.py` 수정, `tests/test_exit_path.py` 추가. **OUT:** 그 외 파일, 네트워크, 커밋.

---

## 0. 먼저 읽기

- **하지 말 것:** 범위 밖 수정 · 네트워크 · 커밋 · `src.` 접두사 임포트 · 미사용 임포트/상수.
- **항상:** 테스트 실행 → §8(감점 사유 먼저) → 상태 `검토대기`. 변이 검증은 **실제 pytest 실패 출력 원문**(요약 금지). **변이는 정책마다 해당 코드 위치 전부**(예: 같은 규칙이 여러 분기에 있으면 모든 분기)를 바꿔서 확인하라 — 이전 태스크들에서 형제 분기의 변이가 테스트를 빠져나간 전례가 있다.
- **모르면 추측 금지:** 멈추고 §8에 질문.

### 실행 환경
- 워크트리: `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`
- 테스트: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q` — **베이스라인 212 passed**.

## 1. 배경

T4b1/T4b2의 `run_exit_path`는 트리거가 하나(마크 손익 PT/SL)이고 실행 방식(market, limit 폴백, V1)이 붙어 있다. 이제 **트리거를 정책별로 분리**한다. 실행 방식은 그대로 두고(트리거가 걸린 폴링에서 `live_close_path`에 따라 market 또는 limit 폴백; V1은 기존대로 V0 트리거 + limit_then_market), 새 트리거 정책을 추가한다.

## 2. 기존 코드 (읽기 전용 아님: `src/exit_path.py`를 확장)

- `src/exit_path.py`: `run_exit_path`(현재 policy `"V0"`, `"V1"`), `_exit_result`, `scaled_table`, `s_orats`, `legacy_window`.
- 현재 트리거: `reason = "profit_target" if mark_pnl >= pt else "stop_loss" if mark_pnl <= sl else None`.

## 3. 구현할 것

### 3-1. 정책 레지스트리 (모듈 상수)
허용 policy: `"V0"`(마크 PT/SL), `"V1"`(V0 트리거 + limit_then_market 실행; T4b2 그대로), `"V2"`, `"V3"`, `"V4"`, `"V5"`, `"V6"`. 그 외 `ValueError`.
**구조 요구:** 트리거 평가와 체결 실행을 분리하라. 매 폴링마다 `reason = _trigger_reason(policy, ...)`로 정책별 트리거를 구한 뒤, 기존 실행 로직(V0 market / V0 limit_fallback / V1 limit_then_market)을 그대로 적용한다. V2~V6은 모두 **V0 실행**(`live_close_path`에 따라 market 또는 limit 폴백)을 쓴다. 기존 T4b1/T4b2 동작은 불변(기존 테스트가 회귀 방어선).

### 3-2. `pt=None` (익절 끄기)
`pt`가 `None`이면 PT 판정을 건너뛴다(**만기까지 보유**). `sl`이 쓰이는 정책(V0, V1, V2)에서는 `sl`이 숫자여야 한다. 어느 정책이든 PT가 꺼진 상태에서 트리거가 없으면 창 끝에서 `None`(만기 정산은 T4d 몫).

### 3-3. 정책별 트리거 (각 폴링, 익절 먼저 → 그 다음 정책의 손절 규칙)
- **V0**: 현행. `pt`는 숫자 또는 `None`.
- **V2 `min_hold_60m`**: V0과 같되, **`entry_ts` 기준 진입 후 60분 미만인 폴링에서는 SL 판정을 건너뛴다**(PT는 항상 판정). `entry_ts` 필수(tz 없는 Timestamp), 없으면 `ValueError`. 60분은 모듈 상수(`MIN_HOLD`, `pd.Timedelta(minutes=60)`). 경계: `bar_time − entry_ts < 60분`이면 건너뜀, `== 60분`이면 판정.
- **V3 `no_stop`**: SL이 없다. PT(있으면) 또는 창 끝/만기까지 보유. `sl` 인자는 무시한다.
- **V4 `underlying_breach`**: 마크 SL을 쓰지 않고, 기초자산 15분봉 종가가 **숏 행사가를 돌파**하면 `stop_loss`: `bull_put`이면 `S < short_strike`, `bear_call`이면 `S > short_strike`(엄격 부등식, 같은 값은 돌파 아님). PT는 V0과 같다(`pt` 숫자 또는 `None`).
- **V5 `account_stop`** (신규, 규칙 A): 마크 손익으로 환산한 **달러 손실**이 계좌 자산의 `stop_account_pct` 이상이면 `stop_loss`. 달러 손익 `= mark_pnl_pct × credit × 100 × qty`(손실이면 음수). 조건: `−(달러 손익) >= stop_account_pct × account_equity`. 인자 `account_equity`(양수), `stop_account_pct`(0~1 사이 양수) 필수. 마크 SL(`sl`)은 쓰지 않는다. PT는 V0과 같다.
- **V6 `underlying_move`** (신규, 규칙 C): 기초자산이 진입 시 가격 `entry_underlying` 대비 **불리한 방향으로 `stop_underlying_pct` 이상** 움직이면 `stop_loss`. 불리한 방향은 `bull_put`이면 하락(`(S0 − S)/S0 >= pct`), `bear_call`이면 상승(`(S − S0)/S0 >= pct`). 인자 `entry_underlying`(양수), `stop_underlying_pct`(0~1 사이 양수) 필수. 마크 SL은 쓰지 않는다. PT는 V0과 같다.
- 필요한 인자가 정책에 없는데 주어져도 무시한다(오류 아님). 필요한 인자가 빠지면 `ValueError`. 시그니처에는 키워드 `entry_ts=None, account_equity=None, stop_account_pct=None, entry_underlying=None, stop_underlying_pct=None`를 추가한다.

### 3-4. 결과 dict
키 불변. `exit_reason`은 `"profit_target"`|`"stop_loss"`. `trigger_mark_pnl_pct`는 트리거 폴링의 마크 손익(V4·V6도 마크 손익을 기록; 레거시 모드에서도 마크를 계산하니 값은 항상 존재).
**레거시 모드(`friction is None`)**: V2, V3, V4, V6은 레거시 마크(mid)로도 동작해야 한다. V5도 마찬가지(마크 = mid). 단 `limit_fallback`/V1은 이전처럼 레거시 금지.

## 4. 필수 테스트 (`tests/test_exit_path.py`에 추가, 합성 데이터)
정책마다 최소 다음을 만든다(합성 경로는 트리거 시각이 서로 달라지게 구성):
1. **V2**: 진입 후 30분 안에 마크 SL 조건이 충족되는 경로 → V2는 건너뛰고 V0은 즉시 청산; 60분이 되는 봉(경계 `==`)에서는 판정; 60분 이후 SL이면 V0과 같은 결과. PT는 60분 이전에도 판정된다.
2. **V3**: 깊은 손실 경로에서도 청산 안 함(`None`), PT가 있으면 PT에서 청산, `pt=None`이면 끝까지 `None`.
3. **V4**: 마크 SL(-100%)에 걸리지 않지만 종가가 숏 행사가를 돌파하는 경로 → 돌파 봉에서 청산; 정확히 행사가와 같은 종가는 돌파 아님; `bull_put`/`bear_call` 두 방향 모두.
4. **V5**: 계좌 대비 손실이 임계에 닿는 봉에서 청산(경계 `>=`), 직전 봉(미달)에서는 청산 안 함. `account_equity`/`stop_account_pct` 누락 → ValueError. 같은 경로에서 마크 SL(-1x)과는 다른 시각에 청산됨을 보여라.
5. **V6**: 불리한 방향 `pct` 이상이면 청산(경계 `>=`), 유리한 방향의 같은 크기 움직임은 청산 안 함, `bull_put`(하락)/`bear_call`(상승) 두 방향. 인자 누락 → ValueError.
6. **`pt=None`**: V0/V4/V5/V6에서 PT 조건이 충족돼도 청산 안 하고 손절 규칙만 작동. `sl`이 필요한 정책에서 `sl=None`이면 ValueError.
7. **트리거/실행 분리 회귀**: V2~V6에 `live_close_path="limit_fallback"`을 주면 트리거 폴링에서 limit 폴백 실행(`n_limit_retries` 집계)이 적용된다(최소 V4 하나와 V5 하나).
8. 정책 이상값 → ValueError. 기존 V0/V1 테스트 전부 그대로 통과.

## 5. 변이(회귀 주입) 검증 — 각각 실제 pytest 실패 출력 원문을 §8에 붙여라
정책별로 **그 규칙이 구현된 모든 코드 위치**를 바꿔라:
(a) V2 최소보유를 60분 → 0분 / `<`를 `<=` (b) V2에서 PT까지 건너뜀 (c) V3에 SL 적용 (d) V4 `<`→`<=`(같은 값을 돌파로) (e) V4 방향 뒤집기(bull_put에 `>`) (f) V5 `>=`→`>` (g) V5 달러 손익에서 `qty` 또는 `100` 누락 (h) V6 방향 뒤집기 (i) V6 분모를 `S` 대신 `S0`가 아닌 값으로 (j) `pt=None`을 0으로 취급 (k) 트리거가 limit 폴백 실행을 우회 (l) 레거시 모드에서 V4/V5/V6이 마크 대신 다른 값 사용. 각 변이에서 실패해야 하는 테스트와 이유 문자열을 함께 적어라.

## 6. Acceptance Rubric (실행 전에 고정)

| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 대부분 맞으나 경계·방향 오류 | V2~V6·pt=None·트리거/실행 분리·T4b 회귀 전부 맞음 |
| 테스트 품질 | 항상 통과 | 일부만 | 8개 테스트가 정책별 경계·방향을 의미 있게 검증, 변이 12건이 원문으로 증명(형제 분기 포함) |
| 범위 준수 | 범위 밖 수정 | 경미 이탈 | 지정 파일 2개만, 미사용 임포트·`src.` 임포트 없음, 커밋 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·감점 사유 먼저·구성 불가 케이스 사유 |

게이트: 모든 차원 ≥ 4.

## 8. Executor response (Codex writes here)

_(비어 있음 — 감점 사유 먼저.)_

## 9. Review log

_(비어 있음 — reviewer가 작성)_
