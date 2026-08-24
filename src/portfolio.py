"""
거래 로그(계약당 raw Black-Scholes 단위) → 실제 달러 손익 변환.

vendor/credit_spread_simulator.py의 trade_log는 옵션 1주(승수 미적용, qty=1)
기준 raw 값이다. 이 모듈이 하는 일:
  1. 승수 100 적용 (계약 표준)
  2. 리스크예산(잔고 대비 %) 기준 실제 계약수 산정 — signals.py의
     risk_pct_for_atr_pct/contracts_for_max_loss와 동일 공식 재사용(백테스트와
     라이브가 같은 사이징 로직을 쓰게 하기 위함, 로직 이원화 금지)
  3. 체결 시점 순서로 잔고를 굴려(entry_date 기준 근사 — 동시 포지션이 겹칠 때
     "그 시점까지의 마지막 확정 잔고"를 기준 삼는 근사, 완전한 동시성 예약은
     대회 스코프 밖) 계약수·잔고곡선을 산출

# ponytail: 동시 3포지션이 리스크예산을 서로 "예약"하지 않고 마지막 확정잔고만
# 보는 근사다 — 실제로는 동시 포지션의 미실현 리스크도 예산에서 빼야 완전하지만,
# 백테스트 리포트 목적(전략간 상대비교)엔 이 정도 근사로 충분하다고 판단.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

CONTRACT_MULTIPLIER = 100


def risk_pct_for_atr_pct(atr_pct: float, atr_pct_low_q: float, atr_pct_high_q: float,
                          pct_min: float = 0.02, pct_max: float = 0.05) -> float:
    if atr_pct_high_q <= atr_pct_low_q:
        return pct_min
    scaled = (atr_pct - atr_pct_low_q) / (atr_pct_high_q - atr_pct_low_q)
    r = pct_max - (pct_max - pct_min) * max(0.0, min(1.0, scaled))
    return max(pct_min, min(pct_max, r))


@dataclass
class DollarTrade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    contracts: int
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
    "일어나지 않은 거래"가 손실로 잡히면 안 된다."""
    if not trades:
        return [], pd.Series(dtype=float)

    sorted_by_entry = sorted(trades, key=lambda t: t["entry_date"])
    equity_path: list[tuple[pd.Timestamp, float]] = [(sorted_by_entry[0]["entry_date"], starting_equity)]
    equity = starting_equity

    dollar_trades: list[DollarTrade] = []
    skipped_zero_contracts = 0
    for t in sorted_by_entry:
        weight = float(t.get("weight", default_weight))
        trade_risk_pct = float(t.get("risk_pct", base_risk_pct))
        risk_budget = equity * trade_risk_pct * weight
        max_loss_dollars = t["max_loss"] * CONTRACT_MULTIPLIER
        contracts = int(risk_budget // max_loss_dollars) if max_loss_dollars > 0 else 0
        if contracts <= 0:
            skipped_zero_contracts += 1
            continue
        dollar_pnl = t["realized_pnl"] * CONTRACT_MULTIPLIER * contracts
        dollar_trades.append(DollarTrade(
            entry_date=t["entry_date"], exit_date=t["exit_date"],
            contracts=contracts, dollar_pnl=dollar_pnl, raw_trade=t,
        ))

    for dt in sorted(dollar_trades, key=lambda x: x.exit_date):
        equity += dt.dollar_pnl
        equity_path.append((dt.exit_date, equity))

    equity_series = pd.Series(
        {ts: eq for ts, eq in equity_path}
    ).sort_index()
    equity_series = equity_series[~equity_series.index.duplicated(keep="last")]
    if skipped_zero_contracts:
        import logging
        logging.getLogger(__name__).info(
            "scale_trades_to_dollars: %d/%d signals skipped (risk budget < 1 contract)",
            skipped_zero_contracts, len(trades),
        )
    return dollar_trades, equity_series
