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


def test_portfolio_circuit_breaker_resumes_after_45min_even_without_recovery():
    """2026-08-27 회귀 방지 — 원래 코드는 halt_until이 지나도 portfolio_dd가
    여전히 20% 밑이면 곧바로 재발동해서 사실상 영구 정지였다. 신규진입이
    막히면 이 시뮬레이터는 잔고가 움직일 방법이 없어(청산 대기 중인 거래가
    없으면) 회복이 원천적으로 불가능한 폐쇄루프라, 실측으로 옵션 챔피언
    6종목 합산 백테스트가 최초 -20% 시점(2024-12-18) 이후 20개월간 거래
    0건으로 얼어붙는 걸 발견했다 — 3년 성과가 아니라 4.5개월 성과를 3년
    성과로 잘못 보고한 것. 45분 뒤엔 잔고가 그대로여도 재개돼야 한다."""
    # 손실이 반영돼 portfolio_dd가 실제로 20%를 넘는 건 "다음 거래 평가 시점"에야
    # 드러난다(그 손실을 낸 거래 자체는 아직 미실현이라 자기 자신은 못 막는다 —
    # 기존 test_portfolio_drawdown_circuit_breaker_blocks_entries_within_45_minutes
    # 와 동일 전제). 그래서 순서는: day1 손실 → day2가 처음으로 -20%를 발견하고
    # 자신이 그 발동 대상이 됨(막힘, 45분 정지 시작) → day3(정지 훨씬 지남)은
    # 여전히 -20% 밑이어도 재개돼야 한다. 일일/주간 킬스위치와 분리하려고
    # 서로 다른 날짜로 구성.
    trades = [
        _trade("2026-01-05 09:30", "2026-01-05 09:30", realized_pnl=-5.0, max_loss=1.0, weight=1.0),  # day1: -25% 손실 발생(체결)
        _trade("2026-01-06 09:30", "2026-01-06 09:30", realized_pnl=10.0, max_loss=1.0, weight=1.0),  # day2: 손실이 이제 보임 → 발동, 이 거래가 막힘
        _trade("2026-01-07 09:30", "2026-01-07 09:30", realized_pnl=10.0, max_loss=1.0, weight=1.0),  # day3: 정지기간 지남, 여전히 -20%대여도 재개
    ]
    dollar_trades, _ = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    # day1(손실)과 day3(45분+하루 지나 재개)는 체결, day2(발동 트리거)만 스킵.
    assert len(dollar_trades) == 2
    assert dollar_trades[-1].entry_date == pd.Timestamp("2026-01-07 09:30")


def test_portfolio_circuit_breaker_re_arms_after_recovery():
    """45분 뒤 재개된 상태에서 회복(<20%)을 한 번 찍으면, 그 다음 새로 20%
    밑으로 떨어질 때 다시 서킷브레이커가 걸려야 한다(무장해제가 영구적이면
    안 됨)."""
    # day1 손실 → day2가 발견해 막힘(발동) → day3 재개, 큰 이익으로 실제 회복
    # (<20%) → day4는 평시 거래로 정상 체결(무장해제 확인) → day5 새 손실 →
    # day6이 그 손실을 발견해 다시 막혀야 함(재무장 확인).
    trades = [
        _trade("2026-01-05 09:30", "2026-01-05 09:30", realized_pnl=-5.0, max_loss=1.0, weight=1.0),  # day1 손실
        _trade("2026-01-06 09:30", "2026-01-06 09:30", realized_pnl=10.0, max_loss=1.0, weight=1.0),  # day2 발동(막힘)
        _trade("2026-01-07 09:30", "2026-01-07 09:30", realized_pnl=10.0, max_loss=1.0, weight=1.0),  # day3 재개+회복
        _trade("2026-01-08 09:30", "2026-01-08 09:30", realized_pnl=1.0, max_loss=1.0, weight=1.0),   # day4 평시 체결
        _trade("2026-01-09 09:30", "2026-01-09 09:30", realized_pnl=-5.0, max_loss=1.0, weight=1.0),  # day5 새 손실
        _trade("2026-01-10 09:30", "2026-01-10 09:30", realized_pnl=1.0, max_loss=1.0, weight=1.0),   # day6 재발동(막힘)
    ]
    dollar_trades, _ = scale_trades_to_dollars(trades, starting_equity=100_000, base_risk_pct=0.05)
    kept_days = {t.entry_date.date() for t in dollar_trades}
    assert kept_days == {
        pd.Timestamp("2026-01-05").date(), pd.Timestamp("2026-01-07").date(),
        pd.Timestamp("2026-01-08").date(), pd.Timestamp("2026-01-09").date(),
    }  # day2, day6만 막혀야 함(각각 첫/두번째 발동의 트리거)


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
