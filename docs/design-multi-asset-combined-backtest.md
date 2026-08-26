# design-multi-asset-combined-backtest

**상태**: 설계 (검토 대기) — 이 문서는 **구현 승인 게이트**다. 이 문서 자체는
`src/signals.py`·launchd 설정·라이브 경로를 건드릴 권한을 주지 않는다 (Trading Safety).

- 작성: 2026-08-26 PDT
- 워크트리: `atlas-options-hackathon-worktrees/multi-asset` (브랜치 `multi-asset-backtest`)
- 목적: **옵션 + 주식 + 크립토 3개 슬리브를 하나의 $100k 계좌·하나의 리스크예산에서
  동시에 돌렸을 때** 무슨 일이 나는지를 백테스트로 측정한다. 3개 독립계좌 합산이
  아니라 **예산 경합(budget contention)이 실제로 일어나는** 구조로.
- 범위: **백테스트 전용.** 라이브 배선은 별도 Task Contract (§7).

---

## 선행조사

### ① 레포 내 조사

지식그래프 훅이 grep/rg를 차단하는 프로젝트라 파일을 직접 Read로 전수 확인했다.
(부정 결과도 남긴다 — 다음 세션이 재조사하지 않도록.)

| 대상 | 확인한 것 | 판정 |
|---|---|---|
| `src/strategies.py` (254줄) | 10개 순수 신호함수 `ALL_STRATEGIES`, `df -> list[StrategySignal(date, spread_type, level, weight)]`. `spread_type ∈ {bull_put, bear_call, iron_condor}` | **재사용** — 방향성 신호가 이미 자산군 무관한 일봉 로직 |
| `src/backtest.py` (491줄) | 옵션 전용 Black-Scholes 파이프라인. `_generate_raw_trades`/`run_combined_portfolio`/`run_combined_portfolio_intraday_entry`, parquet 캐시(`data/cache/`, 20h TTL) | **옵션 슬리브 그대로 재사용**, 주식/크립토용 등가물은 없음 |
| `src/portfolio.py` (180줄) | `risk_pct_for_atr_pct`(ATR 기반 2~10%), `scale_trades_to_dollars`(진입일순 복리 사이징 + 일일/주간/HWM 킬스위치, min-heap 정산) | **재사용 + 2줄 확장** (§4.3) — 계좌 단위 예산 경합·킬스위치가 이미 여기 있다 |
| `src/signals.py` (639줄) | 라이브 전용. 레짐분류 하드코딩(`classify_regime_from_bars`), 리스크게이트 상수 8종. `ALL_STRATEGIES`를 임포트하지 **않음** | **읽기만** — 이 설계에서 건드리지 않음 |
| `src/indicators.py` (105줄) | `wilder_atr`/`adx`/`ema`/`rsi` 등, OHLC df만 받는 순수함수 | **자산군 무관하게 그대로 재사용** |
| `src/vendor/` | `credit_spread_simulator.py`(AlphaBot 복사본) 등 — **외부 레포 코드를 vendor/로 복사해 쓰는 관례가 이미 존재** | signal-alloc 벤더링의 선례 |
| 주식/크립토 손익엔진 | **레포 내에 없음** (전 파일 확인) | 신규 필요 |
| 다자산 배분 로직 | **레포 내에 없음** | 신규 필요 |

### ② 레포 밖 선행작업

| 이름 | 위치 | 라이선스 | 최근 커밋 | 판정 |
|---|---|---|---|---|
| `signal_alloc.py` | `~/dev/signal-alloc` (로컬 전용, 원격 없음) | 사내(본인 작성) | 2026-08-25 작성 | **채택 — `src/vendor/signal_alloc.py`로 복사** |
| AlphaBot `find_entry_signals` | `~/dev/Auto_bot/research/credit_spread_simulator.py:198` | 사내 | — | **이미 전략6으로 포팅 완료 — 추가 작업 없음** |
| trader `casper.py` (431줄) + `casper_backtest_engine.py` (334줄) | `~/dev/trader` | 사내 | — | **불채택 (§6.1)** |
| skfolio (공분산·상관 기반 포트폴리오 최적화) | PyPI | BSD-3 | 미확인(오프라인) | **불채택 — signal-alloc 이전 라운드가 명시적으로 스코프 밖으로 보류** |
| vectorbt / backtrader | PyPI | Apache-2.0 / GPL-3 | 미확인(오프라인) | **불채택 — 이 레포는 이미 자체 손익엔진·사이저·리포터를 갖고 있다. 프레임워크를 넣는 건 기존 검증자산을 버리는 것** |

