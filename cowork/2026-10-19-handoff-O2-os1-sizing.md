# Codex Handoff — O2 OS-1 sizing pure function — 2026-10-19

> **상태(Status):** `대기중 / pending`
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-19T17:00-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** 새 순수 모듈 `src/os1_sizing.py` + `tests/test_os1_sizing.py`. 설계 정본: `docs/design-options-only-selling-2026-10-19.md` §3-1, §3-2 (먼저 읽어라; 이 파일과 충돌하면 이 파일의 시그니처가 우선하되 충돌을 §8에 적어라).
> **범위(IN):** 위 파일 2개. **OUT:** 다른 파일 수정(특히 `src/mcp_runner.py`, `src/signals.py`는 건드리지 않는다), 네트워크, 파일/상태 읽기·쓰기, 커밋.

## 0. 규칙
- 순수 함수만(I/O 없음). `src.` 임포트 금지; 이 모듈은 다른 모듈을 임포트할 필요 없음.
- 테스트는 **리터럴 값**으로 단언한다(구현 상수 임포트·자기비교 금지). 미사용 임포트 금지. 커밋 금지.
- 테스트 실행 후 이 파일 `## 8`에 **직접** 작성(감점 사유 먼저, 변이별 pytest 실패 원문; 채팅 출력 금지) → 상태 `검토대기`. 변이는 같은 규칙이 구현된 **모든 위치(형제 분기)**에 각각 주입.
- 환경: 워크트리 `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`, 인터프리터 `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`, 테스트 `… -m pytest tests/ -q`, **베이스라인 332 passed**(O2k가 병렬 작업 중이면 더 많을 수 있음; 하회하면 회귀).

## 1. 정의 (설계 §3-2)
- 단위 위험: `L1 = (W_used − credit) × 100` (계약 1개 최대손실, 페이오프 기준, 수수료 제외).
  - 단일 수직(`bull_put`, `bear_call`): `W_used = width`(float).
  - 콘도르(`condor`): `width = (put_w, call_w)` 튜플. `verified_structure=True`(공통 만기 + 행사가 비중첩 확인됨)면 `W_used = max(put_w, call_w)`, 아니면(기본값 False) 보수식 `W_used = put_w + call_w`.
- `E_cap = min(e0, e_now)`.
- 캡: 포지션당 `0.02·E_cap`, 총 `0.10·E_cap − ΣL_open`, 방향 `0.06·E_cap − ΣL_dir`.
- 동시 포지션 최대 `max_open_positions`(기본 5).

## 2. 시그니처 / 계약
```
size_os1(e0, e_now, width, credit, side, open_positions, max_open_positions=5, verified_structure=False) -> (qty:int, reason:str)
```
- `open_positions`: dict 리스트 `{"L": float, "side": "bull_put"|"bear_call"|"condor"}`. 체결 포지션과 **제출됐지만 미체결인 진입 주문(주문 qty 전체의 L로 예약)** 을 모두 담아 호출자가 넘긴다(함수는 구분하지 않음).
- `side` ∈ `{"bull_put","bear_call","condor"}`, 그 외 `ValueError`.
- `ΣL_dir`: 신규가 `bull_put`이면 기존 중 side가 `bull_put` 또는 `condor`인 L의 합; `bear_call`이면 `bear_call` 또는 `condor`; 신규가 `condor`면 위 두 방향을 모두 계산해 **남은 방향 한도의 최솟값**을 쓴다(콘도르는 양쪽 모두에 L 가산).
- 검증/거부 순서(앞 조건이 먼저): 
  1. `e0` 이 None/NaN/≤0 이거나 `e_now` ≤0 → `ValueError`.
  2. `credit ≤ 0` 또는 `credit ≥ max(폭들)` → `(0, "bad_credit")`.
  3. `credit / max(폭들) < 0.10` → `(0, "low_credit_ratio")` (경계: 정확히 0.10은 통과; 비교는 `credit / width >= 0.10`로 구현; 폭이 둘인 콘도르는 **큰 폭** 기준).
  4. `len(open_positions) >= max_open_positions` → `(0, "count_cap")`.
  5. `qty = floor(min(0.02·E_cap, 0.10·E_cap − ΣL_open, dir_remaining) / L1)`; `qty < 1`(음수 잔여 포함) → `(0, "risk_cap")`. 성공은 `(qty, "ok")`.
