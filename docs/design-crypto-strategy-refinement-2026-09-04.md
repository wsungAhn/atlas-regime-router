# design-crypto-strategy-refinement

**상태**: 사전설계(제출 마감 이후 첫 작업 — 이제부터는 Design-First 절차대로 간다)

- 작성: 2026-09-04 PDT
- 배경: 해커톤 제출 완료 후, 페이퍼 계좌를 3주/6주/3개월 마일스톤(+15%/+20%/+30%)
  기준으로 계속 운용하기로 결정. 사용자가 크립토 슬리브의 두 가지 약점을
  지적: (1) 재진입 쿨다운 없음, (2) BTC/ETH를 독립 리스크 슬롯으로 취급.
  추가로 "이 둘에 대한 헤지가 될 만한 크립토가 있는지" 질문받아 조사.

## 1. 현재 상태

크립토 사이클(`run_crypto_cycle_once`, 24/7 15분 간격)은 옵션과 완전히
동일한 ADX/EMA 레짐 분류기(`classify_regime_from_bars`, 일봉 기준)를
재사용하며, `trend_up`일 때만 매수한다(숏 불가라 range/cash/trend_down은
무대응). 손절/익절은 ATR×1배 / 2R 고정 하나뿐(`CRYPTO_STOP_ATR_MULT`,
`CRYPTO_R_MULTIPLE`). 포지션 크기는 `equity*(1-CASH_RESERVE_PCT)/len(CRYPTO_SYMBOLS)`로
심볼당 균등 상한을 두는 것 외에, 심볼 간 관계는 전혀 보지 않는다.

**실측 사례** (2026-09-03 로그): BTC가 08:30에 `profit_target`(+4.2%)으로
전량 청산되고, 바로 다음 15분 사이클에 같은(그날 하루 종일 안 바뀌는 일봉
기준) `trend_up` 신호로 즉시 재진입. 우연히 상승장이라 문제없이 넘어갔지만,
레인지/트렌드 경계에서는 손절→즉시재진입→재손절 휩쏘가 구조적으로 가능하다.

## 2. 선행조사

**①레포 내 검색**: `signals.py`/`mcp_runner.py` 전체에서 "cooldown",
"correlation", "cluster" 키워드 없음 — 관련 로직이 아예 없다.

**②실측 데이터** (Alpaca `get_all_assets(asset_class=crypto)` 전수 조회 +
`get_crypto_bars` 일봉 205일 수익률 상관계수, 2026-09-04 측정):

| 페어 | 상관계수 (205일) |
|---|---|
| BTC/USD ↔ ETH/USD | 0.90 |
| BTC/USD ↔ SOL/USD | 0.85 |
| ETH/USD ↔ SOL/USD | 0.85 |
| BTC/USD ↔ PAXG/USD | 0.45 |
| ETH/USD ↔ PAXG/USD | 0.45 |

Alpaca 크립토 자산 73페어 전부 `shortable: false`(비마진 현물) — 진짜
반대포지션형 "헤지"는 애초에 구조적으로 불가능하다. 목록 내 어떤 알트코인도
BTC/ETH와 음의 상관을 보이지 않고, 오히려 SOL 등은 0.85로 더 강하게
동행한다. 유일하게 상관이 낮은 자산은 **PAXG**(금 연동 토큰, 0.45) —
"헤지"가 아니라 "분산 효과가 있는 세 번째 자산"으로 정확히 불러야 한다.
PAXG는 Alpaca 상장일이 최근(2026-02-12부터)이라 205일치가 전체 이력이다 —
표본이 짧다는 한계는 있음.

**③외부 선행작업**: 포트폴리오 이론의 "유효 베팅 수"(effective number of
bets / diversification ratio) 개념 — 동일가중 N자산·평균 상관 ρ̄일 때
`N_eff = N / (1 + (N-1)·ρ̄)`. 특정 라이브러리 코드를 가져오는 게 아니라
표준 공식이라 그대로 적용. BTC+ETH만 있는 현재 상태에 대입하면
`N_eff = 2/(1+1×0.90) ≈ 1.05` — 두 심볼을 갖고 있어도 사실상 독립 베팅
1개와 거의 같다는 뜻.

**결론**: 새 라이브러리·레포 불필요. (a) 쿨다운은 순수 룰 기반, (b) 상관
사이징은 표준 공식을 그대로 적용, (c) PAXG는 신규 심볼 추가(운영 결정,
아래 §5).

## 3. 설계 — A. 재진입 쿨다운

**규칙**: 레짐 판정이 일봉 기준이므로, 청산 후 재진입 금지 기간도 "다음
날짜가 바뀌기 전까지"(같은 UTC 캘린더 날짜)로 맞춘다 — 임의의 시간 상수를
만들지 않고 시스템이 이미 쓰는 신호 주기(일봉)에 맞춘 것. 같은 날짜 안에서
재진입해봐야 어차피 같은 일봉을 다시 확인하는 것이라 정보량이 없다.