**signal-alloc 실측 (전문 읽음, 79줄)**:
`signal_to_weights(signals, confidences, max_gross) -> weights`.
`raw = signal × confidence` → `gross = Σ|raw|` → `scale = min(1, max_gross/gross)` →
`weights = raw × scale`. 상태 없음, 시계열 없음, 손익 시뮬 없음. 입력검증(키 일치,
finite, confidence∈[0,1], max_gross>0)과 `gross==0` 가드가 있고 `demo()` 자체검증 포함.
**즉 "conviction 벡터를 그로스 상한 안으로 정규화하는 5줄짜리 함수"**다.

signal-alloc 이전 세션 노트가 남긴 경계:
> "trader/alphabot 배선(계약 변경 수준)과 상관관계 인지(skfolio 업그레이드)가 둘 다
> 의도적으로 스코프 밖 — 다음에 건드릴 땐 새 설계 라운드로 취급할 것."

**이 문서가 그 "새 설계 라운드"다.** 단 두 항목 중 상관관계(skfolio) 쪽은 여기서도
계속 보류한다 (§6.5).

### ③ 결론

- 옵션 슬리브: **기존 코드 그대로 재사용** (신규 0줄).
- 신호 로직: **전 슬리브가 `ALL_STRATEGIES`를 공유** (신규 0줄, §3).
- 배분기: **signal-alloc 벤더링** (복사 1파일, 수정 0줄).
- 사이징·킬스위치·복리: **`scale_trades_to_dollars` 재사용 + 2줄 일반화** (§4.3).
- **신규 코드는 주식/크립토 손익 루프 + 슬리브 오케스트레이션 1개 파일**(`src/multi_asset.py`)로 끝난다.

---

## 1. 문제 정의

지금 검증된 것은 "$100k 한 계좌 × 옵션 6종목"이다 (`run_combined_portfolio_intraday_entry`).
질문은 그 계좌에 주식·크립토를 **더 넣었을 때** 다음 셋이 어떻게 되는가:

1. **예산 경합** — 옵션 진입이 주식 포지션 때문에 못 나가는 일이 실제로 얼마나 생기나?
2. **총 성과** — 자산군 분산이 MDD를 줄이나, 아니면 같은 매크로 충격에 3배로 맞나?
3. **회전율** — 대회는 **창 안에서 실현된 P&L**만 채점한다. 슬리브 추가가 실현 거래 수를 늘리나?

### 성공 기준 (Acceptance Rubric — 구현 착수 **전** 고정)

| 차원 | 1점 | 3점 | 5점 |
|---|---|---|---|
| **환원 정합성** | 옵션 단독 실행이 기존 수치와 다름 | 근사 일치 | **옵션 슬리브 단독 = 기존 `run_combined_portfolio_intraday_entry`와 소수점까지 동일** |
| **예산 경합 실증** | 합산이 독립계좌 합과 같음(=경합 없음, 2026-08-24 버그 재발) | 다르지만 원인 미확인 | 다르고, 스킵된 진입 건수·사유가 로그로 집계됨 |
| **미래정보 누출** | 미검증 | 육안 확인 | 배분·사이징 전 경로가 trailing-only임을 테스트로 고정 |
| **신규 코드량** | 신규 파일 4개+ | 3개 | **신규 파일 2개 이하 + 기존 수정 10줄 이하** |
| **검증** | 테스트 없음 | 통과하는 테스트 | 회귀를 심으면 **실제로 깨지는** 것까지 실증 |

**게이트**: 환원 정합성 5점 **필수** (여기가 5점이 아니면 나머지 숫자는 전부 의미 없다).
나머지 평균 3.5 이상. 미달 시 재작업.

---

## 2. 슬리브 구조

3개 슬리브를 **하드코딩**한다. N-자산군 프레임워크를 만들지 않는다 (§6.3).

| 슬리브 | 유니버스 | 데이터 | 손익엔진 | 방향 |
|---|---|---|---|---|
| `options` | SPY, QQQ, GLD, TLT, SLV, IWM (라이브와 동일) | `fetch_daily_bars` + `fetch_intraday_bars` (기존 캐시) | 기존 Black-Scholes 크레딧스프레드 | 양방향 (스프레드 타입이 방향을 표현) |
| `equity` | SPY, QQQ, IWM | `fetch_daily_bars` (동일 함수·동일 캐시) | 신규 (§4.2) | **롱/숏 양방향** |
| `crypto` | BTCUSD, ETHUSD | `CryptoHistoricalDataClient` (신규, alpaca-py 0.44.0에 이미 있음) | 신규 (§4.2), equity와 **동일 코드** | **롱/플랫 전용** (§6.4) |

