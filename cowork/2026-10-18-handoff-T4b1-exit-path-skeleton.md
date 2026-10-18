# Codex Handoff — T4b1 exit-path polling skeleton (2-leg, V0 market, legacy parity) — 2026-10-18

> **상태(Status):** `대기중 / pending`
> _(Executor: set `진행중` on start, `검토대기` when done. Only the reviewer sets `완료`.)_
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-18T10:40-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** 청산 경로 폴링 골격 `src/exit_path.py::run_exit_path` (설계 태스크 T4b1)
> **설계 근거:** `docs/design-friction-calibrated-backtest-2026-09-30.md` 「설계 3. 청산 정책 엔진」. 필요한 계약은 아래에 모두 적었다.
> **범위(IN):** 새 파일 `src/exit_path.py`, 새 테스트 `tests/test_exit_path.py`. **OUT:** 다른 모든 파일 수정(특히 `src/friction.py`, `src/backtest.py`는 읽기 전용), 네트워크, 커밋.

---

## 0. 먼저 읽기

- **하지 말 것:** 위 범위 밖 수정 · 네트워크 · 커밋 · 기존 `reprice_exits_intraday`나 vendor 코드 복사 후 수정(필요하면 `import`).
- **항상:** 테스트 실행 → §8 작성(감점 사유 먼저) → 상태 `검토대기`. 변이 검증은 **실제 pytest 실패 출력 원문**을 붙여라(요약 금지).
- **모르면 추측 금지:** 멈추고 §8에 질문.

### 실행 환경
- 워크트리: `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`
- 인터프리터: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`
- 테스트: `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python -m pytest tests/ -q` — **베이스라인 188 passed**.
- 임포트 패턴: `src/friction.py` 상단과 같게(`sys.path.insert(0, str(Path(__file__).resolve().parent))` 후 `from friction import ...`, `from vendor... import ...`). `src.` 접두사 임포트 금지(스크립트 실행이 깨진다 — T4a에서 반려된 결함).

## 1. 배경

호가 마찰을 반영한 옵션 청산 시뮬레이터의 핵심 루프다. 15분봉 경로를 따라 포지션의 마크 손익을 계산하고 익절/손절 트리거를 판정한다. 이 태스크는 **2레그 수직 스프레드 + 정책 V0(market 청산)**만 다룬다. limit 경로·다른 정책·만기 정산·콘도르는 후속 태스크(T4b2, T4c, T4d)이다.

## 2. 기존 코드 (읽기 전용)

- `src/backtest.py:112-185` `reprice_exits_intraday`: 일봉 거래를 15분봉으로 재평가하는 **레거시 동작의 기준**. 핵심:
  - 폴링 창: `bars.index.normalize() > entry_date` 이고 `<= original_exit_date`(날짜 단위 비교).
  - 각 봉: `S_t = row["close"]`, `bar_day = bar_time.normalize()`, `days_to_expiry = (expiry_date.normalize() − bar_day).days`, `iv_t = float(mtm_iv.get(bar_day, entry_iv))`.
  - `days_to_expiry <= 0`이면 내재가치 `bull_put: max(0, min(width, short_strike − S))`, `bear_call: max(0, min(width, S − short_strike))`; 아니면 `price_spread_to_close(S_t, short_strike, long_strike, T_remaining=days/365, r, iv_t, spread_type)`.
  - 익절 `credit − close_debit >= credit × profit_target_pct`, 손절 `close_debit >= credit × stop_loss_multiple` → 첫 번째 만족 봉에서 종료(익절 먼저 판정). 못 찾으면 원래 거래 유지.
- `src/friction.py` (T2/T4a): `parse_occ`, `leg_mids`, `spread_mid_debit`, `leg_halves`, `mark_pnl_pct`, `fill_debit`, `half_spread` — 그대로 import해서 쓴다.
- `src/vendor/credit_spread_simulator.py`: `price_spread_to_close`.

## 3. 구현할 것 (`src/exit_path.py`)

```
run_exit_path(position, bars, mtm_iv, *, expiry, entry_iv, r, qty, policy="V0",
              start_after, end_at, pt=0.5, sl=-1.0, friction=None, iv_scale=1.0) -> dict | None
