# 설계: 마찰 보정(friction-calibrated) 옵션 백테스트 하네스 (2026-09-30)

- 상태: 설계 초안 (Tier 2, 구현 전). 브랜치 `friction-backtest`
- 범위: **다음 레이어만**이다. 라이브 통계를 재현하는 하네스와 그 보정 절차, 그리고 보정을 통과한 뒤 미리 등록한 수정안을 비교하는 절차까지만 다룬다. 라이브 코드(`src/mcp_runner.py`, `src/signals.py`)의 수정, launchd, registry는 이 문서의 범위가 아니다.
- 이 하네스가 판단 근거를 대는 결정: **어떤 수정안 묶음을 라이브 Task Contract(페이퍼)로 올릴지, 아니면 옵션 사이드를 `OPTIONS_CLOSE_ONLY`(커밋 5228d90) 상태로 둘지.** 자세한 판정 규칙은 설계 6에 있다.

## 배경 (확정 사실과 가설 구분)

확정 사실(코드와 로그로 확인함):
- 백테스트 엔진은 BS 가격 + realized-vol IV로 돈다. 청산 판정은 BS 모델가의 `close_debit`로만 한다: `src/vendor/credit_spread_simulator.py:169-195`(`price_spread_to_close`), `:324-331`(PT/SL 판정), `src/backtest.py:110-186`(15분봉 재평가도 같은 BS 모델가, `:164-174`). 호가 스프레드, 시장가 청산 슬리피지, 마크 노이즈 항은 하나도 없다. 제출 문서도 이것을 "upper bound"라고 이미 적어 두었다(`docs/one-page-submission.md:32`).
- 라이브 손절은 Alpaca 포지션의 `unrealized_pl` 합을 수취 크레딧으로 나눈 값을 쓴다(`src/signals.py:624-635`). 트리거는 `profit_pct <= -(2.0-1.0)`이다(`:634`). 청산 주문은 기본이 market mleg이다(`src/signals.py:689-696`, `src/mcp_runner.py:490-496`). limit은 "reenter with a limit"으로 거부됐을 때만 폴백으로 낸다(`src/mcp_runner.py:502-531`, 가격은 mid+5% `src/signals.py:703-720`).
- 진입 크레딧은 `max(0.01, mid_credit*(1-0.05))`이다(`src/signals.py:544`, `:575`). 최소 크레딧 필터나 크레딧/폭 필터는 없다. 계약수는 `width*100` 최대손실 기준이다(`:568-569`, `:417-420`). 백테스트 사이징은 `max_loss=(width-credit)` 기준이고(`src/portfolio.py:175`), 동시 포지션 간에 리스크 예약이 없다(`src/portfolio.py:17-19` ponytail 주석). 라이브에도 포트폴리오 총위험 상한은 없다(종목당 1개 가드뿐, `src/mcp_runner.py:561-575`).
- `registry/decisions.jsonl`의 진입 레코드에는 브로커가 거부한 주문도 `submitted=true`로 섞여 있다. 예: 2026-08-25 SPY 콘도르의 403 "uncovered". 청산 레코드의 `order_result`는 제출 직후 응답이라 `filled_at=None`이고 **체결가가 없다**. 로그에 있는 손익 수치는 트리거 시점 마크 기준 `profit_pct`뿐이다(`src/mcp_runner.py:540-544`).
- 설치된 alpaca-py 0.44.0의 `OptionHistoricalDataClient`에는 `get_option_bars`, `get_option_trades`, `get_option_latest_quote`, `get_option_latest_trade`, `get_option_snapshot`, `get_option_chain`만 있다. **과거 호가(NBBO) 이력 메서드는 없다**(`.venv/.../alpaca/data/historical/option.py:74-204`). Feed는 `opra`(구독 필요)와 `indicative` 두 가지다(`alpaca/data/enums.py:103-115`).

가설(요청자의 사후분석이며 이 문서는 검증하지 않음): 손절의 주원인은 호가/마크 노이즈와 시장가 교차 비용이다. 크레딧/폭이 얇을수록 손실 배수가 커진다. trend_up 불풋이 손실의 76%를 차지한다. 라이브 101건의 통계(승률 약 33%, 손절 트리거 평균 -295%, 1시간 내 손절 23/66)는 진입 크레딧을 지정가로 가정한 로그 매칭 결과이고 **브로커 체결가로는 미검증**이다.

## 선행조사

### ① 레포 내 검색
레포 내 검색: 다음 도구로 찾았고 결과는 아래와 같다.
- `rg -il "bid.?ask|slippage|friction|half.?spread|NBBO|OptionHistoricalDataClient|get_option_bars|get_option_trades"`를 worktree에서 실행했다(reports/ 제외).
  - 결과: `src/signals.py`(라이브 mid 계산 `:291-314`), `src/mcp_runner.py`, `docs/variant-3-index-liquidity-focus.md:16`(지수 ETF는 스프레드가 좁다는 서술만 있음), `docs/one-page-submission.md:32`(BS 이론가, 스프레드 없음을 고지), `docs/design-multi-asset-combined-backtest.md:181`(주식/크립토용 `_apply_slippage` 인용).
  - **옵션 호가 마찰 모델은 없다.**
