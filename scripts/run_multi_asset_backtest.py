"""§A5 — 3슬리브(options+equity+crypto) 백테스트 실행 + 리포트.
사용: source .venv/bin/activate && set -a && source .env.competition && set +a
      python3 scripts/run_multi_asset_backtest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from multi_asset import run_multi_asset_portfolio, EQUITY_SYMBOLS, CRYPTO_SYMBOLS, OPTION_SYMBOLS
from backtest import run_combined_portfolio_intraday_entry, STARTING_EQUITY


def print_result(label: str, r) -> None:
    if r is None:
        print(f"{label}: no trades")
        return
    print(
        f"{label:<40} 거래수={r.n_trades:>5} 총손익=${r.total_pnl:>12,.0f} "
        f"수익률={r.total_pnl/STARTING_EQUITY:>7.1%} 승률={r.win_rate:>6.1%} "
        f"MDD=${r.max_drawdown:>10,.0f} Sharpe={r.sharpe:>6.2f} Calmar={r.calmar:>6.2f} PF={r.profit_factor:>6.2f}"
    )


def main() -> None:
    print(f"옵션 유니버스: {OPTION_SYMBOLS}")
    print(f"주식 유니버스: {EQUITY_SYMBOLS}")
    print(f"크립토 유니버스: {CRYPTO_SYMBOLS}")
    print()

    baseline = run_combined_portfolio_intraday_entry(years=3)
    print_result("기존 옵션단독 (run_combined_portfolio_intraday_entry)", baseline)

    options_only = run_multi_asset_portfolio(sleeves=("options",), years=3)
    print_result("multi_asset 옵션단독 (환원정합성 확인용)", options_only)
    if baseline is not None and options_only is not None:
        assert baseline.n_trades == options_only.n_trades
        assert abs(baseline.total_pnl - options_only.total_pnl) < 0.01
        print("  ✓ 환원 정합성 PASS (기존 결과와 소수점까지 일치)")
    print()

    equity_only = run_multi_asset_portfolio(sleeves=("equity",), years=3)
    print_result("주식 슬리브 단독", equity_only)

    crypto_only = run_multi_asset_portfolio(sleeves=("crypto",), years=3)
    print_result("크립토 슬리브 단독", crypto_only)

    combined = run_multi_asset_portfolio(sleeves=("options", "equity", "crypto"), years=3)
    print_result("3슬리브 공유계좌 합산", combined)
    print()

    if baseline is not None and combined is not None:
        print(f"옵션 단독 대비 3슬리브 합산 손익 차이: ${combined.total_pnl - baseline.total_pnl:,.0f}")


if __name__ == "__main__":
    main()
