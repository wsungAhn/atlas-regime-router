import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import friction_calibration as fc  # noqa: E402
from friction_calibration import (  # noqa: E402
    UNDEFINED_LOSS, build_replay_position,
    bootstrap_ses, calibration_loss, fit_beta, prepare_bars, replay, replay_episode,
    select_beta, split_train_validation, target_stats,
)


def episode(ts, kind="clean"):
    return {"entry_ts": pd.Timestamp(ts, tz="UTC"), "kind": kind}


def replay_episode_fixture(**overrides):
    result = {
        "symbol": "SPY", "entry_ts": pd.Timestamp("2026-01-01 10:00", tz="UTC"),
        "kind": "clean", "legs": ["SPY   260109P00450000", "SPY   260109P00455000"],
        "n_legs": 2, "qty": 1, "limit_credit": 1.0, "live_close_path": "market",
        "exit_reason": "stop_loss", "trigger_profit_pct": -0.2, "minutes_held": 15,
        "regime": "calm",
    }
    result.update(overrides)
    return result


def prepared_replay_bars(closes=(100.0, 100.0, 100.0)):
    return pd.DataFrame(
        {"close": closes},
        index=pd.to_datetime(["2026-01-01 09:45", "2026-01-01 10:15", "2026-01-01 10:30"]),
    )


def friction_table(h=1.0):
    return pd.DataFrame({
        "class": ["single_stock"] * 4,
        "bucket": ["<0.10", "0.10-0.50", "0.50-2.00", ">2.00"],
        "h": [h] * 4,
    })


def fit_replay_fixture():
    episode_row = replay_episode_fixture(
        limit_credit=3.0,
        trigger_profit_pct=-0.667,
        minutes_held=11520,
    )
    bars = pd.DataFrame(
        {"close": [452.0, 450.0, 440.0, 440.0]},
        index=pd.to_datetime([
            "2026-01-01 09:45", "2026-01-01 10:15",
            "2026-01-01 11:00", "2026-01-09 10:00",
        ]),
    )
    iv = pd.Series([1.0], index=pd.to_datetime(["2026-01-01"]))
    return episode_row, bars, iv


def test_split_train_validation():
    rows = [episode(f"2026-01-{day:02d}") for day in range(1, 11)][::-1]
    train, validation, boundary = split_train_validation(rows)
    assert len(train) == 6 and len(validation) == 4
    assert boundary == validation[0]["entry_ts"]
    assert boundary == pd.Timestamp("2026-01-07", tz="UTC")
    assert split_train_validation([episode(f"2026-01-{day:02d}") for day in range(1, 8)])[0].__len__() == 4
    assert len(split_train_validation([episode(f"2026-01-{day:02d}") for day in range(1, 9)])[0]) == 4
    with pytest.raises(ValueError): split_train_validation([episode("2026-01-01", "retry")])
    with pytest.raises(ValueError): split_train_validation([])


def test_split_rejects_non_clean_even_when_size_is_valid():
    rows = [episode(f"2026-01-{day:02d}") for day in range(1, 11)]
    rows[4]["kind"] = "retry"
    with pytest.raises(ValueError): split_train_validation(rows)


def test_prepare_bars_shifts_without_mutating_and_rejects_tz():
    index = pd.date_range("2026-01-01", periods=2, freq="15min")
    bars = pd.DataFrame({"close": [1, 2]}, index=index)
    shifted = prepare_bars({"X": bars})["X"]
    assert shifted.index[0] == index[0] + pd.Timedelta(minutes=15)
    assert bars.index[0] == index[0]
    with pytest.raises(ValueError): prepare_bars({"X": bars.tz_localize("UTC")})


def test_build_replay_position_uses_strike_rules_for_put_call_and_condor():
    put = {"legs": ["SPY   261106P00450000", "SPY   261106P00455000"], "n_legs": 2, "limit_credit": 1}
    result = build_replay_position(put)
    assert {leg["occ"]: leg["side"] for leg in result["legs"]}[put["legs"][0]] == "long"
    call = {"legs": ["SPY   261106C00470000", "SPY   261106C00465000"], "n_legs": 2, "limit_credit": 1}
    assert {leg["occ"]: leg["side"] for leg in build_replay_position(call)["legs"]}[call["legs"][0]] == "long"
    condor = {"legs": put["legs"] + call["legs"], "n_legs": 4, "limit_credit": 1}
    assert len(build_replay_position(condor)["legs"]) == 4
    with pytest.raises(ValueError): build_replay_position({**put, "n_legs": 4})