- 지식그래프 MCP `cross_search_symbols`로 `slippage`, `bid_ask`, `spread_cost`, `price_spread_to_close`, `fetch_live_spread_quote`를 조회했다.
  - trader의 `_apply_slippage`(`scripts/backtest/*`, `src/core/casper_backtest_engine.py:165`): bps 단위 주식/크립토 슬리피지라 옵션 레그 호가에는 맞지 않는다. **불채택.**
  - alphabot `backtest/live_replay.py`(`run_live_poll_replay`, 최근 커밋 2026-07-23): 주식 신호의 라이브 폴링 재현 게이트다. "라이브 폴링을 재생해서 재현성을 게이트로 쓴다"는 **개념만 채택**하고 코드는 도메인이 달라 불채택한다.
  - alphabot `paper_trading/options_data.py:fetch_live_spread_quote`: 테스트 이름상 유동성 0인 후보를 건너뛰는 로직이 있다. 본문은 읽지 않았다(미검증). 진입 필터 설계 시 참고 후보로만 둔다.
  - `price_spread_to_close` 원본은 alphabot `research/credit_spread_simulator.py`에 있다. 이 레포 vendor 사본과 동일 계열이다.
- 메모리 디렉터리 `project_atlas_*`, `feedback_reuse_original_sim_functions_not_reimplement`를 읽었다. 이전 마찰 모델 시도는 **없음**. 관련 실패는 ③에 정리했다.
- `search_lessons` MCP 툴은 이 세션에 없었다. agent_hub `training/raw` 검색은 grep 차단 훅에 막혀 **미실시**했다.

### ② 외부 선행작업
외부 선행작업: (라이선스·최근 커밋은 `gh api`로 2026-09-30에 확인했다. 웹 문서에는 라이선스 개념이 없다.)

| 이름 | URL | 라이선스 / 최근 커밋 | 판단 |
|---|---|---|---|
| optopsy | https://github.com/goldspanlabs/optopsy (구 michaelchu/optopsy) | AGPL-3.0 / 최근 커밋 2026-04-02 | **불채택.** 2026-08-24에 이미 통합을 시도했다가 실패하고 기각했다(커밋 b13d19c). 실제 과거 체인이 입력으로 필요한데 그 데이터가 없다는 한계는 그대로다. AGPL이라 코드를 가져다 쓰기에도 부담이 있다. |
| QuantConnect LEAN | https://github.com/QuantConnect/Lean | Apache-2.0 / 최근 커밋 2026-09-30 | **시스템 단위 선행작업**이다. 호가 데이터로 체결을 모델링하고 fill model을 교체할 수 있다. 세부 체결 규칙은 미검증이다. 불채택 이유: C# 엔진인 데다 과거 옵션 호가 데이터가 있어야 성립한다. "체결가 = 호가 반대편, fill model은 교체 가능한 순수 함수"라는 구조만 채택한다. |
| ORATS 백테스터 슬리피지 관례 | https://orats.com/blog/answers-to-common-questions-backtesting | 상용 서비스(코드 없음) / 해당 없음 | **파라미터 참고로 채택.** 체결가를 bid + (ask−bid)×s로 둔다. s는 1레그 75%, 2레그 66%, 4레그 56%이다. 우리 limit-청산 변형의 "낙관 시나리오" 값으로 쓴다. |
| Muravyev & Pearson, "Options Trading Costs Are Lower than You Think", RFS 33(11) 2020 | https://ideas.repec.org/a/oup/rfinst/v33y2020i11p4973-5014..html | 논문 / 해당 없음 | **채택(범위 설정용).** 체결 타이밍을 조절하는 트레이더의 유효 스프레드는 호가 스프레드의 40% 미만이다. 반대로 즉시 시장가로 교차하는 우리 라이브는 100%에 가깝다고 보는 근거가 된다. 우리 청산은 타이밍 조절이 없는 시장가라서 기본값은 "호가 전폭 교차"로 둔다. |
| tastylive "1/3 of width" | https://www.tastylive.com/shows/options-jive/episodes/credit-strikes-for-spreads-why-13-of-width-works-06-16-2016 | 방송/리서치 / 해당 없음 | **필터 후보로 채택.** 단, 그들의 SPY 연구에서 1/3 폭이 나오는 숏 델타는 약 0.37~0.45로 보고됐다. 우리 0.20 델타에서는 1/3 조건이 거의 모든 진입을 막을 수 있다. 그래서 0.10/0.20/0.33 세 값을 사전등록해 비교한다. |
| tastylive "Managing losers in risk-defined strategies" | https://tastylive.com/shows/market-measures/episodes/managing-losers-in-risk-defined-strategies-10-24-2016 | 방송/리서치 / 해당 없음 | **가설 근거로 채택.** 정의형 위험(IC) 전략에서 1~5x 크레딧 손절은 만기 보유보다 성과가 나빴다고 보고됐다. 그래서 "손절 없음" 변형(V3)을 등록한다. 45DTE 기반 연구라 주간 DTE에 그대로 옮길 수 있을지는 미검증이다. |
| Alpaca Historical Option Data 문서 | https://docs.alpaca.markets/docs/historical-option-data | 문서 / 해당 없음 | 과거 옵션 데이터는 2024-02부터 있다. indicative는 무료 파생 호가이고 opra는 구독이 필요하다. 포럼 https://forum.alpaca.markets/t/historical-options-request/19218 에 따르면 과거 호가 요청은 404이고 bars/trades만 된다(스태프 답변은 없음). 설치된 SDK에 메서드가 없다는 사실(배경 참조)과 맞는다. **결론: Alpaca로는 과거 NBBO를 얻을 수 없다.** 3년치 과거 호가는 상용 데이터(ORATS, ThetaData, Cboe DataShop 등, 가격·조건 미확인)가 아니면 없다. |

