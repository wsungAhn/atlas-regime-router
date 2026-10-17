# Codex Handoff — T2 half-spread table — 2026-10-17

> **상태(Status):** `대기중 / pending`
> _(Executor: set `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-17T14:30-0700` (executor stamps its own line below when it starts)
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** 레그 반폭 조회 테이블 `src/friction.py` (설계문서 태스크 T2)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` 「설계 2. 마찰 모델」항목 1 (읽어도 되고 안 읽어도 된다 — 필요한 계약은 아래에 모두 있다).
> **범위(IN):** 새 파일 `src/friction.py`, 새 테스트 `tests/test_friction.py`. **범위 밖(OUT):** 다른 `src/` 파일 수정, `scripts/`, launchd, `registry/`, 라이브 트레이딩 진입점 실행, 네트워크 호출, 커밋.

---

## 0. 먼저 읽기 (Executor)

당신은 이 프로젝트의 맥락이 없다. 아래 내용만 믿어라.

- **하지 말 것:** 다른 src 수정 · 주문/시장데이터 API 호출(네트워크 전혀 불필요) · 커밋 · 키 값 출력.
- **항상:** 작업 후 테스트 실행 → 통과 확인 → §8 작성 → 상태줄 `검토대기`로.
- **모르면 추측 금지:** 멈추고 §8에 질문을 남겨라.
- **8번 항목 보고 규칙:** 변이(회귀 주입) 검증은 "확인했다"는 요약이 아니라 **실제 pytest 실패 출력 원문**을 §8에 붙여라.

### 실행 환경
- 워크트리(작업 위치): `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python` (pandas, pyarrow 설치됨)
- 테스트: 워크트리에서 `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q`
- **현재 베이스라인: 168 passed.** 그 아래면 회귀.

## 1. 배경

옵션 백테스트에 호가 마찰을 넣으려면 레그별 반폭 `h = (ask−bid)/2`가 필요하다. Alpaca에 과거 옵션 호가가 없어 `scripts/collect_option_quotes.py`가 2026-10-01부터 호가 패널을 쌓고 있다(`data/quote_panel/YYYY-MM-DD.parquet`, 12개 파일, 컬럼 `ts, underlying, occ, bid, ask, mid, delta, dte, feed`; 입력은 이미 bid>0, ask>0, ask>=bid만 들어 있다). 이 패널에서 `(종목군, mid 구간)`별 반폭 조회표를 만든다.

## 2. Task T2 — `src/friction.py`

### 구현할 것 (공개 함수 3개 + 상수)
1. 상수(모듈 최상단, 매직넘버 금지):
   - `INDEX_ETF = ("SPY","QQQ","IWM","DIA")`
   - `OTHER_ETF = ("GLD","TLT","SLV","XLE","XLF")`
   - 그 외 모든 기초자산은 `single_stock`. `SYMBOL_CLASS(underlying) -> "index_etf"|"other_etf"|"single_stock"` 함수로 제공(위 두 튜플에 없으면 single_stock).
   - mid 구간 경계 `MID_EDGES = (0.10, 0.50, 2.00)`, 구간 이름 `("<0.10","0.10-0.50","0.50-2.00",">2.00")`. 경계값 소속: 하한 포함·상한 미포함이 아니라 아래로 정의한다 — `mid < 0.10` → `<0.10`; `0.10 <= mid < 0.50` → `0.10-0.50`; `0.50 <= mid < 2.00` → `0.50-2.00`; `mid >= 2.00` → `>2.00`.
   - 틱 하한 `TICK_FLOOR = 0.005`; 셀 최소 표본 `MIN_CELL_N = 30`; 패널 DTE 범위 `DTE_MIN = 5`, `DTE_MAX = 9`(양끝 포함).
2. `build_half_spread_table(panel_dir) -> pandas.DataFrame`: `panel_dir`의 `20*.parquet`(날짜 파일만; `positions-*.parquet` 제외)을 모두 읽어 concat. `DTE_MIN <= dte <= DTE_MAX` 행만 사용. 각 행의 `h = (ask-bid)/2`, `rel = h/mid`, class는 `SYMBOL_CLASS(underlying)`, 구간은 mid로. 셀(class × 구간) 값:
   `value = max( median(h), median(rel) * median(mid), TICK_FLOOR )` (셀 내 중앙값들).
   반환 컬럼: `class, bucket, n, h`. n은 셀 행 수.
   **표본 부족 처리:** `n < MIN_CELL_N`인 셀과 행이 아예 없는 셀은 값을 같은 class의 "pooled" 값으로 대체한다. pooled 값 = 그 class의 모든 행에서 `max(median(rel) * (그 셀 구간의 대표 mid), TICK_FLOOR)`, 대표 mid = 구간 `<0.10`→0.05, `0.10-0.50`→0.30, `0.50-2.00`→1.00, `>2.00`→3.00. class 전체가 비어 있으면 모든 class 합친 pooled를 쓰고, 그것도 비면 `TICK_FLOOR`. 대체된 셀은 컬럼 `fallback`(bool)을 True로 표시(반환 컬럼 5개: `class, bucket, n, h, fallback`). 항상 3×4=12행을 반환한다.
3. `half_spread(table, underlying, mid) -> float`: 위 규칙으로 class/구간을 정해 표의 `h`를 반환. `mid`가 NaN이거나 <=0이면 `ValueError`.
4. `write_half_spread_table(panel_dir, out_path)`: `build_half_spread_table` 결과를 CSV로 저장(디렉터리 자동 생성, 원자적 쓰기: 임시파일+`os.replace`). 모듈에 `if __name__ == "__main__":` 진입점을 두어 `python src/friction.py --panel-dir data/quote_panel --out data/friction/half_spread_table.csv` 로 실행되게 한다. 결과 12행과 fallback 행을 `logging.info`로 출력.
5. 외부 호출 없으므로 try/except는 불필요하나, 파일 읽기 실패(손상 parquet)는 해당 파일명을 로깅하고 건너뛴 뒤 계속한다.

### 주의
- 코드 스타일은 `src/friction_stats.py`, `src/friction_backtest.py` 참고(같은 워크트리, 읽기 전용). pandas만 사용, 새 의존성 금지.
- 설계 의도: 이 표는 보정 표본과 독립인 **측정값**이다(적합 파라미터 아님).

### 필수 테스트 `tests/test_friction.py` (네트워크 금지, 합성 parquet을 tmp_path에 생성)
1. `test_half_spread_lookup_floor_and_buckets`: mid=0.0999/0.10/0.4999/0.50/1.9999/2.00에서 구간이 정확하고, 모든 셀 값 ≥ `TICK_FLOOR`이다(틱 하한 케이스: 합성 데이터에서 h·rel이 아주 작은 셀이 0.005로 올라간다).
2. 셀 값 공식: 한 셀에 h 중앙값과 rel×mid 중앙값이 서로 다르도록 합성(예: 큰 쪽이 rel×mid가 되는 경우와 h가 되는 경우를 각각 한 셀씩) → 큰 쪽이 선택된다.
3. DTE 필터: `dte=4`, `dte=10` 행은 결과에 영향이 없고 `dte=5`, `dte=9`는 포함된다.
4. 표본 부족 대체: `n < 30`인 셀은 `fallback=True`이고 값이 pooled 규칙대로이며, `n >= 30`인 셀은 `fallback=False`. 항상 12행.
5. `positions-*.parquet`는 읽지 않는다(그 파일에 의도적으로 이상한 컬럼을 넣어도 에러가 안 난다).
6. `SYMBOL_CLASS` 분류(SPY→index_etf, GLD→other_etf, NVDA→single_stock, 목록에 없는 AAA→single_stock). `half_spread`가 mid NaN/0/음수에서 ValueError.
7. `write_half_spread_table`이 CSV를 쓰고 다시 읽으면 12행·컬럼 5개이다.

### 변이(회귀 주입) 검증 — 직접 수행하고 실제 pytest 실패 출력을 §8에 붙여라
스크래치 복사본(또는 임시 수정 후 복구)에서 아래를 각각 주입해 **해당 테스트가 이유 문자열과 함께 실패**함을 보여라: (a) 틱 하한 제거, (b) 구간 경계 `<`를 `<=`로, (c) 공식에서 `max`를 `min`으로, (d) DTE 필터 제거, (e) `MIN_CELL_N` 대체 로직 제거, (f) `positions-*` 제외 제거.

## 3. 실데이터 실행(필수, 1회)

테스트와 별개로 실제 패널에 대해 실행한다(읽기 전용, 네트워크 불필요):
`/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python src/friction.py --panel-dir data/quote_panel --out /tmp/half_spread_table_t2.csv`
결과 12행 전체를 §8에 표로 붙이고 `fallback=True` 셀이 몇 개인지 적어라. **`data/friction/`에는 쓰지 마라**(리뷰어가 처리). 값이 비상식적이면(음수, NaN, index_etf의 `>2.00` 구간이 single_stock보다 큰 등) 추측으로 고치지 말고 §8에 보고하라.

## 4. Acceptance Rubric (실행 전에 고정)

| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 공식은 맞으나 경계/대체 오류 | 공식·경계·대체·DTE·파일 필터 전부 맞음 |
| 테스트 품질 | 항상 통과하는 테스트 | 일부 규칙만 검증 | 7개 테스트가 모두 의미 있고 변이 6개가 실패 출력으로 증명됨 |
| 범위 준수 | 범위 밖 수정 | 경미한 이탈 | 지정 파일 2개만, 커밋 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문 출력·실행 결과 표·fallback 개수 모두 붙임 |

게이트: 모든 차원 ≥ 4.

## 8. Executor response (Codex writes here)

_(비어 있음 — 수행자가 작성. 감점 사유를 먼저 쓰고 점수는 그 다음.)_

## 9. Review log

_(비어 있음 — reviewer가 작성)_
