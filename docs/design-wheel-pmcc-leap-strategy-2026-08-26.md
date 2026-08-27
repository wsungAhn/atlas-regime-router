# design-wheel-pmcc-leap-strategy

**상태**: 설계 (검토 대기) — 이 문서는 **구현 승인 게이트**다. 이 문서 자체는
`src/signals.py`·launchd 설정·라이브 경로를 건드릴 권한을 주지 않는다 (Trading Safety).

- 작성: 2026-08-26 PDT
- 워크트리: `.claude/worktrees/agent-ad25809d59c0a2b00` (라이브 체크아웃과 분리)
- 목적: **wheel + PMCC(LEAP 파이낸싱) 계열 전략 2~3종**을 이 레포의 유동성 ETF
  유니버스(SPY/QQQ/GLD/TLT/SLV/IWM)에 맞게 설계하고, 그걸 실제로 시뮬레이션할 수
  있는 **상태 유지형(stateful) 멀티레그 백테스트 엔진 확장**을 설계한다.
- 범위: **설계 전용.** 코드 구현은 별도 스텝 (Codex 핸드오프 대상, §9).

---

## 선행조사

### ① 레포 내 조사

`grep -rniE "leap|pmcc|wheel|assignment|roll"` 전수 검색 결과
(부정 결과도 남긴다 — 다음 세션이 재조사하지 않도록):

| 대상 | 확인한 것 | 판정 |
|---|---|---|
| LEAP / PMCC / wheel 구현 | **레포 내에 없음.** 유일한 문구 히트는 `docs/one-page-submission.md:149`의 "assignment risk" 언급 1건(설명 텍스트), `roll`은 전부 pandas `.rolling()` | **신규 필요** |
| `src/vendor/credit_spread_simulator.py` (459줄) | `simulate_trade` = 단일 거래 1진입-1청산 라이프사이클. `run_portfolio_simulation` = 신호일 순회하며 독립 거래 나열. **포지션 간 상태 공유 없음, 만기 넘어 보유 불가, 레그 2개(크레딧 스프레드) 고정** | 그대로는 불가 — §5에서 확장/병렬 판정 |
| `src/vendor/options_pricing.py` (130줄) | `black_scholes_price` / `black_scholes_delta` / `strike_for_delta`(델타→행사가 이분탐색). 순수함수, 만기 임의 | **그대로 재사용** — LEAP 가격결정·델타타깃 행사가 선택에 필요한 프리미티브가 이미 전부 있다 |
| `src/vendor/iv_approximation.py` | `estimate_historical_vol` — realized vol 근사 IV. `_rolling_iv_series`(backtest.py)가 날짜별 시리즈로 이미 제공 | **그대로 재사용** — LEAP MTM에 같은 IV 근사 사용 (기존 관례와 일관) |
| `src/backtest.py` (491줄) | `fetch_daily_bars`/`fetch_intraday_bars`(parquet 캐시), `_rolling_iv_series`, `_risk_pct_series`, `_metrics_from_dollar_trades`, `run_combined_portfolio*` | 데이터 fetch·IV·리포팅 **재사용**, 사이징(`scale_trades_to_dollars`)은 **재사용 불가** (§5.4 — 진입일순 1회 정산 모델이 지속 포지션과 안 맞음) |
| `src/strategies.py` (254줄) | 10개 순수 신호함수, `StrategySignal(date, spread_type, level, weight)`. 전략7의 ADX/EMA 레짐판정 | 레짐 게이트(§3)로 **재사용** |
| `src/portfolio.py` | `scale_trades_to_dollars` = 진입일순 복리 + min-heap 정산 + 킬스위치. **거래가 (진입일, 청산일, max_loss)로 완결된다는 가정** | LEAP 계열엔 부적합 (§5.4) — 자본 분할 방식으로 우회 |
| `docs/design-multi-asset-backtest.md` | 슬리브 구조·환원 정합성 루브릭의 선례 | 문서 형식·검증 패턴 차용 |

### ② 레포 밖 선행작업