시스템 단위 판단: "라이브 체결을 재현한 뒤에야 전략을 비교한다"를 파이프라인 전체로 푸는 기성 제품은 두 부류다. LEAN처럼 호가 이력 + 교체형 fill model을 쓰는 쪽과 ORATS처럼 호가 이력 + 슬리피지 % 관례를 쓰는 쪽이다. 둘 다 **과거 호가 이력이 전제**라 우리에게는 입력이 없다. 그래서 파이프라인 구조(호가 → fill model → 정책 비교)만 채택한다. 호가는 ①지금부터 수집하는 라이브 패널로 채우고 ②"상대 스프레드는 정상성을 가진다"는 가정을 명시한다.

### ③ 실패 사례
실패 사례: 같은 영역의 과거 실패, 수정, 거절 기록이다.
1. **재구현으로 결과 전량이 오염된 사례**(`feedback_reuse_original_sim_functions_not_reimplement`, 2026-09-07). A/B 스크립트가 `run_portfolio_simulation`의 워밍업 스킵을 재구현하다가 261건 전부 "최대손실"이라는 가짜 결과를 냈다. 원본을 유지하고 `simulate_trade`만 몽키패치하라는 교훈이 남았다.
2. **백테스트-라이브 메커니즘 불일치 두 번**. 커밋 b18dc84는 `SHORT_DELTA_TARGET` 0.15와 0.20이 최초 커밋부터 어긋나 있던 것을 고쳤다(`src/signals.py:60-68`). 커밋 5a31fe2는 보호레그 델타 0.06을 1.5% 폭으로 바꾸고 DTE 강제청산을 제거했다(`src/signals.py:69-75`). 두 번 다 **라이브가 백테스트를 따르도록** 고쳤고, 백테스트가 라이브의 마찰을 따라가게 만든 적은 한 번도 없다.
3. **경고를 무시한 사례**. 0.25 델타에서 승률이 오히려 오르는 "비정상 패턴"을 보고 "이 백테스트의 이론가 체결 가정이 실제 호가스프레드·슬리피지를 못 잡는다"고 적어 두었다(`src/signals.py:65-67`). 그러고도 마찰 모델은 만들지 않았다.
4. **가짜 신호 대량 발생 버그**(`src/backtest.py:118-126`). 15분 재평가가 vendor와 다른 IV를 써서 가짜 트리거가 생겼다. 교훈: 같은 가격모델을 써야 한다. 하네스의 무마찰 모드는 기존 재평가와 바이트 단위로 같아야 한다.
5. **서킷브레이커 영구동결 버그**(`src/portfolio.py:151-161`). "그럴듯한 숫자"가 사실은 거래 0건 구간이었다. 교훈: 하네스 출력에 거래 수와 스킵 수를 항상 함께 내야 한다.
6. **optopsy 통합 실패와 기각**(b13d19c). 새 엔진으로 바꿔도 합성가격 한계는 사라지지 않았다.
7. **phantom submit**(`src/generate_report.py:81-94`, 메모리 `project_atlas_phantom_submit_logging_bug_2026-09-11`). 로그의 `submitted=true`는 체결을 뜻하지 않는다.

이 설계에 주는 제약:
- (a) vendor 함수(`construct_credit_spread`, `price_spread_to_close`, `run_portfolio_simulation`, `black_scholes_price`)는 **호출만** 한다. 대체 로직은 `simulate_trade` 몽키패치 래퍼 한 곳에만 둔다.
- (b) 무마찰 설정의 출력이 기존 `reprice_exits_intraday`와 같아야 한다는 패리티 테스트를 필수로 둔다.
- (c) 라이브 거래 파서는 브로커 오류가 난 레코드를 반드시 제외한다.
- (d) 모든 결과표에 n, 스킵 수, 데이터 기간을 함께 싣는다.
- (e) 목표 지표는 로그에 실제로 있는 측정치(트리거 시점 마크 `profit_pct`)로 정의한다. 체결가처럼 없는 값은 목표로 쓰지 않는다.

### ④ 결론
결론: **개선해 사용.**
- vendor 시뮬레이터와 `backtest.py`의 데이터 로더(`fetch_intraday_bars`, `_rolling_iv_series`, `_generate_raw_trades`), `portfolio.scale_trades_to_dollars`는 재사용한다.
- 마찰은 새로 구현한다. 호가 반폭 조회 테이블, 마크 노이즈, 청산 체결 정책을 순수 함수로 만들고 `simulate_trade` 래퍼로 주입한다.
- 새 엔진(LEAN, optopsy)은 도입하지 않는다. 입력 데이터(과거 NBBO)가 없다는 문제는 엔진을 바꿔도 해결되지 않고, ③-6에서 이미 한 번 확인했다.