**옵션과 equity 유니버스가 겹치는 것은 의도**다. 같은 기초자산에 옵션 포지션과 주식
포지션이 동시에 잡히는 상황이 라이브의 `SAME_UNDERLYING_RISK_CAP_PCT`(3%)가
막으려는 바로 그 상황이고, 백테스트가 그걸 재현해야 의미가 있다.

크립토는 24/7이라 주말 진입일자가 생긴다. `scale_trades_to_dollars`는 진입일 정렬
기반이라 주말 타임스탬프가 섞여도 그대로 동작한다 (주간 킬스위치 버킷팅은
`day - Timedelta(days=day.dayofweek)`라 토/일도 그 주에 정상 귀속).

---

## 3. 신호: 3개 슬리브가 같은 전략함수를 공유한다

**결정: 주식/크립토 슬리브도 `ALL_STRATEGIES`의 같은 일봉 신호함수를 쓴다.**
Casper를 포팅하지 않는다 (§6.1).

`StrategySignal.spread_type`은 이미 방향을 표현한다. 주식/크립토는 매핑만 하면 된다:

```python
# src/multi_asset.py
DIRECTION = {"bull_put": +1, "bear_call": -1, "iron_condor": 0}

def directional_signals(name: str, df: pd.DataFrame, allow_short: bool) -> list[tuple[pd.Timestamp, int]]:
    """StrategySignal -> (date, +1|-1) 진입. iron_condor(중립)는 방향성
    포지션으로 표현 불가하므로 주식/크립토 슬리브에서는 진입 없음.
    allow_short=False면 -1 신호는 억제하고 그 건수를 집계해서 리포트한다
    (억제된 숏 신호 수를 안 보고하면 크립토 슬리브의 롱 편향이
    '전략이 좋아서'인지 '숏을 못 해서'인지 구분이 안 된다)."""
```

근거 (Simplicity First / Working Skeleton First):
- 옵션 슬리브가 이미 이 신호로 검증됐다 — 슬리브별로 다른 신호엔진을 쓰면 결과 차이가
  "자산군 효과"인지 "전략 차이"인지 분리가 안 된다. **같은 레짐, 다른 표현수단**이
  이 실험의 통제변인이다.
- 신규 신호코드 0줄.

**알려진 천장**: `iron_condor`(횡보) 레짐 날에는 주식/크립토 슬리브가 아무것도 안 한다.
옵션의 존재 이유(중립 레짐에서도 수익)가 그대로 드러나는 것이라 버그가 아니라 결과지만,
리포트에 "슬리브별 신호 발생일 수 / 중립레짐으로 스킵된 일수"를 반드시 찍는다.

---

## 4. 손익엔진

### 4.1 옵션 슬리브 — 변경 없음

`_generate_raw_trades` → `reprice_exits_intraday` → raw trade dict. 그대로.

### 4.2 주식/크립토 슬리브 — 신규 (최소 롱/숏 모델)

옵션 그릭스 없음. 진입/청산만 있는 바 단위 루프:

```python
# src/multi_asset.py
STOP_ATR_MULT = 2.0      # 손절 = 진입가 -/+ 2×ATR14
R_MULTIPLE    = 2.0      # 익절 = 손절폭 × 2 (trader Casper의 3.0보다 보수적 — 일봉 스윙)
MAX_HOLD_DAYS = 10       # 옵션 슬리브 DTE 5~9일과 보유기간 스케일을 맞춘다

def simulate_directional_trades(
    df: pd.DataFrame,            # 일봉 OHLC (crypto도 동일 스키마)
    entries: list[tuple[pd.Timestamp, int]],
    symbol: str,
    sleeve: str,
    qty_increment: float,        # equity/options=1.0, crypto=1e-4
) -> list[dict]:
    """entries의 각 진입에 대해 다음 봉 시가 진입 → 이후 봉의 high/low로
    손절/익절 도달을 확인 → MAX_HOLD_DAYS 초과시 종가 청산.

    반환 dict는 scale_trades_to_dollars가 그대로 먹을 수 있는 스키마:
      entry_date, exit_date, symbol, sleeve, direction,
      max_loss        = |entry_px - stop_px|          (1주당)
      realized_pnl    = (exit_px - entry_px) * dir    (1주당)
      multiplier      = 1.0                            (옵션은 100)
      qty_increment   = 1.0 | 1e-4
      risk_pct, weight (사이저가 읽음)

    동시보유는 종목당 1개 (라이브 mcp_runner의 _symbols_with_open_exposure
    가드, 그리고 옵션 슬리브의 max_concurrent_positions=1과 동일 규칙).

    # ponytail: 갭·부분체결·수수료·슬리피지 미반영. 3년 일봉 ETF/메이저코인
    # 기준 1차 근사로 충분하다고 판단 — 결과가 경계선이면 그때 trader의
    # _apply_slippage/_fee_amount 공식을 가져온다(라이선스·경로 문제 없음, 20줄).
    """
```