| 이름 | 위치/URL | 라이선스 | 최근성 | 판정 |
|---|---|---|---|---|
| tastytrade PMCC 가이드라인 | tastytrade research (projectfinance/optionalpha 요약 경유) | 콘텐츠(코드 아님) | 현행 | **관례 채택**: 롱 레그 delta ≥ 0.70~0.80·DTE 300일+, 숏 레그 delta ≤ 0.35·45DTE(주간도 통용), **"트레이드 비용 < 행사가 폭의 75%"** 건전성 규칙 |
| Blue Collar Investor PMCC 방법론 | thebluecollarinvestor.com | 콘텐츠 | 현행 | **관례 채택**: LEAP delta 0.75~1.0, **만기 90일 전 LEAP 롤** |
| Cboe BXM/PUT 인덱스 방법론 | cdn.cboe.com (BXM_Methodology.pdf, PutWrite Methodology) | 공개 방법론 문서 | 현행(공식) | **관례 채택**: 커버드콜/풋라이트의 표준 벤치마크 롤 규칙 — ATM 매도·만기 보유·만기일 재롤. 숏 레그 "만기까지 보유 + 현금정산" 모델의 근거 |
| AQR "PutWrite vs BuyWrite" | aqr.com 백서 | 학술 백서 | 2017~ | 참고 — 풋라이트와 커버드콜의 등가성(풋콜 패리티). wheel의 CSP 국면과 CC 국면을 같은 엔진 코드로 다뤄도 됨을 뒷받침 |
| optopsy | github.com/goldspanlabs/optopsy (구 michaelchu) | **GPL-3.0** | 2024~25 활동 | **불채택** — GPL-3(이 레포에 코드 유입 불가), 그리고 실제 옵션체인 이력 데이터를 전제로 한다. 이 레포는 BS 근사 노선(§backtest.py 독스트링에 명시된 기존 결정)이라 데이터 전제부터 안 맞음. 델타타깃 행사가 선택·수명주기 이벤트 개념만 참고 |
| QuantConnect LEAN wheel 구현 | quantconnect.com 포럼/LEAN | Apache-2.0 | 현행 | **불채택** — 프레임워크 통째 도입은 기존 검증자산(vendor 엔진·사이저·리포터) 폐기와 같다 (multi-asset 설계의 vectorbt 불채택과 동일 논리). 배정(assignment) 이벤트 처리 순서(만기 → 배정 → 주식 전환 → CC 국면 전환)만 참고 |
| AlphaBot / trader | `~/dev/Auto_bot`, `~/dev/trader` | 사내 | — | 확인 — 양쪽 다 LEAP/PMCC/wheel 시뮬 없음 (AlphaBot R1-B는 이 레포 vendor의 원본, 동일 단일거래 모델) |

### ③ 결론

- **가격 프리미티브·IV·데이터 fetch·리포팅: 전부 재사용** (신규 0줄).
- **거래 수명주기 엔진: 신규 병렬 엔진** — `credit_spread_simulator.py`를 확장하지
  않는다 (§5.1에 이유). 신규 파일 1개 (`src/leap_engine.py`).
- **전략 관례: 발명하지 않고 채택** — LEAP delta 0.70~0.80 / DTE~365 / 90일 전 롤
  (tastytrade·BCI), 숏 레그 만기보유+현금정산 (Cboe BXM/PUT 방법론).
- 외부 라이브러리 유입 0 (optopsy는 GPL, LEAN은 프레임워크 통째라 불채택).

---

## 1. 문제 정의

기존 10개 전략은 전부 "5~9일 DTE 크레딧 스프레드 1진입-1청산"이다. 사용자가 원하는
wheel + PMCC(LEAP 파이낸싱) 계열은 구조가 다르다:

1. **롱 레그(LEAP)를 수개월 보유**하면서
2. 그 위에 **숏 옵션을 반복 매도/롤**하고
3. 걷힌 프리미엄이 **LEAP 사이징/롤에 되먹임**(funding)되며
4. 숏 레그의 **배정(assignment)이 롱 레그를 닫지 않는다**.

기존 엔진의 가정(거래 = 독립적·완결적·고정 DTE 창)이 넷 다 깨진다. 질문:

1. 주간 숏 프리미엄이 LEAP 보유비용을 실제로 얼마나 상쇄하나? (PMCC의 존재 이유 검증)
2. 프리미엄을 현금으로 쌓는 것 vs LEAP에 재투자하는 것 — 3년 복리에서 어느 쪽이 이기나?
3. **대회 7일 채점창** 안에서 이 계열이 실현 P&L을 얼마나 내나? (LEAP 자체는 창 안에서
   미실현 — 숏 레그 주간 사이클만 실현된다. multi-asset 설계 §7의 지적과 동일한 함정)

### 성공 기준 (Acceptance Rubric — 구현 착수 **전** 고정)