```
- `position`: T4a 형식 `{"legs":[{"occ","side"}...], "credit": float}`. 레그가 2개가 아니면 `ValueError`("4-leg paths arrive in T4d").
- `bars`: `DataFrame`, 인덱스는 tz 없는 `DatetimeIndex`, 컬럼 `close`(기초자산 15분봉 종가). `mtm_iv`: 인덱스가 날짜(정규화된 Timestamp)인 `pd.Series`.
- `expiry`: `date`/Timestamp(청산 전 만기 판정용). `entry_iv`: IV 폴백값. `r`: 무위험이자율(기본값 없음). `qty`: 계약 수.
- `policy`: `"V0"`만 허용, 다른 값은 `ValueError`.
- `start_after`(배타) 와 `end_at`(포함): 폴링 대상은 `start_after < bar_time <= end_at`인 봉만, 시간순.
- `pt`: 익절 임계 `mark_pnl_pct >= pt`. `sl`: 손절 임계 `mark_pnl_pct <= sl`(음수). 각 봉에서 **익절을 먼저** 판정.
- `friction`: `None`이면 **레거시 모드**(마크 = mid, 체결 = mid 데빗, 교차비용 없음). 아니면 dict `{"table": DataFrame, "beta": float, "h_mult": 1.0, "s": 1.0, "k": 0.0, "rng": None}` — `mark_pnl_pct`에 `beta`, `k`, `rng`를 넘기고 `table`은 **h_mult로 스케일한 복사본**(`h` 컬럼에 h_mult 곱, 원본 불변)을 쓴다(`scaled_table(table, h_mult)` 헬퍼로 분리). 체결가: 트리거된 봉에서 레그별 `bid = max(mid − h, 0)`, `ask = mid + h`(h는 `leg_halves`)로 `fill_debit(legs, s)`를 호출한다(T4b1에서는 s=1 market만 사용; `s`가 1이 아니면 `ValueError`).
- 각 봉의 가격 입력: `S = close`, `T = days_to_expiry/365`(`days_to_expiry = (expiry normalize − bar_day).days`), `iv = float(mtm_iv.get(bar_day, entry_iv)) × iv_scale`. `days_to_expiry <= 0`이면 `T=0`을 넘겨 T4a의 내재가치 분기를 쓴다(만기 정산의 교차비용 0 처리는 T4d 몫이므로 여기서는 건드리지 말 것).
- 반환 `dict`: `exit_ts`(봉 시각), `exit_reason`(`"profit_target"`|`"stop_loss"`), `trigger_mark_pnl_pct`, `close_debit`(주당; 레거시 모드는 mid 데빗, friction 모드는 `fill_debit` 결과), `realized_pnl`(달러 = `(credit − close_debit) × 100 × qty`), `n_polls`(트리거 봉까지 평가한 봉 수), `n_limit_retries`(항상 0). 트리거가 없으면 `None`.
- `legacy_window(entry_date, exit_date) -> (start_after, end_at)` 헬퍼: `reprice_exits_intraday`의 창과 정확히 일치하도록 `start_after = entry_date.normalize() + 1일 − 1ns`, `end_at = exit_date.normalize() + 1일 − 1ns`.
- 모든 입력 검증 실패는 `ValueError`(조용한 기본값 금지). 매직넘버는 모듈 상수.

## 4. 필수 테스트 `tests/test_exit_path.py` (합성 데이터, 네트워크 금지)

1. **`test_legacy_mode_parity_with_reprice_exits_intraday`**: 합성 `bars`(15분봉 close)와 `mtm_iv`로 vendor 거래 dict(`symbol, entry_date, exit_date, effective_dte_days, short_strike, long_strike, width, credit_received, spread_type, entry_iv, profit_target_pct, stop_loss_multiple`)를 직접 만들어 `backtest.reprice_exits_intraday`에 넣는다(`run_portfolio_simulation`은 쓰지 않는다 — 설계의 완화). 사유가 `*_intraday`인 거래는 `run_exit_path`(레거시 모드, `legacy_window` 사용, `pt=profit_target_pct`, `sl=−(stop_loss_multiple−1)`)의 `exit_ts`·`close_debit`이 같고 사유는 접미사를 뺀 값과 같다. 트리거가 안 나는 거래는 `None`. `bull_put`과 `bear_call` 각 2건 이상, 익절·손절 사례 모두 포함, `mtm_iv`에 없는 날짜가 섞여 `entry_iv` 폴백이 쓰이는 사례 포함. (OCC 심볼은 `f"{sym}{YYMMDD}{P|C}{int(strike*1000):08d}"`로 만든다. bull_put은 높은 행사가 put이 short, bear_call은 낮은 행사가 call이 short.)
2. **`test_friction_path_beta0_same_trigger_beta1_earlier`**: β=0, h>0이면 트리거 시각·사유는 레거시와 같고 `realized_pnl`은 교차비용 `Σh × 100 × qty`만큼 작다(`Σh`는 두 레그 반폭 합). β=1이면 손절 경로 거래의 트리거가 같거나 더 이르다. **엄격 부등식 케이스**: 크레딧/폭 3%, `Σh/credit = 1.5`가 되도록 만든 합성 거래는 β=1에서 첫 폴링에 손절이 나고 레거시는 그보다 뒤에 손절이 나도록 경로를 구성한다 → `exit_ts(β=1) < exit_ts(legacy)`.
3. 창 경계: `start_after`와 같은 시각의 봉은 제외(배타), `end_at`과 같은 시각의 봉은 포함. `n_polls` 값 검증.
4. 익절/손절이 같은 봉에서 모두 만족할 때 익절이 우선(합성으로 구성 가능한 경우; 불가능하면 §8에 이유를 적어라).
5. 트리거 없이 끝나면 `None`.
6. 검증 오류: 4레그 → ValueError, `policy != "V0"` → ValueError, `friction["s"] != 1` → ValueError, `r` 누락은 TypeError(기본값 없음).
7. `scaled_table`이 `h_mult` 곱셈만 하고 원본 DataFrame을 바꾸지 않는다. `h_mult=1.5`가 `realized_pnl`의 교차비용을 정확히 1.5배로 만든다(β=0 케이스).
8. 만기 도달 봉(`days_to_expiry <= 0`)에서 내재가치로 판정된다(레거시 파리티 테스트와 별개의 단위 케이스).

## 5. 변이(회귀 주입) 검증 — 각각 실제 pytest 실패 출력 원문을 §8에 붙여라
(a) 폴링 시작을 진입 당일로(`start_after` 배타→포함 또는 창 시작 변경), (b) IV 폴백 제거(`mtm_iv.get(bar_day, entry_iv)` → 필수 키), (c) 익절/손절 판정 순서 뒤집기, (d) `sl` 비교 부호 반전, (e) 마크에 h를 가산하지 않음(β 무시), (f) β 오프셋 부호 반전, (g) `scaled_table`이 원본을 변경, (h) `h_mult` 무시, (i) `end_at` 배타로 변경, (j) 만기 봉에서 `T=0` 대신 `T=1/365` 사용. 각 변이에서 실패해야 하는 테스트와 이유 문자열을 함께 적어라.

## 6. Acceptance Rubric (실행 전에 고정)

| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 레거시와 다름 | 대부분 일치하나 경계 오류 | 레거시 파리티·마찰 경로·창 경계 전부 일치 |
| 테스트 품질 | 항상 통과 | 일부 규칙만 | 8개 테스트가 의미 있고 변이 10건이 원문으로 증명 |
| 범위 준수 | 범위 밖 수정 | 경미 이탈 | 지정 파일 2개만, 커밋 없음, `src.` 임포트 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·불가능 케이스 사유·감점 사유 먼저 |

게이트: 모든 차원 ≥ 4.

## 8. Executor response (Codex writes here)

_(비어 있음 — 감점 사유 먼저.)_

## 9. Review log

_(비어 있음 — reviewer가 작성)_