**같은 봉에서 손절·익절 둘 다 도달한 경우 손절 우선** (보수적). 이 규칙을 안 정하면
일봉 백테스트가 조용히 낙관 편향된다.

### 4.3 사이저: `scale_trades_to_dollars` 2줄 일반화

이 함수가 이미 하는 일 — 진입일순 복리, min-heap 정산, 일일/주간/HWM 킬스위치,
예산부족 진입 스킵 — 이 전부가 **다자산 공유계좌에서 필요한 것과 정확히 같다.**
자산군마다 다른 것은 계약승수와 수량 단위뿐이다.

```python
# src/portfolio.py — 기존 (옵션 전용)
max_loss_dollars = t["max_loss"] * CONTRACT_MULTIPLIER
contracts = int(risk_budget // max_loss_dollars) if max_loss_dollars > 0 else 0

# 변경 후 (자산군 무관)
max_loss_dollars = t["max_loss"] * float(t.get("multiplier", CONTRACT_MULTIPLIER))
inc = float(t.get("qty_increment", 1.0))
contracts = math.floor(risk_budget / max_loss_dollars / inc) * inc if max_loss_dollars > 0 else 0.0
```

옵션 거래 dict는 두 키가 없으므로 기존 동작이 **바이트 단위로 동일**하다
(`multiplier=100`, `inc=1.0` → `floor(b/m/1)*1 == int(b//m)`). 이게 §1 루브릭의
"환원 정합성 5점"을 성립시키는 지점이다.

`DollarTrade.contracts` 타입이 `int` → `float`로 바뀐다 (크립토 소수 수량).
`tests/test_portfolio.py` 영향 여부를 구현 시 확인할 것.

**이 함수가 곧 예산 경합 지점이다.** 3개 슬리브의 raw trade를 한 리스트에 합쳐서
한 번 호출하면, 진입일자 순으로 같은 `equity`를 나눠 쓰고 같은 킬스위치를 공유한다.
독립계좌 3개 합산과 결과가 **같게 나오면 그건 경합이 안 걸린 것 = 버그**다
(2026-08-24에 정확히 이 증상으로 flat-사이징 버그를 잡았던 이력).

---

## 5. `signal_to_weights` 배선

### 5.1 이 함수를 무엇으로 쓰는가

`signal_to_weights`는 **횡단면 1회 호출**이고 백테스트 루프가 아니다. 여기서의 역할을
정확히 못 박는다:

> **공유 그로스 예산을 슬리브별 confidence 비율로 쪼개는 정규화기.**
> 넷팅(옵션 롱 vs 주식 숏 상계) 엔진이 **아니다.**

넷팅은 상관관계 인지를 요구하고, 그건 signal-alloc 이전 라운드가 명시적으로 보류한
항목이다 (§6.5).

### 5.2 입력 구성

```python
def sleeve_conviction(sleeve_signals: list[StrategySignal], adx_by_date: pd.Series,
                      d: pd.Timestamp) -> tuple[float, float]:
    """그날 그 슬리브가 낸 신호들 -> (signal, confidence).

    signal     = mean(DIRECTION[s.spread_type])  in [-1, +1]
                 (그날 신호 없으면 0.0)
    confidence = mean(레짐강도):
                 방향성(bull_put/bear_call): clip((adx - 20) / 20, 0, 1)
                 중립(iron_condor):          clip((18 - adx) / 18, 0, 1)

    ADX를 쓰는 이유: 전략7의 레짐분류 임계값(18/20)이 그대로 ADX 기반이라
    '그 전략 자신의 확신도'가 별도 모델 없이 나온다. 고정 confidence(1.0)를
    쓰면 signal_to_weights가 방향 부호만 보는 셈이 되어 배분이 사실상
    '신호 낸 슬리브 균등분할'로 퇴화한다 — 그러면 이 함수를 쓸 이유가 없다.

    # ponytail: 슬리브 내 종목별 가중 없이 단순평균. 슬리브 안에서의 종목
    # 배분은 기존 per-trade risk_pct(ATR 기반)가 이미 하고 있다.
    """
```

