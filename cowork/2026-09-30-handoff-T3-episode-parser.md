# Codex Handoff — T3 live episode parser — 2026-09-30

> **상태(Status):** `검토대기 / review-pending`
> _(Executor: `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** `session=codex machine=friction-backtest started=2026-09-30T12:12:28-07:00`
>
> **작성자:** Claude Sonnet 5.5 (reviewer) → **수행자:** Codex gpt-reserve (effort medium)
> **작업명:** 라이브 `decisions.jsonl` → 옵션 포지션 에피소드 파서 (설계문서 태스크 T3)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` 배경(L7-56)과 설계 1의 "T3 파서 규칙"(L136-143), 테스트 #6(L349). 규칙은 아래에 그대로 옮겨 놓았으니 문서를 읽지 않아도 된다.
> **범위(IN):** 새 파일 `src/friction_backtest.py`(이 태스크에선 `load_live_episodes`와 그 헬퍼만), 새 테스트 `tests/test_friction_backtest_parser.py`. **OUT:** 다른 `src/` 파일 수정, 라이브 registry 파일 쓰기, 커밋.

---

## 0. 먼저 읽기 (Executor)
- **하지 말 것:** 기존 `src/` 파일 수정 · `registry/` 쓰기(읽기만; 테스트는 tmp 경로의 합성 fixture만) · 라이브 진입점 실행 · 커밋.
- **항상:** 작업 후 테스트 실행 → 통과 확인 → §8 작성 → 상태줄 `검토대기`. 모르면 추측 말고 §8에 질문.

### 실행 환경
- 워크트리(작업 위치): `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`
- 테스트: 워크트리에서 `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q`
- **현재 베이스라인: 148 passed.** 그 아래로 떨어지면 회귀.
- 실제 로그 파일(읽기 전용 참고): `/Users/Mung/dev/atlas-options-hackathon/registry/decisions.jsonl` (JSON Lines, 약 2만 줄, 계속 자란다).

## 1. 배경
라이브 옵션 전략이 백테스트와 달리 큰 손실을 냈다. 원인을 가리기 위해 백테스트에 마찰을 넣고 **라이브 거래와 같은 통계가 나오는지 보정**하려 한다. 그 보정의 입력이 이 파서다: 라이브 `decisions.jsonl`의 진입·청산 레코드를 **포지션 단위 에피소드**로 묶는다. 같은 포지션에 청산 레코드가 여러 개 찍히는 재시도가 많아서(브로커가 시장가를 거부해 limit으로 재시도) 단순 레코드 집계는 틀린다.

## 2. Task T3 — `src/friction_backtest.py::load_live_episodes`

### 레코드 스키마 (실측, `registry/decisions.jsonl`)
모든 줄: `symbol`, `ts`(UTC ISO8601, 예 `2026-09-30T17:15:09.249042+00:00`), `submitted`(bool).
- **옵션 진입**: `action` 키 없음, `submitted: true`, `order_intent`(`order_class:"mleg"`, `limit_price`(음수 문자열=크레딧), `qty`(문자열), `client_order_id`, `legs:[{symbol(OCC), side, position_intent, ratio_qty}]`), `order_result`, `regime`, `macro_reason`.
  - 브로커가 거부했는데도 `submitted:true`로 찍힌 진입이 있다: 그 경우 `order_result`에 **`"error"` 키**가 있다(`src/mcp_runner.py:188-204` `_order_error`의 설명 참고). 이런 진입은 제외한다.
- **시장가 청산(clean 후보)**: `action:"close"`, `submitted:true`, `exit_reason`(`profit_target`|`stop_loss`|`max_hold_days`), `profit_pct`(float), `order_intent`(`type:"market"`, `legs`), `order_result`.
- **limit 폴백 청산**: `action:"close"`, `submitted:true`, `exit_reason`, `limit_fallback_price`(float), **`profit_pct` 없음**(`src/mcp_runner.py:523-527`). 최근 것은 `order_intent`/`order_result`가 있고, **구 버전 로그 66건은 `order_intent`(레그)가 없다**.
- **크립토**: `sleeve:"crypto"`가 붙은 레코드(진입·청산 모두) → 전부 제외.
- 그 외 `submitted:false` 스킵 레코드(`skip_reason`…)는 에피소드에 영향 없음: 무시.

### 구현할 규칙 (설계 L136-143, 번호 그대로)
1. `sleeve=="crypto"` 레코드는 제외한다(`max_hold_days` 크립토 3건 포함).
2. **진입**: `submitted`가 참, `order_intent` 있음, `order_result`에 `error` 없음, `action!="close"`, 옵션(= `sleeve` 없음).
3. **에피소드**: 같은 `symbol`에서 한 진입부터 **다음 진입 직전까지의** `action=="close" and submitted` 레코드들(시각 오름차순). 청산 레코드와 진입 레코드 **둘 다에 레그 목록이 있으면 레그 OCC 심볼 집합이 같아야** 한다; 다르면 그 청산 레코드는 `leg_mismatch`로 제외(에피소드엔 안 넣고 제외 카운트만 +1). 레그가 없는 구 버전 limit 레코드는 **심볼만으로 묶고** 에피소드에 `legs_unverified=True`를 붙인다.
4. **분류(`kind`)**: `clean` = 청산 1회이고 그 레코드에 `profit_pct`가 있음. `single_limit` = 청산 1회인데 limit 폴백(`profit_pct` 없음). `retry` = 청산 2회 이상; 그 청산들의 `exit_reason`이 서로 다르면 `reason_flip=True`도 붙인다. (청산 0회 에피소드는 "아직 열림"으로 별도 집계하고 `episodes`에는 넣지 않는다.)
5. **청산 이벤트 = 첫 청산 레코드**: `exit_reason`, `exit_ts`, `minutes_held`(= exit_ts − 진입 ts, 분)가 여기서 나온다. `trigger_profit_pct`는 **첫 레코드에 `profit_pct`가 있을 때만** 채운다(없으면 `None`). `live_close_path` = 첫 레코드에 `limit_fallback_price`가 있으면 `"limit_fallback"`, 아니면 `"market"`.
6. 보정 표본은 `clean`만이다 — 이 태스크는 **분류까지만** 한다(표본 선택은 호출부 몫): `episodes`에 전부 담고 각각 `kind`를 붙인다.
7. **종목별 limit 폴백 비율**도 산출한다: 종목마다 `(첫 청산이 limit_fallback인 에피소드 수) / (청산이 있는 에피소드 수)`를 별도 함수 `limit_fallback_rate_by_symbol(episodes) -> dict[str, float]`로 제공(분모 0인 종목은 포함하지 않는다). 또한 전체 풀링 비율 `pooled_limit_fallback_rate(episodes) -> float`(청산 있는 에피소드가 0이면 0.0).

### 반환 계약
```python
def load_live_episodes(path: str | Path) -> tuple[list[dict], dict[str, int]]:
    """episodes: 각 dict는 키 ≥
       symbol, entry_ts, regime, legs(list[str] OCC), n_legs(int), qty(int),
       limit_credit(float, 양수 = abs(limit_price)), kind, reason_flip(bool),
       legs_unverified(bool), exit_reason, exit_ts, minutes_held(float),
       trigger_profit_pct(float|None), live_close_path, n_close_records(int)
       exclusion_counts: 최소 키 crypto, entry_with_error, leg_mismatch, open_no_close"""
```
- `entry_ts`/`exit_ts`는 같은 형식의 tz-aware `datetime` 또는 ISO 문자열 중 하나로 **일관되게** 쓴다(선택해서 §8에 적어라).
- 잘못된 JSON 줄은 건너뛰고 카운트(`malformed_lines`)한다(예외로 죽지 않는다). 파일이 없으면 명확한 예외.
- 마법 숫자 금지, 새 추상화 금지(Simplicity First). 모듈 docstring은 영어.

### 주의
- 포지션 진행 중에는 같은 심볼에 새 진입이 나가지 않는다(라이브 가드, `src/mcp_runner.py:561-575`) — 그러니 에피소드는 "진입부터 다음 진입 직전까지"로 자르면 된다.
- `ts`는 문자열이다. 정렬은 파싱한 시각 기준으로 하고, 파일 순서가 이미 시간순이라고 가정하지 마라.

### 필수 테스트 `tests/test_friction_backtest_parser.py` (합성 JSONL fixture만, 각 케이스를 **독립** 테스트로)
1. 브로커 거부(`order_result.error`) 진입은 제외되고 `entry_with_error` 카운트에 잡힌다.
2. 크립토(`sleeve:"crypto"`) 진입·청산(`max_hold_days` 포함)은 모두 제외, `crypto` 카운트.
3. 레그 불일치 청산은 에피소드에 안 들어가고 `leg_mismatch`에 잡힌다.
4. 레그 없는 구 limit 청산은 심볼로 묶이고 `legs_unverified=True`.
5. 청산 1회 + `profit_pct` → `clean`; 청산 1회 limit 폴백 → `single_limit`.
6. 청산 20회 재시도(사유가 PT/SL 교대) → `retry` + `reason_flip=True`; **첫 레코드가 이벤트**(`exit_reason`·`exit_ts`·`minutes_held`가 첫 레코드 기준)이고 첫 레코드가 limit 폴백이면 `trigger_profit_pct is None`; 첫 레코드에 `profit_pct`가 있으면 그 값.
7. 같은 심볼에 진입 두 번(사이에 청산 1회): 에피소드 2개로 갈리고 청산이 올바른 쪽에 붙는다. 시각이 **뒤섞인** 입력 순서에서도 같은 결과.
8. 청산 없는 진입은 `open_no_close`로 집계되고 `episodes`에 없다.
9. `limit_fallback_rate_by_symbol`/`pooled_limit_fallback_rate`: 손으로 계산 가능한 작은 fixture(예: A 종목 에피소드 4개 중 limit 폴백 1 → 0.25)에서 정확한 값, 분모 0 종목은 키 없음, 전체 0이면 0.0.
10. 잘못된 JSON 줄이 섞여도 죽지 않고 `malformed_lines` 집계.
각 테스트는 **구현을 의도적으로 깨뜨리면 실패해야 한다**(리뷰어가 scratch 사본에 회귀를 주입해 검증한다: 크립토 필터 제거, 마지막 레코드를 이벤트로 사용, `error` 체크 제거, 레그 비교 제거, 정렬 제거 등).

### 실제 로그 확인(보고에 붙일 것)
`load_live_episodes("/Users/Mung/dev/atlas-options-hackathon/registry/decisions.jsonl")`을 읽기 전용으로 1회 실행해 **에피소드 수, kind별 개수, `reason_flip` 개수, 4레그(`n_legs==4`) clean 개수, exclusion_counts**를 §8에 붙여라. 참고로 오늘 오전 분석의 기대치: 청산 있는 에피소드 약 112(clean 96 / single_limit 5 / retry 11, reason_flip 5), clean 중 4레그 29. 로그가 계속 자라므로 **정확히 같지 않아도 된다**(clean ≥ 96 근처); 크게 벗어나면 이유를 §8에 적어라.

## 5. 코딩 원칙
- 식별자·주석·문서는 영어, type hints, 외부 입력(파일)은 try/except+`logging`(Explicit Error Handling), 새 추상화·설정값 금지, 완료 선언 전 실제로 테스트 실행(Verification Before Done).

## 6. 완료 기준
- [ ] `src/friction_backtest.py`(`load_live_episodes`, `limit_fallback_rate_by_symbol`, `pooled_limit_fallback_rate`)
- [ ] `tests/test_friction_backtest_parser.py` 10개 이상 통과
- [ ] 전체 스위트 ≥148 passed
- [ ] 실제 로그 1회 실행 결과 §8에 붙임
- [ ] 자기채점표 §8-6 (감점 사유 먼저)
- [ ] 커밋 안 함

### Acceptance rubric (실행 전 고정)
| 차원 | 5점 기준 | 게이트 |
|---|---|---|
| 규칙 정확성 | 규칙 1~7이 명세대로, 회귀 주입(첫 레코드→마지막 레코드, 크립토 필터 제거, error 체크 제거, 레그 비교 제거, 정렬 제거) 시 해당 테스트가 실패 | 4+ |
| 실제 로그 정합 | 실제 로그 결과가 기대치 근처이고 벗어나면 원인 설명 | 4+ |
| 범위 준수 | 기존 src 무수정, registry 무쓰기, 커밋 없음 | 5 |

---

## 7. 보고 항목 (§8에 작성)
1. 변경 파일+한 줄 요약 2. 새 테스트 파일·개수 3. 전체 테스트 결과 4. 실제 로그 1회 실행 결과 5. 판단한 점(시각 표현 선택 등)/막힌 점 6. 루브릭 자기채점(감점 사유 먼저)

## 8. Executor response (Codex writes here)
1. 변경 파일+한 줄 요약
   - `src/friction_backtest.py`: live `decisions.jsonl`을 시각순으로 option position episode로 묶고, close 분류·제외 카운트·limit fallback 비율을 제공.
   - `tests/test_friction_backtest_parser.py`: 명세의 독립 parser fixture 테스트 11개 추가.

2. 새 테스트 파일·개수
   - `tests/test_friction_backtest_parser.py`, 11 tests.

3. 전체 테스트 결과
   - `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q`
   - `159 passed, 1 warning` (기준 148 passed 이상).

4. 실제 로그 1회 실행 결과
   - 대상: `/Users/Mung/dev/atlas-options-hackathon/registry/decisions.jsonl` (read-only)
   - episodes: `112`
   - kind별: `clean=96`, `single_limit=5`, `retry=11`
   - `reason_flip`: `5`
   - 4-leg clean: `29`
   - exclusion_counts: `{"entry_with_error": 1, "open_no_close": 394, "crypto": 9022, "leg_mismatch": 1}`

5. 판단한 점(시각 표현 선택 등)/막힌 점
   - `entry_ts`/`exit_ts`는 timezone-aware `datetime`으로 일관되게 반환한다.
   - 입력 전체를 parsed timestamp 기준 정렬하고, 심볼별 다음 진입을 episode 경계로 사용한다. 첫 유효 close를 exit event로 사용한다.
   - 막힌 점 없음. 기존 `src/` 파일과 `registry/`는 수정하지 않았고 커밋하지 않았다.

6. 루브릭 자기채점(감점 사유 먼저)
   - 감점 사유: 없음.
   - 규칙 정확성: `5/5` — 규칙 1~7과 회귀 방지 테스트를 구현했고 11개 parser test 및 전체 suite 통과.
   - 실제 로그 정합: `5/5` — 기대치 `112 / 96 / 5 / 11 / 5 / 29`와 정확히 일치.
   - 범위 준수: `5/5` — 지정된 새 `src`/`tests` 파일만 추가, registry read-only 실행, 기존 `src` 미수정, no commit.

## 9. Reviewer log (reviewer writes here)

### 라운드 1
**Verdict:** approved
검증: 전체 스위트 159 passed 직접 재실행 · 코드 전문 읽음(규칙 1~7 일치) · scratch 사본에 회귀 주입 9종(크립토 필터 제거, 마지막 레코드를 이벤트로, error 체크 제거, 레그 비교 제거, 정렬 제거, single_limit→clean, reason_flip 끔, open_no_close 제거, 비율 분모 조건 제거) → **전부 해당 테스트가 실패로 포착**. 실제 로그를 독립 스크립트로 재실행: 에피소드 112 = clean 96 / single_limit 5 / retry 11, reason_flip 5, clean 4레그 29, exclusion {entry_with_error 1, open_no_close 394, crypto 9022, leg_mismatch 1}; clean 중 SL 65·PT 31, 조기(<60분) SL 23·PT 0 — 설계 배경 수치와 정확히 일치. 종목별 limit 폴백 비율 TLT 0.80, DIA 0.67, SLV 0.25, IWM 0.22, XLE 0.22; 풀링 0.143(설계 4-7 폴백 기본값 16/112=0.143과 일치).
**경미한 지적(수용):** 레그 목록이 깨진 진입은 카운트 없이 조용히 스킵됨(`_legs is None`) — 현재 로그엔 해당 없음. `open_no_close` 394는 미체결·취소된 진입 지정가가 다수 포함된 것으로 보이며(미검증) 에피소드 표본에는 영향 없음.
**잘한 점:** 입력 순서와 무관한 정렬, 미체결/거부 진입이 앞 에피소드를 끊지 않게 처리, 잘못된 JSON 줄 격리.

