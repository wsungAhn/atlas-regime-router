# Codex Handoff — T1 option quote panel collector — 2026-09-30

> **상태(Status):** `대기중 / pending`
> _(Executor: set `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** _(비워둠 — 착수 주체가 `session=<id> machine=<tag> started=<ISO8601>` 기록)_
>
> **작성자:** Claude Sonnet 5.5 (reviewer) → **수행자:** Codex gpt-reserve (effort low)
> **작업명:** 옵션 호가 패널 수집기 `scripts/collect_option_quotes.py` (설계문서 태스크 T1)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` (T1 행, 설계 2의 h 테이블 입력). 읽지 않아도 되지만 배경이 궁금하면 참고.
> **범위(IN):** 새 파일 `scripts/collect_option_quotes.py`, 새 테스트 `tests/test_collect_option_quotes.py`. **범위 밖(OUT):** `src/` 전부(읽기만), launchd/스케줄링, `registry/`, 라이브 트레이딩 진입점 실행, 커밋.

---

## 0. 먼저 읽기 (Executor)

당신은 이 프로젝트의 맥락이 없다. 아래 내용만 믿어라.

- **하지 말 것:** src/ 수정 · `src/mcp_runner.py` 실행 · 주문 API 호출(읽기 전용 시장데이터·포지션 조회만) · 커밋 · 스케줄 등록 · 키 값을 로그·출력·테스트에 기록.
- **항상:** 작업 후 테스트 실행 → 통과 확인 → §8 작성 → 상태줄 `검토대기`로.
- **모르면 추측 금지:** 멈추고 §8에 질문을 남겨라.

### 실행 환경
- 워크트리(작업 위치): `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python` (워크트리엔 .venv 없음; 이 경로 사용, pyarrow·pandas 설치됨)
- 테스트: 워크트리에서 `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q`
- **현재 베이스라인: 141 passed.** 그 아래로 떨어지면 회귀.
- 키 파일: `/Users/Mung/dev/atlas-options-hackathon/.env.competition` (KEY=VALUE 줄, 변수명 `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`). 워크트리엔 없다. `--env-file` 인자로 받고 기본값은 이 절대경로. 값은 절대 출력하지 마라.

## 1. 배경

라이브 옵션 전략이 백테스트와 달리 크게 손실을 냈다(호가 스프레드+시장가 청산 슬리피지 가설). 백테스트에 마찰을 넣으려면 종목별 상대 호가폭(half-spread)이 필요한데 Alpaca는 **과거 옵션 호가를 제공하지 않는다**. 그래서 지금부터 라이브 호가를 패널로 모은다. 이 수집기는 1회 실행하면 호가 스냅샷을 parquet에 추가하고, 사람이 장중 하루 여러 번 수동 실행한다(스케줄링 안 함).

## 2. Task T1 — `scripts/collect_option_quotes.py`

### 기존 코드 (읽기 전용, 이 좌표 그대로)
- `src/signals.py:80-` `OPTION_SYMBOLS`: 수집 대상 기초자산 튜플(23종목). `from signals import OPTION_SYMBOLS, fetch_chain` (scripts에서 `sys.path`에 `<repo>/src` 추가 필요; 기존 `src/` 모듈들이 `from signals import ...`처럼 import하는 방식을 `tests/test_signals.py` 상단에서 확인하라).
- `src/signals.py:394-404` `fetch_chain(data_client, symbol, contract_type, dte_min, dte_max) -> dict`: `{occ_symbol: snapshot}` 반환. snapshot 속성: `latest_quote.bid_price`, `latest_quote.ask_price`, `greeks.delta`(없을 수 있음). `ContractType.PUT`/`CALL`은 `alpaca.trading.enums`.
- `src/mcp_runner.py:129-149` `_load_env_file` 패턴과 `:433-435` `OptionHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])` 생성 방식을 참고(복붙 말고 스크립트에 작은 로더를 새로 써라; mcp_runner를 import하면 부작용이 있으니 **import 금지**).
- `src/signals.py:257-266` `_occ_strike`, `:589-` `_occ_expiration`: 심볼 파싱 재사용 가능(`dte` 계산에 만기 필요).

### 구현할 것
1. `collect_once(option_client, symbols, now, panel_dir, feed=None)`(순수하게 테스트 가능한 함수): 종목별로 PUT/CALL 체인을 `fetch_chain`으로 받아(기본 DTE 범위 그대로) 행을 만든다. 행 스키마(고정): `ts`(UTC ISO8601 문자열), `underlying`, `occ`, `bid`, `ask`, `mid`, `delta`(없으면 null), `dte`(만기일-오늘 정수), `feed`(문자열, 기본 `"default"`).
2. **제외 규칙:** bid<=0 또는 ask<=0 또는 ask<bid 인 행은 저장하지 않는다(스프레드 통계를 왜곡). 제외 건수는 종목별로 로그.
3. **오류 처리:** 종목별 `try/except`로 예외를 로깅(`logging`, 종목명 포함)하고 **나머지 종목은 계속**한다. 한 종목이 전부 실패해도 스크립트는 죽지 않는다. 끝에 성공/실패 종목 수 요약 로그.
4. **저장:** `data/quote_panel/YYYY-MM-DD.parquet`(UTC 날짜)에 **추가(append)** — 기존 파일이 있으면 읽어서 concat 후 다시 쓴다. 쓰기는 임시파일+`os.replace`로 원자적으로.
5. **열린 포지션(선택 기능, best-effort):** `alpaca.trading.client.TradingClient(key, secret, paper=True).get_all_positions()`로 각 포지션의 `symbol`, `current_price`를 받아 같은 시각의 패널 `position_marks` 별도 파일 `data/quote_panel/positions-YYYY-MM-DD.parquet`(`ts, occ, current_price, qty`)에 추가. **이 호출이 실패(401 포함)해도 전체가 실패하면 안 된다** — 경고 로그만 남기고 계속. 주문 관련 메서드는 절대 호출 금지.
6. CLI: `python scripts/collect_option_quotes.py [--env-file PATH] [--panel-dir PATH] [--symbols A,B,...]` — 기본 패널 디렉터리는 스크립트 위치 기준 `<repo>/data/quote_panel`(워크트리 안). `.gitignore`에 `data/quote_panel/` 추가는 **하지 마라**(리뷰어가 처리).
7. 스케줄링·루프 없음. 1회 실행하고 종료.

### 주의·제약
- 시장 휴장이면 호가가 비어 있을 수 있다 — 행 0개여도 정상 종료(빈 파일을 만들지 말 것, 로그 한 줄).
- 마법 숫자 금지: 스키마 컬럼 목록은 모듈 상수 하나로.
- `src/` 파일은 수정하지 마라.

### 필수 테스트 `tests/test_collect_option_quotes.py` (실제 네트워크 호출 금지, 가짜 클라이언트/체인 사용)
1. 스키마 고정: 출력 parquet 컬럼이 정확히 `ts, underlying, occ, bid, ask, mid, delta, dte, feed`이고 `mid == (bid+ask)/2`.
2. 제외 규칙: bid=0, ask=0, ask<bid 행이 각각 제외되고 정상 행만 남는다. (세 경우 모두 별도 케이스로, 경계 bid=ask>0 는 **포함**됨도 확인.)
3. 종목 예외 격리: 가짜 `fetch_chain`이 종목 B에서 예외를 던져도 A·C 행은 저장되고 예외가 전파되지 않는다.
4. 추가 저장: 같은 날짜 파일에 두 번 `collect_once`를 호출하면 행이 누적된다(덮어쓰기 아님).
5. 포지션 조회 실패 격리: 포지션 함수가 예외를 던져도 호가 패널은 저장된다.
6. 빈 체인: 모든 종목이 빈 체인이면 파일이 생성되지 않는다.
각 테스트는 **구현을 의도적으로 깨뜨리면 실패해야 한다**(예: 제외 조건 `<=`를 `<`로 바꾸면 #2가 실패). 리뷰어가 그렇게 주입해 검증한다.

## 5. 코딩 원칙
- 코드 식별자·주석·커밋 메시지·문서는 영어, 기존 파일 스타일(type hints, 모듈 docstring)을 따른다. 외부 호출은 모두 try/except+로깅(Explicit Error Handling). 새 추상화·설정값을 추측으로 만들지 마라(Simplicity First). 완료 선언 전 실제로 테스트를 실행하라(Verification Before Done).

## 6. 완료 기준
- [ ] `scripts/collect_option_quotes.py` 구현
- [ ] `tests/test_collect_option_quotes.py` 6개 통과
- [ ] 전체 스위트 ≥141 passed
- [ ] (장중이라면) `--symbols SPY,QQQ --panel-dir /tmp/qp-test`로 실제 1회 실행해 출력 요약(행 수, 컬럼) §8에 붙여넣기; 장 휴장/실패면 사유 기록. **`--panel-dir`을 /tmp로 줘서 워크트리를 오염시키지 마라.**
- [ ] 자기채점표 §8-7 (감점 사유 먼저)
- [ ] 커밋 안 함

### Acceptance rubric (실행 전 고정)
| 차원 | 5점 기준 | 게이트 |
|---|---|---|
| 정확성 | 스키마·제외규칙·원자적 append가 명세대로, 회귀 주입 시 테스트가 실패 | 4+ |
| 오류 격리 | 종목/포지션 실패가 전체를 죽이지 않고 로그에 종목명이 남음 | 4+ |
| 범위 준수 | src/ 무수정, 주문 호출 없음, 키 미출력, 커밋 없음 | 5 |

---

## 7. 보고 항목 (§8에 작성)
1. 변경 파일+한 줄 요약 2. 새 테스트 파일·개수 3. 전체 테스트 결과 4. 실제 1회 실행 결과(6번 기준) 5. 막힌 점/판단한 점 6. 루브릭 자기채점(감점 사유 먼저)

## 8. Executor response (Codex writes here)
_(write here)_

## 9. Reviewer log (reviewer writes here)
_(reviewer writes here)_