**중립(iron_condor) 슬리브의 부호 문제 — 명시적 결정**:
델타중립 콘도르는 `signal = 0` → `raw = 0` → 배분 0이 되어버린다. 이는 함수의 의미론
(방향성 익스포저 배분)과 우리 용도(리스크예산 배분)의 불일치다. 해결:

- 중립 신호의 `DIRECTION`을 0이 아니라 **`+1`로 매핑하되, 옵션 슬리브에서는 반환
  weight의 `|w|`만 쓰고 부호는 버린다** (스프레드 타입이 이미 방향을 담고 있으므로
  부호가 필요 없다).
- 주식/크립토 슬리브는 부호를 **쓴다** (롱/숏 판정).
- 결과: 옵션 슬리브의 `signals` 값은 "방향"이 아니라 **"리스크 배치 확신도"**다.
  이 비대칭을 코드 주석과 리포트에 반드시 명시한다. 이걸 안 적으면 다음 세션이
  "옵션 슬리브가 왜 항상 +냐"로 하루를 태운다.

### 5.3 weight → 슬리브별 사이징

```python
def sleeve_scales(weights: dict[str, float]) -> dict[str, float]:
    """|w| 정규화 지분. 한 슬리브만 신호를 내면 그 슬리브가 1.0
    (= 기존 옵션 단독 동작과 동일)."""
    total = sum(abs(w) for w in weights.values())
    return {k: (abs(w) / total if total > 0 else 0.0) for k, w in weights.items()}

# 각 raw trade에 최종 리스크% 부여
t["risk_pct"] = risk_pct_for_atr_pct(...) * sleeve_scale[t["sleeve"]]
```

`max_gross`는 **이 용도에서 무의미하다** — 균일 스케일이라 §5.3의 비율에서 약분된다.
그러니 `1.0`으로 고정하고, "튜닝 노브"인 척하지 않는다. 절대 레버리지는
`risk_pct_for_atr_pct`의 2~10% 상한과 `scale_trades_to_dollars`의 킬스위치가 이미 지배한다.
(`signal_to_weights`를 자체 정규화기로 재구현하지 않고 그대로 쓰는 이유는 사다리 2단 —
검증된 입력검증·`gross==0` 가드가 공짜로 딸려온다.)

### 5.4 루프 배치

```
for d in 전체 백테스트 날짜:
    per_sleeve = {s: sleeve_conviction(...) for s in ("options","equity","crypto")}
    w = signal_to_weights({s: sig}, {s: conf}, max_gross=1.0)
    scale = sleeve_scales(w)
    그날 진입하는 모든 raw trade에 risk_pct *= scale[sleeve]
→ 3슬리브 raw trade 전체를 하나로 합쳐 scale_trades_to_dollars(..., STARTING_EQUITY) 1회 호출
```

**미래정보 누출 금지**: `sleeve_conviction`은 날짜 `d`까지의 trailing 지표만 본다
(`adx_by_date`는 `_common_indicators`가 이미 trailing으로 계산). 배분 계산이 그날
이후의 손익을 절대 안 본다 — 테스트로 고정 (§8).

---

## 6. 하지 않는 것 (Scope Cuts)

### 6.1 Casper를 포팅하지 않는다

trader의 챔피언 `casper.py::run()`은 **불채택**. 근거:

- `src.data.loader_utils._is_webull_eligible`, `src.utils.global_time`,
  `.common.CONTEXT`(전역 가변 상태), `.position_rules.choose_exit_rule`,
  `slot_cache` 프로토콜에 의존한다. `casper_backtest_engine.py`는 이걸 돌리려고
  **eligibility 게이트를 monkeypatch로 우회**하고 `config/config.yaml`(gitignore, 로컬)을
  읽는다. 즉 "신호함수만 떼어내기"가 불가능한 형태다.
