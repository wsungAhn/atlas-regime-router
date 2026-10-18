# Codex Handoff — T4a leg prices, IV anchor, mark P&L, fill debit — 2026-10-17

> **상태(Status):** `대기중 / pending`
> _(Executor: set `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-17T19:30-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** `src/friction.py`에 가격·IV 앵커·마크 손익·체결 함수 추가 (설계 태스크 T4a)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` 「설계 2. 마찰 모델」(항목 2 마크, 3 IV 앵커, 4 체결 규칙). 필요한 계약은 아래에 모두 적었다.
> **범위(IN):** `src/friction.py`에 **추가**(기존 T2 함수·상수는 변경 금지), `tests/test_friction.py`에 **추가**(기존 테스트 유지). **OUT:** 그 외 모든 파일 수정, 네트워크, 커밋.

---

## 0. 먼저 읽기

- **하지 말 것:** 기존 `src/friction.py`의 T2 코드(상수·`SYMBOL_CLASS`·`build_half_spread_table`·`half_spread`·`write_half_spread_table`) 수정 · 다른 src 수정 · 네트워크 · 커밋.
- **항상:** 테스트 실행 → §8 작성(감점 사유 먼저) → 상태 `검토대기`. 변이 검증은 "확인했다" 요약이 아니라 **실제 pytest 실패 출력 원문**을 붙여라.
- **모르면 추측 금지:** 멈추고 §8에 질문.

### 실행 환경
- 워크트리: `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`
- 테스트: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q` — **베이스라인 180 passed**, 그 아래면 회귀.

## 1. 배경

옵션 청산 정책을 호가 마찰까지 넣어 시뮬레이션하려고 한다. 이 태스크는 시뮬레이터의 **가격 계산 부품**이다: 레그별 BS mid, 진입 IV 앵커, 마크 손익(불리한 방향 오프셋 β·h와 선택적 노이즈), 체결 가격. 폴링 루프와 정책은 후속 태스크(T4b~)이다.

## 2. 사용할 기존 코드 (읽기 전용)
- `src/vendor/options_pricing.py`: `black_scholes_price(S, K, T, r, sigma, option_type)` (`option_type`은 `"call"`/`"put"`, T는 연 단위 양수 필수).
- `src/vendor/credit_spread_simulator.py`: `price_spread_to_close(S_t, short_strike, long_strike, T_remaining, r, iv_t, spread_type)` (`spread_type`은 `"bull_put"`/`"bear_call"`; T<=0이면 내재가치 분기). 테스트에서 레그 mid 합과 대조용으로만 쓴다.
- `src/signals.py`: `_occ_strike(symbol)`(마지막 8자리/1000), `_occ_expiration(symbol)`. OCC 타입은 심볼 뒤쪽 `...YYMMDD` 다음 문자 `C`/`P`(예: `GLD260831P00414000` → put, 행사가 414.0, 만기 2026-08-31). 기초자산은 심볼 앞 알파벳 부분. 직접 파싱 함수를 `parse_occ`로 새로 만들되 위 두 헬퍼를 재사용하라.
- `src/friction.py`(T2): `half_spread(table, underlying, mid)`.
- 위험무풍이자율은 호출자가 `r`로 넘긴다(기본값 만들지 말 것; 참고로 백테스트는 0.045).

## 3. 구현할 것 (모두 `src/friction.py`에 추가)

데이터 모양(고정):
- `position` = `{"legs": [{"occ": str, "side": "short"|"long"}, ...], "credit": float}`. `credit`은 주당 순크레딧(양수, ×100 전). 레그 수는 2(수직) 또는 4(콘도르). 레그 비율은 항상 1.
- 레그 가격 입력은 `S`(기초자산 가격), `T`(연 단위 잔존만기, `T<=0`이면 만기), `r`, `iv`(단일 변동성).

함수:
1. `parse_occ(occ) -> (underlying: str, expiry: date, option_type: "call"|"put", strike: float)`. 파싱 불가 시 `ValueError`.
2. `leg_mids(position, S, T, r, iv) -> list[float]`: 레그 순서대로 BS mid. `T<=0`이면 내재가치(call `max(S-K,0)`, put `max(K-S,0)`).
3. `spread_mid_debit(position, mids) -> float`: 순 청산 데빗 `Σ(short mid) − Σ(long mid)`.
4. `implied_iv_scale(position, S, T, r, base_iv, haircut=0.05) -> float | None`: 진입 시점 IV 앵커. `scale`을 이분법(stdlib만, numpy/scipy 루트파인더 금지)으로 풀어 `credit_bs(scale·base_iv) × (1 − haircut) == position["credit"]`(허용오차 1e-9 이내로 수렴)이 되게 한다. `credit_bs = Σ(short mid) − Σ(long mid)`(콘도르는 4레그 합). 탐색 구간 `[0.05, 5.0]`(모듈 상수 `IV_SCALE_BOUNDS`), 구간 안에 해가 없거나(양끝 부호 같음) `position["credit"]`가 **최대 수직 폭 이상**이면(`unanchorable`) `None`을 반환한다. 폭은 레그의 행사가에서 계산(같은 타입 레그끼리 `max strike − min strike`의 최대값).
5. `anchor_scales(entries, ...) -> (scales: list[float|None], n_unanchorable: int)`: `entries`는 `(position, S, T, r, base_iv)` 튜플 리스트. 각각 `implied_iv_scale`을 적용하고 `None` 개수를 센다. 스킵(제외)은 호출자 몫이고 이 함수는 개수만 보고한다. **iv_scale=1 폴백을 만들지 마라.**
6. `leg_halves(position, mids, table) -> list[float]`: 레그별 `half_spread(table, underlying, mid)`. `mid <= 0`인 레그는 `half_spread`가 ValueError를 내므로, mid가 0 이하이면 구간 `<0.10`의 값으로 대체하라: `half_spread(table, underlying, 0.05)`.
7. `leg_noise(rng, halves, k) -> list[float]`: `k == 0`이면 **rng를 건드리지 않고** `[0.0]*len(halves)` 반환. 아니면 레그마다 독립으로 `rng.normal(0.0, k*h)`.
8. `episode_rng(episode_index, seed=20260930) -> numpy Generator`: `numpy.random.default_rng([seed, episode_index])`.
9. `mark_pnl_pct(position, S, T, r, iv, beta, table, k=0.0, rng=None) -> float`: 레그 마크 = 숏 `mid + β·h + ε`, 롱 `mid − β·h + ε`(ε는 `leg_noise`, 레그·폴링마다 독립, `k>0`이면 `rng` 필수 → 없으면 `ValueError`). **각 마크는 0.0 아래로 내려가지 않게 클램프**(가격은 음수일 수 없음 — 설계에 없는 보정이므로 §8에 명시 보고). 반환 `(credit − Σ숏 mark + Σ롱 mark) / credit`. `credit <= 0`이면 `ValueError`.
10. `fill_debit(legs, s) -> float`: `legs`는 `[{"side":"short"|"long","bid":float,"ask":float}, ...]`. 숏 레그(buy-to-close) 체결가 `bid + s·(ask−bid)`, 롱 레그(sell-to-close) 체결가 `ask − s·(ask−bid)`. 순 데빗 `Σ숏 체결가 − Σ롱 체결가`. `s`는 0~1 밖이면 `ValueError`. **노이즈·β를 체결가에 넣지 마라.**

모든 입력 검증 실패는 `ValueError`(조용히 기본값으로 대체 금지). 매직넘버는 모듈 상수로.

## 4. 필수 테스트 (`tests/test_friction.py`에 추가, 합성 데이터, 네트워크 금지)
1. `test_leg_mid_sum_matches_price_spread_to_close`: 2레그 bull_put, 2레그 bear_call, 4레그 콘도르(put 수직 + call 수직)에서 `spread_mid_debit(leg_mids(...))`가 vendor `price_spread_to_close` 합과 1e-9 이내로 같다. `T<=0`(만기) 케이스도 포함.
2. `test_fill_direction_buy_at_ask_sell_at_bid`: s=1이면 숏은 ask·롱은 bid, s=0.66이면 각각 `bid+0.66Δ`, `ask−0.66Δ`; s=0이면 숏 bid·롱 ask; 범위 밖 s는 ValueError.
3. `test_iv_anchor_unanchorable_skipped`: 크레딧 ≥ 폭인 포지션은 `None`, `anchor_scales`가 그 개수를 정확히 보고한다. 정상 포지션은 앵커 후 `credit_bs(scale·base_iv)×0.95`가 목표 크레딧과 1e-6 이내로 같다. 콘도르도 한 건 포함.
4. `test_symmetric_noise_mark`: k=0이면 rng가 호출되지 않고(스파이/모킹으로 확인) 결과가 결정적이다. k=1, seed 고정, 레그 2개 × 10,000 샘플에서 ε 평균의 절대값 < 0.03·h, 표준편차가 k·h의 ±5% 이내, 레그 간 상관의 절대값 < 0.05. 같은 seed면 같은 결과. `fill_debit`는 노이즈를 받지 않는다(시그니처에 rng/k 없음, 같은 입력이면 항상 같은 결과). `k>0`인데 `rng=None`이면 ValueError.
5. `test_mark_pnl_beta_offset`: k=0에서 β=0이면 `mark_pnl_pct == (credit − 순 mid 데빗)/credit`, β>0이면 클램프가 안 걸리는 구성에서 정확히 `−β·Σh/credit`만큼 낮아진다(숏 레그 수와 롱 레그 수 모두 합산 — 롱 레그도 불리한 방향). 콘도르 4레그 케이스 포함.
6. `test_mark_clamp_nonnegative`: 롱 레그 mid가 β·h보다 작은 구성에서 마크가 0으로 클램프된다.
7. `parse_occ` 정상 2건(call/put, 티커 길이 다른 것 포함)과 비정상(짧은 문자열) ValueError. `leg_halves`가 mid<=0 레그에 `<0.10` 구간 값을 쓴다. `episode_rng` 재현성.

## 5. 변이(회귀 주입) 검증 — 각각 실제 pytest 실패 출력 원문을 §8에 붙여라
(a) 롱 레그 부호 반전(`spread_mid_debit`), (b) `fill_debit`에서 롱 레그에도 `bid+sΔ` 적용, (c) 앵커 실패 시 `scale=1.0` 폴백, (d) ε를 롱 레그에도 같은 값으로 공유, (e) 노이즈가 `fill_debit`에 들어감(체결가에 ε 가산), (f) 롱 레그 마크에 `+β·h`(방향 반전), (g) 클램프 제거, (h) k=0인데 rng를 호출, (i) 앵커 haircut 0.05 제거. 각 변이에서 실패해야 하는 테스트와 이유 문자열을 함께 적어라.

## 6. Acceptance Rubric (실행 전에 고정)

| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 대부분 맞으나 경계·부호 오류 | 레그 mid·앵커·마크·체결 규칙 전부 맞음 |
| 테스트 품질 | 항상 통과 | 일부 규칙만 | 7개 테스트 의미 있고 변이 9개가 원문으로 증명 |
| 범위 준수 | 범위 밖/T2 코드 수정 | 경미 이탈 | 지정 파일 2개 추가만, 커밋 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·클램프 보고·감점 사유 먼저 |

게이트: 모든 차원 ≥ 4.

## 8. Executor response (Codex writes here)

_(비어 있음 — 감점 사유 먼저.)_

## 9. Review log

_(비어 있음 — reviewer가 작성)_