## Acceptance Rubric

(설계 본문을 확정하기 전에 고정한다. 채점은 이 세션이 아니라 **새 컨텍스트의 분리 채점자**가 한다. 채점 순서는 감점 사유 나열 → 차원별 점수 → 게이트 판정이다.)

| # | 차원 | 1점 | 3점 | 5점 |
|---|---|---|---|---|
| R1 | 보정 목표의 정합성 | 목표가 로그에 없는 값(체결가 등)이거나 허용오차가 임의 | 로그 측정치 기반, 허용오차는 있으나 근거 약함 | 목표 4종이 모두 로그 측정치와 같은 정의이고, 허용오차는 라이브 부트스트랩 CI, 음성 대조군 포함 |
| R2 | 재사용·패리티 | vendor 로직 재구현 | 재사용하지만 패리티 검증 없음 | 몽키패치 한 지점만 두고, 무마찰 모드가 기존 출력과 동일함을 테스트로 강제 |
| R3 | 과적합 통제와 한계 정직성 | 101건 전체에 다수 파라미터 적합 | 시계열 분할은 있으나 자유 파라미터가 많음 | 자유 파라미터 1개는 train에서만 적합하고, 호가표는 독립 데이터에서 측정, 변형은 사전등록, 한계(n, 지정가 가정, 단일 레짐, 페이퍼 체결)를 명시 |
| R4 | 결정 유용성 | 결과가 어떤 결정으로 이어지는지 불명 | 결정은 있으나 판정 규칙이 모호 | 수치 판정 규칙이 있고 "아무것도 통과 못 함" 결과도 명시적 결정으로 연결 |
| R5 | 구현 가능성 | 태스크가 크거나 테스트 불명 | 태스크 분할은 있으나 회귀 주입 없음 | 태스크마다 디스패치 1회, 입출력·테스트 명시, 회귀 주입 테스트 포함 |

게이트: **모든 차원 ≥ 3, 평균 ≥ 4.0.** 미달이면 해당 차원을 재작업한 뒤 재채점한다.

## 설계

### 1. 두 가지 실행 모드

| 모드 | 입력 | 용도 | 한계 |
|---|---|---|---|
| **replay** | 라이브 매칭 거래 101건(진입 시각, 레그 OCC, 수량, 지정가 크레딧, regime) + 해당 기간 15분봉 | **보정의 1차 무대.** 같은 진입을 두고 청산 정책만 바꿔 쌍대(paired) 비교한다. 변형 (a) 청산 정책과 (b)(c)(d)의 부분집합 필터 효과를 본다. | n=101, 2026-08-28~09-29 단일 4주 레짐 |
| **generative** | 전략7 신호(`_generate_raw_trades`, `src/backtest.py:312-344`) × 3년 일봉·15분봉 | 보정된 마찰로 (b)~(e) 진입 필터, 유니버스, 포트폴리오 캡을 장기간 검증한다. | 과거 호가가 없어 호가표 정상성을 가정. BS+RV 크레딧이라 라이브의 크레딧/폭 분포를 못 만들 수 있음(설계 4-6 진단) |

### 2. 마찰 모델 (관측 가능한 입력만 사용)

1. **레그별 반폭 h.**
   - 조회 테이블 `h = table[class][mid_bucket]`을 쓴다. class는 `index_etf`(SPY/QQQ/IWM/DIA), `other_etf`, `single_stock` 셋이다. mid_bucket은 `<0.10`, `0.10–0.50`, `0.50–2.00`, `>2.00` 넷이다.
   - 셀 값은 **절대 반폭의 중앙값**과 **상대 반폭(h/mid)의 중앙값**이다. 둘 중 큰 쪽을 쓰고, 틱/2(0.005)를 하한으로 둔다.
   - 데이터 출처는 **이번에 새로 수집하는 호가 패널**이다(구현 태스크 T1). `fetch_chain`(`src/signals.py:394-404`)을 그대로 호출해 5~9 DTE 체인 스냅샷의 `latest_quote.bid_price/ask_price`를 기록한다. 대상은 `OPTION_SYMBOLS` 23종목(`src/signals.py:80-94`)이고, 하루 4회(개장 +30분, 정오, 14:30 ET, 마감 −30분)씩 **5거래일 이상** 수집한다.
   - 라이브와 같은 client와 같은 feed 기본값을 쓴다. feed 기본값이 indicative인지는 미검증이다. 알아내면 테이블에 기록한다.
   - 보정용 101건과 **독립인 데이터**라 이 테이블은 과적합 대상이 아니다.
2. **마크.**
   - 폴링 시점마다 레그 마크를 `mark = mid_model + ε`로 둔다. ε ~ N(0, (k·h)²)이고 레그·폴링마다 독립이다.
   - `mid_model`은 `black_scholes_price`(`src/vendor/options_pricing.py:41-58`)로 계산한다. 레그 합은 `price_spread_to_close`(`credit_spread_simulator.py:169-195`)와 일치해야 한다(테스트로 강제).
   - **k가 유일한 자유 파라미터**다. Alpaca가 `unrealized_pl`에 어떤 가격(mid인지 last trade인지)을 쓰는지는 미검증이다(열린 질문 1). k는 그 차이를 흡수하는 손잡이다.
