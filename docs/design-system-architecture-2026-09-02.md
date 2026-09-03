# design-system-architecture

**상태**: 사후 기록(retroactive) — 설계가 구현보다 나중에 나왔다. 이 레포는
대회 준비 기간(2026-08-24 시작, 마감 2026-09-04) 동안 절차(Design-First)를
계속 생략하고 실거래 압박 속에서 기능을 얹어왔다 — 이 문서는 그 결과물을
사후에 한 번 통으로 정리한 것이다. "없는 것보다는 낫다"는 게 이 문서의
존재 이유이지, 이게 사전설계를 생략해도 된다는 선례가 되면 안 된다.

- 작성: 2026-09-02 PDT
- 왜 지금 쓰나: 전체 코드베이스 대상 Codex 적대감사(§4)를 돌리기 직전,
  "이 시스템이 뭘 하려는 거였는지" 한 곳에 있어야 감사도 더 정확해지고,
  감사가 놓친 것(배포 토폴로지)도 여기 문서화해두면 다음에 또 놓치지
  않는다.

## 1. 무엇을 하는 시스템인가

`$100,000` Alpaca 페이퍼 계좌 하나로 **옵션(6~9종목) + 크립토 현물(BTC/ETH)
두 슬리브를 동시에 자동매매**하는 lablab.ai×Alpaca 해커톤 제출작. 사람
개입 없이 15분마다 레짐을 판정하고 주문을 내는 게 핵심 서사(README/
one-page-submission.md 참고) — "정의된 리스크, 완전자동, 백테스트로
검증된 전략 선택 과정"이 셀링포인트다.

## 2. 배포 토폴로지 — 여기가 이번에 놓쳤던 부분

**이 레포는 하나의 프로세스가 아니라 macOS launchd로 등록된 4개의 독립
프로세스다.** 서로 공유 메모리도, 락도, IPC도 없고, `registry/` 아래
JSON 파일로만 상태를 주고받는다. 코드 diff 리뷰만 해서는 이 상호작용이
안 보인다 — 각 잡이 언제 도는지 `.plist`를 같이 봐야 한다.

| 잡 | 스크립트 | 스케줄 | 비고 |
|---|---|---|---|
| `com.atlas.options-runner` | `mcp_runner.py`(인자 없음) → `run_cycle_once()` | `StartCalendarInterval`, 매일 06:30~13:00 PDT(미국장 시간), 정각/15/30/45분 | 시장 열렸을 때만 |
| `com.atlas.crypto-runner` | `mcp_runner.py crypto` → `run_crypto_cycle_once()` | `StartCalendarInterval`, **하루 종일** 00:00~23:45, 정각/15/30/45분 | 24/7 |
| `com.atlas.health-check` | `health_check.py` | `StartInterval=900`(로드 시점부터 900초 간격 — **캘린더 정렬 아님**, 다른 두 잡과 시각이 계속 어긋난다) | 리컨실러+워치독 |
| `com.atlas.report-generator` | `generate_report.py` | 매일 13:10 PDT 1회 | 일별 리포트 생성 |