def test_replay_uses_only_completed_entry_bar(monkeypatch):
    captured = {}

    def anchor(position, S, T, r, base_iv):
        captured["S"] = S
        captured["T"] = T
        return 1.0

    def exit_path(*args, **kwargs):
        captured["entry_underlying"] = kwargs["entry_underlying"]
        return {"exit_ts": pd.Timestamp("2026-01-01 10:15"), "exit_reason": "stop_loss",
                "trigger_mark_pnl_pct": -1.0, "close_debit": 2.0, "realized_pnl": -100,
                "n_polls": 1, "n_limit_retries": 0}

    monkeypatch.setattr(fc, "implied_iv_scale", anchor)
    monkeypatch.setattr(fc, "run_exit_path", exit_path)
    episode_row = replay_episode_fixture()
    bars = prepared_replay_bars((101.0, 999.0, 100.0))
    iv = pd.Series([0.2], index=pd.to_datetime(["2026-01-01"]))
    replay_episode(episode_row, bars, iv, pd.DataFrame(), beta=0.0, r=0.0)
    assert captured["S"] == 101.0
    bars.iloc[0, 0] = 102.0
    replay_episode(episode_row, bars, iv, pd.DataFrame(), beta=0.0, r=0.0)
    assert captured["S"] == 102.0


@pytest.mark.parametrize("minutes", [45, 60, 75])
def test_replay_records_minutes_held_in_minutes(monkeypatch, minutes):
    def exit_path(*args, **kwargs):
        return {
            "exit_ts": kwargs["start_after"] + pd.Timedelta(minutes=minutes),
            "exit_reason": "stop_loss", "trigger_mark_pnl_pct": -0.2,
            "close_debit": 2.0, "realized_pnl": -100,
            "n_polls": 1, "n_limit_retries": 0,
        }

    monkeypatch.setattr(fc, "implied_iv_scale", lambda *args: 1.0)
    monkeypatch.setattr(fc, "run_exit_path", exit_path)
    row = replay_episode(
        replay_episode_fixture(), prepared_replay_bars(),
        pd.Series([.2], index=pd.to_datetime(["2026-01-01"])),
        pd.DataFrame(), beta=0.0, r=0.0,
    )
    assert row["minutes_held"] == minutes


def test_replay_passes_limit_fallback_and_preserves_fill_result(monkeypatch):
    captured = {}

    def exit_path(*args, **kwargs):
        captured["live_close_path"] = kwargs["live_close_path"]
        return {
            "exit_ts": pd.Timestamp("2026-01-01 10:15"),
            "exit_reason": "stop_loss", "trigger_mark_pnl_pct": -0.2,
            "close_debit": 2.35, "realized_pnl": -135,
            "n_polls": 2, "n_limit_retries": 1,
        }

    monkeypatch.setattr(fc, "implied_iv_scale", lambda *args: 1.0)
    monkeypatch.setattr(fc, "run_exit_path", exit_path)
    row = replay_episode(
        replay_episode_fixture(live_close_path="limit_fallback"),
        prepared_replay_bars(), pd.Series([.2], index=pd.to_datetime(["2026-01-01"])),
        friction_table(), beta=0.0, r=0.0,
    )
    assert captured["live_close_path"] == "limit_fallback"
    assert row["n_limit_retries"] == 1 and row["close_debit"] == 2.35


