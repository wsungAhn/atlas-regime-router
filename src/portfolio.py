"""
거래 로그(계약당 raw Black-Scholes 단위) → 실제 달러 손익 변환.

vendor/credit_spread_simulator.py의 trade_log는 옵션 1주(승수 미적용, qty=1)
기준 raw 값이다. 이 모듈이 하는 일:
  1. 승수 100 적용 (계약 표준)
  2. 리스크예산(잔고 대비 %) 기준 실제 계약수 산정 — signals.py의
     risk_pct_for_atr_pct/contracts_for_max_loss와 동일 공식 재사용(백테스트와
     라이브가 같은 사이징 로직을 쓰게 하기 위함, 로직 이원화 금지)
  3. entry_date 순으로 거래를 처리하되, 그 진입 시점까지 이미 청산된 거래의
     손익을 먼저 잔고에 반영한 뒤 사이징 — 진짜 복리(min-heap으로 미청산
     거래의 청산일자 추적, 2026-08-24 수정: 이전엔 사이징 루프가 잔고를 한 번도
     안 갱신해서 전 거래가 항상 starting_equity 기준 flat 사이징이었던 버그가
     있었다 — 4종목 합산 포트폴리오 결과가 4개 독립계좌 총합과 소수점까지
     정확히 일치하는 걸 보고 발견)

# ponytail: 동시 포지션이 리스크예산을 서로 "예약"하지 않는다 — 실제로는 동시
# 보유 중인 포지션의 미실현 리스크도 신규 진입 예산에서 빼야 완전하지만,
# 백테스트 리포트 목적(전략간 상대비교)엔 이 정도로 충분하다고 판단.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import pandas as pd

CONTRACT_MULTIPLIER = 100

# signals.py DAILY_LOSS_KILL_PCT/WEEKLY_LOSS_KILL_PCT와 값을 맞춘 사본 — signals.py는
# sqlite/alpaca-py를 임포트해서(브로커 의존) 백테스트 쪽에서 그대로 못 끌어온다.
# risk_pct_for_atr_pct와 같은 이유로 이미 값 복제 중이던 패턴 재사용.
# 2026-08-24 발견: 이 두 상수가 정의만 되고 백테스트·라이브 어디서도 실제로
# 안 걸려 있었다 — 4종목 합산 계좌가 MDD 71.5%까지 찍는 걸 보고서야 드러남
# (진짜 킬스위치가 있었다면 그 지점 훨씬 전에 신규진입이 막혔어야 함).
DAILY_LOSS_KILL_PCT = 0.20  # 2026-08-25 오전: 0.03→0.20, signals.py와 동일 값(사용자 지시)
WEEKLY_LOSS_KILL_PCT = 0.50  # 2026-08-25 오전: 0.06→0.50, signals.py와 동일 값(사용자 지시)

# 2026-08-24 추가 — 사용자 지시: 계좌 사상최고치(HWM) 대비 -20% 낙폭 도달 시
# 전체 시스템 신규진입 45분 정지(라이브 15분 루프 기준 2회 패스 후 3번째 루프에서
# 재개). **정지가 잔고를 리셋하는 게 아니다** — HWM은 절대 낮추지 않고, 정지가
# 풀려도 이미 난 손실은 그대로 이어간다(사용자가 명시적으로 강조한 요구사항).
PORTFOLIO_DD_KILL_PCT = 0.20
PORTFOLIO_DD_HALT_MINUTES = 45


def risk_pct_for_atr_pct(atr_pct: float, atr_pct_low_q: float, atr_pct_high_q: float,
                          pct_min: float = 0.02, pct_max: float = 0.10) -> float:
    if atr_pct_high_q <= atr_pct_low_q:
        return pct_min
    scaled = (atr_pct - atr_pct_low_q) / (atr_pct_high_q - atr_pct_low_q)
    r = pct_max - (pct_max - pct_min) * max(0.0, min(1.0, scaled))
    return max(pct_min, min(pct_max, r))


@dataclass
class DollarTrade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    contracts: float
    dollar_pnl: float
    raw_trade: dict = field(repr=False, default_factory=dict)


def scale_trades_to_dollars(
    trades: list[dict],
    starting_equity: float,
    base_risk_pct: float = 0.05,
    default_weight: float = 1.0,
) -> tuple[list[DollarTrade], pd.Series]:
    """entry_date 순서로 정렬해 각 거래를 그 시점 잔고 기준으로 사이징하고,
    exit_date 순서로 잔고를 정산한다. weight(래더 레벨별 예산 배분, 기본 1.0)는
    trade dict에 'weight' 키가 있으면 그걸 쓴다.

    사이징 기준 리스크%는 trade dict에 'risk_pct'가 있으면 그걸 쓰고(변동성
    기반 동적 2~5% — signals.py 라이브 코드와 동일 공식으로 backtest.py가
    거래마다 미리 계산해 넣는다), 없으면 base_risk_pct(기본 5%, 상수)로
    폴백한다. **라이브가 죽어있던 risk_pct_for_atr_pct를 실제로 배선한 뒤
    (2026-08-24), 백테스트도 같은 동적 사이징을 안 쓰면 검증한 성과와 실제
    라이브 동작이 다른 사이징 기준으로 갈라진다** — 이 파라미터는 그 정합성을
    맞추기 위해 추가됐다.

    이 값(risk_pct 또는 base_risk_pct)은 전략묶음당 리스크(문서 §리스크 게이트
    2~5%)를 쓴다 — 3%(동일종목 상한)는 **여러 포지션 합산 개념**이지 개별
    거래 하나의 예산이 아니다. 최초 구현이 이 둘을 혼동해 래더 레벨 대부분이
    0계약으로 잘려나가는 버그가 있었다(실측: 전략2/3 SPY 151거래 전부 총손익 0
    — weight×3%가 SPY 스프레드 최대손실보다 항상 작았음). **0계약(예산 부족으로
    실제 진입 안 된) 거래는 반환 리스트에서 아예 제외** — 승률·PF 등 지표에
    "일어나지 않은 거래"가 손실로 잡히면 안 된다.

    일일 -3%/주간 -6% 손절 킬스위치도 시뮬레이션한다(그날/그주 시작 잔고 대비
    누적손실) — 이 임계값을 넘으면 이후 신규 진입 후보는 이미 계산된 신호라도
    사이징 자체를 스킵한다(청산은 이 함수 스코프 밖이라 계속 정상 진행)."""
    if not trades:
        return [], pd.Series(dtype=float)

    # 2026-08-24 수정: 예전엔 사이징 루프(risk_budget = equity * ...)가 전체 루프
    # 동안 `equity`를 한 번도 갱신 안 해서 모든 거래가 항상 starting_equity 기준
    # flat 사이징이었다(복리효과 0) — equity 갱신은 별도의 두 번째 루프(정산)에서만
    # 일어났는데 그건 이미 다 사이징이 끝난 뒤였다. 다종목 합산 포트폴리오
    # 백테스트에서 "합산 결과가 4개 독립계좌 합과 소수점까지 정확히 같다"는
    # 걸 발견해서 드러남 — 있을 수 없는 우연이라 역추적해서 찾은 버그.
    # 지금은 진입일자 순으로 처리하되, 그 진입일자 이전에 이미 청산된 거래의
    # 손익을 먼저 equity에 반영한 뒤 사이징한다(min-heap으로 미청산 거래의
    # 청산일자를 추적) — 이게 진짜 "그 시점 잔고 기준 사이징"이다.
    sorted_by_entry = sorted(trades, key=lambda t: t["entry_date"])
    equity = starting_equity
    equity_path: list[tuple[pd.Timestamp, float]] = [(sorted_by_entry[0]["entry_date"], starting_equity)]
    pending_exits: list[tuple[pd.Timestamp, float]] = []  # (exit_date, dollar_pnl) min-heap
    day_start_equity: dict[pd.Timestamp, float] = {}
    week_start_equity: dict[pd.Timestamp, float] = {}
    high_water_mark = starting_equity
    halt_until: pd.Timestamp | None = None
    portfolio_dd_breach_active = False  # 2026-08-27 버그 수정 — 아래 참고

    dollar_trades: list[DollarTrade] = []
    skipped_zero_contracts = 0
    skipped_kill_switch = 0
    for t in sorted_by_entry:
        while pending_exits and pending_exits[0][0] <= t["entry_date"]:
            exit_date, pnl = heapq.heappop(pending_exits)
            equity += pnl
            equity_path.append((exit_date, equity))

        high_water_mark = max(high_water_mark, equity)  # 절대 낮아지지 않는 사상최고치

        day = pd.Timestamp(t["entry_date"]).normalize()
        week = day - pd.Timedelta(days=day.dayofweek)
        day_start_equity.setdefault(day, equity)
        week_start_equity.setdefault(week, equity)
        daily_dd = 1.0 - equity / day_start_equity[day] if day_start_equity[day] > 0 else 0.0
        weekly_dd = 1.0 - equity / week_start_equity[week] if week_start_equity[week] > 0 else 0.0
        portfolio_dd = 1.0 - equity / high_water_mark if high_water_mark > 0 else 0.0

        if halt_until is not None and t["entry_date"] < halt_until:
            # 이미 -20% 정지 중 — 45분 지나기 전까지 신규진입 계속 억제
            skipped_kill_switch += 1
            continue
        if daily_dd >= DAILY_LOSS_KILL_PCT or weekly_dd >= WEEKLY_LOSS_KILL_PCT:
            # 일일/주간 손절 킬스위치 발동 — 그날/그주엔 신규 진입 억제(청산 감시는
            # 별개 경로라 이 함수 스코프 밖, 이미 열린 포지션엔 영향 없음)
            skipped_kill_switch += 1
            continue
        if portfolio_dd < PORTFOLIO_DD_KILL_PCT:
            portfolio_dd_breach_active = False  # 회복 확인 — 다음 하락은 새 서킷브레이커로 취급
        elif not portfolio_dd_breach_active:
            # 계좌 HWM 대비 -20% 서킷브레이커 최초 발동 — 45분 정지 시작(잔고는
            # 그대로 유지, HWM도 그대로 — "정지=리셋"이 아니다).
            #
            # **2026-08-27 버그 수정**: 원래 이 분기가 매 거래 후보마다 무조건
            # 재평가돼서, 45분 정지가 풀린 뒤에도 portfolio_dd가 아직 20%
            # 밑이면(신규진입이 막혀 잔고가 못 움직이니 당연히 그대로) 곧바로
            # 또 20% 이상으로 판정돼 무한 재발동했다 — 백테스트가 3년치를 다
            # 도는 게 아니라 최초 -20% 발생 시점에서 사실상 영구 동결됐다
            # (실측: 옵션 챔피언 6종목 합산 백테스트가 2024-12-18 이후 20개월간
            # 거래 0건, 그 시점 잔고를 "3년 성과"로 잘못 보고). 설계
            # 문서·docstring이 명시한 의도("정지 후 재개, 이미 난 손실은 그대로
            # 이어간다")대로 엣지트리거로 바꿈 — 서킷브레이커는 최초 하락 진입
            # 시 1회만 발동하고, 45분 뒤엔(여전히 20% 밑이어도) 거래를
            # 재개한다. 회복(<20%)을 한 번 찍어야 다음 하락에 다시 발동한다.
            halt_until = t["entry_date"] + pd.Timedelta(minutes=PORTFOLIO_DD_HALT_MINUTES)
            portfolio_dd_breach_active = True
            skipped_kill_switch += 1
            continue

        weight = float(t.get("weight", default_weight))
        trade_risk_pct = float(t.get("risk_pct", base_risk_pct))
        risk_budget = equity * trade_risk_pct * weight
        # multiplier/qty_increment는 옵션 외 자산군(주식/크립토)을 같은 사이저로
        # 태우기 위한 확장 — 옵션 거래 dict엔 이 키가 없어 기존 동작과 바이트
        # 단위로 동일하다(multiplier=100, inc=1.0 → floor(b/m/1)*1 == int(b//m)).
        multiplier = float(t.get("multiplier", CONTRACT_MULTIPLIER))
        qty_increment = float(t.get("qty_increment", 1.0))
        max_loss_dollars = t["max_loss"] * multiplier
        contracts = (
            math.floor(risk_budget / max_loss_dollars / qty_increment) * qty_increment
            if max_loss_dollars > 0 else 0.0
        )
        # entry_price가 있는 거래(직접 롱/숏 — 옵션 스프레드는 이 키가 없다)는
        # 손절폭이 가격 대비 좁으면 risk_budget/max_loss 역산이 계좌 잔고를
        # 넘는 notional을 요구할 수 있다(무마진 계좌는 못 산다). **실측 발견
        # (2026-08-26)**: 라이브 크립토 드라이런에서 같은 계산으로 notional이
        # 계좌의 162%가 나옴 — 옵션은 스프레드 폭이 손실을 자연히 캡해서 이
        # 문제가 없었지만, 직접 포지션은 명시적 상한이 필요하다. risk_budget이
        # 아니라 equity로 캡한다 — signals.py 라이브 크립토 사이징과 동일 철학
        # (risk_budget 캡은 손절폭 기반 사이징 자체를 무의미하게 만든다).
        entry_price = t.get("entry_price")
        if entry_price and entry_price > 0:
            max_notional_contracts = math.floor(equity / entry_price / multiplier / qty_increment) * qty_increment
            contracts = min(contracts, max_notional_contracts)
        if contracts <= 0:
            skipped_zero_contracts += 1
            continue
        dollar_pnl = t["realized_pnl"] * multiplier * contracts
        dollar_trades.append(DollarTrade(
            entry_date=t["entry_date"], exit_date=t["exit_date"],
            contracts=contracts, dollar_pnl=dollar_pnl, raw_trade=t,
        ))
        heapq.heappush(pending_exits, (t["exit_date"], dollar_pnl))

    while pending_exits:
        exit_date, pnl = heapq.heappop(pending_exits)
        equity += pnl
        equity_path.append((exit_date, equity))

    equity_series = pd.Series(
        {ts: eq for ts, eq in equity_path}
    ).sort_index()
    equity_series = equity_series[~equity_series.index.duplicated(keep="last")]
    if skipped_zero_contracts or skipped_kill_switch:
        import logging
        logging.getLogger(__name__).info(
            "scale_trades_to_dollars: %d/%d skipped (budget<1 contract), %d/%d skipped (kill switch)",
            skipped_zero_contracts, len(trades), skipped_kill_switch, len(trades),
        )
    return dollar_trades, equity_series