| 차원 | 1점 | 3점 | 5점 |
|---|---|---|---|
| **현금 보존식** | equity ≠ cash + MTM(legs) + 주식가치 (돈이 새거나 생김) | 오차 있으나 원인 파악 | **매일 자산 항등식이 부동소수점 오차 내로 성립함을 테스트로 고정** |
| **기존 경로 무영향** | 기존 10전략 백테스트 수치 변화 | — | **기존 파일 수정 0줄에 가깝고(§5.5), 기존 테스트 전부 통과** |
| **배정 메커니즘** | 배정 없이 항상 현금정산 (wheel이 wheel이 아님) | 배정은 되나 CC 국면 전환 누락 | CSP 배정→주식→CC→콜어웨이 전체 사이클이 로그로 추적됨 |
| **funding 되먹임** | 프리미엄이 사이징에 무반영 | 반영되나 집계 없음 | premium bank 잔고·재투자 이벤트가 리포트에 명시 |
| **7일 창 실현 P&L** | 미측정 | 측정 | 임의의 7일 창 실현 P&L 분포를 기존 전략과 나란히 리포트 |
| **검증** | 테스트 없음 | 통과하는 테스트 | 회귀를 심으면 **실제로 깨지는** 것까지 실증 |

**게이트**: 현금 보존식 5점 **필수** (지속 포지션 엔진에서 돈이 새면 나머지 숫자는
전부 무의미). 나머지 평균 3.5 이상. 미달 시 재작업.

---

## 2. 전략 변형 3종

공통 사항:
- 유니버스는 라이브와 동일 6종 ETF. **단일종목 없음** — wheel의 최대 리스크인
  개별기업 이벤트(실적 갭)가 구조적으로 제거된 대신, 프리미엄도 얇다. 이게 이
  유니버스에서 wheel/PMCC를 백테스트하는 이유 자체다: "지수 ETF에서도 되나?"
- 레짐판정은 전략7의 기존 ADX/EMA 로직 재사용 (`ema20>ema50 & adx>20` = bull,
  반대 = bear, 그 외 = neutral). 신규 신호코드 0줄.
- IV는 기존 `_rolling_iv_series` (realized vol 근사) — 기존 노선과 일관.
- 옵션 만기는 실체인 없이 BS 근사이므로 "DTE n일"을 캘린더로 계산하고 가장 가까운
  거래일로 스냅한다 (vendor의 `expiry_candidates` 패턴 그대로).

**자본 분할**: LEAP 계열 슬리브에 시작자본의 **30% ($30k)** 를 고정 배정한다.
기존 크레딧 스프레드 포트폴리오(70%)와는 **자본 수준에서 분리**하고 거래 수준에서
섞지 않는다. 이유는 §5.4 — 지속 포지션은 `scale_trades_to_dollars`의 진입일순
정산 모델에 낄 수 없고, 억지로 끼우면 기존 검증 수치가 오염된다.

### 2.1 V1 — PMCC Classic (SPY/QQQ, 델타 0.80 LEAP + 주간 0.20 콜)

표준 PMCC를 관례 그대로 구현한 **베이스라인**. 가설: *"주간 콜 프리미엄이 LEAP
세타 소모 + 보유비용을 상쇄하고도 남는가"* — 이게 안 되면 V2/V3은 볼 것도 없다.

| 항목 | 규칙 |
|---|---|
| 대상 | SPY, QQQ (bull 레짐인 종목만; 둘 다 bull이면 둘 다, 각각 독립 북) |
| LEAP 진입 | 레짐이 bull로 **전환된 다음 거래일**, delta 0.80 콜 (`strike_for_delta`), DTE 365 |
| LEAP 사이징 | 계약당 비용 ≤ 슬리브 자본의 40% → 계약수 = `floor(0.4 * sleeve_equity / (leap_cost*100))`, 최소 1계약이 안 되면 그 종목 스킵(로그) |
| 숏 콜 진입 | LEAP 보유 중 + 숏 레그 없음 → delta 0.20 콜 매도, DTE 7, 수량 = LEAP 계약수. **단 숏 행사가 > LEAP 행사가 + 순비용**(tastytrade 75% 규칙의 취지: 콜어웨이 시나리오에서도 손실이 안 나는 행사가만) — 조건 불충족 시 그 주는 매도 스킵(로그) |
| 숏 콜 청산 | ① 프리미엄 50% 익절 ② 프리미엄 2.0x 손절(기존 전략과 동일 상수) ③ 만기 도달 — ITM이면 **현금정산**(내재가치 지불; LEAP는 그대로 보유. 유럽형 근사 — §5.3) |
| 숏 콜 재진입 | 청산 다음 거래일 (연속 롤) |
| LEAP 롤 | DTE < 90 → 청산 후 같은 규칙으로 재진입 (BCI 90일 규칙). 이때 실현 P&L 발생 |
| LEAP 청산 | ① 레짐이 bear로 전환 ② LEAP 가치가 진입가의 -50% (하드 스톱) — 청산 시 숏 레그도 동시 청산 (네이키드 콜 금지) |
| funding | 숏 프리미엄 실현분은 슬리브 현금으로 귀속 → LEAP 롤/신규 진입 시 그 시점 `sleeve_equity`로 사이징 (복리). **별도 뱅크 없음** — 가장 단순한 되먹임 |
| 리스크 상한 | 종목당 LEAP 비용 40%·슬리브 합산 80% 상한, LEAP -50% 스톱, bear 전환 시 전량 청산 후 현금 대기 |