3. **IV 앵커 (replay만).**
   - 거래마다 진입 시점의 `iv_scale`을 구한다. 진입 시점 BS 크레딧 × (1−0.05)가 라이브 지정가 크레딧과 같아지도록 푼다(이분법, stdlib).
   - 이후에는 `iv_t = mtm_iv(date) × iv_scale`로 쓴다(`mtm_iv`는 `_rolling_iv_series`, `src/backtest.py:189-197`).
   - 관측값(라이브 크레딧)에서 결정적으로 나오므로 자유 파라미터가 아니다. 해가 없으면(크레딧 ≥ 폭 등) 그 거래는 `unanchorable`로 스킵하고 개수를 보고한다.
4. **청산 체결.**
   - market: 숏 레그는 ask(mid+h), 롱 레그는 bid(mid−h)에 체결된다고 본다. 곧 `close_cost = Σ mid ± h`이고 **전폭을 교차**한다.
   - limit-mid 변형: ORATS 관례로 s∈{0.5, 0.66}을 쓴다. 체결가는 레그별 `bid + s·(ask−bid)`이고, W폴링 동안 체결되지 않으면 market으로 폴백한다. limit 체결 확률에 대한 실측 데이터는 **없다**. 그래서 이 변형은 점추정이 아니라 s의 **민감도 밴드**로만 보고한다.
5. **진입.** replay는 라이브 지정가 크레딧을 그대로 쓴다(지정가 가정은 한계로 명시). generative는 vendor `construct_credit_spread`의 `credit_haircut_pct=0.05`를 그대로 쓴다(`credit_spread_simulator.py:148-151`). 진입 마찰을 이중으로 적용하지 않는다.

### 3. 청산 정책 엔진 (`run_exit_path`)

거래 하나와 15분봉 경로, 정책을 받아 순수 함수로 계산한다. 반환값은 `{exit_ts, exit_reason, trigger_mark_pnl_pct, realized_pnl, n_polls}`이다.

정책은 사전등록한다. 이 밖의 정책은 결과를 본 뒤 추가하지 않는다.
- `V0 live`: 마크 pnl%가 +50% 이상이면 PT, −100% 이하면 SL이다. 청산은 market. `src/signals.py:632-635`를 재현한다.
- `V1 limit_then_market`: 트리거는 V0과 같다. limit-mid로 내고 W=2폴링 뒤 market으로 폴백한다. s∈{0.5, 0.66} 밴드로 본다.
- `V2 min_hold_60m`: 진입 후 60분 동안은 SL을 판정하지 않는다. PT는 그대로다.
- `V3 no_stop`: PT이거나 만기일 때만 청산한다. 만기 정산은 `price_spread_to_close`의 T=0 분기를 쓴다.
- `V4 underlying_breach`: 기초자산 종가가 숏 행사가를 돌파하면 SL로 청산하고, 마크 SL은 쓰지 않는다.

폴링 격자는 15분봉(`fetch_intraday_bars`, `src/backtest.py:87-107`)이다. replay에서는 진입 시각 이후 첫 봉부터 시작한다.

### 4. 보정 절차 (과적합 통제)

1. **분할.** 매칭 거래를 진입 시각 순으로 정렬해 앞 60%를 train, 뒤 40%를 validation으로 나눈다. 경계 날짜는 출력에 기록한다. 9/16과 9/28 급락일이 validation에 들어갈 수 있고, 이는 의도된 난이도다.
2. **적합.** k를 격자 {0, 0.25, 0.5, 1, 1.5, 2, 3}에서 고른다. **train 거래만으로** 목표 지표 손실(아래 T-a~T-c의 정규화 오차 합)이 최소인 값을 택한다. 적합 함수는 validation 행을 받으면 예외를 던진다.
3. **목표 지표.** 모두 로그와 **같은 정의**를 쓴다. 트리거 시점 마크 기준이며 체결가가 아니다.
   - T-a 승률: `profit_target` 비율이다. 라이브 전체 약 33%.
   - T-b 손절 거래의 트리거 `profit_pct` 중앙값과 p75. 라이브 평균은 −295%이지만 게이트에는 중앙값을 쓴다(평균은 −4200% 같은 이상치에 끌려감).
   - T-c 손절 중 진입 후 60분 안에 트리거된 비율. 라이브 23/66.
   - T-d regime별 승률의 **순서**: trend_up이 가장 낮아야 한다. regime별 n이 작아 수치 게이트는 두지 않는다.
4. **판정(validation에서).**
   - T-a, T-b 중앙값, T-c의 시뮬 값이 모두 **validation 라이브 통계의 90% 부트스트랩 CI**(거래 단위 리샘플 2000회, 시드 고정) 안에 들어와야 한다. T-d 순서도 일치해야 한다.
   - validation n≈40이라 CI가 넓다(검정력 약함). 이 한계는 결과표에 그대로 적는다.
