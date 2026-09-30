# 설계: 마찰 보정(friction-calibrated) 옵션 백테스트 하네스 (2026-09-30)

- 상태: 설계 초안 (Tier 2, 구현 전). 브랜치 `friction-backtest`. 독립 채점 1차 FAIL(평균 3.4) 후 해당 절을 직접 수정한 개정본이다.
- 범위: **다음 레이어만**이다. 라이브 통계를 재현하는 하네스와 그 보정 절차, 그리고 보정을 통과한 뒤 사전등록한 수정안을 비교하는 절차까지만 다룬다. 라이브 코드(`src/mcp_runner.py`, `src/signals.py`)의 수정, launchd, registry는 이 문서의 범위가 아니다.
- 이 하네스가 판단 근거를 대는 결정: **어떤 수정안 묶음을 라이브 Task Contract(페이퍼)로 올릴지, 아니면 옵션 사이드를 `OPTIONS_CLOSE_ONLY`(커밋 5228d90) 상태로 둘지.** 판정 규칙은 설계 6에 있다.

## 배경 (확정 사실과 가설 구분)

확정 사실(코드와 로그로 확인함):
- 백테스트 엔진은 BS 가격 + realized-vol IV로 돈다. 청산 판정은 BS 모델가의 `close_debit`로만 한다: `src/vendor/credit_spread_simulator.py:169-195`(`price_spread_to_close`), `:324-331`(PT/SL 판정), `src/backtest.py:110-186`(15분봉 재평가도 같은 BS 모델가, `:164-174`). 호가 스프레드, 시장가 청산 슬리피지, 마크 편향 항은 하나도 없다. 제출 문서도 이것을 "upper bound"라고 이미 적어 두었다(`docs/one-page-submission.md:32`).
- 15분 재평가(`reprice_exits_intraday`)의 동작:
  - 탐색 창은 **진입 다음날부터** 원래 일봉 청산일까지다(`src/backtest.py:143`).
  - 창 안에서 트리거를 못 찾으면 vendor의 일봉 판정을 그대로 둔다(`:176-178`).
  - 찾으면 사유에 `_intraday` 접미사를 붙인다(`:170`, `:173`).
- 라이브 청산 판정:
  - Alpaca 포지션 **전 레그**의 `unrealized_pl` 합을 전 레그 `cost_basis` 합의 절대값(=수취 크레딧)으로 나눈 값을 쓴다(`src/signals.py:624-635`). 콘도르면 4레그 합산이다.
  - 손절 트리거는 `profit_pct <= -(2.0-1.0)`이다(`:634`).
  - 청산 주문은 먼저 market mleg로 낸다(`src/signals.py:689-696`, `src/mcp_runner.py:490-496`). 브로커가 "reenter with a limit"으로 거부하면 그 자리에서 limit으로 재시도한다(`src/mcp_runner.py:502-531`). limit 가격은 레그 mid 합 + 5%이다(`src/signals.py:703-720`).
  - **limit 폴백 청산 레코드에는 `profit_pct`가 없다**(`src/mcp_runner.py:523-527`). market 청산 레코드에만 있다(`:540-544`).
- 백테스트의 콘도르 처리: `iron_condor` 신호를 `bull_put`과 `bear_call`로 **분리해 각각 독립 시뮬레이션**한다(`src/backtest.py:333`). 라이브는 4레그 합산 손익으로 한 번에 청산한다. 두 방식이 다르다.
- 사이징:
  - 라이브 진입 크레딧은 `max(0.01, mid_credit*(1-0.05))`이다(`src/signals.py:544`, `:575`). 최소 크레딧 필터나 크레딧/폭 필터는 없다.
  - 라이브 계약수는 `floor(B/(width*100))`이다(`:568-569`, `:417-420`). 백테스트는 `floor(B/((width-credit)*100))`이다(`src/portfolio.py:175`).
  - 따라서 백테스트 계약수는 라이브의 `w/(w-c)`배다. **크레딧/폭이 클수록 차이가 커진다**(c/w=0.20이면 1.25배, 0.33이면 1.5배).
  - 백테스트는 동시 포지션 간 리스크를 예약하지 않는다(`src/portfolio.py:17-19`). 라이브에도 포트폴리오 총위험 상한이 없다(종목당 1개 가드뿐, `src/mcp_runner.py:561-575`).
  - 백테스트의 `risk_pct`는 `backtest._risk_pct_series`가 `risk_pct_for_atr_pct`(`src/backtest.py:29` import, `:287` 호출, 기본 `pct_max=0.10` `src/portfolio.py:48-49`)로 계산해 거래 dict에 넣는다. `scale_trades_to_dollars`는 `t["risk_pct"]`만 읽는다(`src/portfolio.py:168`).
- 설치된 alpaca-py 0.44.0의 `OptionHistoricalDataClient`에는 `get_option_bars`, `get_option_trades`, `get_option_latest_quote`, `get_option_latest_trade`, `get_option_snapshot`, `get_option_chain`만 있다. **과거 호가(NBBO) 이력 메서드는 없다**(`.venv/.../alpaca/data/historical/option.py:74-204`). Feed는 `opra`(구독 필요)와 `indicative` 두 가지다(`alpaca/data/enums.py:103-115`).

라이브 로그 재집계(2026-09-30, 메인 `registry/decisions.jsonl`, 읽기 전용, 1회성 스크립트):
- `action=close, submitted=true` 레코드는 223건이다.
  - 그중 13건은 `sleeve=crypto`이다. **`max_hold_days` 3건은 전부 크립토**(PAXG 2건, ETH 1건)라 옵션 보정 대상이 아니다. 채점자 지적 중 "max_hold_days를 V0~V4에 반영하라"는 이 근거로 **기각**한다.
  - 옵션 청산 210건 중 101건은 `profit_pct`가 있다(SL 66, PT 35). **109건은 전부 limit 폴백**이라 `profit_pct`가 없다.
  - limit 폴백 109건 중 66건은 구 버전 로그라 `order_intent`(레그)가 없다. 43건의 `order_result.status`는 `pending_new`이다.
- **포지션 에피소드로 묶기**: 심볼별로 오류 없는 진입부터 다음 진입까지의 청산을 한 포지션으로 묶었다. 라이브는 포지션이 열린 심볼에 신규 진입을 내지 않으므로(`src/mcp_runner.py:561-575`), 포지션 도중에 다른 진입이 끼어 에피소드가 잘못 분할될 수는 없다. 청산이 있는 에피소드는 112개이다.
  - clean(청산 1회, `profit_pct` 있음): **96개**
  - single_limit(limit 폴백 1회, `profit_pct` 없음): 5개
  - retry(같은 포지션에 청산 레코드 2~24회, 첫 레코드는 limit 폴백): 11개. 그중 5개는 레코드마다 PT/SL 사유가 **뒤바뀐다**(예: DIA 20회 중 SL 14회, PT 6회). 109건의 대부분은 **같은 포지션의 재시도 중복**이다(TLT 39, XLE 25, DIA 23, GLD 9, SPY 7). 재시도가 생긴 원인(limit 미체결 후 취소인지 등)은 미검증이다.
  - 요청자가 쓴 "101건"은 clean 96개에 retry 에피소드 안의 `profit_pct` 레코드 5개가 섞인 것이다.