지속 하락장 시나리오: bear 전환 청산이 1차 방어(-50% 스톱보다 거의 항상 먼저
걸린다 — EMA 크로스가 더 빠름), 스톱은 갭 폭락(레짐 전환 전 급락)용 2차 방어.

### 2.2 V2 — Wheel + LEAP 스윕 (SLV/TLT wheel → SPY LEAP 매수)

고전 wheel을 돌리되, 누적 프리미엄을 **주기적으로 LEAP 매수로 스윕**하는 변형.
사용자가 말한 "wheel 프리미엄으로 LEAP를 사 모은다" 메커닉의 직접 구현.

**유니버스 적응이 핵심**: SPY CSP 1계약은 현금담보 ~$63k라 $30k 슬리브에 불가능.
저가 ETF인 **SLV(~$3k/계약)·TLT(~$9k/계약)** 에서 wheel을 돌리고, 스윕 대상만
SPY LEAP로 한다. (이 비대칭이 6종 ETF 유니버스에서 wheel이 성립하는 유일한 구성이다.)

| 항목 | 규칙 |
|---|---|
| CSP 진입 | SLV·TLT 각각, 레짐 bear가 **아닐 때**(bull/neutral), delta 0.25 풋 매도, DTE 7, 현금담보 100% 예치. 수량 = 담보 가능 최대 (SLV 우선, 남는 현금으로 TLT) |
| CSP 만기 | OTM → 프리미엄 전액 실현, 다음 거래일 재진입. ITM → **배정**: `strike*100*qty` 지불, 주식 보유 전환 |
| CC 국면 | 주식 보유 중 → delta 0.25 콜 매도, DTE 7 (단 행사가 ≥ 배정단가 — 손실 콜어웨이 방지; ATM보다 낮으면 배정단가로 스냅). ITM 만기 → 콜어웨이(주식 매도 + 프리미엄 실현) → CSP 국면 복귀 |
| CC 국면 제한 | 배정 후 주가가 배정단가 -20% 아래로 가면 **주식 손절** (wheel의 고전적 파산 경로 — "물리면 영원히 CC" — 차단). 로그에 명시 |
| **LEAP 스윕** | 월말(매월 마지막 거래일)마다: `premium_bank`(그 달 실현 프리미엄 누계) ≥ 그 시점 SPY delta 0.70/DTE 365 콜 1계약 비용이면 매수, bank 차감. 미달이면 이월 |
| LEAP 관리 | 매수한 LEAP는 **보유 전용** (숏 매도 없음 — V1과의 통제변인 분리). DTE < 90 롤, -50% 스톱, bear 전환 시 청산은 V1과 동일 |
| 리스크 상한 | LEAP 누적 비용이 슬리브 자본의 50% 도달 시 스윕 중단(현금 이월). wheel 자체는 현금담보라 레버리지 0 |

가설: *"프리미엄의 재투자처로서 LEAP(볼록성 매수)가 현금 복리를 이기는가."*
V1과 달리 숏 레그와 롱 레그가 **다른 종목**이라 상관 리스크가 낮고, 배정 메커니즘을
실제로 요구하는 유일한 변형이다 (엔진의 배정 경로 검증도 겸한다).

### 2.3 V3 — Bull-Put 스프레드 파이낸싱 LEAP 래더 (기존 챔피언과의 브릿지)

숏 레그를 네이키드 CSP가 아니라 **기존에 검증된 bull_put 크레딧 스프레드**로 대체한
변형. 숏 사이클은 기존 vendor 엔진을 사이클 단위로 그대로 호출하고, 신규 엔진은
LEAP 원장만 관리한다 — 3종 중 **엔진 신규 코드가 가장 적다** (Working Skeleton
First: 구현 순서 V3 → V1 → V2 권고, §9).

