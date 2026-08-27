# design-wheel-pmcc-leap-strategy

<!-- lint:doc-meta round=3 -->

**상태**: 설계 (감사 3라운드 CLEAN — 단 3라운드는 대체 감사자) — 이 문서는 **구현 승인 게이트**다. 이 문서 자체는
`src/signals.py`·launchd 설정·라이브 경로를 건드릴 권한을 주지 않는다 (Trading Safety).

> **Codex 핸드오프 전 필수**: 3라운드 CLEAN은 대체 감사자(Gemini — Codex 쿼터 소진)
> 판정이라 이 레포의 Tier 2+ 필수 Codex 감사 요건을 단독으로 충족하지 못한다.
> 구현 핸드오프 전에 실제 `codex exec` 감사 1라운드를 반드시 재실행할 것
> (쿼터 리셋 ~2026-08-27 01:09 PDT).

- 작성: 2026-08-26 PDT
- 워크트리: `atlas-options-hackathon-worktrees/wheel-pmcc-leap` 브랜치 `wheel-pmcc-leap-strategy` (라이브 체크아웃과 분리)
- 목적: **wheel + PMCC(LEAP 파이낸싱) 계열 전략 2~3종**을 이 레포의 유동성 ETF
  유니버스(SPY/QQQ/GLD/TLT/SLV/IWM)에 맞게 설계하고, 그걸 실제로 시뮬레이션할 수
  있는 **상태 유지형(stateful) 멀티레그 백테스트 엔진 확장**을 설계한다.
- 범위: **설계 전용.** 코드 구현은 별도 스텝 (Codex 핸드오프 대상, §9).

---

## 선행조사

(레포 내 검색: ① / 외부 선행작업: ② / 결론: ③)

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
| `design-multi-asset-combined-backtest.md` | 슬리브 구조·환원 정합성 루브릭의 선례 | 문서 형식·검증 패턴 차용 |

### ② 레포 밖 선행작업

| 이름 | 위치/URL | 라이선스 | 최근성 | 판정 |
|---|---|---|---|---|
| tastytrade PMCC 가이드라인 | tastytrade research (projectfinance/optionalpha 요약 경유) | 콘텐츠(코드 아님) | 현행 | **관례 채택**: 롱 레그 delta ≥ 0.70~0.80·DTE 300일+, 숏 레그 delta ≤ 0.35·45DTE(주간도 통용), **"트레이드 비용 < 행사가 폭의 75%"** 건전성 규칙 |
| Blue Collar Investor PMCC 방법론 | thebluecollarinvestor.com | 콘텐츠 | 현행 | **관례 채택**: LEAP delta 0.75~1.0, **만기 90일 전 LEAP 롤** |
| Cboe BXM/PUT 인덱스 방법론 | cdn.cboe.com (BXM_Methodology.pdf, PutWrite Methodology) | 공개 방법론 문서 | 현행(공식) | **관례 채택 (범위 한정)**: 벤치마크 롤 규칙(만기 보유·만기일 재롤)의 근거로만 쓴다. **SPX 인덱스(현금결제·유럽형) 방법론이라 ETF 옵션(실물결제·미국식)의 배정 현실 근거는 아니다** — V1의 현금정산은 Cboe 근거가 아니라 §5.3의 명시적 단순화 가정 (감사 1R #3 반영) |
| AQR "PutWrite vs BuyWrite" | aqr.com 백서 | 학술 백서 | 2017~ | 참고 — 풋라이트와 커버드콜의 등가성(풋콜 패리티). wheel의 CSP 국면과 CC 국면을 같은 엔진 코드로 다뤄도 됨을 뒷받침 |
| optopsy | github.com/goldspanlabs/optopsy (구 michaelchu) | **GPL-3.0** | 2024~25 활동 | **불채택** — GPL-3(이 레포에 코드 유입 불가), 그리고 실제 옵션체인 이력 데이터를 전제로 한다. 이 레포는 BS 근사 노선(§backtest.py 독스트링에 명시된 기존 결정)이라 데이터 전제부터 안 맞음. 델타타깃 행사가 선택·수명주기 이벤트 개념만 참고 |
| QuantConnect LEAN wheel 구현 | quantconnect.com 포럼/LEAN | Apache-2.0 | 현행 | **불채택** — 프레임워크 통째 도입은 기존 검증자산(vendor 엔진·사이저·리포터) 폐기와 같다 (`design-multi-asset-combined-backtest.md`의 vectorbt 불채택과 동일 논리). 배정(assignment) 이벤트 처리 순서(만기 → 배정 → 주식 전환 → CC 국면 전환)만 참고 |
| AlphaBot / trader | `~/dev/Auto_bot`, `~/dev/trader` | 사내 | — | 확인 — 양쪽 다 LEAP/PMCC/wheel 시뮬 없음 (AlphaBot R1-B는 이 레포 vendor의 원본, 동일 단일거래 모델) |

### ③ 결론

- **가격 프리미티브·IV·데이터 fetch·리포팅: 전부 재사용** (신규 0줄).
- **거래 수명주기 엔진: 신규 병렬 엔진** — `credit_spread_simulator.py`를 확장하지
  않는다 (§5.1에 이유). 신규 파일 1개 (`src/leap_engine.py`).
- **전략 관례: 발명하지 않고 채택** — LEAP delta 0.70~0.80 / DTE~365 / 90일 전 롤
  (tastytrade·BCI), 숏 레그 만기보유·만기일 재롤 (Cboe BXM/PUT — 롤 캘린더 근거로만).
  V1의 ITM 현금정산은 채택된 관례가 아니라 **이 설계의 단순화 가정**이다 (§5.3).
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
   미실현 — 숏 레그 주간 사이클만 실현된다. `design-multi-asset-combined-backtest.md §7`의 지적과 동일한 함정)

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
| 대상 | SPY, QQQ (bull 레짐인 종목만; 둘 다 bull이면 둘 다 — 레그 관리는 종목별 독립, 현금은 슬리브 공유 §5.2) |
| LEAP 진입 | 레짐이 bull로 **전환된 다음 거래일**, delta 0.80 콜 (`strike_for_delta`), DTE 365 |
| LEAP 사이징 | 계약당 비용 ≤ 슬리브 자본의 40% → 계약수 = `floor(0.4 * sleeve_equity / (leap_cost*100))`, 최소 1계약이 안 되면 그 종목 스킵(로그) |
| 숏 콜 진입 | LEAP 보유 중 + 숏 레그 없음 → delta 0.20 콜 매도, DTE 7, 수량 = LEAP 계약수. **단 숏 행사가 > LEAP 행사가 + 순비용**(tastytrade 75% 규칙의 취지: 콜어웨이 시나리오에서도 손실이 안 나는 행사가만). **순비용의 정의는 동적** (감사 3R P2-2): `net_cost = LEAP 진입비용(1주당) − 그 LEAP 위에서 걷은 숏콜 실현 크레딧 누계(1주당)` — LEAP 진입 시점에 고정되는 값이 아니라 주간 프리미엄이 쌓일수록 감소한다. 따라서 LEAP 수명 초기(누적 크레딧이 적을 때)엔 이 조건 미충족으로 연속 매도 스킵이 여러 주 이어질 수 있고, **그건 버그가 아니라 기대 동작**이다(로그로 추적). 조건 불충족 시 그 주는 매도 스킵(로그) |
| 숏 콜 청산 | ① 프리미엄 50% 익절 ② 프리미엄 2.0x 손절(기존 전략과 동일 상수) ③ 만기 도달 — ITM이면 **현금정산**(내재가치 지불; LEAP는 그대로 보유). **주의: 이건 ETF 옵션 현실(실물배정·숏스탁 발생·LEAP 언와인드)이 아니라 명시적 단순화 가정이다** — §5.3의 "V1 정산 근사" 참조. V1 결과는 이 근사 하의 낙관 편향 상한으로 읽는다 |
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
| CSP 진입 | SLV·TLT 각각, 레짐 bear가 **아닐 때**(bull/neutral), delta 0.25 풋 매도, DTE 7, 현금담보 100% 예치(`reserved_collateral`로 예약 — §5.2.1 원칙 4). 수량 = `available_cash`로 담보 가능한 최대 (SLV 우선, 남는 `available_cash`로 TLT) |
| CSP 만기 | OTM → 프리미엄 전액 실현, 다음 거래일 재진입. ITM → **배정**: `strike*100*qty` 지불, 주식 보유 전환 |
| CC 국면 | 주식 보유 중 → delta 0.25 콜 매도, DTE 7 (단 행사가 ≥ 배정단가 — 손실 콜어웨이 방지; ATM보다 낮으면 배정단가로 스냅). ITM 만기 → 콜어웨이(주식 매도 + 프리미엄 실현) → CSP 국면 복귀 |
| CC 국면 제한 | 배정 후 주가가 배정단가 -20% 아래로 가면 **주식 손절** (wheel의 고전적 파산 경로 — "물리면 영원히 CC" — 차단). 로그에 명시 |
| **LEAP 스윕** | 월말(매월 마지막 거래일) 종가로 판단, **다음 거래일 체결**(§5.3): SPY delta 0.70/DTE 365 콜 1계약 비용 `cost`가 `cost ≤ min(premium_bank, available_cash)` (§5.2.1 원칙 4 — bank는 실현 프리미엄 누계·이월 포함)이면 매수, cash·bank 동시 차감. 미달이면 이월 |
| LEAP 관리 | 매수한 LEAP는 **보유 전용** (숏 매도 없음 — V1과의 통제변인 분리). DTE < 90 롤, -50% 스톱, bear 전환 시 청산은 V1과 동일 |
| 리스크 상한 | LEAP 누적 비용이 슬리브 자본의 50% 도달 시 스윕 중단(현금 이월). wheel 자체는 현금담보라 레버리지 0 |