- **clean 96개 통계**:
  - PT 31, SL 65 → 승률 32%
  - SL 트리거 `profit_pct` 중앙값은 −1.40(−140%)
  - 진입 후 60분 안에 트리거된 비율은 **SL 23/65, PT 0/31**이다.
  - 이 비대칭은 "평균이 0인 대칭 노이즈"로는 설명되지 않는다. 마크가 불리한 쪽(숏 레그는 ask 쪽)으로 치우친 **체계적 편향**과 들어맞는다(해석은 가설).
  - 진입 시각 범위는 2026-08-25~09-30이다(08-28 계좌 리셋 이전 거래 포함).

가설(요청자의 사후분석이며 이 문서는 검증하지 않음): 손절의 주원인은 마크 편향과 시장가 교차 비용이다. 크레딧/폭이 얇을수록 손실 배수가 커진다. trend_up 불풋이 손실의 76%를 차지한다. 진입 크레딧은 지정가 가정이고 **브로커 체결가로는 미검증**이다.

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
외부 선행작업: (라이선스와 최근 커밋은 `gh api`로 2026-09-30에 확인했다. "최근 커밋"은 기본 브랜치의 최신 커밋 committer date이다.)

| 이름 | URL | 라이선스 / 최근 커밋 | 판단 |
|---|---|---|---|
| optopsy | https://github.com/goldspanlabs/optopsy (구 michaelchu/optopsy) | AGPL-3.0 / 최근 커밋 2026-04-02(main `40bb8b2`). 저장소 `pushed_at`은 2026-06-30인데, 다른 브랜치 push로 보이며 main 커밋일과 구분한다. | **불채택.** 2026-08-24에 통합을 시도했다가 실패하고 기각했다(커밋 b13d19c). 실제 과거 체인이 입력으로 필요한데 그 데이터가 없다는 한계는 그대로다. AGPL이라 코드를 가져다 쓰기에도 부담이 있다. |
| QuantConnect LEAN | https://github.com/QuantConnect/Lean | Apache-2.0 / 최근 커밋 2026-09-30 | **시스템 단위 선행작업**이다. 호가 데이터로 체결을 모델링하고 fill model을 교체할 수 있다(세부 체결 규칙은 미검증). 불채택 이유: C# 엔진인 데다 과거 옵션 호가 데이터가 있어야 성립한다. "체결가 = 호가 반대편, fill model은 교체 가능한 순수 함수"라는 구조만 채택한다. |
| ORATS 백테스터 슬리피지 관례 | https://orats.com/blog/answers-to-common-questions-backtesting | 상용 서비스(코드 없음) / 해당 없음 | **파라미터 참고로 채택.** 체결가를 "호가 스프레드의 s만큼 불리하게 이동"으로 둔다. s는 1레그 75%, 2레그 66%, 4레그 56%이다. 우리 limit 청산의 민감도 값으로 쓴다(방향 규약은 설계 2-4). |
| Muravyev & Pearson, "Options Trading Costs Are Lower than You Think", RFS 33(11) 2020 | https://ideas.repec.org/a/oup/rfinst/v33y2020i11p4973-5014..html | 논문 / 해당 없음 | **채택(범위 설정용).** 체결 타이밍을 조절하는 트레이더의 유효 스프레드는 호가 스프레드의 40% 미만이다. 반대로 즉시 시장가로 교차하면 100%에 가깝다고 보는 근거가 된다. 우리 청산은 타이밍 조절이 없는 시장가라서 market은 "호가 전폭 교차"로 둔다. |
| tastylive "1/3 of width" | https://www.tastylive.com/shows/options-jive/episodes/credit-strikes-for-spreads-why-13-of-width-works-06-16-2016 | 방송/리서치 / 해당 없음 | **필터 후보로 채택.** 단, 그들의 SPY 연구에서 1/3 폭이 나오는 숏 델타는 약 0.37~0.45로 보고됐다. 우리 0.20 델타에서는 1/3 조건이 거의 모든 진입을 막을 수 있다. 그래서 0.10/0.20/0.33 세 값을 사전등록한다. |
| tastylive "Managing losers in risk-defined strategies" | https://tastylive.com/shows/market-measures/episodes/managing-losers-in-risk-defined-strategies-10-24-2016 | 방송/리서치 / 해당 없음 | **가설 근거로 채택.** 정의형 위험(IC) 전략에서 1~5x 크레딧 손절은 만기 보유보다 성과가 나빴다고 보고됐다. 그래서 "손절 없음" 변형(V3)을 등록한다. 45DTE 기반 연구라 주간 DTE에 그대로 옮길 수 있을지는 미검증이다. |
| Alpaca Historical Option Data 문서 | https://docs.alpaca.markets/docs/historical-option-data | 문서 / 해당 없음 | 과거 옵션 데이터는 2024-02부터 있다. indicative는 무료 파생 호가이고 opra는 구독이 필요하다. 포럼 https://forum.alpaca.markets/t/historical-options-request/19218 에 따르면 과거 호가 요청은 404이고 bars/trades만 된다(스태프 답변은 없음). 설치된 SDK에 메서드가 없다는 사실(배경 참조)과 맞는다. **결론: Alpaca로는 과거 NBBO를 얻을 수 없다.** 3년치 과거 호가는 상용 데이터(ORATS, ThetaData, Cboe DataShop 등, 가격·조건 미확인)가 아니면 없다. |

시스템 단위 판단: "라이브 체결을 재현한 뒤에야 전략을 비교한다"를 파이프라인 전체로 푸는 기성 제품은 두 부류다. LEAN처럼 호가 이력 + 교체형 fill model을 쓰는 쪽과 ORATS처럼 호가 이력 + 슬리피지 % 관례를 쓰는 쪽이다. 둘 다 **과거 호가 이력이 전제**라 우리에게는 입력이 없다. 그래서 파이프라인 구조(호가 → fill model → 정책 비교)만 채택한다. 호가는 ①지금부터 수집하는 라이브 패널로 채우고 ②"상대 스프레드는 정상성을 가진다"는 가정을 명시한다.