| 항목 | 규칙 |
|---|---|
| 숏 사이클 | SPY·QQQ·IWM에 전략7(`7_atlas_mvp`) 신호 그대로 + 기존 `run_portfolio_simulation` 파라미터 그대로 (DTE 7, delta 0.20, width 1.5%, PT 50%, SL 2.0x). 슬리브 자본에서 리스크 5%/건 사이징 |
| **funding** | 스프레드 실현 프리미엄 누계가 `premium_bank`에 적립 → 주말(매주 금요일 종가)마다 bank ≥ SPY 또는 QQQ delta 0.70/DTE 365 콜 1계약 비용 && 해당 종목 bull 레짐이면 매수 (레짐 게이트 — 하락장에서 LEAP를 사 모으는 자살 방지) |
| LEAP 관리 | V2와 동일 (보유 전용, DTE<90 롤, -50% 스톱, bear 청산) |
| 리스크 상한 | 스프레드 자체가 정의된 리스크(width−credit) — 배정 모델링 불필요. LEAP 누적 50% 상한 동일 |

가설: *"이미 검증된 프리미엄 수확기의 출력을 현금이 아니라 LEAP에 꽂으면 3년 성과가
개선되는가"* — 대회 서사로도 가장 강하다 (기존 봇의 자연 확장 + funding 창의성).
V2와의 차이는 숏 레그의 리스크 정의 방식(무제한 배정 vs 정의된 스프레드)이며,
둘을 비교하면 "wheel의 배정 리스크가 프리미엄 값을 하는가"가 분리 측정된다.

### 2.4 변형 간 비교 매트릭스 (무엇을 분리 측정하나)

| | 숏 레그 | 롱 레그 위치 | 배정 | 분리 검증하는 가설 |
|---|---|---|---|---|
| V1 | 커버드 콜 (LEAP 담보) | 숏과 같은 종목 | 현금정산 근사 | 주간 프리미엄 > LEAP 세타 |
| V2 | 네이키드 CSP/CC (현금담보) | 다른 종목 (SPY) | **실제 모델링** | LEAP 재투자 > 현금 복리 |
| V3 | bull_put 스프레드 (기존 검증) | SPY/QQQ | 불필요 | 검증된 수확기 + LEAP 되먹임 |

---

## 3. 신호·레짐: 신규 0줄

세 변형 모두 레짐 게이트는 전략7의 ADX/EMA 판정을 그대로 쓴다.
`_common_indicators`(strategies.py)가 이미 trailing으로 계산하므로 미래정보 누출
없음. 신규 신호함수를 만들지 않는다 — 이 계열의 신규성은 신호가 아니라
**포지션 구조와 funding 되먹임**에 있다.

---

## 4. 데이터: 신규 fetch 0줄

`fetch_daily_bars` + `_rolling_iv_series` + `_risk_pct_series` 재사용. LEAP 가격은
실체인이 아니라 BS 근사이므로 추가 데이터가 필요 없다 (기존 노선의 명시적 연장).

**알려진 한계 (정직하게)**: realized-vol 근사 IV는 장기 만기의 볼 텀스트럭처·스큐를
무시한다. LEAP 딥 ITM(delta 0.8)은 시간가치 비중이 작아 IV 오차의 가격 영향이
상대적으로 작다 — 딥 ITM을 관례로 쓰는 이유가 설계상 근사 오차도 줄여준다.
ATM LEAP(delta 0.5) 변형을 안 만드는 이유이기도 하다 (근사 오차가 결과를 지배).

---

## 5. 엔진 확장 설계

### 5.1 결정: 확장도 래핑도 아닌 **병렬 엔진** (`src/leap_engine.py`, 신규 1파일)

`credit_spread_simulator.py`를 **수정하지 않는다**. 이유:

1. 그 모듈은 라이브 봇이 검증된 수치의 근거로 쓰는 **동결 자산**이다 (AlphaBot R1-B
   검증 이력 + 이 레포 10전략 백테스트 전부가 그 위에 서 있다). 지속 포지션을
   끼워 넣으려면 `simulate_trade`의 "거래=완결" 가정, `run_portfolio_simulation`의
   신호일 순회 구조를 둘 다 뜯어야 한다 — 확장이 아니라 재작성이고, 재작성이면
   기존 검증이 무효가 된다.