**저장**: `registry/crypto_cooldown.json` — `{"BTC/USD": "2026-09-03", ...}`
(심볼 → 마지막 청산 날짜). `crypto_positions.json`은 청산되면 그 심볼
엔트리가 지워지므로(오픈 포지션 상태만 추적) 쿨다운은 별도 파일이 필요.

**순수함수**: `is_in_crypto_cooldown(symbol, last_exit_by_symbol, today) -> bool`
— `last_exit_by_symbol.get(symbol) == today`(같은 날짜)면 `True`.

## 4. 설계 — B. 상관 클러스터 공유 예산

**문제 재정의**: 현재도 코드는 이미 `equity*(1-reserve)/len(CRYPTO_SYMBOLS)`로
심볼당 상한을 두어 "BTC+ETH 동시진입 시 합산 명목가"는 이미
`equity*(1-reserve)`로 안전하게 잡혀있다(2배로 새지 않음, 확인 완료). 진짜
문제는 **운용 관점의 단일종목 집중 한도**와 **통계적 분산 효과**를 구분 못
하는 것 — BTC/ETH를 "서로 다른 두 자산"으로 취급해 각자 독립 슬롯 예산을
주는데, 실제로는 0.90 상관이라 "거의 같은 베팅을 두 장 산 것"에 가깝다.

**규칙**: 상관계수 ≥ `CRYPTO_CORRELATION_CLUSTER_THRESHOLD`(0.70)인 심볼들은
"클러스터"로 묶어, 클러스터 전체의 미결제 명목가 합이 **슬롯 하나치
예산**(`max_notional_per_symbol`, 기존 계산 그대로)을 넘지 못하게 한다.
BTC+ETH는 이 임계값(0.90 > 0.70)을 넘어 하나의 클러스터로 묶이고, PAXG는
둘과 모두 0.45로 임계값 아래라 독립 슬롯을 유지한다 — 진짜 분산 자산에게
불이익을 주지 않으면서, 사실상 같은 베팅인 심볼들만 예산을 나눠 쓰게 한다.

**상관계수 값**: §2 표의 실측값을 코드 상수(`CRYPTO_CORRELATION_PAIRS`)로
박아넣는다 — 매 사이클(15분마다) 상관계수를 재계산할 이유가 없다(205일
윈도우 상관계수가 15분마다 유의미하게 바뀌지 않음, 불필요한 API 호출·복잡도
추가). 대신 상수 옆에 "월 1회 정도 재측정 필요"라고 주석으로 남긴다 —
자동 재측정 파이프라인은 이번 스코프 밖(Working Skeleton First).

**순수함수**: `cluster_capped_notional(symbol, candidate_notional, open_notional_by_symbol, correlation_pairs, threshold, slot_budget) -> float`
— 후보 명목가를 "클러스터에 남은 여유 예산"으로 한 번 더 캡.

## 5. 설계 — C. PAXG 추가 (운영 결정)

`CRYPTO_SYMBOLS`에 `"PAXG/USD"` 추가. §4의 클러스터 로직 덕분에 PAXG는
BTC/ETH와 예산을 공유하지 않고 자기 슬롯을 온전히 갖는다. 단, PAXG는
Alpaca 상장 이력이 짧아(205일) 백테스트 검증이 얕다 — 라이브에서 소액
관찰부터 시작하고, 다른 크립토와 같은 리스크 게이트·손절/익절 로직을
그대로 적용한다(신규 로직 없음).

## 6. 구현 스코프

- `src/signals.py`: `CRYPTO_SYMBOLS`에 PAXG 추가, `CRYPTO_CORRELATION_PAIRS`/
  `CRYPTO_CORRELATION_CLUSTER_THRESHOLD` 상수, `is_in_crypto_cooldown`,
  `cluster_capped_notional` 순수함수 추가, `decide_crypto_for_symbol`에
  `open_notional_by_symbol` 파라미터로 배선.
- `src/mcp_runner.py`: `registry/crypto_cooldown.json` 로드/저장(기존
  `_load_risk_gate_state`류 패턴 재사용), 청산 시 오늘 날짜 기록, 진입 전
  쿨다운 체크로 스킵, 현재 오픈 포지션의 `market_value`로
  `open_notional_by_symbol` 구성해서 전달.
- `tests/test_signals.py`: 쿨다운·클러스터캡 순수함수 단위테스트.
- 감사: 이번 라운드는 로컬 테스트 + 셀프 리뷰로 충분(신규 로직이지만 전부
  순수함수 + 소규모, Codex 풀 감사는 다음 큰 변경 때 다시 돌린다).
