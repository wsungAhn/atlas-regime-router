"""portfolio.py 유닛테스트 — 사이징 버그(0계약 거래가 손실로 잡히던 것) 회귀 방지."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio import scale_trades_to_dollars  # noqa: E402


def _trade(entry, exit_, realized_pnl, max_loss, weight=1.0):
    return {
        "entry_date": pd.Timestamp(entry), "exit_date": pd.Timestamp(exit_),
        "realized_pnl": realized_pnl, "max_loss": max_loss, "weight": weight,
    }


def test_zero_contract_trades_are_excluded_not_counted_as_losses():
    """예산이 max_loss*100보다 작으면 계약수가 0 — 이 거래는 '일어나지 않은 것'이라
    반환 리스트에서 빠져야 한다(과거 버그: realized_pnl이 양수였는데도 dollar_pnl=0이라
    win_rate 계산에서 패배로 잡혔었음)."""
    trades = [
        _trade("2026-01-01", "2026-01-30", realized_pnl=5.0, max_loss=1000.0, weight=0.2),  # 너무 큼 → 0계약
        _trade("2026-02-01", "2026-03-01", realized_pnl=3.0, max_loss=1.0, weight=1.0),  # 작음 → 여러 계약
    ]
    dollar_trades, equity = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    assert len(dollar_trades) == 1
    assert dollar_trades[0].contracts > 0
    assert dollar_trades[0].dollar_pnl > 0


def test_ladder_weight_scales_position_size():
    """같은 max_loss라도 weight가 크면(레벨3=0.5) 더 많은 계약이 들어가야 한다."""
    trades = [
        _trade("2026-01-01", "2026-01-30", realized_pnl=1.0, max_loss=5.0, weight=0.2),
        _trade("2026-01-01", "2026-01-30", realized_pnl=1.0, max_loss=5.0, weight=0.5),
    ]
    dollar_trades, _ = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    assert len(dollar_trades) == 2
    low_weight, high_weight = dollar_trades
    assert high_weight.contracts > low_weight.contracts


def test_equity_compounds_across_sequential_trades():
    trades = [
        _trade("2026-01-01", "2026-01-15", realized_pnl=10.0, max_loss=1.0),
        _trade("2026-02-01", "2026-02-15", realized_pnl=-5.0, max_loss=1.0),
    ]
    dollar_trades, equity = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    assert len(dollar_trades) == 2
    assert equity.iloc[-1] != 100_000


def test_sizing_uses_equity_after_prior_trade_settled_not_flat_starting_equity():
    """회귀 방지 — 2026-08-24 발견: 사이징 루프가 잔고를 한 번도 안 갱신해서
    두 번째 거래도 항상 starting_equity 기준으로 사이징되던 버그(다종목 합산
    포트폴리오 결과가 개별 계좌 총합과 정확히 일치하는 걸 보고 발견). 첫 거래가
    청산(exit)된 *뒤에* 진입하는 두 번째 거래는 불어난 잔고 기준으로 사이징돼야
    계약수가 더 나온다 — 이전 버그에선 두 거래의 계약수가 항상 같았다."""
    trades = [
        _trade("2026-01-01", "2026-01-15", realized_pnl=1.0, max_loss=1.0, weight=1.0),  # 큰 승리
        _trade("2026-02-01", "2026-02-15", realized_pnl=1.0, max_loss=1.0, weight=1.0),  # 1번 청산 후 진입
    ]
    dollar_trades, _ = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    assert len(dollar_trades) == 2
    first, second = dollar_trades
    assert second.contracts > first.contracts


def test_daily_loss_kill_switch_blocks_same_day_reentry():
    """하루 안에 -20% 넘게 잃으면 그날 남은 신규 진입은 다 막혀야 한다(2026-08-24
    발견 — 이 킬스위치가 정의만 되고 백테스트·라이브 어디서도 안 걸려 있었다.
    2026-08-25 오전 임계값 3%→20%로 재조정, 사용자 지시). 같은 날 두 번째
    거래는 손실 크기와 무관하게 스킵돼야 함(0계약 스킵과는 다른 사유 —
    budget<1이 아니라 kill switch)."""
    trades = [
        _trade("2026-01-05", "2026-01-05", realized_pnl=-4.5, max_loss=1.0, weight=1.0),  # 당일청산 -22.5% 손실
        _trade("2026-01-05", "2026-01-20", realized_pnl=10.0, max_loss=1.0, weight=1.0),  # 같은날 재진입 시도
    ]
    dollar_trades, _ = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    assert len(dollar_trades) == 1
    assert dollar_trades[0].dollar_pnl < 0


def test_portfolio_drawdown_circuit_breaker_blocks_entries_within_45_minutes():
    """계좌 HWM 대비 -20% 도달 시 45분간 신규진입 억제(2026-08-24, 사용자 지시:
    라이브 15분 루프 기준 2루프 패스 후 3번째 루프에서 재개). 손실을 낸 첫 거래
    자체는 막히지 않는다(그 시점엔 아직 손실이 반영 안 됐으므로) — 그 다음부터
    45분 안의 재진입 시도가 막혀야 한다."""
    trades = [
        _trade("2026-01-05 09:30", "2026-01-05 09:30", realized_pnl=-5.0, max_loss=1.0, weight=1.0),  # -25%
        _trade("2026-01-05 09:35", "2026-01-05 09:35", realized_pnl=10.0, max_loss=1.0, weight=1.0),  # 5분후, 정지구간
        _trade("2026-01-05 10:10", "2026-01-05 10:10", realized_pnl=10.0, max_loss=1.0, weight=1.0),  # 40분후, 정지구간
    ]
    dollar_trades, _ = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    assert len(dollar_trades) == 1
    assert dollar_trades[0].dollar_pnl < 0


def test_portfolio_circuit_breaker_does_not_erase_realized_losses():
    """서킷브레이커 정지가 걸려도 이미 난 손실은 그대로 유지돼야 한다 — "정지=잔고
    리셋"이 아니라는 사용자의 명시적 요구사항 회귀 방지. 정지 중에도 최종 잔고는
    starting_equity보다 한참 낮아야 한다(막힌 거래들의 잠재이익이 손실을 메꾸지
    못한다 — 애초에 정지시켰으니까)."""
    trades = [
        _trade("2026-01-05 09:30", "2026-01-05 09:30", realized_pnl=-5.0, max_loss=1.0, weight=1.0),
        _trade("2026-01-05 09:35", "2026-01-05 09:35", realized_pnl=10.0, max_loss=1.0, weight=1.0),
    ]
    _, equity = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    assert equity.iloc[-1] < 100_000 * 0.8