2. 래핑(wrapping)도 안 맞다: 지속 포지션의 본질은 **일자 순회가 바깥 루프**이고
   거래가 안쪽인 구조인데, vendor는 거래가 바깥 루프다. 루프 중첩 방향이 반대라
   래퍼가 어댑터 지옥이 된다.
3. 단, **가격 프리미티브는 공유한다** — `black_scholes_price`/`strike_for_delta`/
   `estimate_historical_vol`은 순수함수라 양쪽 엔진이 그대로 임포트. "같은 가격모델,
   다른 수명주기 엔진"이 유지된다. V3의 숏 사이클은 아예 vendor 엔진을 사이클
   단위로 호출한다 (per-cycle로는 vendor의 가정이 전부 성립하므로).

### 5.2 데이터 모델

```python
# src/leap_engine.py — 전부 dataclass, 직렬화·DB 없음
@dataclass
class OptionLeg:
    option_type: str        # "call" | "put"
    strike: float
    expiry: pd.Timestamp
    qty: int                # +N 롱 / -N 숏 (계약수)
    entry_price: float      # 진입 시 옵션가 (1주당)
    entry_date: pd.Timestamp
    role: str               # "leap" | "short_call" | "csp" | "covered_call"

@dataclass
class Book:                 # 종목당 1개
    symbol: str
    cash: float             # 이 북에 배정된 현금 (담보 예치 포함)
    shares: int             # wheel 배정 주식 (V2만 사용)
    share_cost_basis: float
    legs: list[OptionLeg]
    premium_bank: float     # funding 스윕 대기 프리미엄 (V2/V3)
    realized: list[dict]    # 실현 이벤트 로그 (아래 스키마)
    equity_curve: dict[pd.Timestamp, float]  # 매일 MTM

# 실현 이벤트 스키마 — 기존 리포팅과의 접점
# {entry_date, exit_date, symbol, kind: "short_cycle"|"leap_roll"|"leap_close"|
#  "assignment"|"called_away"|"share_stop", dollar_pnl: float, detail: {...}}
```

`dollar_pnl`이 **이미 달러**라는 게 기존 raw trade dict와의 결정적 차이다 —
사이징이 엔진 안(북의 현금)에서 일어나므로 사후 스케일링이 없다 (§5.4).

### 5.3 이벤트 루프

일자(daily bar)가 바깥 루프. 하루 안의 처리 순서를 고정한다 (순서가 결과를 바꾸므로
명세가 필요한 지점):

```
for d in trading_days:                        # 종목별 Book 각각
    1. EXPIRY   — 오늘 만기 레그 정산:
                  숏콜 OTM → 프리미엄 전액 실현 / ITM →
                    (V1, LEAP 담보) 내재가치 현금정산, LEAP 유지
                    (V2, 주식 담보) 콜어웨이: 주식 strike 매도 + 프리미엄 실현
                  CSP OTM → 프리미엄 실현 / ITM → 배정: cash -= strike*100*qty,
                    shares += 100*qty, cost_basis = strike - premium
    2. TRIGGER  — 만기 전 청산 트리거 (그날 종가 MTM 기준):
                  숏 레그 50% PT / 2.0x SL, LEAP -50% 스톱,
                  레짐 bear 전환 → LEAP+숏 동시 청산, V2 주식 -20% 손절
    3. ROLL     — LEAP DTE < 90 → 청산(실현) 후 재진입
    4. ENTRY    — 변형별 진입 규칙 (§2): LEAP 신규, 숏 레그 재매도, CSP/CC
    5. FUNDING  — 스윕 판정 (V2 월말 / V3 금요일): bank → LEAP 매수
    6. MTM      — equity = cash + Σ(leg MTM × 100 × qty) + shares × close
                  equity_curve[d] 기록 + 자산 항등식 assert
```

- **배정은 만기 시점만** (유럽형 근사). 미국식 조기배정(배당락 전 딥 ITM 콜)은
  모델링하지 않는다 — BS 자체가 유럽형이고, 조기배정은 보유자에게 대체로 유리
  (시간가치 포기를 받는 쪽)라 이 근사는 보수적이거나 중립이다. 리포트에 명시.
- 같은 날 PT·SL 동시 도달 시 **SL 우선** (multi-asset 설계 §4.2와 동일한 보수 규칙).
- 갭·슬리피지·수수료 미반영 — 기존 크레딧 스프레드 백테스트와 동일 수준의 근사
  (`credit_haircut_pct` 5%는 숏 레그 진입 크레딧에 동일 적용해 비교 가능성 유지).

### 5.4 사이징: `scale_trades_to_dollars`를 **쓰지 않는다** — 이유 명시