5. **음성 대조군(필수).** k=0이고 h=0인 무마찰 설정으로 같은 판정을 돌린다. 이 설정은 **T-a 또는 T-c에서 반드시 CI를 벗어나야** 한다. 무마찰로도 통과하면 목표 지표가 마찰을 판별하지 못한다는 뜻이다. 그 경우 하네스 결론은 "귀속 불가"로 끝나고 수정안 비교로 넘어가지 않는다.
6. **generative 진단(게이트 아님).** 같은 4주 동안 전략7을 generative로 돌려 크레딧/폭 분포를 라이브와 비교한다. 비교 구간은 `<5%`, `5–10%`, `10–20%`, `≥20%`이다. generative에 `<5%` 구간이 사실상 없으면, 진입 필터 (b)에 대한 generative 결론은 무효로 표시하고 (b)는 replay 부분집합 분석만으로 판단한다. 원인 후보는 두 가지다. RV와 IV의 차이, 그리고 vendor의 고정 `STRIKE_INCREMENT=0.5`(`src/backtest.py:50`)와 실제 상장 행사가 격자의 차이다.

### 5. generative 주입 지점 (재구현 금지)

- `vendor.credit_spread_simulator.simulate_trade`를 컨텍스트 매니저로 교체한다. 래퍼는 먼저 **원본 `simulate_trade`를 `profit_target_pct=1.0, stop_loss_multiple=None`으로 호출**해 스프레드 구성과 만기를 얻는다. 그다음 `run_exit_path`로 `exit_date/exit_reason/close_debit/realized_pnl`을 덮어쓴다.
- `run_portfolio_simulation`은 `simulate_trade`를 모듈 전역 이름으로 호출한다(`:435`). 그래서 워밍업, 스킵, 동시보유 로직(`:388-426`)은 원본 그대로 유지된다. 동시보유 판정(`:421`)도 덮어쓴 `exit_date`를 따른다.
- 15분봉과 IV는 클로저로 주입한다. 종료 시 원본을 복원한다(테스트로 강제).
- 이 경로에서는 기존 `reprice_exits_intraday`를 **호출하지 않는다**. 청산 판정을 두 번 하게 되기 때문이다.
- 진입 필터 (b), 유니버스 (c), 콘도르 전용 (d)는 신호나 거래 리스트에 거는 필터 함수다. 적용 시점은 `construct_credit_spread` 결과가 나온 뒤이고, 판정에는 크레딧/폭과 반폭 테이블로 추정한 스프레드 폭 비율을 쓴다.
  - OI/volume 필터는 과거 데이터가 없어 generative에서는 **평가 불가**다. replay에서도 진입 당시 OI는 로그에 없다(미기록). 이 필터는 호가 패널 수집 이후의 라이브 관찰 과제로 넘긴다.
- (e) 포트폴리오 캡: `scale_trades_to_dollars`(`src/portfolio.py:66`)에 선택 인자 `max_open_risk_pct: float | None = None`을 추가한다. None이면 기존 동작과 바이트 단위로 같다. 값이 있으면 미청산 거래들의 `max_loss×contracts` 합이 `equity×cap`을 넘는 신규 진입을 스킵하고, 스킵 수를 집계한다. 개별 포지션 리스크 인하는 기존 `risk_pct` 경로를 그대로 쓴다(`pct_max` 인자, `:48-54`).
- 사이징 기준 차이(라이브는 `width*100`, `signals.py:568`, 백테스트는 `width−credit`, `portfolio.py:175`)는 이 레이어에서 **고치지 않는다**. 차이가 커지는 경우는 크레딧이 폭 대비 작을 때(<5%)인데, 그때는 영향이 5% 미만이다. 대신 결과표 주석으로 명시한다.

### 6. 비교 절차와 결정 규칙

사전등록 조합은 최대 12개다. 결과를 본 뒤에는 추가하지 않는다.
- 청산: V0~V4
- 진입: E0(없음), E1 크레딧/폭 ≥ 0.10, E2 ≥ 0.20, E3 ≥ 0.33, E4 최소 절대 크레딧 ≥ $0.15이면서 반폭 합/크레딧 ≤ 0.5
- 유니버스: U0 전체, U1 SPY/QQQ/IWM/DIA
- 구조: D0 레짐 라우터, D1 콘도르 전용
- 포트폴리오: P0 없음, P1 총 미청산 위험 20% + `pct_max` 0.05

조합 목록은 T7에서 파일로 고정한다.

**수정안 묶음 F를 라이브 Task Contract(페이퍼)로 올리는 조건.** 아래를 모두 만족해야 한다.
1. 설계 4 보정을 통과했고, 음성 대조군 조건도 만족했다.
2. replay validation에서 F와 V0의 거래별 쌍대 손익 차이 평균이 양수이고, 90% 부트스트랩 CI 하한도 0보다 크다. 청산 정책 외 필터는 부분집합 효과로 본다.
3. generative 3년, 마찰 적용 기준으로 총손익 > 0, PF ≥ 1.2, MDD ≤ 20%(`PORTFOLIO_DD_KILL_PCT`, `src/portfolio.py:44`), 거래 수 ≥ 30을 만족한다. 이 조건은 **중앙 호가표와 비관 호가표(h×1.5) 두 경우 모두**에서 성립해야 한다.
4. V1은 s 밴드의 하단(s=0.66)에서도 조건 2와 3을 유지해야 한다.

통과하는 F가 여럿이면 가장 단순한 쪽(변경 레버 수가 적은 쪽)을 고른다. **하나도 통과하지 못하면** 옵션 사이드는 `OPTIONS_CLOSE_ONLY`로 유지하고, 결론은 "라이브 가능한 수정안 없음"으로 기록한다. 이것도 유효한 결정이다.