가설: *"프리미엄의 재투자처로서 LEAP(볼록성 매수)가 현금 복리를 이기는가."*
V1과 달리 숏 레그와 롱 레그가 **다른 종목**이라 상관 리스크가 낮고, 배정 메커니즘을
실제로 요구하는 유일한 변형이다 (엔진의 배정 경로 검증도 겸한다).

**기대 빈도의 정직한 추정 (감사 1R #7 반영)**: SPY delta 0.70/DTE 365 콜은 현
BS 근사로 계약당 ~$7k다. SLV(~$3k 담보)·TLT(~$9k 담보) 주간 delta 0.25 프리미엄으로
$30k 슬리브가 이걸 모으려면 **수개월~1년 이상 이월이 정상**이고, 3년 백테스트에서
스윕이 0~수 회에 그칠 수 있다. 즉 V2는 "LEAP 스윕 전략"이라기보다 **"대부분 기간
현금이 이월되는 wheel + 드문 스윕"** 으로 읽어야 하며, 스윕 0회면 순수 wheel
베이스라인으로서의 가치만 남는다 (그것대로 V1/V3 대비 통제군으로 유효). 리포트에
스윕 횟수·bank 잔고 추이를 필수 항목으로 넣어 이 추정을 실측으로 판정한다.
임계값을 낮추려고 저가 LEAP(저델타·단기)로 바꾸지 않는다 — §4의 근사 오차
논리(딥 ITM이라 IV 오차 영향이 작다)가 깨진다.
결론무효화: **부분** — 스윕이 0회면 "LEAP 재투자 > 현금 복리" 가설(V2의 존재
이유)은 검증 불가로 무효가 되지만, 순수 wheel 통제군으로서의 결론은 유지된다.

### 2.3 V3 — 전략7 스프레드 파이낸싱 LEAP 래더 (기존 챔피언과의 브릿지)

숏 레그를 네이키드 CSP가 아니라 **기존에 검증된 전략7 크레딧 스프레드 사이클**로
대체한 변형. 거래 라이프사이클(진입가·PT/SL 경로·청산)은
`backtest._generate_raw_trades`가 계산한 **raw 거래(계약당, 사이징 전)** 를 그대로 쓰고,
신규 엔진은 사이징과 LEAP 원장만 관리한다.

**사이징 명세 (감사 1R #4 반영 — 기존 사이저 재사용 아님)**:
`scale_trades_to_dollars`는 쓰지 않는다 — 그 함수는 자기 완결 잔고로 복리하는데
V3에서는 LEAP 스윕이 같은 현금을 인출하므로 그 복리 가정이 깨진다 (돈 이중 사용).
대신 `leap_engine`이 vendor raw 거래 스트림을 진입일 순으로 재생하며 자체 사이징한다:

1. `backtest._generate_raw_trades("7_atlas_mvp", df, mtm_iv, risk_pct_series)`
   (`backtest.py:312`) → raw trade 리스트 (계약당). **`run_portfolio_simulation`을
   직접 부르지 않는다 (감사 2R #3 반영)** — vendor 함수는 `candidate_signal_dates`
   리스트와 단일 `spread_type`을 받는 시그니처라 전략7 신호를 그대로 못 받고,
   전략7의 `iron_condor`를 bull_put/bear_call 양쪽으로 분해하는 것도
   `_generate_raw_trades`의 그룹핑 루프(`backtest.py:333`)다. private 헬퍼지만
   임포트 재사용만 하고 `backtest.py`는 수정하지 않는다 (§5.5의 0줄 유지).
2. 진입일 순 재생: `contracts = floor(available_cash × 0.05 / (max_loss × 100))`
   (§5.2.1 원칙 4), 0계약이면 스킵(로그). 진입 시 담보 `max_loss × 100 × q`를
   `reserved_collateral`로 예약.
3. 청산일에 `dollar_pnl = realized_pnl × 100 × q` 반영, 담보 예약 해제, 양수 실현분 bank 적립.

**V3은 전략7의 신호 생성·청산 로직만 재사용한다 (감사 2R #4 반영).** 사이징
(available_cash 고정 5%/건)과 킬스위치 미사용은 이 슬리브의 **새 선택**이지
전략7의 확장이 아니다 — 전략7 본체는 거래별 `risk_pct`(ATR% 백분위 기반 2~10%
volatility-scaled, `backtest.py:342` / `one-page-submission.md` "AI Logic" 절)와
킬스위치를 쓴다. 즉 V3은 "전략7이 LEAP 파이낸싱을 단 것"이 아니라 **관련되지만
별개인 실험**이고, 백테스트 리포트에 "raw signal/exit만 재사용, sizing·risk
gates는 별도 실험"을 명시 표기한다. 킬스위치·동적 risk_pct를 안 가져오는 이유:
슬리브는 상대비교용 백테스트고, 그 기능들이 필요해지면 그때가 사이저 통합을
다시 설계할 시점이다 (지금 하면 두 사이저의 이원화 관리).
이 재생 루프는 ~15줄이고, "V3이 신규 코드 최소"라는 이전 판정은 이만큼 **하향
수정**된다 — 그래도 배정·숏레그 자체 관리가 없어 3종 중 최소는 유지.

| 항목 | 규칙 |
|---|---|
| 숏 사이클 | SPY·QQQ·IWM에 전략7(`7_atlas_mvp`) 신호 그대로 + 기존 `run_portfolio_simulation` 파라미터 그대로 (DTE 7, delta 0.20, width 1.5%, PT 50%, SL 2.0x). **전략7이 내는 3종 구조(bull_put·bear_call·iron_condor) 전부 그대로 거래한다** — bull_put만 골라내지 않는다 (감사 1R #8 반영: 변형 이름의 "Bull-Put 파이낸싱"은 대표 구조 지칭이었으나 부정확해 아래처럼 정정) |
| **funding** | **세 구조 모두의** 실현 프리미엄 중 양수 실현분이 `premium_bank`에 적립 (§5.2.1 — bank는 cash의 하위 원장) → 매주 금요일 종가 판단, **다음 거래일 체결**(§5.3): SPY 또는 QQQ delta 0.70/DTE 365 콜 1계약 비용 `cost ≤ min(premium_bank, available_cash)` (§5.2.1 원칙 4) && 해당 종목 bull 레짐이면 매수 (레짐 게이트 — 하락장에서 LEAP를 사 모으는 자살 방지) |
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
| V3 | 전략7 스프레드 3종 (기존 검증) | SPY/QQQ | 불필요 | 검증된 수확기 + LEAP 되먹임 |

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
결론무효화: **아니오** — 결론이 절대 수익이 아니라 변형 간 상대비교이고, 세 변형이
같은 IV 근사를 공유하므로 오차가 비교 방향을 뒤집을 경로가 좁다 (딥 ITM 한정 조건 하).

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
   다른 수명주기 엔진"이 유지된다. V3의 숏 사이클은 `backtest._generate_raw_trades`
   경유로 vendor 엔진을 통째 재사용한다 (per-cycle로는 vendor의 가정이 전부
   성립하므로 — 호출 명세는 §2.3).

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
class SleeveBook:           # 슬리브당 1개 — 돈은 슬리브 수준에서 공유 (감사 3R P2-1)
    cash: float             # 슬리브 유일의 현금 (담보 예치 포함, §5.2.1 원칙 1)
    premium_bank: float     # funding 스윕 대기 프리미엄 (V2/V3) — cash의 하위 원장
    reserved_collateral: float  # §5.2.1 원칙 4
    legs: dict[str, list[OptionLeg]]   # symbol → 레그 목록
    shares: dict[str, int]             # symbol → wheel 배정 주식 (V2만 사용)
    share_cost_basis: dict[str, float] # symbol → 배정단가
    realized: list[dict]    # 실현 이벤트 로그 (아래 스키마 — symbol 필드 포함)
    equity_curve: dict[pd.Timestamp, float]  # 매일 슬리브 전체 MTM

# 실현 이벤트 스키마 — 기존 리포팅과의 접점
# {entry_date, exit_date, symbol, kind: "short_cycle"|"leap_roll"|"leap_close"|
#  "assignment"|"called_away"|"share_stop", dollar_pnl: float, detail: {...}}
```

**북은 슬리브당 1개, 종목당이 아니다 (감사 3R P2-1 반영)**: 이전 판의
`Book(symbol, cash, ...)` per-symbol 모델은 틀렸다 — V2(SLV/TLT wheel → SPY LEAP
스윕)와 V3(SPY/QQQ/IWM 스프레드 → SPY/QQQ LEAP 스윕)은 숏 레그가 번 프리미엄이
**다른 종목**의 LEAP 매수로 흐르고, V1도 "슬리브 합산 80% 상한"·"남는
`available_cash`로 TLT" 같은 규칙이 전부 슬리브 공유 현금을 전제한다. 종목별
독립 지갑으로 구현하면 이 흐름이 전부 끊긴다. 따라서 `cash`/`premium_bank`/
`reserved_collateral`/`available_cash`는 슬리브 수준 단일 값이고, 종목별로 갈리는
것은 `legs`/`shares`/`share_cost_basis` **뿐**이다. Codex 구현 시 per-symbol
지갑을 만들지 말 것. (§2.1의 "각각 독립 북"은 레그 관리가 종목별 독립이라는
뜻으로 정정 — 현금은 공유.)

`dollar_pnl`이 **이미 달러**라는 게 기존 raw trade dict와의 결정적 차이다 —
사이징이 엔진 안(슬리브 북의 현금)에서 일어나므로 사후 스케일링이 없다 (§5.4).

### 5.2.1 회계 모델 (감사 1R #1 반영 — 현금 흐름 시점의 완전 명세)

원칙 3개:

1. **`cash`가 유일한 돈이다.** `premium_bank`는 **cash의 하위 원장(sub-ledger)**,
   즉 "cash 중 스윕 판정에 쓸 수 있는 몫"을 추적하는 회계 태그일 뿐 별도의
   지출 가능 현금이 아니다. 스윕으로 LEAP를 사면 `cash`와 `premium_bank`를
   **동시에** 차감한다 — 돈이 두 번 쓰일 경로가 없다. `premium_bank ≤ cash`가
   불변식이고, 자산 항등식에는 `premium_bank`가 **등장하지 않는다**.
2. **옵션 현금은 진입 시점에 움직인다.** 숏 매도 → 진입일에
   `cash += credit × 100 × qty` (haircut 적용 후). 롱 매수 → 진입일에
   `cash -= price × 100 × qty`. 만기/청산은 청산 현금 흐름
   (`cash -= close_debit × 100 × qty` 등)만 일으킨다 — "만기 때 프리미엄 실현"은
   현금 이벤트가 아니라 **realized 이벤트 로그의 분류**다.
3. **레그 MTM은 부호 있는 자산가치다.** 롱 레그 MTM = `+price × 100 × qty`,
   숏 레그 MTM = `−price × 100 × |qty|` (부채). 자산 항등식:

   ```
   equity(d) = cash + Σ signed_leg_mtm(d) + shares × close(d)
   ```

   숏 매도 직후를 검산하면: cash가 credit만큼 늘고 숏 레그 MTM이 −credit이라
   equity 불변 (거래는 부를 만들지 않는다) — 이 상쇄가 항등식이 닫혀 있다는
   증명의 핵심이고, 테스트 1(§8)이 매 이벤트 직후 이걸 assert한다.

4. **`available_cash`가 지출 판정의 유일한 기준이다 (감사 2R #2 반영).** cash에는
   담보로 묶인 몫과 pending 큐에 이미 배정된 몫이 섞여 있으므로, 원시 `cash`를
   임계값과 비교하면 담보와 스윕이 같은 돈을 두 번 쓸 수 있다. 정의:

   ```
   reserved_collateral = Σ CSP 현금담보(strike×100×q, V2)
                       + Σ 스프레드 담보(max_loss×100×q, V3)
   pending_debits      = pending 큐에 적재된 매수/진입 주문의 예상 비용 합
   available_cash      = cash − reserved_collateral − pending_debits
   ```

   모든 지출 판정은 `available_cash` 기준: CSP 담보 예치 가능 수량(V2),
   LEAP 스윕 `cost ≤ min(premium_bank, available_cash)` (V2 월말·V3 금요일 공통),
   V3 스프레드 사이징 `contracts = floor(available_cash × 0.05 / (max_loss×100))`.
   불변식: `available_cash ≥ 0` (담보 예치·pending 적재 직후 포함) — 테스트 1에서
   `premium_bank ≤ cash`와 함께 assert. 담보는 별도 계좌가 아니라 cash 안의
   예약 태그이므로 자산 항등식(§5.2.1 원칙 3)에는 여전히 등장하지 않는다.

이벤트별 원장 분개 (전부 이 표가 정본, 루프 의사코드는 이 표를 따른다):

| 이벤트 | cash | premium_bank | legs / shares |
|---|---|---|---|
| 숏 옵션 매도 (진입) | `+credit×100×q` | 변동 없음 (아직 미실현) | 숏 레그 추가 |
| 숏 옵션 청산/만기 OTM | `−close_debit×100×q` (OTM 만기면 0) | `+net_realized` (양수일 때만; V2/V3) | 숏 레그 제거 |
| 숏 옵션 만기 ITM (V1 현금정산) | `−내재가치×100×q` | net 실현이 양수면 `+` | 숏 레그 제거, LEAP 불변 |
| CSP 배정 (V2) | `−strike×100×q` | 변동 없음 | 풋 제거, `shares += 100q` |
| 콜어웨이 (V2) | `+strike×100×q` | 주식+프리미엄 net 실현 양수분 `+` | 콜 제거, `shares −= 100q` |
| LEAP 매수 (신규/스윕) | `−cost×100×q` | 스윕이면 **동시 차감** `−cost×100×q` | LEAP 추가 |
| LEAP 청산/롤 청산분 | `+price×100×q` | 변동 없음 | LEAP 제거 |
| 주식 손절 (V2) | `+px×100q` | 변동 없음 | shares 0 |

`premium_bank`에는 **실현 net이 양수인 사이클의 실현분만** 적립한다 (손실 사이클은
bank를 깎지 않는다 — bank는 "스윕 자격 판정용 누계"이지 손익계산서가 아니다.
손실은 cash에 이미 반영돼 있고, `premium_bank ≤ cash` 클램프가 과대적립을 막는다).

**스윕 체결 시 바닥 클램프 (감사 3R P3-1)**: 스윕 사이징은 DECIDE일 종가 기준인데
체결은 다음 거래일 종가라(§5.3) 갭 상승 시 `actual_cost > 판단일 cost`가 될 수
있다. 체결 시 `premium_bank = max(0.0, premium_bank − actual_cost)`로 차감해
bank 음수 진입을 막는다 (cash는 실제 지출 그대로 차감 — 항등식 불변, 바닥은
하위 원장인 bank에만 적용).

### 5.3 이벤트 루프

일자(daily bar)가 바깥 루프. 하루 안의 처리 순서를 고정한다 (순서가 결과를 바꾸므로
명세가 필요한 지점):

**타이밍 원칙 (감사 1R #2 · 2R #1 반영)**: 그날 종가로 **판단**한 것은 전부
**다음 거래일에 체결**한다 — vendor 엔진이 signal_date 다음 거래일에 진입하는
관례(`credit_spread_simulator.py:397`)와 동일. 종가를 보고 그 종가에 진입하는
경로는 루프 구조상 존재하지 않는다: 진입·스윕 판단은 `pending` 큐에 넣고 다음
거래일 스텝 1에서 그날 종가로 체결한다.

**IV 결측 처리 (2026-08-27 구현 리뷰 P1/P2 반영)**: MTM/equity 기록은 매 거래일
남겨야 한다(§1 현금 보존식, §5.2 `equity_curve` 정의). 따라서 특정 거래일의
IV가 비어 있으면 그 시점 **이전의 마지막 IV를 forward-fill**해 MTM·자산항등식·
만기일 주변 continuity 검산에만 사용한다. 이는 과거값이라 미래정보 누출이 아니며,
금지되는 것은 미래 IV(`iloc[-1]` 같은 시리즈 꼬리값)를 결측일에 끌어오는 것이다.
반대로 신규 진입·스윕 판단, fresh 가격터치형 PT/SL trigger, V3 spread entry
gate처럼 "그날 옵션가"가 필요한 판단은 **fresh IV가 있는 심볼에만** 수행한다.
IV 결측은 심볼 단위로 격리한다: SPY IV 결측이 SLV/TLT의 만기 정산·MTM·fresh IV가
있는 주문 실행을 밀어서는 안 된다. 결측 사실은 `iv_coverage_skip` 이벤트로 남기되
그날 전체 루프를 중단하지 않는다.

청산은 두 클래스로 나뉜다 (감사 2R #1):

- **가격 터치형 (PT/SL)**: 당일 종가 판정·당일 종가 체결. vendor `simulate_trade`의
  세션 루프가 종가 MTM으로 PT/SL을 판정하고 그 종가를 청산가로 쓰는 관례
  (`credit_spread_simulator.py:309-331`)와 동일 — 판정과 체결이 같은 종가라
  룩어헤드가 아니고, 기존 10전략 백테스트와의 비교 가능성을 유지한다.
- **전략 판단형 (레짐 bear 전환 청산, LEAP -50% 스톱, V2 주식 -20% 손절)**:
  종가로 확인한 조건에 근거한 재량적 청산 결정이므로, "확인한 그 종가에 체결"은
  MOC 사전 제출 없이는 라이브에서 불가능한 체결이다. 진입과 동일하게
  **DECIDE(당일 종가 판단) → pending → 다음 거래일 EXECUTE(그날 종가 체결)** 로
  보낸다. 갭 하락 하루치 노출이 추가되는 게 정직한 모델이고, -50%/-20% 스톱은
  체결일 가격 기준으로 실제 손실이 임계보다 깊을 수 있다 — 리포트에 트리거일
  대비 체결일 슬리피지를 로그로 남긴다.

```
for d in trading_days:                        # SleeveBook 1개, 종목은 내부 순회 (§5.2)
    1. EXECUTE  — 전일 종가 기준으로 pending에 쌓인 주문을 오늘 종가로 체결:
                  LEAP 신규/롤 재진입, 숏 레그 매도, CSP/CC 진입, funding 스윕 매수
                  (체결가는 오늘 종가 기준 BS — 판단일 정보만 쓰고 체결일 가격을 씀)
    2. EXPIRY   — 오늘 만기 레그 정산 (만기는 판단이 아니라 계약 조건이므로 당일):
                  숏콜 OTM → 만기 소멸 (close_debit 0) / ITM →
                    (V1, LEAP 담보) 내재가치 현금정산, LEAP 유지 (§5.3 "V1 정산 근사")
                    (V2, 주식 담보) 콜어웨이: 주식 strike 매도 + 콜 소멸
                  CSP OTM → 소멸 / ITM → 배정: cash -= strike*100*qty,
                    shares += 100*qty, cost_basis = strike - premium
    3. TRIGGER  — 가격 터치형 청산만 (fresh IV가 있는 심볼의 그날 종가 MTM 기준, **당일 체결**):
                  숏 레그 50% PT / 2.0x SL
                  (vendor `simulate_trade`의 close-to-close 관례와 동일,
                  `credit_spread_simulator.py:309-331` — 판정·체결이 같은 종가)
    4. DECIDE   — 오늘 종가 기준 **판단**만 → pending 큐 적재 (다음 거래일 체결):
                  LEAP DTE<90 롤, 변형별 진입 규칙 (§2),
                  전략 판단형 청산 — LEAP -50% 스톱, 레짐 bear 전환 →
                  LEAP+숏 동시 청산, V2 주식 -20% 손절 (감사 2R #1),
                  funding 스윕 판정 (V2 월말 / V3 금요일):
                  cost ≤ min(bank, available_cash) && 레짐 확인
    5. MTM      — equity = cash + Σ signed_leg_mtm + shares × close (§5.2.1)
                  (IV 결측 심볼은 과거 IV forward-fill, 미래 IV 사용 금지)
                  equity_curve[d] 기록 + 자산 항등식 assert
```

- **배정은 만기 시점만** (유럽형 근사). 미국식 조기배정(배당락 전 딥 ITM 콜)은
  모델링하지 않는다. **"V1 정산 근사" (감사 1R #3 반영)**: V1의 ITM 숏콜
  내재가치 현금정산은 유럽형 현금결제를 가정한 **명시적 단순화**다. 실제 SPY/QQQ
  옵션은 미국식·실물결제라 ITM 만기는 숏스탁을 만들고, 청산하려면 LEAP 일부
  언와인드 또는 주식 매수가 필요하며, 배당락 전 조기배정 리스크도 있다 — 이
  마찰비용(스프레드·언와인드 슬리피지·조기배정의 잔여 시간가치 상실)을 전부
  생략하므로 **V1 결과는 낙관 편향 상한(upper bound)으로 읽어야 한다**. 이건
  Cboe BXM이 뒷받침하는 관례가 아니다 (BXM은 SPX 현금결제 인덱스 방법론).
  리포트 헤더에 이 가정을 명시한다.
  결론무효화 (V1 현금정산): **부분** — V1의 절대 수치는 상한이라 단독으로는 결론
  근거가 못 되지만, "상한조차 기존 챔피언에 못 미치면 탈락"이라는 부정 방향
  판정에는 그대로 유효하다.
  결론무효화 (조기배정 제외): **아니오** — 생략된 마찰의 방향이 알려져 있고(낙관),
  위 상한 해석에 이미 포섭된다.
- 같은 날 PT·SL 동시 도달 시 **SL 우선** (`design-multi-asset-combined-backtest.md §4.2`와 동일한 보수 규칙).
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
결론무효화: **아니오** — 고정 30/70 분할은 슬리브 **내부** 비교(V1/V2/V3 상호,
그리고 동일 $30k 기준 지표)의 유효성을 건드리지 않는다.

### 5.5 기존 플로우 접속점

```python
# src/leap_backtest.py (신규, 오케스트레이터 — backtest.py는 수정하지 않음)
def run_leap_family(variant: str, years: int = 3) -> StrategyResult:
    # 1. fetch_daily_bars / _rolling_iv_series / 레짐 시리즈 (전부 기존 함수 임포트)
    # 2. SleeveBook 초기화 (슬리브 자본 $30k 단일 cash — 종목별 분배 없음, §5.2)
    # 3. leap_engine.run(books, bars, iv, regime, variant_rules)
    # 4. realized 이벤트 → 경량 어댑터(dollar_pnl 속성만 있는 namedtuple)로 감싸
    #    _metrics_from_dollar_trades(...) 재사용 → StrategyResult
    # 5. 리포트 추가 항목: 7일 창 실현 P&L 분포, premium_bank 이벤트 로그,
    #    배정/콜어웨이 횟수, 숏 매도 스킵 횟수(V1의 75% 규칙), LEAP 롤 이력
```

`backtest.py`·`portfolio.py`·`vendor/` **수정 0줄**.

**어댑터 명세 (감사 1R #5 반영)**: 이전 판의 "`dt.dollar_pnl`만 읽는다"는
**틀렸다** — `_metrics_from_dollar_trades(dollar_trades, equity_series, n_years)`는
`equity_series`와 모듈 상수 `STARTING_EQUITY`($100k)도 쓴다
(`backtest.py:248,252,256-259`). 코드를 실제로 읽고 확정한 재사용 조건:

1. `equity_series` = **슬리브 자체의 `equity_curve`** ($30k 시작)를 반드시 넘긴다.
   비우면 안 된다 — 빈 시리즈 폴백(`np.cumsum(pnls) + STARTING_EQUITY`,
   `backtest.py:252`)이 $100k 기준 곡선을 만들어 max_drawdown·final_equity가 오염된다.
2. `n_years` = 백테스트 실제 연수.
3. `STARTING_EQUITY`($100k)가 남는 자리는 sharpe(`pnls/SE`의 mean/std **비율** —
   SE 소거)와 calmar(`cagr_like/mdd_pct` = `total_pnl/(n_years×max_dd)` — SE 소거)
   뿐이라 **수치가 스케일 불변으로 옳다**. max_drawdown·final_equity는 1의
   equity_series에서 오고, total_pnl·win_rate·profit_factor는 SE 무관.

즉 조건 1을 지키는 한 함수 수정 없이 재사용 가능하고, 이 소거 논리는
`tests/test_leap_engine.py`에 주석으로 남긴다 (STARTING_EQUITY가 향후 sharpe
공식 변경 등으로 소거가 깨지면 이 재사용도 깨진다 — 그때는 자체 metrics 함수로 대체).

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

- **V1/V3의 대회 적합은 결론이 아니라 검증할 가설이다** (감사 1R #6 반영):
  주간 실현 사이클이 있긴 하지만, 슬리브의 30~80%가 LEAP 비용으로 묶이면 7일
  창에서 실현 P&L을 만드는 가용 자본이 기존 챔피언(전략7 combined, $100k 전액
  가동)보다 **작다**. 따라서 대회 적합 판정은 "임의 7일 rolling 창의 실현 P&L
  분포가 기존 챔피언 분포를 이길 때만" 성립하는 조건부 명제고, 그 비교가
  백테스트 리포트의 필수 항목이다. 이기지 못하면 이 계열 전체가 대회 제출이
  아니라 운용 연구다. V2는 배정 주기까지 끼어 실현이 더 불규칙 — 처음부터
  운용 연구 성격.
  결론무효화: **부분** — 분포 비교에서 지면 "대회 제출" 결론만 무효가 되고,
  운용 연구로서의 결론(가설 1·2의 답)은 유지된다.
- 창의성 점수 서사는 V3이 최강: "검증된 프리미엄 수확기가 자기 수확물로 장기
  볼록성을 사 모은다"는 한 문장이 된다.
- 백테스트 리포트의 **7일 창 실현 P&L 분포**(루브릭 항목)가 대회 투입 여부의
  최종 판정 기준. 기존 챔피언(전략7 combined)보다 분포가 나쁘면 대회에는 안 넣고
  운용 연구로 성격 전환.

---

## 8. 검증 계획

`tests/test_leap_engine.py` — 기존 pytest, 프레임워크 추가 없음. 전부 합성 가격
데이터 (API 불필요).

1. **현금 보존식 (게이트)**: 매 스텝 `equity == cash + Σsigned_leg_mtm + shares*px`
   (§5.2.1)가 전 구간 성립. 진입·청산·배정·롤·스윕 각 이벤트 직후 검증. 특히
   숏 매도 직후 equity 불변(현금 유입 = 부채 MTM), 스윕 직후
   `premium_bank ≤ cash` 불변식, 담보 예치·pending 적재·스윕 직후
   `available_cash ≥ 0` 불변식(§5.2.1 원칙 4)도 함께 assert. V3 spread entry도
   실제 `decide_v3_spread_financing → _execute_spread_entry` 경로에서 같은 gate를
   통과해야 하며, raw credit이 같은 날 theoretical close debit보다 큰 불가능한
   입력은 assert로 거부한다(credit haircut 손실만 허용, 부의 창조 금지).
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
   IV 결측일 MTM은 그날 이전의 마지막 IV만 forward-fill하며, 미래 IV를 쓰지 않는
   것을 별도 회귀 테스트로 고정한다. 결측일 realized 이벤트는 rolling 7일 실현
   P&L 창에서 누락되면 안 된다.
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

## 감사 라운드 이력

### 1라운드 (2026-08-26, codex exec gpt-5.5, 판정: NOT CLEAN — P1 6건, P2 3건)

각 지적은 코드 원문 대조로 재검증한 뒤 판정했다 (감사를 그대로 믿지 않음).

| # | 지적 요지 | 판정 | 근거·반영 |
|---|---|---|---|
| 1 (P1) | cash/premium_bank 분리 불명확, 진입 현금흐름 미명세 — 돈 이중 사용 가능 | **수용** | 사실 — 이전 판은 "만기 때 프리미엄 실현"만 말해 진입 현금 유입이 없는 회계였다. §5.2.1 신설: cash 단일 원장, bank는 하위 원장(항등식 비등장, `bank ≤ cash` 불변식, 스윕 시 동시 차감), 이벤트별 분개 표, 숏 매도 직후 equity 불변 검산. 테스트 1에 반영 |
| 2 (P1) | 종가 판단 → 같은 날 진입은 룩어헤드; vendor는 next-day entry (`credit_spread_simulator.py:397`) | **수용** | 코드 확인 — vendor는 `index > signal_date`로 다음 거래일 진입. §5.3 루프를 EXECUTE(pending 체결)/DECIDE(판단→큐) 분리로 재구성: 판단은 종가, 체결은 다음 거래일 종가. PT/SL은 vendor 관례대로 당일 체결(판정·체결이 같은 종가라 룩어헤드 아님) |
| 3 (P1) | V1 ITM 현금정산의 Cboe BXM 인용은 SPX 현금결제 방법론 — ETF 실물배정 근거 아님 | **수용** | 사실 — BXM/PUT은 SPX 인덱스(유럽형·현금결제). 실물배정 모델링 대신 **명시적 단순화 가정** 노선 채택: §5.3 "V1 정산 근사"에 낙관 편향 상한임을 명기, 선행조사 ②·③의 Cboe 인용 범위를 롤 캘린더로 축소, V1 표에도 경고 |
| 4 (P1) | V3 "vendor 엔진 그대로 호출"은 함수 시그니처·사이징 소유권과 충돌 | **수용** | 코드 확인 — `run_portfolio_simulation`은 신호 리스트 전체를 받아 내부 상태 관리, 사이징은 `scale_trades_to_dollars` 소관인데 그 사이저는 안 쓰기로 했으니 공백이었다. §2.3에 명세 확정: vendor는 raw 거래 스트림 생성까지만 재사용, 사이징은 leap_engine 자체 재생 루프(cash 5%/건, 담보 예치·해제) — `scale_trades_to_dollars` 재사용은 LEAP 스윕이 같은 현금을 빼가는 순간 그 함수의 복리 가정이 깨져 기각. "V3 신규 코드 최소" 주장 하향 |
| 5 (P1) | `_metrics_from_dollar_trades`가 dollar_pnl만 읽는다는 주장 오류 — equity_series·STARTING_EQUITY·n_years 사용 | **부분 수용** | 지적의 사실관계는 맞다 (`backtest.py:248-264` 확인, 이전 판 주장 철회). 단 "$30k를 넣으면 $100k 지표와 섞인다"는 절반만 맞다: sharpe·calmar에서 STARTING_EQUITY는 비율로 **소거**되고, max_dd·final_equity는 호출자가 주는 equity_series에서 나온다. §5.5에 정확한 재사용 조건 명세 (슬리브 equity_curve 필수 전달 — 빈 시리즈 폴백이 유일한 오염 경로) |
| 6 (P1) | V1/V3 "대회 적합" 단정 과함 — LEAP가 자본을 묶어 7일 실현 P&L 생산력이 기존보다 줄 수 있음 | **수용** | `one-page-submission.md:25,42` 확인 (realized P&L 채점, weekly DTE 전환 이유). §7을 조건부 가설로 강등: "7일 rolling 실현 P&L 분포가 기존 챔피언을 이길 때만 적합" |
| 7 (P2) | V2 스윕이 규모상 거의 발동 안 할 수 있음 (SPY 0.70 LEAP ~$7k vs SLV/TLT 월 프리미엄) | **수용** | 산수 타당. §2.2에 기대 빈도 추정 명기: 스윕 0~수 회가 정상, 미발동 시 순수 wheel 통제군으로 재해석. 임계 완화(저델타 LEAP)는 §4 근사 논리 훼손이라 기각 |
| 8 (P2) | 전략7은 bear_call·iron_condor도 냄 — V3 "Bull-Put"은 부정확, 그 프리미엄의 bank 귀속 미명세 | **수용** | `strategies.py:158-164` 확인. V3 명칭을 "전략7 스프레드 파이낸싱"으로 정정, 3종 구조 전부 거래·전부 bank 적립 명시 |
| 9 (P2) | doc-meta 부재 등 lint WARN 13건 | **수용** | doc-meta round=1 추가, 이 섹션 제목에 라운드 토큰, 선행조사 라벨 3종 추가. C3 "§ 반복 언급" WARN은 상호참조가 문서 가독성에 필요해 잔류 허용 (ERROR 아님) |

### 2라운드 (2026-08-26, codex exec gpt-5.5, 판정: NOT CLEAN — P1 2건, P2 3건)

각 지적은 코드·문서 원문 대조로 재검증한 뒤 판정했다.

| # | 지적 요지 | 판정 | 근거·반영 |
|---|---|---|---|
| 1 (P1) | TRIGGER가 PT/SL(vendor 관례)과 전략 판단형 exit(bear 전환·LEAP -50%·주식 -20%)을 전부 "종가 확인 후 같은 종가 체결"로 묶음 — 후자는 라이브 불가능 체결 | **수용** | `credit_spread_simulator.py:309-331` 확인 — vendor의 same-close는 PT/SL 가격 터치에만 존재. §5.3을 두 클래스로 분리: PT/SL은 close-to-close 유지(vendor 호환·비교 가능성), 전략 판단형 3종은 DECIDE→다음 거래일 EXECUTE로 이동(진입과 동일 규율). 갭 노출 하루 추가가 정직한 모델 — 트리거일 대비 체결일 슬리피지 로그 요구 추가 |
| 2 (P1) | 담보/예약 현금 명세 부재 — bank와 담보가 같은 cash를 동시에 쓸 수 있음 | **수용** | 사실 — §5.2.1은 `bank ≤ cash`만 있었고 담보는 V3 본문의 산문 한 줄뿐. §5.2.1 원칙 4 신설: `reserved_collateral`·`pending_debits`·`available_cash` 정의, 모든 지출 판정(V2 CSP 수량·V2/V3 스윕·V3 사이징)을 `available_cash` 기준으로 재기술, 스윕 조건 `cost ≤ min(premium_bank, available_cash)` 고정, `available_cash ≥ 0` 불변식을 테스트 1에 추가 |
| 3 (P2) | V3의 `run_portfolio_simulation(전략7 신호, ...)` 호출 명세가 실제 시그니처(`candidate_signal_dates`+단일 `spread_type`)와 불일치, iron_condor 분해는 backtest 헬퍼 소관 | **수용** | `credit_spread_simulator.py:360-374`·`backtest.py:312-344` 확인 — iron_condor→bull_put/bear_call 분해와 그룹핑은 `_generate_raw_trades`(backtest.py:333)가 한다. §2.3 스텝 1을 `backtest._generate_raw_trades("7_atlas_mvp", df, mtm_iv, risk_pct_series)` 재사용으로 확정 (private 헬퍼지만 임포트만, backtest.py 수정 0줄 유지), §5.1의 서술도 정정 |
| 4 (P2) | V3 고정 5% 사이징·킬스위치 미사용은 전략7(risk_pct 2~10% volatility-scaled + 킬스위치)과 다른 전략 — "기존 챔피언의 자연 확장" 서사 과장 | **수용** | `backtest.py:342`·`one-page-submission.md:22` 확인. §2.3에 명시: V3은 signal/exit만 재사용, sizing·risk gates는 이 슬리브의 새 선택 — "관련되지만 별개인 실험"이며 리포트에 이 분리를 표기하도록 요구 |
| 5 (P2) | 1R에서 추가된 Limitations/가정 항목들에 `결론무효화` 판정 라벨 부재 | **수용** | 각 항목에 자체 판정으로 라벨 부여 (감사 제안 라벨은 참고만, 전부 재판단): IV 근사(§4)=**아니오**(상대비교+딥 ITM 한정), V1 현금정산(§5.3)=**부분**(절대 수치 무효·상한 판정 유효), 조기배정 제외(§5.3)=**아니오**(방향 기지·상한에 포섭), V2 스윕 저빈도(§2.2)=**부분**(재투자 가설 무효·통제군 유지), 슬리브 경합 제외(§5.4)=**아니오**(슬리브 내부 비교 무영향), 대회 적합 조건부(§7)=**부분**(제출 결론만 무효) — 감사 제안과 결과적으로 일치하나 §5.4를 명시 추가 판정 |

### 3라운드 (2026-08-26, Gemini/Antigravity — `degraded_substitution: codex_unavailable→gemini`, 판정: CLEAN — P0 0건, P1 0건)

실 Codex CLI가 쿼터 소진(리셋 ~2026-08-27 01:09 PDT)이라 랩 머신의 Gemini/
Antigravity로 대체 수행 — 문서 전문 + 인용 코드 발췌를 인라인으로 넘긴 자체완결
리뷰(감사자 측 라이브 파일시스템 접근 없음). **이 CLEAN은 대체 감사자 판정이므로
Tier 2+ 필수 Codex 감사 요건을 단독으로 충족하지 못한다 — Codex 구현 핸드오프 전
실제 `codex exec` 감사 1라운드 재실행 필수** (문서 상단 경고 참조).

부수 지적 P2 2건·P3 2건 — 전부 수용·반영:

| # | 지적 요지 | 판정 | 근거·반영 |
|---|---|---|---|
| 1 (P2) | §5.2 `Book`이 per-symbol인데 V2/V3(과 V1의 슬리브 합산 규칙)은 슬리브 공유 cash/bank를 전제 — 종목별 독립 지갑으로 오구현될 위험 | **수용** | 사실 — 스윕은 다른 종목으로 돈이 흐른다. §5.2를 `SleeveBook`(슬리브 단일 cash/premium_bank/reserved_collateral + 종목별 legs/shares dict)으로 재정의, §2.1·§5.3·§5.5의 per-symbol 북 서술 정정 |
| 2 (P2) | V1 "숏 행사가 > LEAP 행사가 + 순비용"의 순비용이 정적 값으로 오독 가능 | **수용** | §2.1에 동적 정의 명기: `net_cost = LEAP 진입비용 − 숏콜 실현 크레딧 누계` (수명 따라 감소), 초기 연속 매도 스킵은 기대 동작임을 명시 |
| 3 (P3) | 스윕 체결일 갭 상승 시 premium_bank 음수 가능 | **수용** | §5.2.1에 바닥 클램프 명기: `premium_bank = max(0.0, premium_bank − actual_cost)` (cash는 실지출 차감 — 항등식 불변) |
| 4 (P3) | doc-meta round=2 미갱신 | **수용** | round=3으로 갱신 |

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