기존 사이저는 "거래 리스트를 진입일순으로 정렬해 그 시점 잔고로 사이징하고 min-heap
으로 청산 정산"하는 모델이다. 이 모델은 거래가 (진입, 청산, max_loss)로 **사전에
완결**되어 있어야 성립한다. LEAP 계열은:
- 거래의 청산일·손익이 진입 시점에 미정 (경로의존 — 롤·레짐·스톱)
- 포지션 하나가 수십 개의 실현 이벤트를 낳음 (숏 사이클들)
- 담보(현금·주식·LEAP)가 사이징의 입력 (max_loss 스칼라로 환원 불가)

억지로 끼우면 기존 검증 수치가 오염될 위험만 있다. 대신 **북 내부 현금이 곧
사이징**이고(진입 시점 잔고로 계약수 결정 = 자연 복리), 슬리브 간 결합은 §2의
고정 자본 분할(30/70)로 한다.

**알려진 천장**: 고정 분할은 슬리브 간 예산 경합을 모델링하지 않는다. multi-asset
설계가 만드는 경합 모델과의 통합은 이 계열이 백테스트에서 생존한 **다음** 라운드의
일이다 (Working Skeleton First — 지금 통합하면 두 미검증 시스템을 동시에 짓는 것).

### 5.5 기존 플로우 접속점

```python
# src/leap_backtest.py (신규, 오케스트레이터 — backtest.py는 수정하지 않음)
def run_leap_family(variant: str, years: int = 3) -> StrategyResult:
    # 1. fetch_daily_bars / _rolling_iv_series / 레짐 시리즈 (전부 기존 함수 임포트)
    # 2. Book 초기화 (슬리브 자본 $30k를 변형별 규칙대로 종목 북에 분배)
    # 3. leap_engine.run(books, bars, iv, regime, variant_rules)
    # 4. realized 이벤트 → 경량 어댑터(dollar_pnl 속성만 있는 namedtuple)로 감싸
    #    _metrics_from_dollar_trades(...) 재사용 → StrategyResult
    # 5. 리포트 추가 항목: 7일 창 실현 P&L 분포, premium_bank 이벤트 로그,
    #    배정/콜어웨이 횟수, 숏 매도 스킵 횟수(V1의 75% 규칙), LEAP 롤 이력
```

`backtest.py`·`portfolio.py`·`vendor/` **수정 0줄**. `_metrics_from_dollar_trades`가
`dt.dollar_pnl`만 읽으므로 어댑터로 충분하다 (읽는 속성이 늘어 있으면 구현 시
확인 — 현재 코드 기준으론 `dollar_pnl` 단일 속성).

---

## 6. 하지 않는 것 (Scope Cuts)

1. **실옵션체인 데이터 도입 안 함** — BS 근사 노선 유지 (기존 결정의 연장, §4).
2. **조기배정 모델링 안 함** — §5.3. 근사 방향이 보수적/중립.
3. **ATM/저델타 LEAP 변형 안 만듦** — IV 근사 오차가 결과를 지배 (§4).
4. **multi-asset 슬리브 경합 통합 안 함** — §5.4 천장. 다음 라운드.
5. **라이브 배선 안 함** — 백테스트에서 생존하면 별도 Task Contract. 특히 배정
   처리는 라이브에서 완전히 다른 문제 (Alpaca 페이퍼의 실제 배정 이벤트 수신).
6. **N-변형 프레임워크 안 만듦** — 변형 3개는 `variant_rules` dict가 아니라
   각각 명시적 함수로. 규칙이 3벌뿐인데 규칙 DSL을 만드는 건 YAGNI.
7. **vendor 수정 안 함** — §5.1.

---

## 7. 대회 관점의 정직한 판정

대회 채점은 **창 안 실현 P&L**이다. 이 계열에서 창 안에 실현되는 것은 숏 사이클
(주간)뿐이고 LEAP 손익은 대부분 미실현이다. 즉:

- **V1/V3이 대회 적합** (주간 실현 사이클 존재). V2는 배정 주기가 끼면 실현이
  더 불규칙 — 대회보다는 운용 연구 성격.
- 창의성 점수 서사는 V3이 최강: "검증된 프리미엄 수확기가 자기 수확물로 장기
  볼록성을 사 모은다"는 한 문장이 된다.
- 백테스트 리포트의 **7일 창 실현 P&L 분포**(루브릭 항목)가 대회 투입 여부의
  최종 판정 기준. 기존 챔피언(전략7 combined)보다 분포가 나쁘면 대회에는 안 넣고
  운용 연구로 성격 전환.