### 범위 밖 (이 레이어에서 안 함)

- 크립토 슬리브 손익 분해
- `decision_error` 사유 로깅(`src/mcp_runner.py:604`, `:630`은 사유를 남기지 않음)
- 브로커 체결가 대조. Alpaca 계정 조회가 복구되면 T3 파서에 체결가 열을 추가한다.
- 라이브 코드 수정. Task Contract 이후의 일이다.

## 테스트

모든 테스트는 네트워크 없이 도는 합성 데이터만 쓴다. 리뷰어는 테스트마다 scratch 사본에 회귀를 주입해 **그 이유 문자열로 실패하는지** 확인한다(아래 "주입" 열).

| # | 테스트 | 검증 | 회귀 주입(실패해야 함) |
|---|---|---|---|
| 1 | `test_zero_friction_parity_with_reprice_exits_intraday` | 설정이 k=0, h=0, V0, market s=1이고 IV 앵커가 꺼져 있을 때, `run_exit_path`의 exit 시각·사유·close_debit이 같은 거래에 대한 `reprice_exits_intraday` 출력과 같다 | 레그 가격 합을 `price_spread_to_close` 대신 다른 공식으로 계산 |
| 2 | `test_leg_mid_sum_matches_price_spread_to_close` | 레그별 BS mid 합이 vendor 함수 값과 1e-9 이내로 같다 | 롱 레그 부호 반전 |
| 3 | `test_market_close_crosses_full_spread` | market 청산의 realized는 트리거 시점 mid 손익에서 Σh×100×qty를 뺀 값과 같다 | h 가산 제거 |
| 4 | `test_fit_rejects_validation_rows` | 적합 함수에 validation 기간 행이 섞이면 ValueError | 날짜 경계 검사 제거 |
| 5 | `test_negative_control_fails_calibration` | 합성 "라이브" 데이터(k=1로 생성)에 대해 무마찰 설정이 CI 밖으로 판정된다 | 판정에서 T-c 누락 |
| 6 | `test_parser_excludes_broker_errors_and_leg_mismatch` | `order_result`에 `error`가 있는 진입과, 레그 집합이 다른 청산 매칭을 제외한다 | error 필터 제거(fixture에 2026-08-25 403 레코드 형태 포함) |
| 7 | `test_simulate_trade_monkeypatch_restored` | 컨텍스트 종료 후(예외가 나도) `vendor.credit_spread_simulator.simulate_trade is original` | `finally` 복원 제거 |
| 8 | `test_generative_skips_preserved` | 워밍업 이전 신호는 원본과 같은 `skipped_candidates` 사유로 스킵된다 | 래퍼 안에서 워밍업 로직 재구현(메모리 사례 재현) |
| 9 | `test_open_risk_cap_none_is_identical` | `max_open_risk_pct=None`이면 기존 `tests/test_portfolio.py` 입력에 대한 출력이 동일하다 | 기본값을 0.2로 변경 |
| 10 | `test_open_risk_cap_blocks_excess` | 캡을 넘는 동시 진입이 스킵되고 스킵 수가 집계된다 | 청산 반영 전에 캡 계산 |
| 11 | `test_credit_width_filter` | 크레딧 0.02, 폭 5인 거래는 E1~E3에서 탈락하고 크레딧 2.0인 거래는 통과한다 | 비교 부등호 반전 |
| 12 | `test_half_spread_lookup_floor_and_buckets` | 버킷 경계값과 틱/2 하한이 적용된다 | 하한 제거 |
| 13 | `test_iv_anchor_unanchorable_skipped` | 크레딧 ≥ 폭인 거래는 `unanchorable`로 스킵되고 개수가 보고된다 | 스킵 대신 iv_scale=1 폴백 |
| 14 | `test_bootstrap_ci_deterministic` | 시드를 고정하면 CI가 재현된다 | 시드 미전달 |

테스트 실행 명령: `.venv/bin/python -m pytest tests/test_friction.py tests/test_friction_backtest.py tests/test_portfolio.py -q`. 기존 스위트 전체(`pytest -q`)도 계속 통과해야 한다.

## 열린 질문

1. **Alpaca 페이퍼의 마크와 체결 규칙.** `unrealized_pl`이 쓰는 가격이 mid인지, last trade인지, indicative인지, 그리고 페이퍼 체결이 어떤 호가에 대해 일어나는지는 미검증이다. → **권고:** 보정 대상은 "페이퍼 계좌 통계"라고 명시하고 진행한다. T1 수집기가 열린 포지션이 있을 때 포지션의 `current_price`를 같은 시각의 bid/ask와 함께 기록하도록 해서, 나중에 k의 의미를 해석하는 근거로 쓴다. 이 질문 때문에 설계를 막지는 않는다.
2. **상용 과거 NBBO 구매 여부.** 대상은 ORATS, ThetaData, Cboe 등이고 가격·조건은 미확인이다. → **권고:** 지금은 사지 않는다. 보정은 통과했는데 generative 결과가 판정 경계에 걸릴 때만 재검토한다. 그 경우 중앙과 비관 호가표의 결론이 엇갈리게 된다.
3. **k를 전역 1개로 둘지, class별로 둘지.** → **권고:** 전역 1개로 둔다. validation 잔차가 class별로 체계적으로 실패할 때만 index_etf와 그 외로 나눠 2개로 늘리고, 그때는 과적합 경고를 결과에 표기한다.