def test_replay_uses_entry_day_iv_and_qty_for_realized_pnl(monkeypatch):
    captured = []

    def anchor(position, S, T, r, base_iv):
        captured.append(base_iv)
        return 1.0

    def exit_path(*args, **kwargs):
        qty = kwargs["qty"]
        return {
            "exit_ts": pd.Timestamp("2026-01-01 10:15"),
            "exit_reason": "stop_loss", "trigger_mark_pnl_pct": -0.2,
            "close_debit": 2.0, "realized_pnl": 100.0 * qty,
            "n_polls": 1, "n_limit_retries": 0,
        }

    monkeypatch.setattr(fc, "implied_iv_scale", anchor)
    monkeypatch.setattr(fc, "run_exit_path", exit_path)
    bars = prepared_replay_bars()
    iv = pd.Series([0.2, 9.9], index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    first = replay_episode(replay_episode_fixture(qty=1), bars, iv, pd.DataFrame(), beta=0.0, r=0.0)
    second = replay_episode(replay_episode_fixture(qty=3), bars, iv, pd.DataFrame(), beta=0.0, r=0.0)
    assert captured == [0.2, 0.2]
    assert second["realized_pnl"] == pytest.approx(3 * first["realized_pnl"])


def test_replay_unanchorable_is_skipped_and_counts_are_fixed(monkeypatch):
    monkeypatch.setattr(fc, "implied_iv_scale", lambda *args: None)
    monkeypatch.setattr(fc, "run_exit_path", lambda *args, **kwargs: None)
    row = replay_episode_fixture()
    rows, counts = replay([row], {"SPY": prepared_replay_bars()},
                          {"SPY": pd.Series([.2], index=pd.to_datetime(["2026-01-01"]))},
                          pd.DataFrame(), beta=0.0, r=0.0)
    assert rows == []
    assert counts == {"no_bars": 0, "no_entry_bar": 0, "no_iv": 0, "unanchorable": 1, "no_exit": 0}


def test_replay_missing_symbol_counts_as_no_bars():
    rows, counts = replay([replay_episode_fixture()], {}, {}, pd.DataFrame(), beta=0.0, r=0.0)
    assert rows == [] and counts["no_bars"] == 1 and set(counts) == {
        "no_bars", "no_entry_bar", "no_iv", "unanchorable", "no_exit"
    }


def test_replay_uses_365_day_expiry_and_exact_end(monkeypatch):
    captured = {}
    monkeypatch.setattr(fc, "implied_iv_scale",
                        lambda position, S, T, r, base_iv: captured.setdefault("T", T) or 1.0)

    def exit_path(*args, **kwargs):
        captured["end_at"] = kwargs["end_at"]
        return None

    monkeypatch.setattr(fc, "run_exit_path", exit_path)
    row = replay_episode_fixture(legs=["SPY   260109P00450000", "SPY   260109P00455000"])
    replay_episode(row, prepared_replay_bars(), pd.Series([.2], index=pd.to_datetime(["2026-01-01"])),
                   pd.DataFrame(), beta=0.0, r=0.0)
    assert captured["T"] == pytest.approx(8 / 365)
    assert captured["end_at"] == pd.Timestamp("2026-01-10") - pd.Timedelta(nanoseconds=1)


def test_target_stats_boundaries_and_undefined():
    rows = [
        {"exit_reason": "profit_target", "trigger_pnl": .5, "minutes_held": 60, "regime": "a"},
        {"exit_reason": "stop_loss", "trigger_pnl": -.2, "minutes_held": 60, "regime": "a"},
        {"exit_reason": "stop_loss", "trigger_pnl": -.4, "minutes_held": 61, "regime": "b"},
        {"exit_reason": "expiry", "trigger_pnl": None, "minutes_held": 10, "regime": "b"},
    ]
    stats = target_stats(rows)
    assert stats["t_a"] == .25 and stats["t_b"] == pytest.approx(-.3) and stats["t_c_i"] == .5
    assert stats["t_c_ii"] == -.5 and stats["t_d"] == {"a": .5, "b": 0.0}
    assert target_stats([rows[-1]])["t_b"] is None
    with pytest.raises(ValueError): target_stats([])


def test_target_stats_uses_median_not_mean_and_marks_missing_targets():
    rows = [{"exit_reason": "stop_loss", "trigger_pnl": value, "minutes_held": 1, "regime": "a"}
            for value in (-.1, -.2, -10.0, -.4)]
    stats = target_stats(rows)
    assert stats["t_b"] == pytest.approx(-.3)
    assert stats["undefined"] == {"t_c_ii"}
    assert target_stats([{"exit_reason": "expiry", "trigger_pnl": None,
                          "minutes_held": 1, "regime": "a"}])["undefined"] == {
                              "t_b", "t_c_i", "t_c_ii"
                          }
    assert target_stats([{"exit_reason": "expiry", "trigger_pnl": None,
                          "minutes_held": 1, "regime": "a"}])["t_c_ii"] == 0.0


def test_calibration_loss_and_beta_selection():
    live = {"t_a": .5, "t_b": -.5, "t_c_i": .2, "t_c_ii": .1}
    sim = {"t_a": .6, "t_b": -.4, "t_c_i": .1, "t_c_ii": .0}
    se = {key: .1 for key in live}
    assert calibration_loss(sim, live, se) == pytest.approx(4)
    assert calibration_loss({**sim, "t_b": None}, live, se) == pytest.approx(UNDEFINED_LOSS + 3)
    with pytest.raises(ValueError): calibration_loss(sim, {**live, "t_b": None}, se)
    assert select_beta(lambda beta: (beta - .25) ** 2)["beta"] == .25
    extended = select_beta(lambda beta: (beta - 3) ** 2)
    assert extended["extended"] and extended["beta"] == 3


def test_calibration_loss_squares_all_terms():
    live = {key: 0.0 for key in ("t_a", "t_b", "t_c_i", "t_c_ii")}
    sim = {key: 0.2 for key in live}
    assert calibration_loss(sim, live, {key: 0.1 for key in live}) == pytest.approx(16)


def test_bootstrap_ses_reproducible_and_floored():
    rows = [{"exit_reason": "profit_target", "trigger_pnl": None, "minutes_held": 1, "regime": "a"}]
    first = bootstrap_ses(rows, n_resamples=20, seed=7)
    assert first == bootstrap_ses(rows, n_resamples=20, seed=7)
    assert first["t_a"] == 0.01 and first["t_b"] == 0.05


def test_bootstrap_ses_uses_seed_and_applies_both_floors():
    rows = [
        {"exit_reason": "profit_target", "trigger_pnl": None, "minutes_held": 1, "regime": "a"},
        {"exit_reason": "stop_loss", "trigger_pnl": -.2, "minutes_held": 100, "regime": "a"},
        {"exit_reason": "stop_loss", "trigger_pnl": -.8, "minutes_held": 1, "regime": "a"},
    ]
    first = bootstrap_ses(rows, n_resamples=200, seed=1)
    second = bootstrap_ses(rows, n_resamples=200, seed=2)
    assert first["t_a"] >= 0.01 and first["t_b"] >= 0.05
    assert first != second


def test_select_beta_ties_zero_and_extended_insufficiency():
    assert select_beta(lambda beta: 0.0)["beta"] == 0.0
    result = select_beta(lambda beta: (2.0 + 1.5 - beta) if beta <= 1.5
                        else (0.0 if beta == 4 else 1.0))
    assert result["extended"] and result["model_insufficient"] and result["beta"] == 4.0


def test_select_beta_does_not_extend_when_upper_edge_only_ties():
    calls = []
    result = select_beta(lambda beta: calls.append(beta) or (0.0 if beta in (1.5, 1.25) else 1.0))
    assert result["beta"] == 1.25 and not result["extended"]
    assert calls == list(fc.BETA_GRID)


def test_fit_rejects_validation_rows_at_boundary(monkeypatch):
    row = replay_episode_fixture(entry_ts=pd.Timestamp("2026-01-07", tz="UTC"))
    monkeypatch.setattr(fc, "replay", lambda *args, **kwargs: (
        [{"exit_reason": "stop_loss", "trigger_mark_pnl_pct": -.2,
          "minutes_held": 15, "regime": "calm"}],
        {"no_bars": 0, "no_entry_bar": 0, "no_iv": 0, "unanchorable": 0, "no_exit": 0},
    ))
    with pytest.raises(ValueError):
        fit_beta([row], {}, {}, pd.DataFrame(), boundary_ts=row["entry_ts"], r=0.0)


def test_fit_passes_pessimistic_h_mult_and_returns_integration_fields(monkeypatch):
    calls = []
    row = replay_episode_fixture()
    sim = {"exit_reason": "stop_loss", "trigger_mark_pnl_pct": -.2,
           "minutes_held": 15, "regime": "calm"}

    def fake_replay(episodes, bars, iv, table, *, beta, r, h_mult=1.0, s=0.5, **kwargs):
        calls.append((beta, h_mult))
        return [sim], {"no_bars": 0, "no_entry_bar": 0, "no_iv": 0, "unanchorable": 0, "no_exit": 0}

    monkeypatch.setattr(fc, "replay", fake_replay)
    result = fit_beta([row], {}, {}, pd.DataFrame(), boundary_ts=pd.Timestamp("2026-02-01", tz="UTC"),
                      r=0.0, h_mult=1.5, n_resamples=10)
    assert len(result["loss_by_beta"]) == 13
    assert all(value == 1.5 for _, value in calls)
    assert set(result) >= {"skip_counts", "live_stats", "sim_stats", "residuals_by_regime", "h_mult"}


def test_fit_beta_uses_real_replay_and_selects_nonzero_beta():
    episode_row, bars, iv = fit_replay_fixture()
    result = fit_beta(
        [episode_row], {"SPY": bars}, {"SPY": iv}, friction_table(),
        boundary_ts=pd.Timestamp("2026-02-01", tz="UTC"), r=0.0,
        h_mult=1.0, n_resamples=10,
    )
    assert result["beta"] != 0.0
    assert len(set(result["loss_by_beta"].values())) > 1
    assert result["loss_by_beta"][0.0] > result["loss_by_beta"][result["beta"]]


def test_fit_beta_preserves_beta_times_h_mult_with_real_replay():
    episode_row, bars, iv = fit_replay_fixture()
    common = ({"SPY": bars}, {"SPY": iv}, friction_table())
    first = fit_beta(
        [episode_row], *common, boundary_ts=pd.Timestamp("2026-02-01", tz="UTC"),
        r=0.0, h_mult=1.0, n_resamples=10,
    )
    pessimistic = fit_beta(
        [episode_row], *common, boundary_ts=pd.Timestamp("2026-02-01", tz="UTC"),
        r=0.0, h_mult=1.5, n_resamples=10,
    )
    assert abs(pessimistic["beta"] * 1.5 - first["beta"]) <= 0.125