---

## 8. 검증 계획

`tests/test_leap_engine.py` — 기존 pytest, 프레임워크 추가 없음. 전부 합성 가격
데이터 (API 불필요).

1. **현금 보존식 (게이트)**: 매 스텝 `equity == cash + Σleg_mtm + shares*px`가
   전 구간 성립. 진입·청산·배정·롤·스윕 각 이벤트 직후 검증.
2. **배정 사이클**: 가격을 행사가 아래로 보내는 합성 시나리오에서 CSP 배정 →
   shares 증가·cash 감소 → CC 매도 → 가격 회복 → 콜어웨이 → 현금 복귀. 각 단계의
   realized 이벤트 kind가 순서대로 기록됨.
3. **LEAP 담보 현금정산 (V1)**: 숏콜 ITM 만기에서 LEAP가 닫히지 **않고** 내재가치만
   지불됨. LEAP qty 불변 확인.
4. **funding 스윕**: bank가 LEAP 1계약 비용 직전/직후인 두 시나리오 — 미달이면 이월,
   충족이면 매수+차감. bear 레짐에서는 충족해도 미매수 (V3 게이트).
5. **롤 실현**: DTE<90 롤에서 realized 이벤트가 발생하고 equity가 연속 (롤 자체는
   P&L 중립 이벤트 + 실현/미실현 재분류).
6. **미래정보 누출**: 백테스트 종료일 이후 봉을 추가해도 그 이전 equity_curve 불변.
7. **기존 무영향**: 기존 테스트 스위트 전체 통과 (수정 0줄이므로 당연해야 하나
   임포트 부작용 확인 목적).

**회귀 심기 실증** ([[feedback_prove_tests_fail_by_injecting_the_regression]]):
배정 경로에서 `cash -= strike*100*qty`를 주석 처리해 테스트 1·2가 **실제로 깨지는
것**을 확인 후 편집으로 원복 (`git checkout` 금지 — 공유 워크트리).

---

## 9. 산출물·구현 순서

| 파일 | 성격 | 규모 추정 |
|---|---|---|
| `src/leap_engine.py` | 신규 (데이터모델 + 이벤트 루프 + 변형 3종 규칙) | ~350줄 |
| `src/leap_backtest.py` | 신규 (오케스트레이터 + 리포트) | ~150줄 |
| `tests/test_leap_engine.py` | 신규 | ~200줄 |
| 기존 파일 | **수정 0줄** | — |

**구현 순서 (Working Skeleton First)**: V3 (vendor 재사용 최대, 배정 없음 — 엔진
스켈레톤 + funding만으로 E2E) → V1 (숏 레그 자체 관리 + 현금정산) → V2 (배정 전체).
각 단계에서 3년 백테스트를 실제로 돌려 숫자를 본 뒤 다음 변형 착수.

Tier 2+ (신규 모듈·설계문서 존재) → **Codex 핸드오프 대상** (설계=이 문서,
타이핑=Codex). 워크트리는 이미 있음 (현 워크트리에서 진행).

**건드리지 않는 것**: `src/signals.py`, `src/mcp_runner.py`, `src/vendor/*`,
`src/backtest.py`, `src/portfolio.py`, launchd 설정, 라이브 체크아웃 전체.

---

## 참고 출처 (선행조사 ②의 URL)

- tastytrade PMCC 관례 (projectfinance 정리): https://www.projectfinance.com/poor-mans-covered-call/
- Option Alpha PMCC 가이드: https://optionalpha.com/learn/poor-mans-covered-call
- Blue Collar Investor — LEAP 행사가 선택·90일 롤: https://www.thebluecollarinvestor.com/poor-mans-covered-call-selecting-the-best-leaps-strike/
- Cboe BXM 방법론: https://cdn.cboe.com/api/global/us_indices/governance/BXM_Methodology.pdf
- Cboe PutWrite 방법론: https://cdn.cboe.com/api/global/us_indices/governance/Cboe_PutWrite_Indices_Methodology.pdf
- AQR PutWrite vs BuyWrite: https://images.aqr.com/-/media/AQR/Documents/Insights/White-Papers/AQR-PutWrite-vs-BuyWritevF.pdf
- optopsy (GPL-3, 불채택): https://github.com/goldspanlabs/optopsy
- QuantConnect wheel 구현 (불채택, 참고): https://www.quantconnect.com/forum/discussion/8014/problem-trying-to-backtest-the-wheel-strategy/