- 데이터 요구가 다르다: 1m/5m/15m 3개 타임프레임 × 3년 × 5종목 이상. 이 레포는
  15분봉조차 캐시로 겨우 감당 중이고 라이브 봇과 레이트리밋을 다투는 상태다.
- **결정적으로**: Casper는 NY 09:30-11:00 세션 전략이라 **크립토(24/7)에 그대로 안 붙는다.**
  "주식+크립토 공유예산"이라는 이 설계의 질문에 답하려면 어차피 자산군 공통 신호가 필요하다.

**업그레이드 경로**: §3의 일봉 공통신호 주식 슬리브가 명확히 부진하면(예: 3년 손익 음수),
그때 **별도 설계 라운드**로 Casper 포팅을 다룬다. 이 문서에 얹지 않는다.

### 6.2 trader / Auto_bot / signal-alloc 코드를 수정하지 않는다
읽기만 했다. signal-alloc은 `src/vendor/`로 **복사**(기존 vendor 관례) — 원본 무수정.

### 6.3 일반 N-자산군 프레임워크를 만들지 않는다
슬리브 3개 하드코딩. 슬리브 레지스트리·플러그인·설정파일 없음. 4번째 자산군이
실제로 생기면 그때 추상화한다 (YAGNI).

### 6.4 크립토 숏을 구현하지 않는다
Alpaca 크립토는 현물 롱 전용(비마진, 숏 미지원)이다. 따라서 크립토 슬리브는
**롱/플랫**. `bear_call`(하락) 신호는 진입 없이 카운트만 집계해 리포트한다 —
크립토 슬리브의 구조적 롱편향을 결과 해석에서 분리하기 위해서다.
주식 슬리브는 `allow_short=True`(SPY/QQQ/IWM은 대차 가능). **구현 첫 스텝에서
페이퍼 계좌의 실제 shortable 플래그를 확인**하고, 아니면 equity도 롱/플랫으로 내린다
(가정을 코드에 굳히지 말고 실측할 것).

### 6.5 상관관계·공분산 배분(skfolio)을 넣지 않는다
signal-alloc 이전 라운드가 명시적으로 보류한 항목. 슬리브가 3개뿐이고 3년치라
공분산 추정 자체가 노이즈다. `signal_to_weights`는 넷팅기가 아니라 정규화기로만 쓴다 (§5.1).

### 6.6 스크리너·옵션메트릭스·설정시스템을 만들지 않는다
유니버스는 §2의 고정 리스트, 파라미터는 모듈 상수. trader의 Stage0→1→2 스크리너를
가져오지 않는다.

### 6.7 주식/크립토 슬리브에 15분봉 청산 재평가를 하지 않는다 (v1)
옵션 슬리브는 기존 `reprice_exits_intraday`를 유지한다. 주식/크립토는 일봉
high/low로 손절·익절 도달을 보는 것만으로 이미 종가확인보다 정밀하다.

### 6.8 라이브 배선을 하지 않는다 → §7

---

## 7. 타임라인 — 정직한 판정

오늘 **2026-08-26**, 대회 개시 **2026-08-28**. 실질 가용 시간 **약 2일**.

### (a) 백테스트 전용 합산 시뮬 — **가능하다** (08-27 안)

| 스텝 | 산출물 | 규모 |
|---|---|---|
| A1 | `src/vendor/signal_alloc.py` 복사 + 출처 헤더 | 복사 |
| A2 | `src/portfolio.py` multiplier/qty_increment 일반화 | ~4줄 |
| A3 | `src/multi_asset.py` — 크립토 봉 fetch(캐시 재사용), `simulate_directional_trades`, `sleeve_conviction`, `sleeve_scales`, `run_multi_asset_portfolio` | ~250줄 |
| A4 | `tests/test_multi_asset.py` (§8) | ~120줄 |
| A5 | 3년 실행 + 리포트 (슬리브별/합산, 억제된 숏 신호 수, 킬스위치 스킵 수) | 실행 |

A3은 Tier 2+ → **Codex 핸드오프 대상**(설계=이 문서, 타이핑=Codex). 리스크는
크립토 일봉 fetch뿐인데 alpaca-py 0.44.0에 이미 있고 옵션 슬리브 캐시 패턴을
그대로 쓴다. **(a)는 여유 있게 들어간다.**

### (b) 라이브 배선 — **08-28 전에는 못 한다. 하지 말아야 한다.**