## 구현 핸드오프 태스크 분할

Codex는 `-m gpt-reserve`를 쓴다. 태스크 1개가 디스패치 1개다. 모든 태스크는 worktree `friction-backtest`에서만 작업하고, `src/mcp_runner.py`, `src/signals.py`, launchd, registry는 수정하지 않는다(읽기 전용).

| 태스크 | effort | 입력 | 출력 | 테스트 |
|---|---|---|---|---|
| **T1 호가 패널 수집기** | low | `signals.fetch_chain`, `OPTION_SYMBOLS`, `.env.competition` 키(읽기 전용 시장데이터) | `scripts/collect_option_quotes.py`: 1회 실행 시 23종목 put/call 5~9 DTE 스냅샷에서 `ts, underlying, occ, bid, ask, mid, delta, dte, feed`를 `data/quote_panel/YYYY-MM-DD.parquet`에 추가한다. 종목별 try/except와 로깅을 넣는다. 스케줄링은 하지 않는다(수동 실행). | 가짜 chain 객체로 행 스키마, 음수·0 호가 제외, 한 종목 예외 시 나머지 계속 진행을 검증한다. |
| **T2 반폭 테이블** | low | T1 parquet 스키마 | `src/friction.py`: `build_half_spread_table(df) -> DataFrame`, `half_spread(mid, cls, table) -> float`, `SYMBOL_CLASS` dict. 테이블은 `data/friction/half_spread_table.csv`로 저장한다. | 테스트 #12, 빈 셀은 상위 class 폴백 대신 ValueError |
| **T3 라이브 거래 파서** | medium | `registry/decisions.jsonl` 형식(메인 체크아웃, 읽기 전용 경로 인자) | `src/friction_backtest.py::load_live_trades(path) -> list[dict]`: 진입(오류 없는 `submitted`, `limit_price`, legs, qty, regime, ts)과 청산(`action=close`, 같은 레그 집합, 첫 청산)을 매칭한다. 출력 열은 `trigger_profit_pct, exit_reason, minutes_held`이고, 제외 사유별 카운트를 함께 반환한다. | 테스트 #6. fixture는 합성 jsonl 10줄로 만든다. |
| **T4 청산 경로 엔진** | medium | T2, vendor 함수, `backtest.fetch_intraday_bars` 형식의 DataFrame | `src/friction.py::implied_iv_scale(...)`, `run_exit_path(trade, bars, mtm_iv, policy, friction, rng)`, 정책 V0~V4 | 테스트 #1, #2, #3, #13 |
| **T5 replay와 보정 CLI** | medium | T3, T4 | `src/friction_backtest.py`의 `replay(...)`, `fit_k(train)`, `bootstrap_ci(...)`, `calibrate(...)`. `python src/friction_backtest.py calibrate --decisions <path>`가 `reports/friction/calibration-<date>.md`를 쓴다. 내용은 train/val 경계, k, 목표별 라이브 CI와 시뮬 값, 음성 대조군, n과 스킵 수다. | 테스트 #4, #5, #14 |
| **T6 generative 주입과 포트폴리오 캡** | medium | T4, `backtest._generate_raw_trades`, `portfolio.scale_trades_to_dollars` | `src/friction_backtest.py::patched_simulate_trade(...)` 컨텍스트 매니저, 진입 필터 함수(E1~E4, U1, D1), `portfolio.scale_trades_to_dollars(max_open_risk_pct=None)` | 테스트 #7, #8, #9, #10, #11 |
| **T7 사전등록 비교 러너** | low | T5, T6, 설계 6 조합 목록 | `config/friction_variants.json`(12개 고정), `python src/friction_backtest.py compare` → `reports/friction/compare-<date>.md`. 조합마다 replay 쌍대 CI, generative 중앙·비관 지표, 결정 규칙 판정 열을 싣는다. | 조합 파일에 없는 변형 이름을 주면 거부한다. 판정 함수 단위 테스트는 경계값 PF=1.2, MDD=20%로 한다. |

순서는 다음과 같다. T1과 T3는 병렬로 진행한다. T1 데이터는 **장중 5거래일 수동 실행**으로 모은다. 그다음 T2 → T4 → (T5, T6) → T7이다. T5 보정이 음성 대조군이나 validation에서 실패하면 T7은 돌리지 않고, 그 실패 보고서가 곧 결론이다.

## 데이터 한계 (결과 해석 시 반드시 함께 인용)

- n=101(validation 약 40)이다. 부트스트랩 CI가 넓고 regime별 n은 10~48이다.
- 진입 크레딧은 지정가를 가정했다(실체결이 더 유리했을 수 있음). 청산 체결가는 로그에 없다.
- 기간은 2026-08-28~09-29 단일 4주 레짐이고 급락일이 여러 번 있었다.
- 보정 대상은 Alpaca **페이퍼** 체결·마크다. 실계좌 마찰과 다를 수 있다.
- 호가표는 2026년 10월 이후 수집분을 3년 과거에 적용한다(상대 스프레드 정상성 가정).