- 추가 순수 함수 `check_quote_credit(raw_net_credit, leg_quotes) -> None | "bad_credit"`: `leg_quotes`는 `[(bid, ask), ...]`. 어느 레그든 `bid ≤ 0` 또는 `ask ≤ 0`이면 `"bad_credit"`; `raw_net_credit × (1 − 0.05) ≤ 0.01`(0.01 하한이 값을 끌어올리는 경우)이면 `"bad_credit"`; 그 외 `None`.
- 모듈 상단에 이름 있는 상수(2%, 10%, 6%, 0.10, 0.05, 0.01, 5)를 두되 테스트는 리터럴로 단언.

## 3. 필수 테스트 (합성)
1. 2% 캡이 걸리는 경우 / 10% 총 캡이 걸리는 경우 / 6% 방향 캡이 걸리는 경우 각각, 손계산 qty 리터럴(예: e0=e_now=50000, width=1.5, credit=0.30 → L1=120 → 2% 캡 1000 → qty 8).
2. 콘도르 verified=True는 큰 폭 기준, verified=False(기본)는 `put_w+call_w` 기준(같은 입력에서 qty가 달라짐을 리터럴로); 콘도르 L은 방향 한도에서 양쪽에 모두 가산되어 한 방향에 이미 5%가 있으면 콘도르 신규의 남은 한도는 그 방향 1%.
3. `bad_credit`: credit==width, credit==0, credit==−0.05 각각. `check_quote_credit`: 한쪽 호가 레그(bid=0, ask=0 각각), raw_net_credit=0.0105(→0.009975≤0.01)는 `"bad_credit"`, 정상값 `None`.
4. 비율: c/w=0.10 통과(`"ok"` 이상), 0.10−1e-9 탈락(`low_credit_ratio`) — 구현이 부동소수점으로 실패하지 않는 입력을 고르고, 안 되면 §8에 적어라.
5. count: 소형 L 포지션 5개가 열려 있으면 위험 여유가 있어도 6번째는 `count_cap`; 4개면 통과. `max_open_positions` 인자 변경 반영.
6. `E_cap`: `e_now=47500, e0=50000`이면 47,500 기준; `e_now=52000`이면 50,000 기준(qty 리터럴로 구분).
7. 예약: 체결 8%·E 상당 L + 미체결 주문 2% 상당 L이 `open_positions`에 있으면 신규 2% 주문은 `risk_cap`(총 10% 경계); 음수 잔여(이미 캡 초과)도 `risk_cap`.
8. `e0=None`, `e0=0`, `e0=-1`, `e_now=0` → `ValueError`; 잘못된 `side` → `ValueError`.
9. 거부 순서: bad_credit 입력이면서 count 초과인 경우 `bad_credit`이 우선(리터럴).

## 4. 변이 검증 (각각 pytest 실패 원문을 §8에)
(a) `>=`→`>`(비율/ count 각각) (b) 콘도르 한쪽만 방향 가산 (c) count 검사 제거 (d) `E_cap`을 `max`로 (e) 콘도르 verified=False에도 max 식 (f) `credit < width` 검사 제거 (g) 방향 캡에서 condor 기존 포지션 누락 (h) ΣL_open 계산에서 미체결(리스트 후반) 제외 (i) 거부 순서 뒤바꿈 (j) `floor`→`round` (k) `check_quote_credit`의 `≤`→`<`(두 위치 각각).

## 6. Rubric (게이트: 모두 ≥ 4)
| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 대부분 맞음 | §1·§2 전 규칙·거부 순서 정확 |
| 테스트 품질 | 항상 통과 | 일부 | 리터럴 단언, 형제 분기 전부, 변이 원문 증명 |
| 범위 준수 | 범위 밖 수정 | 경미 | 파일 2개만, 미사용 임포트 없음, 커밋 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·감점 사유 먼저·모호점 명시 |

## 8. Executor response (Codex writes here)

(비어 있음)

## 9. Reviewer log (Claude writes here)

(비어 있음)