**핵심 함의**: 미국장 시간(06:30~13:00)에는 **options-runner와
crypto-runner가 같은 분(:00/:15/:30/:45)에 동시에 실행된다.** 둘 다
`registry/risk_gate_state.json`을 공유하는데(§3.3, Task Contract: "같은
계좌·같은 리스크게이트"), 이 동시 실행이 바로 §4에서 나온 P0 발견의
전제조건이다.

## 3. 사이클 하나가 하는 일

### 3.1 옵션 사이클 (`run_cycle_once`)

```
get_clock (장 열림?) 
  → 미체결 주문 조회 + 스테일 주문 취소(30분 초과)
  → 청산 감시 (열린 포지션 전부, evaluate_exit: profit_target/stop_loss/dte_forced)
  → risk_gate_state 로드 → evaluate_risk_gates → 저장 (§3.3, 무조건)
  → (risk_gate 막히면 신규진입 전체 스킵, 청산감시는 계속)
  → 대상종목(SYMBOLS, 9개) 중 노출 없는 것만: 레짐 조회 → 강도 랭킹
  → PRIORITY_ALLOCATION_ENABLED면 사전배분, 아니면 순차소진 (§3.4)
  → decide_for_symbol → order_intent → place_option_order
```

### 3.2 크립토 사이클 (`run_crypto_cycle_once`)

```
get_account_info (equity, non_marginable_buying_power)
  → risk_gate_state 로드 → evaluate_risk_gates → 저장 (§3.3, 무조건 — 옵션과 같은 파일!)
  → crypto_positions.json 로드 + 브로커 포지션 조회
  → 청산 감시 (evaluate_crypto_exit) → 청산 발생시에만 저장 (2026-09-02 c07eefe로 수정)
  → (risk_gate 막히면 신규진입 스킵)
  → BTC/ETH 각각: decide_crypto_for_symbol(available_cash 캡) → place_crypto_order
  → 신규진입 성사시 crypto_positions.json에 저장(원래도 조건부였음)
```

### 3.3 리스크게이트 — 옵션·크립토 공유, 안전장치

`registry/risk_gate_state.json` 하나를 옵션 사이클과 크립토 사이클이
**둘 다 무조건 로드→평가→저장**한다(§4 P0). 담고 있는 것: 일일/주간
손실 킬스위치, 계좌 HWM 대비 -20% 서킷브레이커(45분 halt, edge-triggered
— 2026-08-27 버그수정으로 영구동결 문제 고침). 이 파일이 유실/레이스로
잘못되면 손실이 나는데도 신규진입 억제가 안 걸릴 수 있다 — **시스템에서
가장 안전이 중요한 상태**.

### 3.4 우선순위 배분 (feature/priority-config, main 미병합)

옵션 사이클에서 여러 종목이 같은 사이클에 신호를 내면 실제 매수여력
(`options_buying_power`)을 놓고 경쟁한다. `PRIORITY_ALLOCATION_ENABLED`가
켜져 있으면 신호강도(ADX가 18~20 구간에서 얼마나 먼지)로 정렬해 상위 N개에
`PRIORITY_WEIGHTS` 비율로 사전배분, 꺼져 있으면(또는 매크로차단) 순차소진.
상세 설계·감사이력은 `docs/design-priority-allocation-2026-09-02.md`
(워크트리 `atlas-options-hackathon-worktrees/priority-config`)에 별도
문서화돼 있다 — **이 기능은 아직 main에 없다**, 백로그.

### 3.5 헬스체크 — 리컨실러+워치독

`health_check.py`는 15분(캘린더 비정렬)마다: 브로커에 열려있는데
로컬 `crypto_positions.json`엔 없는 심볼을 감지해 오늘 ATR로 근사
재구성(§4 P1 잔존 위험 있음), `risk_gate_state.json`이 파싱 안 되면
알림만(쓰기는 안 함 — 유일하게 이 파일에 대해 안전한 참여자), macOS
네이티브 알림으로 사람에게 알린다.

## 4. Codex 적대감사 (2026-09-02, 레포 전체 + 배포 토폴로지 명시)

**왜 이제야**: 지금까지의 Codex 감사는 전부 diff/커밋 스코프였다(8/25
leg-matching, 오늘 priority-config 등) — "이 변경이 옳은가"만 봤지, 4개
독립 launchd 잡이 같은 파일을 어떻게 동시에 건드리는지는 애초에 감사
범위 밖이었다. 사용자가 크립토 상태파일 레이스(§4.1의 3번, 이미 수정)를
발견한 뒤 "이 정도로 크리티컬이면 왜 감사에서 안 걸렸냐"고 지적해서,
배포 토폴로지를 명시적으로 알려주고 레포 전체를 다시 감사했다.

### 4.1 발견 (등급순)

| # | 등급 | 내용 | 관련 launchd 잡 | 상태 |
|---|---|---|---|---|
| 1 | **P0** | `risk_gate_state.json` — 옵션·크립토 둘 다 무조건 저장(가드 없음), market hours엔 같은 분에 동시 실행 → lost-update로 halt/HWM/킬스위치 상태가 조용히 사라질 수 있음 | options-runner + crypto-runner | **미수정** |
| 2 | P0/P1 | 두 프로세스가 같은 고정 이름의 `.json.tmp`를 씀 → 동시 쓰기 시 내용 교차·순서역전·`FileNotFoundError` 가능(원자적 replace는 "부분파일 방지"만 함, 다중writer 안전은 아님) | risk state: options+crypto / crypto positions: crypto+health-check | **미수정** |
| 3 | P1 | `crypto_positions.json` — 2026-09-02 `c07eefe`로 "변경없으면 저장안함" 가드는 넣었지만, **실제 변경(BTC 청산 등)과 health-check의 동시 복구**가 겹치면 여전히 lost-update 가능 | crypto-runner + health-check | 부분수정(c07eefe), 잔존위험 |
| 4 | P1 | 일일/주간 리스크게이트의 day/week 경계가 UTC 기준 — crypto-runner(24/7, UTC 자정에 baseline 갱신)가 옵션 거래일(ET) 기준보다 먼저 baseline을 바꿔버림. 실측: registry의 `day_key`가 PT 전날 저녁 크립토 사이클이 갱신한 값으로 보임 | crypto-runner → 다음 options-runner | **미수정** |
| 5 | P1/P2 | 스테일 옵션 주문 취소 요청이 accepted됐지만 브로커가 아직 open/fill 처리 중일 수 있는데, 같은 사이클에서 그 심볼을 노출집합에서 바로 빼서 재진입 — 원래 막으려던 중복노출 가드가 브로커 TOCTOU로 뚫릴 수 있음 | options-runner 단독(브로커 비동기 상태와의 TOCTOU) | **미수정** |
| 6 | P2 | 크립토 청산 주문이 accepted 응답만 보고 로컬 상태(stop/target)를 바로 삭제 — 실제 미체결/부분체결/사후거부시 브로커 포지션은 남는데 보호상태만 사라짐 | crypto-runner 단독(이후 health-check가 근사 복구는 함) | **미수정** |
| 7 | P2 | 옵션 청산 limit 폴백 호출이 try/except 밖 — 실패시 그 사이클 전체(청산감시+신규진입)가 죽을 수 있음 | options-runner 단독 | **미수정** |
| 8 | P2 | 포지션을 종목(underlying) 단위로 뭉뚱그려 "하나의 스프레드"로 취급 — 수동개입/부분체결/중복주문으로 레그가 4개 넘게 쌓이면 손익판정·DTE판정·청산 intent가 뒤섞일 수 있음 | options-runner 단독 | **미수정** |

### 4.2 왜 이전 감사들이 못 잡았나 (교훈)

diff 스코프 리뷰는 "이 커밋이 하는 일이 올바른가"를 검증하지, "이 파일을
또 누가 언제 건드리는가"는 diff 안에 안 보인다 — `.plist` 스케줄은 별도
파일이고, 애초에 리뷰 프롬프트에 "이 시스템이 여러 프로세스로 나뉘어
돈다"는 맥락을 넣은 적이 없었다. **배포 토폴로지는 코드가 아니라 운영
설정에 있고, 코드 리뷰만으론 구조적으로 볼 수 없는 카테고리의 버그였다.**
다음에 이 레포를 감사할 때는 이 문서(§2)를 먼저 프롬프트에 포함시킬 것.

## 5. 대응 (이 문서 작성 직후 진행)

- [ ] P0(#1) 수정: `risk_gate_state.json`에 프로세스간 락(`fcntl.flock`)
      도입, load→evaluate→save 전체를 락 안에서 수행
- [ ] P0/P1(#2) 수정: tmp 파일명에 pid/uuid 포함해 유일하게 — 락으로
      임계구역을 직렬화하면 사실상 같이 해결됨
- [ ] P1(#3) 잔존위험 수정: crypto_positions.json도 같은 락 적용
      (health-check와 crypto-runner 공유)
- [ ] #4~#8: 이번 세션에서 시간 내 처리 가능한 만큼만, 나머지는 백로그로
      명시 이관 (마감 2026-09-04 임박 — 전부 지금 고치려다 새 버그 낼
      위험이 더 크다는 판단이면 후순위로 미룸, 아래 재감사 라운드에서
      최종 상태 기록)

(이 섹션은 대응 완료 후 실제로 무엇을 고쳤고 무엇을 미뤘는지로
갱신한다 — 아래 "감사 라운드" 표 참고.)

## 6. 재감사 라운드

| 라운드 | 시점 | 대상 | 결과 |
|---|---|---|---|
| 1 | 2026-09-02 | 레포 전체 + 배포 토폴로지 | 8건 발견(P0 1, P0/P1 1, P1 3, P2 3) |
| 2 | 2026-09-02 | 라운드1 P0 수정(`_locked`) 검증 | 락 자체는 정확·데드락 없음 확인. **새 발견**: 임계구역 안에 네트워크 I/O(MCP 호출)가 있는데 `flock`이 타임아웃 없이 블로킹 — 한쪽이 API 호출에서 멈추면 다른 launchd 잡까지 무기한 같이 멈출 수 있음(커밋 `4213a31`로 대기자측 타임아웃 수정) |
| 3 | 2026-09-02 | 라운드2 타임아웃 수정 검증 + 전체 재스캔 | 대기자측 타임아웃 정상 확인(4개 호출부 전부 `except TimeoutError` 있음, 데드락 없음, fail-closed가 기존 halt 상태를 안 지움 확인). **새 발견 2건**(아래 §6.1) |

### 6.1 최종 잔존 항목 (마감 임박으로 지금 안 고침, 명시적 백로그)

| # | 등급 | 내용 | 왜 지금 안 고치나 |
|---|---|---|---|
| A | P1 | `_locked()`는 대기자(waiter)만 타임아웃 보호됨 — **락을 쥔 프로세스(holder) 자신**이 임계구역 안의 MCP 세션 호출(브로커 주문, 시세조회)에서 멈추면 그 프로세스는 락을 영원히 들고 있고, 크립토 처리 자체가 그 프로세스가 죽을 때까지 계속 비활성 — "다른 잡까지 같이 멈추는" 최악은 막았지만 "이 기능 자체가 멈춘 채 감지만 되는" 상태는 남음. 고치려면 MCP 호출 자체에 `asyncio.wait_for` 타임아웃을 걸어야 하는데, MCP stdio 세션이 타임아웃/취소에 안전하게 반응하는지 검증 안 된 상태로 대회 마감 이틀 전에 건드리는 게 더 위험하다고 판단 |
| B | P2 | `build_close_intent()`(`src/signals.py:557`)가 브로커 포지션 dict의 `qty` 누락시 `1`로 기본값, `side`가 `"short"`가 아니면 전부 long 취급 — 응답 shape가 흔들리면 실제 수량과 다른 청산주문을 낼 수 있음. 주문생성 로직 자체를 마감 임박에 건드리는 리스크 대비 발생 가능성이 낮다고 판단해 보류 |

이 두 항목은 대회 종료 후(또는 안전하게 시간 있을 때) 재검토 대상 — §4의 #4~#8과 같은 성격의 "알고 있고 의도적으로 미룬" 목록에 합류.