포장하지 않고 그대로 적는다:

- 필요한 변경: `src/signals.py`에 주식/크립토 order-intent 생성 경로 추가,
  `mcp_runner.py`에 `place_option_order`와 별개인 주식/크립토 주문 경로,
  자산군 교차 익스포저 추적, 슬리브 공유 킬스위치, 페이퍼 스모크 사이클 1회 이상.
  **639줄짜리, 지금 실전에서 15분마다 돌고 있고 실제로 작동 중인 모듈**을 대회
  개시 하루 전에 뜯는 것이다.
- 대회는 **창 안에서 실현된 P&L만** 채점한다. 라이브 배선이 미묘하게 깨지면
  점수가 0이 되지, 낮아지는 게 아니다. 지금 잘 돌고 있는 옵션 봇을 그 리스크에
  걸 이유가 없다.
- Trading Safety상 어차피 **별도 Task Contract + 명시적 사용자 승인**이 필요하다.

**권고**:
1. 08-28 개시는 **현행 옵션 전용 라이브 그대로** 맞는다.
2. (a)를 08-27까지 끝내고 숫자를 본다.
3. 개선폭이 크고 견고하면, **대회 창 안에서** 별도 Task Contract로 배선한다 —
   페이퍼 스모크 통과 후, 그리고 옵션 슬리브를 끄지 않고 **추가**하는 형태로만.
4. 개선폭이 애매하면 대회 후로 미룬다.

### 사용자가 놓치고 있을 수 있는 것 (별개 지적)

대회 창은 **7일**이다. 주식 슬리브의 보유기간(§4.2, 최대 10일)은 그 창보다 길다 —
**창 안에 청산되지 않으면 실현 P&L에 안 잡힌다.** 백테스트가 3년 CAGR로 좋게 나와도
7일 채점에는 기여가 0일 수 있다. (a)의 리포트에 **"임의의 7일 창에서 실현된 거래 수"**
분포를 반드시 포함할 것. 이 지표가 낮으면 다자산 확장은 대회 목적으로는 무의미하고,
대회 후 운용 목적의 작업으로 성격이 바뀐다.

---

## 8. 검증 계획

`tests/test_multi_asset.py` — 프레임워크 추가 없음(기존 pytest).

1. **환원 정합성 (게이트)**: `run_multi_asset_portfolio(sleeves=("options",))`의
   `StrategyResult`가 기존 `run_combined_portfolio_intraday_entry`와 **완전 일치**.
2. **예산 경합 실증**: 합성 데이터로 3슬리브 실행 → 각 슬리브의 총 계약수가
   해당 슬리브 단독 실행 대비 **엄격히 작아야** 한다. 같으면 실패(2026-08-24 버그 패턴).
3. **크립토 소수 수량**: `qty_increment=1e-4`에서 BTC 가격대 진입이 0수량으로
   잘리지 않음. `qty_increment=1.0`에서는 정수로 떨어짐.
4. **배분 의미론**: 중립(iron_condor) 전용 날에 옵션 슬리브가 0이 아닌 배분을 받음
   (§5.2의 부호 결정이 실제로 동작하는지).
5. **숏 억제 집계**: `allow_short=False`에서 `bear_call` 신호가 진입 0건 + 카운터 증가.
6. **미래정보 누출**: 백테스트 종료일 이후 봉을 df에 추가해도 그 이전 날짜들의
   배분·사이징 결과가 불변.

**회귀 심기 실증**(메모리 규칙 `feedback_prove_tests_fail_by_injecting_the_regression`):
`sleeve_scales`를 상수 1.0 반환으로 바꿔서 테스트 2가 **실제로 깨지는 것**을 확인한 뒤
되돌린다. 되돌릴 때 `git checkout` 금지(공유 워크트리) — 편집으로 원복.

---

## 9. 산출물 요약

| 파일 | 성격 | 규모 |
|---|---|---|
| `src/vendor/signal_alloc.py` | 신규(복사) | 79줄 |
| `src/multi_asset.py` | 신규 | ~250줄 |
| `src/portfolio.py` | 수정 | ~4줄 |
| `tests/test_multi_asset.py` | 신규 | ~120줄 |
| `reports/multi-asset-*.md` | 실행 결과 | — |

**건드리지 않는 것**: `src/signals.py`, `src/backtest.py`(읽기만), launchd 설정,
`~/dev/trader`, `~/dev/Auto_bot`, `~/dev/signal-alloc`.