### ③ 실패 사례
실패 사례: 같은 영역의 과거 실패, 수정, 거절 기록이다.
1. **재구현으로 결과 전량이 오염된 사례**(`feedback_reuse_original_sim_functions_not_reimplement`, 2026-09-07). A/B 스크립트가 `run_portfolio_simulation`의 워밍업 스킵을 재구현하다가 261건 전부 "최대손실"이라는 가짜 결과를 냈다. 원본을 유지하고 `simulate_trade`만 몽키패치하라는 교훈이 남았다.
2. **백테스트-라이브 메커니즘 불일치 두 번**. 커밋 b18dc84는 `SHORT_DELTA_TARGET` 0.15와 0.20이 최초 커밋부터 어긋나 있던 것을 고쳤다(`src/signals.py:60-68`). 커밋 5a31fe2는 보호레그 델타 0.06을 1.5% 폭으로 바꾸고 DTE 강제청산을 제거했다(`src/signals.py:69-75`). 두 번 다 **라이브가 백테스트를 따르도록** 고쳤고, 백테스트가 라이브의 마찰을 따라가게 만든 적은 한 번도 없다.
3. **경고를 무시한 사례**. 0.25 델타에서 승률이 오히려 오르는 "비정상 패턴"을 보고 "이 백테스트의 이론가 체결 가정이 실제 호가스프레드·슬리피지를 못 잡는다"고 적어 두었다(`src/signals.py:65-67`). 그러고도 마찰 모델은 만들지 않았다.
4. **가짜 신호 대량 발생 버그**(`src/backtest.py:118-126`). 15분 재평가가 vendor와 다른 IV를 써서 가짜 트리거가 생겼다. 교훈: 같은 가격모델을 써야 한다. 하네스의 무마찰 모드는 기존 재평가와 같은 결과를 내야 한다(테스트 #1).
5. **서킷브레이커 영구동결 버그**(`src/portfolio.py:151-161`). "그럴듯한 숫자"가 사실은 거래 0건 구간이었다. 교훈: 하네스 출력에 거래 수와 스킵 수를 항상 함께 내야 한다.
6. **optopsy 통합 실패와 기각**(b13d19c). 새 엔진으로 바꿔도 합성가격 한계는 사라지지 않았다.
7. **phantom submit**(`src/generate_report.py:81-94`, 메모리 `project_atlas_phantom_submit_logging_bug_2026-09-11`). 로그의 `submitted=true`는 체결을 뜻하지 않는다.
8. **이 문서 1차 초안의 결함**(2026-09-30 독립 채점 FAIL). limit 폴백 레코드에 `profit_pct`가 없다는 사실과 재시도 중복을 확인하지 않고 "101건"을 그대로 보정 표본으로 삼았다. 이 개정본에서 에피소드 단위로 다시 정의했다.

이 설계에 주는 제약:
- (a) vendor 함수(`construct_credit_spread`, `price_spread_to_close`, `run_portfolio_simulation`, `simulate_trade`, `black_scholes_price`)는 **호출만** 한다. 대체 로직은 `simulate_trade` 몽키패치 래퍼와 그 뒤의 후처리 한 곳에만 둔다.
- (b) 무마찰 레거시 모드가 기존 `reprice_exits_intraday`와 같은 결과를 내는지 패리티 테스트로 확인한다.
- (c) 라이브 거래 파서는 브로커 오류, 크립토, 재시도 중복을 규칙대로 분류해 제외하거나 표시한다.
- (d) 모든 결과표에 n, 스킵 수, 데이터 기간을 함께 싣는다.
- (e) 목표 지표는 로그에 실제로 있는 측정치(트리거 시점 마크 `profit_pct`, 트리거 시각, 사유)로 정의한다. 체결가처럼 로그에 없는 값은 목표로 쓰지 않는다.

### ④ 결론
결론: **개선해 사용.**
- vendor 시뮬레이터와 `backtest.py`의 데이터 로더(`fetch_intraday_bars`, `_rolling_iv_series`, `_generate_raw_trades`), `portfolio.scale_trades_to_dollars`는 재사용한다.
- 마찰은 새로 구현한다. 호가 반폭 조회 테이블, 마크 편향, 청산 체결 정책을 순수 함수로 만들고 `simulate_trade` 래퍼와 후처리로 주입한다.
- 새 엔진(LEAN, optopsy)은 도입하지 않는다. 입력 데이터(과거 NBBO)가 없다는 문제는 엔진을 바꿔도 해결되지 않고, ③-6에서 이미 한 번 확인했다.

## Acceptance Rubric

(설계 본문을 확정하기 전에 고정했다. 채점은 이 세션이 아니라 **새 컨텍스트의 분리 채점자**가 한다. 채점 순서는 감점 사유 나열 → 차원별 점수 → 게이트 판정이다.)

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
| **replay** | T3 파서의 옵션 포지션 에피소드(진입 시각, 레그 OCC, 수량, 지정가 크레딧, regime, 첫 청산 시각·사유·`profit_pct`, 에피소드 분류) + 해당 기간 15분봉 | **보정의 유일한 무대.** 같은 진입을 두고 청산 정책만 바꿔 쌍대 비교한다. (a) 청산 정책과 (b)(c)(d) 부분집합 효과를 본다. | 보정 표본은 clean 96개, 기간은 2026-08-25~09-30 단일 레짐 |
| **generative** | 전략7 신호(`_generate_raw_trades`, `src/backtest.py:312-344`) × 3년 일봉·15분봉 | 보정된 마찰로 (b)~(e) 진입 필터, 유니버스, 구조, 포트폴리오 캡을 장기간 검증한다. | 과거 호가가 없어 호가표 정상성을 가정. BS+RV 크레딧이라 라이브의 크레딧/폭 분포를 못 만들 수 있다. 이 경우의 처리는 설계 6 조건 3에 있다. |

**T3 파서 규칙**(배경의 재집계를 코드로 고정한 것):
1. `sleeve=="crypto"`인 레코드는 제외한다(`max_hold_days` 3건 포함).
2. 진입은 `submitted`가 참이고, `order_intent`가 있으며, `order_result`에 `error`가 없고, `action!="close"`인 레코드이다.
3. 에피소드는 같은 심볼에서 진입부터 다음 진입 직전까지의 `action=close, submitted` 레코드들이다. 청산 레코드와 진입 레코드 둘 다에 레그 목록이 있으면 레그 집합이 같아야 한다. 다르면 그 청산은 `leg_mismatch`로 제외한다. 레그가 없는 구 버전 limit 레코드는 심볼만으로 묶고 `legs_unverified` 플래그를 붙인다.
4. 에피소드 분류는 셋이다. `clean`은 청산 1회이고 `profit_pct`가 있는 경우이다. `single_limit`은 청산 1회인 limit 폴백이다. `retry`는 청산이 2회 이상인 경우이고, 사유가 뒤바뀌면 `reason_flip`도 붙인다.
5. 에피소드의 청산 이벤트는 **첫 청산 레코드**로 정한다. 사유, 시각, `minutes_held`가 여기서 나온다. `trigger_profit_pct`는 첫 레코드에 있을 때만 채운다. `live_close_path`는 첫 레코드가 limit 폴백이면 `limit_fallback`, 아니면 `market`이다.
6. **보정 표본은 `clean`만**이다. 목표 T-a와 T-c는 전체 112개 에피소드(첫 레코드 기준)로도 따로 계산해 민감도로 보고한다. 이 둘은 게이트에 쓰지 않는다.
7. 제외된 16개는 TLT, XLE, DIA, GLD, SPY처럼 호가 공백이 난 종목에 몰려 있다. 그래서 clean 표본은 **최악의 마찰을 과소대표**한다. 결과 해석 시 함께 인용한다.

### 2. 마찰 모델 (관측 가능한 입력 + 자유 파라미터 1개)

1. **레그별 반폭 h** (측정값이며 적합하지 않음).
   - 조회 테이블 `h = table[class][mid_bucket]`을 쓴다. class는 `index_etf`(SPY/QQQ/IWM/DIA), `other_etf`, `single_stock` 셋이다. mid_bucket은 `<0.10`, `0.10–0.50`, `0.50–2.00`, `>2.00` 넷이다.
   - 셀 값은 **절대 반폭의 중앙값**과 **상대 반폭(h/mid) 중앙값 × mid** 중 큰 쪽이다. 틱/2(0.005)를 하한으로 둔다.
   - 데이터 출처는 새로 수집하는 호가 패널이다(구현 태스크 T1). `fetch_chain`(`src/signals.py:394-404`)을 그대로 호출해 5~9 DTE 스냅샷의 `latest_quote.bid_price/ask_price`를 기록한다. 대상은 `OPTION_SYMBOLS` 23종목(`src/signals.py:80-94`)이고, 하루 4회씩 5거래일 이상 수동 실행으로 모은다.
   - 라이브와 같은 client와 같은 feed 기본값을 쓴다. 기본값이 indicative인지는 미검증이며, 알아내면 테이블에 기록한다.
   - 보정 표본과 **독립인 데이터**이다. 비관 시나리오에서는 h×1.5를 쓴다.
2. **마크** (자유 파라미터는 β 하나).
   - 레그 마크를 불리한 방향으로 β·h만큼 치운다. 숏 레그는 `mark = mid + β·h`(buy-to-close 쪽), 롱 레그는 `mark = mid − β·h`(sell-to-close 쪽)이다.
   - 포지션의 마크 손익률은 `(credit − Σ숏 mark + Σ롱 mark) / credit`이고 **전 레그 합산**이다. 이는 `src/signals.py:624-635`를 재현한다.
   - β=0은 마크가 mid라는 뜻이고, β=1은 마크가 즉시 체결 가능한 가격이라는 뜻이다. β>1은 stale last-trade 같은 추가 편향을 흡수한다.
   - β 격자는 {0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5}이다.
   - `mid`는 `black_scholes_price`(`src/vendor/options_pricing.py:41-58`)로 계산한다. 레그 합은 `price_spread_to_close`와 일치해야 한다(테스트 #2).
   - **식별 논리**:
     - β는 진입 직후 마크 손익에 결정적 오프셋 `−β·Σh/credit`을 만든다. 그래서 조기 SL만 늘고 조기 PT는 만들지 않는다. 라이브의 조기 SL 23/65 대 조기 PT 0/31이라는 비대칭이 β를 식별한다.
     - 오프셋은 크레딧/폭이 얇을수록 커지므로 T-b(SL 과슈팅 크기)가 같은 β로 맞아야 한다. 이것이 구조 검증이다.
     - 평균 0 대칭 노이즈 k는 조기 PT도 함께 만들기 때문에 라이브 비대칭과 충돌한다. 그래서 적합하지 않는다. k=0.5 한 값은 **민감도 표로만** 보고한다.
   - **시장가 교차 비용**은 호가 정의상 h로 고정되며 적합하지 않는다. 로그에 체결가가 없으므로 이 비용은 로그로 식별할 수 없다. 따라서 달러 손익(realized)은 **미보정 모델 출력**으로 표시한다.
3. **IV 앵커 (replay만).**
   - 거래마다 진입 시점의 `iv_scale`을 이분법(stdlib)으로 푼다. 조건은 BS 크레딧 × (1−0.05)가 라이브 지정가 크레딧과 같아지는 것이다. 콘도르는 4레그 합산 크레딧을 쓴다.
   - 이후 `iv_t = mtm_iv(date) × iv_scale`로 쓴다(`mtm_iv`는 `_rolling_iv_series`, `src/backtest.py:189-197`).
   - 관측값에서 결정적으로 나오므로 자유 파라미터가 아니다. 해가 없으면 `unanchorable`로 스킵하고 개수를 보고한다.
4. **체결 규칙.**
   - 레그 방향 규약: buy-to-close 레그(숏 레그 청산)의 체결가는 `bid + s·(ask−bid)`이다. sell-to-close 레그(롱 레그 청산)의 체결가는 `ask − s·(ask−bid)`이다. 순 청산 데빗은 Σbuy − Σsell이다.
   - market은 s=1이다(전폭 교차).
   - limit 주문(가격 L, 데빗)은 폴링 시점에 s 기준 순 데빗이 L 이하이면 **L에 체결된 것으로 본다**. 가격 개선은 가정하지 않는다.
   - s는 중앙값 0.5(mid 터치), 민감도 0.66(ORATS 2레그)으로 둔다. limit 체결 확률에 대한 실측 데이터가 없으므로 s는 밴드로만 보고한다.
5. **진입.** replay는 라이브 지정가 크레딧을 그대로 쓴다. generative는 vendor `construct_credit_spread`의 `credit_haircut_pct=0.05`를 그대로 쓴다(`credit_spread_simulator.py:148-151`). 진입 마찰을 이중으로 적용하지 않는다.

### 3. 청산 정책 엔진 (`run_exit_path`)

**입력**: `position`(2레그 또는 4레그, 크레딧, 수량, 만기), 15분봉 경로, IV 계열, `policy`, `friction`(table, β, s, h_mult), `start_after`, `end_at`, `live_close_path`(replay만).

**출력**: `{exit_ts, exit_reason, trigger_mark_pnl_pct, realized_pnl, n_polls, n_limit_retries}`. `end_at`까지 트리거가 없으면 `None`을 반환한다(레거시 패리티 모드 전용).

폴링 격자는 15분봉(`fetch_intraday_bars`, `src/backtest.py:87-107`)이다. replay는 진입 시각 이후 첫 봉부터 시작한다.

**콘도르**: 4레그 포지션은 put 수직과 call 수직의 `price_spread_to_close` 두 번 호출과 레그 mid 합으로 가격을 낸다. 트리거는 **4레그 합산** 마크 손익률로 판정하며, 이는 `src/signals.py:624-635`와 같다. 한쪽 수직 단독으로는 판정하지 않는다.

**만기**: 청산 전에 만기에 도달하면 수직마다 `price_spread_to_close`의 T=0 내재가치 분기로 정산하고, 교차 비용은 0으로 둔다. 배정이나 핀 리스크는 무시한다(미검증 한계).

정책(사전등록 5개, 추가 금지):
- `V0 live`
  - 마크 손익률이 +0.5 이상이면 PT, −1.0 이하이면 SL이다.
  - 청산은 **라이브 경로를 재현**한다. 기본은 market(s=1)이다.
  - replay에서 `live_close_path=="limit_fallback"`인 에피소드는 limit으로 청산한다. L = mid 순 데빗 + 5%·|mid 순 데빗|(`src/signals.py:703-720`)이다. 미체결이면 다음 폴링에서 PT/SL을 재평가하고 L을 새로 계산해 재제출한다. 라이브에서 관찰된 재시도를 재현한 것이다.
  - generative에서는 종목별 limit 폴백 확률(T3가 산출하는 에피소드 경험 비율)로 폴백 여부를 정한다. 시드를 고정한다.
- `V1 limit_then_market`
  - 트리거는 V0과 같다.
  - 트리거 폴링 t0에 L = mid 순 데빗으로 limit을 낸다.
  - t0, t0+1, t0+2 폴링에서 체결 규칙을 확인한다. 셋 다 미체결이면 t0+2에서 market(s=1)으로 청산한다.
  - s=0.5면 t0에서 즉시 체결되므로 낙관 쪽이다. s=0.66이면 mid가 유리하게 움직여야 체결된다.
- `V2 min_hold_60m`: 진입 후 60분 미만인 폴링에서는 SL 판정을 건너뛴다. PT와 체결 방식은 V0과 같다.
- `V3 no_stop`: SL이 없다. PT나 만기일 때만 청산한다.
- `V4 underlying_breach`: 기초자산 15분봉 종가가 숏 행사가를 돌파하면(콘도르는 어느 쪽이든) SL로 청산한다. 마크 SL은 쓰지 않고, PT와 체결 방식은 V0과 같다.

**레거시 패리티 모드**(테스트 전용): β=0, h=0, V0, market이다. `start_after`는 진입일 다음날 첫 봉(`src/backtest.py:143`과 동일)이다. IV는 `mtm_iv(bar_day)`를 쓰고 없으면 `entry_iv`로 폴백한다(`:159`와 동일). IV 앵커는 끈다. `end_at`은 원래 일봉 청산일의 마지막 봉이다.

### 4. 보정 절차 (과적합 통제)

1. **분할.** clean 에피소드를 진입 시각 순으로 정렬해 앞 60%(약 58개)를 train, 뒤 40%(약 38개)를 validation으로 둔다. 경계 날짜는 출력에 기록한다(예상은 2026-09-18 부근이며 구현 시 산출).
2. **적합.** β만 격자에서 고른다. 기준은 **train만으로** 계산한 손실, 즉 T-a, T-b, T-c의 정규화 오차 합이 최소인 값이다. 적합 함수는 경계 이후 행을 받으면 `ValueError`를 던진다. h, s, k, IV 앵커는 적합하지 않는다.
3. **목표 지표 4종.** 모두 로그와 같은 정의를 쓴다. 트리거 시점 마크 기준이며, 청산 정책은 V0이다.
   - T-a 승률: 첫 청산 사유가 `profit_target`인 비율. clean 전체는 31/96.
   - T-b SL 에피소드의 `trigger_profit_pct` 중앙값. clean 전체는 −1.40.
   - T-c 조기 트리거 비대칭: (i) 60분 안에 난 SL의 비율(clean 23/65)과 (ii) 60분 안에 난 PT의 비율(clean 0/31)이 한 쌍이다.
   - T-d regime별 승률의 순서: trend_up이 가장 낮아야 한다. regime별 n이 작아 수치 게이트는 두지 않는다.
4. **판정(validation에서).**
   - T-a, T-b, T-c(i)의 시뮬 값이 **validation 라이브 통계의 90% 부트스트랩 CI**(에피소드 단위 리샘플 2000회, 시드 고정) 안에 있어야 한다.
   - T-c(ii)는 라이브가 0이므로 CI 대신 **시뮬 조기 PT 비율 ≤ 0.10**을 기준으로 한다.
   - T-d는 순서가 일치해야 한다.
   - validation n≈38이라 CI가 넓다(검정력 약함). 이 한계는 결과표에 적는다.
5. **음성 대조군(필수 게이트).**
   - (A) 무마찰 설정(β=0, h=0)은 T-a 또는 T-c(i)에서 반드시 CI를 벗어나야 한다.
   - (B) "교차 비용만 있고 마크는 mid"인 설정(β=0, h 측정값)도 T-c(i)에서 CI를 벗어나야 한다. market 교차 비용은 트리거 뒤에 발생하므로 조기 SL을 만들 수 없기 때문이다.
   - 어느 쪽이든 CI 안에 들어오면 목표 지표가 마크 편향을 판별하지 못한다는 뜻이다. 이 경우 결론은 "귀속 불가"이고 설계 6으로 넘어가지 않는다.
6. **generative 진단(게이트가 아니며, 결과는 설계 6 조건 3이 사용).** 같은 기간 전략7을 generative로 돌려 크레딧/폭 분포를 라이브 clean 표본과 비교한다. 구간은 `<5%`, `5–10%`, `10–20%`, `≥20%`이다. 라이브 대비 `<5%` 구간 비율이 절반 미만이면 `credit_width_mismatch=True`로 기록한다. 원인 후보는 RV와 IV의 차이, 그리고 vendor 고정 `STRIKE_INCREMENT=0.5`(`src/backtest.py:50`)와 실제 행사가 격자의 차이다.

### 5. generative 주입 지점 (재구현 금지)

- **래퍼**: `vendor.credit_spread_simulator.simulate_trade`를 컨텍스트 매니저로 교체한다.
  - 래퍼는 원본을 `profit_target_pct=1.0, stop_loss_multiple=None`으로 호출해 스프레드 구성과 만기를 얻는다.
  - 그다음 2레그 수직으로 `run_exit_path`를 돌려 `exit_date/exit_reason/close_debit/realized_pnl`을 덮어쓴다.
  - `run_portfolio_simulation`은 `simulate_trade`를 모듈 전역 이름으로 호출한다(`:435`). 그래서 워밍업, 스킵, 동시보유 로직(`:388-426`)은 원본 그대로 유지되고, 동시보유 판정(`:421`)은 덮어쓴 `exit_date`를 따른다.
  - 15분봉과 IV는 클로저로 주입하며, 종료 시(예외가 나도) 원본을 복원한다. 이 경로에서는 `reprice_exits_intraday`를 호출하지 않는다.
- **콘도르 후처리**: 전략 신호 목록(`StrategySignal.spread_type`, `src/strategies.py:35-39`)에서 `iron_condor` 날짜 집합을 구한다.
  - 같은 (심볼, signal_date)인 `bull_put`과 `bear_call` 거래를 짝지어 4레그 포지션으로 `run_exit_path`를 다시 돌린다. 두 거래의 exit 필드는 **합산 판정 결과로 덮어쓴다**. `realized_pnl`은 각 수직의 청산 비용으로 나눈다.
  - 한 날짜가 콘도르와 방향성 신호에 동시에 나타나면 `ValueError`를 던진다(미검증 가정을 강제하는 장치).
  - 알려진 편향: vendor 동시보유 판정은 래퍼 단계의 수직별 exit를 봤다. 그래서 콘도르의 재진입 가능 시점이 근사치다. 영향받은 거래 수를 결과에 보고한다.
- **사이징 정합**: 후처리에서 `t["max_loss"] = t["width"]`로 덮어쓴다. 라이브 `width*100` 기준(`src/signals.py:568`)을 재현하기 위해서이며, 배경에서 말한 w/(w−c)배 과대 계약을 제거한다. 테스트 #24로 강제한다.
- **필터**(부분집합, 거래 리스트에 적용):
  - E1~E3: 크레딧/폭 ≥ 임계값(비교는 `>=`)
  - E4: 크레딧 ≥ $0.15이면서 (Σ레그 h)/크레딧 ≤ 0.5
  - U1: 심볼이 {SPY, QQQ, IWM, DIA}에 속함
  - D1: 전략 신호 중 `iron_condor`만 남긴다. 구현은 `ALL_STRATEGIES`(`src/strategies.py:275`, `src/backtest.py:323`)에 임시 이름을 등록하는 방식이며, 컨텍스트 종료 시 제거한다.
  - OI/volume 필터는 과거 데이터가 없어 **평가 불가**이다. 사전등록 목록에서 제외하고 라이브 관찰 과제로 넘긴다.
- **P1 포트폴리오**:
  - `scale_trades_to_dollars`(`src/portfolio.py:66`)에 `max_open_risk_pct: float | None = None`을 추가한다. None이면 기존과 같다.
  - 값이 있으면 미청산 거래의 `max_loss×100×contracts` 합에 신규 거래 리스크를 더한 값이 `equity×cap`을 넘을 때 그 진입을 스킵하고 개수를 집계한다. 미청산 목록은 기존 `pending_exits` 힙의 청산일로 판단한다.
  - `pct_max=0.05` 전달: `scale_trades_to_dollars`는 `t["risk_pct"]`만 읽는다(`:168`). 그래서 P1 컨텍스트에서 `backtest.risk_pct_for_atr_pct`를 `functools.partial(portfolio.risk_pct_for_atr_pct, pct_max=0.05)`로 몽키패치한다. `_risk_pct_series`(`src/backtest.py:287`)가 모듈 전역 이름으로 호출하기 때문에 이 방식이 통한다. 라이브에 반영할 때는 `signals.RISK_PCT_MAX`(`src/signals.py:41`)가 대상이다.

### 6. 비교 절차와 결정 규칙

**사전등록 조합(동결).** 기준 1개와 후보 12개이며, 결과를 본 뒤 추가하거나 변경하지 않는다. T7b는 아래 표를 그대로 `config/friction_variants.json`에 옮긴다.

| ID | 청산 | 진입 | 유니버스 | 구조 | 포트폴리오 | replay 평가 |
|---|---|---|---|---|---|---|
| C00 기준 | V0 | E0 | U0 | D0 | P0 | 기준 |
| C01 | V1 | E0 | U0 | D0 | P0 | 가능 |
| C02 | V2 | E0 | U0 | D0 | P0 | 가능 |
| C03 | V3 | E0 | U0 | D0 | P0 | 가능 |
| C04 | V4 | E0 | U0 | D0 | P0 | 가능 |
| C05 | V0 | E1 ≥0.10 | U0 | D0 | P0 | 가능 |
| C06 | V0 | E2 ≥0.20 | U0 | D0 | P0 | 가능 |
| C07 | V0 | E3 ≥0.33 | U0 | D0 | P0 | 가능 |
| C08 | V0 | E4 | U0 | D0 | P0 | 가능 |
| C09 | V0 | E0 | U1 | D0 | P0 | 가능 |
| C10 | V0 | E0 | U0 | D1 | P0 | 가능(replay에서는 regime=range 부분집합) |
| C11 | V0 | E0 | U0 | D0 | P1 | 불가(generative만) |
| **C12 주가설 H1** | V2 | E2 ≥0.20 | U0 | D0 | P1 | V2+E2 부분만 |

- **H1(C12)이 단일 사전등록 주가설**이다. 근거는 라이브에서 관찰된 두 기제에 각각 대응한다는 점이다. 조기 SL 23/65에는 V2가, 얇은 크레딧에는 E2가 대응한다. P1은 동시 26포지션 노출을 제어한다.
- H1은 보정 없는 유의수준으로 판정한다. C01~C11은 탐색 조합이며, 조건 2의 단측 부트스트랩 p값에 **Holm 보정**(11개, 가족 α=0.10)을 적용한다.
- **쌍대 차이 정의**: validation 에피소드 i마다 `d_i = pnl_F,i − pnl_C00,i`로 둔다. 필터로 탈락한 에피소드는 `pnl_F,i = 0`(미거래)으로 둔다. 평균은 **validation 전체 N**으로 나눈다. 손익은 모델 realized(라이브 qty×100 기준)이고 미보정 출력이다.

**수정안 묶음 F를 라이브 Task Contract(페이퍼)로 올리는 조건.** 아래를 모두 만족해야 한다.
1. 설계 4 보정(목표 4종과 음성 대조군 A·B)을 통과했다.
2. replay validation에서 d_i 평균이 0보다 크다. H1은 90% 부트스트랩 CI 하한이 0보다 커야 하고, 탐색 조합은 Holm 보정 후 유의해야 한다. `replay 평가=불가`인 C11은 이 조건 대신 조건 3만 본다.
3. generative 3년, 마찰 적용 기준으로 총손익 > 0, PF ≥ 1.2, MDD ≤ 20%(`PORTFOLIO_DD_KILL_PCT`, `src/portfolio.py:44`), 거래 수 ≥ 30을 만족한다. 이 조건은 **중앙 호가표와 비관 호가표(h×1.5) 두 경우 모두**에서 성립해야 한다. 단, E1~E4를 포함한 조합은 설계 4-6 진단이 `credit_width_mismatch=True`이면 조건 3을 **평가 불가**로 보고, 그 조합은 Task Contract 대상에서 **제외**한다(replay만으로 올리지 않는다).
4. V1을 포함한 조합은 s=0.66에서도 조건 2와 3을 유지해야 한다.

통과하는 F가 여럿이면 H1을 우선하고, 그다음은 변경 레버 수가 적은 조합을 고른다. **하나도 통과하지 못하면** 옵션 사이드는 `OPTIONS_CLOSE_ONLY`로 유지하고, 결론은 "라이브 가능한 수정안 없음"으로 기록한다. 이것도 유효한 결정이다.

### 범위 밖 (이 레이어에서 안 함)

- 크립토 슬리브 손익 분해
- `decision_error` 사유 로깅(`src/mcp_runner.py:604`, `:630`은 사유를 남기지 않음)
- 브로커 체결가 대조. Alpaca 계정 조회가 복구되면 T3 파서에 체결가 열을 추가하고 시장가 교차 비용 h를 검증한다.
- limit 재시도가 반복된 원인 조사(retry 11개)
- 라이브 코드 수정. Task Contract 이후의 일이다.

## 테스트

모든 테스트는 네트워크 없이 도는 합성 데이터만 쓴다. 리뷰어는 테스트마다 scratch 사본에 회귀를 주입해 **그 이유 문자열로 실패하는지** 확인한다(아래 "주입" 열).

| # | 테스트 | 검증 | 회귀 주입(실패해야 함) |
|---|---|---|---|
| 1 | `test_legacy_mode_parity_with_reprice_exits_intraday` | 합성 일봉과 15분봉(일봉 종가 = 그날 마지막 15분봉 종가)으로 `run_portfolio_simulation`(PT 0.5, SL 2.0)을 돌린 뒤 `reprice_exits_intraday`를 적용한다. 사유가 `*_intraday`인 거래는 레거시 모드 `run_exit_path`의 exit_ts와 close_debit이 같고, 사유는 접미사를 뺀 값과 같다. 일봉 판정이 유지된 거래는 레거시 모드가 `None`을 반환한다. | 폴링 시작을 진입 당일로 변경, 또는 IV 폴백 제거 |
| 1b | `test_friction_path_shifts_realized_not_trigger_when_beta0` | β=0, h>0이면 트리거 시각과 사유는 레거시 모드와 같고, realized는 교차 비용 Σh×100×qty만큼 작다. β=1이면 SL 경로 거래의 트리거가 같거나 더 이르다. | 마크에 h를 잘못 가산(β 무시) |
| 2 | `test_leg_mid_sum_matches_price_spread_to_close` | 레그별 BS mid 합이 vendor 함수 값과 1e-9 이내로 같다(2레그와 4레그 모두) | 롱 레그 부호 반전 |
| 3 | `test_fill_direction_buy_at_ask_sell_at_bid` | s=1이면 buy-to-close 레그는 ask, sell-to-close 레그는 bid에 체결된다. s=0.66이면 각각 `bid+0.66Δ`, `ask−0.66Δ`이다. | sell 레그에도 `bid+sΔ` 적용 |
| 4 | `test_fit_rejects_validation_rows` | 적합 함수에 경계 이후 행이 섞이면 `ValueError` | 날짜 경계 검사 제거 |
| 5 | `test_negative_controls_independent` | 기초자산 가격이 고정되고 IV가 일정한 경로를 쓴다. 이 경우 무마찰에서는 세타로 손익이 단조 증가하므로 SL이 해석적으로 0건이다. 하드코딩한 라이브 통계(조기 SL 35%, CI [0.2, 0.5])에 대해 대조군 A와 B는 판정 **실패**다. 크레딧/폭 2%인 거래를 β=1로 돌리면 첫 폴링 손익률이 −Σh/credit(해석값)이 되고 SL이 즉시 트리거된다. | 판정에서 T-c(i) 누락 |
| 6 | `test_parser_episode_rules` | fixture로 검증한다: 403 오류 진입은 제외, crypto `max_hold_days`는 제외, 레그 불일치 청산은 `leg_mismatch`, 레그 없는 limit은 `legs_unverified`, 청산 1회 limit은 `single_limit`, 20회 재시도(사유 교대)는 `retry`+`reason_flip`, 첫 레코드가 이벤트이며 `trigger_profit_pct`는 그 레코드에 있을 때만 채워진다 | 크립토 필터 제거, 또는 마지막 레코드를 이벤트로 사용 |
| 7 | `test_simulate_trade_monkeypatch_restored` | 컨텍스트 종료 후(예외가 나도) `vendor.credit_spread_simulator.simulate_trade is original`. D1 임시 전략과 P1 패치도 복원된다. | `finally` 복원 제거 |
| 8 | `test_generative_skips_preserved` | 워밍업 이전, 만기 거래일 없음, `max_concurrent_positions` 도달 세 가지 사유가 원본과 같은 `skipped_candidates`로 나온다 | 래퍼 안에서 워밍업 로직 재구현(메모리 사례 재현) |
| 9 | `test_open_risk_cap_none_is_identical` | **보유 기간이 겹치는 거래 3개 이상**을 입력으로 `max_open_risk_pct=None`이 기존 출력과 동일하다 | 기본값을 0.2로 변경 |
| 10 | `test_open_risk_cap_blocks_excess` | 캡을 넘는 동시 진입이 스킵되고, 앞선 거래가 청산된 뒤의 진입은 허용되며, 스킵 수가 집계된다 | 청산 반영 전에 캡 계산 |
| 11 | `test_credit_width_filter_boundaries` | 크레딧/폭 = 0.20이면 E2 통과, 0.20−1e-9이면 탈락, 0.20+1e-9이면 통과 | `>=`를 `>`로 변경 |
| 12 | `test_half_spread_lookup_floor_and_buckets` | 버킷 경계값(0.10, 0.50, 2.00)과 틱/2 하한이 적용된다 | 하한 제거 |
| 13 | `test_iv_anchor_unanchorable_skipped` | 크레딧 ≥ 폭인 거래는 `unanchorable`로 스킵되고 개수가 보고된다. 정상 거래는 앵커 후 BS 크레딧×0.95가 목표와 1e-6 이내로 같다. | 스킵 대신 iv_scale=1 폴백 |
| 14 | `test_bootstrap_ci_known_values` | 상수 입력이면 CI가 [x, x]이다. 0/1 배열(30개 1, 70개 0)의 비율 CI는 정규근사 [0.225, 0.375]와 ±0.03 이내이다. 같은 시드면 같은 결과이다. | 리샘플을 비복원 추출로 변경 |
| 15 | `test_v1_limit_fill_rule` | s=0.5면 t0에서 L에 즉시 체결된다. s=0.66이고 mid가 불변이면 t0+2에서 market(s=1)으로 체결된다. s=0.66이고 t0+1에 mid가 충분히 하락하면 L에 체결된다. | 폴백 대기를 W=0으로 변경 |
| 16 | `test_v2_min_hold` | 30분 시점의 SL급 마크는 무시되고 75분 시점에 SL이 난다. 15분 시점 PT는 허용된다. | 60분 게이트를 PT에도 적용 |
| 17 | `test_v3_no_stop_and_expiry_settlement` | −300% 마크에서도 청산하지 않는다. 만기 정산은 `price_spread_to_close`(T=0) 내재가치이고 교차 비용은 0이다. | 만기에 교차 비용 가산 |
| 18 | `test_v4_underlying_breach` | 마크가 −50%여도 기초자산이 숏 행사가를 돌파하면 SL이 난다. 콘도르는 콜과 풋 어느 쪽이든 동일하다. | 콘도르 콜 쪽 검사 누락 |
| 19 | `test_v0_limit_fallback_retry` | `live_close_path=limit_fallback`이면 L=mid+5%·|mid|이다. 미체결이면 다음 폴링에 재평가하고 재제출한다. `n_limit_retries`가 집계된다. | 재평가 없이 첫 L 고정 |
| 20 | `test_condor_combined_trigger` | 두 수직 크레딧이 같고 풋 쪽 −150%, 콜 쪽 +80%(합산 −35%)이면 SL이 나지 않는다. 수직별 판정이었다면 SL이 났을 입력이다. | 수직별 판정으로 변경 |
| 21 | `test_generative_condor_pairing` | 같은 날짜의 풋과 콜 거래가 짝지어져 exit 필드가 같다. 콘도르와 방향성 신호 날짜가 겹치면 `ValueError`이다. 영향받은 거래 수가 보고된다. | 짝짓기 키에서 심볼 누락 |
| 22 | `test_filters_e4_u1_d1` | E4는 크레딧 0.14(탈락)와 0.15(통과), Σh/크레딧 0.5 경계를 본다. U1은 NVDA 탈락, SPY 통과를 본다. D1은 방향성 신호를 제거하고 `ALL_STRATEGIES`를 원상 복원한다. | D1 등록 해제 누락 |
| 23 | `test_paired_diff_counts_filtered_as_zero` | 10개 중 4개가 탈락하면 평균 분모가 10이고 탈락분은 0으로 계산된다 | 분모를 남은 6개로 변경 |
| 24 | `test_sizing_max_loss_is_width` | 후처리 후 `max_loss == width`이다. 크레딧/폭 0.33 거래의 계약수가 라이브 공식 `floor(B/(w*100))`과 같다. | 덮어쓰기 제거 |
| 25 | `test_p1_pct_max_path` | P1 컨텍스트 안에서 `_risk_pct_series`의 최대값이 ≤ 0.05이고, 컨텍스트 밖에서는 ≤ 0.10으로 돌아온다 | partial 대신 사후 clip |
| 26 | `test_holm_known_pvalues` | p=[0.01, 0.04, 0.03], α=0.10 → 기각 집합이 해석값과 일치한다 | 보정 없이 α 비교 |
| 27 | `test_decision_rule_boundaries` | PF=1.2, MDD=0.20, n=30은 통과한다. PF=1.1999, MDD=0.2001, n=29는 실패한다. E 조합 + mismatch는 제외된다. | mismatch 제외 분기 삭제 |

테스트 실행 명령: `.venv/bin/python -m pytest tests/test_friction.py tests/test_friction_backtest.py tests/test_portfolio.py -q`. 기존 스위트 전체(`pytest -q`)도 계속 통과해야 한다.

## 열린 질문

1. **Alpaca 페이퍼의 마크와 체결 규칙.** `unrealized_pl`이 쓰는 가격(mid, last, indicative)과 페이퍼 체결 규칙은 미검증이다. → **권고:** 보정 대상을 "페이퍼 계좌 통계"로 명시하고 진행한다. 적합된 β가 그 차이를 요약한다. T1 수집기는 열린 포지션이 있을 때 포지션의 `current_price`와 같은 시각의 bid/ask를 함께 기록해, 나중에 β를 해석하는 근거로 쓴다.
2. **상용 과거 NBBO 구매 여부.** 대상은 ORATS, ThetaData, Cboe 등이고 가격·조건은 미확인이다. → **권고:** 지금은 사지 않는다. 보정은 통과했는데 중앙 호가표와 비관 호가표의 generative 결론이 엇갈릴 때만 재검토한다.
3. **retry 에피소드(11개)를 보정에서 빼도 되는가.** 빼면 최악의 마찰 표본이 누락된다. → **권고:** 게이트는 clean만으로 판정한다(`profit_pct`가 없어 T-b를 정의할 수 없음). T-a와 T-c는 112개 전체로 민감도를 보고한다. 두 결과가 결론을 뒤집으면(전체에서 CI 밖) 보정을 "조건부 통과"로 표기하고 Task Contract에 리스크로 명시한다.

## 구현 핸드오프 태스크 분할

Codex는 `-m gpt-reserve`를 쓴다. 태스크 1개가 디스패치 1개다. 모든 태스크는 worktree `friction-backtest`에서만 작업하고, `src/mcp_runner.py`, `src/signals.py`, launchd, registry는 수정하지 않는다(읽기 전용).

| 태스크 | effort | 입력 | 출력 | 테스트 |
|---|---|---|---|---|
| **T1 호가 패널 수집기** | low | `signals.fetch_chain`, `OPTION_SYMBOLS`, `.env.competition` 키(읽기 전용 시장데이터) | `scripts/collect_option_quotes.py`: 1회 실행 시 `ts, underlying, occ, bid, ask, mid, delta, dte, feed`를 `data/quote_panel/YYYY-MM-DD.parquet`에 추가한다. 열린 포지션의 `current_price`도 함께 기록한다. 종목별 try/except와 로깅을 넣는다. 스케줄링은 하지 않는다. | 가짜 chain으로 스키마, 0·음수 호가 제외, 한 종목 예외 시 나머지 계속 진행을 검증 |
| **T2 반폭 테이블** | low | T1 스키마 | `src/friction.py`: `SYMBOL_CLASS`, `build_half_spread_table`, `half_spread` → `data/friction/half_spread_table.csv` | #12 |
| **T3 라이브 에피소드 파서** | medium | `decisions.jsonl` 경로 인자(읽기 전용) | `src/friction_backtest.py::load_live_episodes(path) -> (episodes, exclusion_counts)`, 설계 1의 규칙 1~7, 종목별 limit 폴백 비율 | #6 |
| **T4a 가격·앵커·마크** | low | T2, vendor 함수 | `src/friction.py`: `leg_mids`, `implied_iv_scale`, `mark_pnl_pct(position, β, table)`, `fill_debit(legs, s)` | #2, #3, #13 |
| **T4b 정책 V0–V4(2레그)** | medium | T4a | `run_exit_path`(2레그), 레거시 패리티 모드, V1 limit 규칙, V0 폴백 재시도 | #1, #1b, #15, #16, #18(2레그), #19 |
| **T4c 만기·콘도르 4레그** | medium | T4b | `run_exit_path`의 4레그 합산 경로와 만기 정산 | #17, #18(콘도르), #20 |
| **T5a replay와 β 적합** | medium | T3, T4c | `replay(episodes, policy, friction)`, `fit_beta(train)`, 음성 대조군 A·B 실행 | #4, #5 |
| **T5b 통계 유틸** | low | 없음 | `bootstrap_ci`, `paired_diff`, `holm` | #14, #23, #26 |
| **T5c 보정 CLI/보고서** | low | T5a, T5b | `python src/friction_backtest.py calibrate --decisions <path>` → `reports/friction/calibration-<date>.md`(경계, β, 목표별 CI/시뮬, 대조군, 민감도 k=0.5, 112개 민감도, n/스킵) | fixture로 보고서 필수 항목 존재를 확인 |
| **T6a 몽키패치 래퍼·콘도르 후처리** | medium | T4c, vendor | `patched_simulate_trade` 컨텍스트, `pair_condors`, `max_loss=width` 덮어쓰기 | #7, #8, #21, #24 |
| **T6b 필터** | low | T2 | `filter_e(threshold)`, `filter_e4`, `filter_u1`, `d1_strategy` 컨텍스트 | #11, #22 |
| **T6c 포트폴리오 수정** | medium | `src/portfolio.py` | `scale_trades_to_dollars(max_open_risk_pct=None)`, `p1_risk_pct` 컨텍스트 | #9, #10, #25 |
| **T7a 실행 입력 준비** | low | T2 테이블, T3 폴백 비율, `backtest.fetch_daily_bars/fetch_intraday_bars`(캐시 재사용) | `load_run_inputs(symbols, years)`: 호가표 로드, 23종목 봉과 IV 계열, 폴백 비율 | 가짜 client로 캐시 경로와 누락 종목 스킵 집계를 검증 |
| **T7b 조합 실행·판정** | low | T5b, T5c, T6a~c, T7a, 설계 6 표 | `config/friction_variants.json`(표 그대로 13행), `compare` → `reports/friction/compare-<date>.md` | #27, 표에 없는 ID는 거부 |

순서는 다음과 같다. T1, T3, T5b는 병렬로 진행한다. T1 데이터는 장중 5거래일 수동 실행으로 모은다. 그다음 T2 → T4a → T4b → T4c → (T5a → T5c), (T6a, T6b, T6c) → T7a → T7b이다. T5c 보정이 음성 대조군이나 validation에서 실패하면 T7b는 돌리지 않고, 그 실패 보고서가 곧 결론이다.

## 데이터 한계 (결과 해석 시 반드시 함께 인용)

- 보정 표본은 clean 96개(validation 약 38개)다. 부트스트랩 CI가 넓고 regime별 n이 작다. 제외된 16개(limit·retry)는 호가 공백 종목에 몰려 있어 최악의 마찰을 과소대표한다.
- 진입 크레딧은 지정가를 가정했다(실체결이 더 유리했을 수 있음). 청산 체결가는 로그에 없으므로 달러 손익은 미보정 모델 출력이다.
- 기간은 2026-08-25~09-30 단일 레짐이고 급락일이 여러 번 있었다(08-28 계좌 리셋 이전 거래 포함).
- 보정 대상은 Alpaca **페이퍼** 체결·마크다. 실계좌 마찰과 다를 수 있다.
- 호가표는 2026년 10월 이후 수집분을 3년 과거에 적용한다(상대 스프레드 정상성 가정).
