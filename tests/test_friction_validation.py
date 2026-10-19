import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import friction_validation as fv  # noqa: E402


def row(exit_reason="stop_loss", minutes=30, pnl=-0.2, regime="trend_up"):
    return {"exit_reason": exit_reason, "minutes_held": minutes,
            "trigger_pnl": pnl, "regime": regime}


def live_rows():
    return [row("stop_loss", 30, -0.1), row("stop_loss", 75, -0.3),
            row("profit_target", 75, 0.5), row("profit_target", 30, 0.4)]


def test_validation_constants_are_literal_contract():
    assert fv.CI_LEVEL == 0.90
    assert fv.K_GRID == tuple(0.25 * i for i in range(13))
    assert fv.PROBATION_STOP == -0.10
    assert fv.PROBATION_STOP_CONDITIONAL == -0.05


def test_bootstrap_ci_reproducible_boundaries_and_special_cases():
    rows = live_rows()
    first = fv.bootstrap_ci(rows, n_resamples=200, seed=7)
    second = fv.bootstrap_ci(rows, n_resamples=200, seed=7)
    assert first["t_a"] == second["t_a"]
    assert first["in_ci"](first["t_a"][0], first["t_a"])
    assert not first["in_ci"](None, first["t_a"])
    assert not first["in_ci"](0.5, (None, None))
    assert fv.bootstrap_ci([row("profit_target", 20, .5)], n_resamples=20)["t_a"] == (1.0, 1.0)
    with pytest.raises(ValueError): fv.bootstrap_ci([])
    with pytest.raises(ValueError): fv.bootstrap_ci([row("stop_loss", regime="calm")])


def test_bootstrap_t_c_ii_resamples_sl_and_pt_separately():
    rows = [row("stop_loss", 60), row("stop_loss", 45),
            row("profit_target", 75, .5), row("profit_target", 90, .5)]
    ci = fv.bootstrap_ci(rows, n_resamples=100, seed=3)
    assert ci["t_c_ii"] == (1.0, 1.0)


@pytest.mark.parametrize("key", ["t_a", "t_b", "t_c_i", "t_c_ii"])
def test_judge_each_target_outside_fails(monkeypatch, key):
    ci = {name: (0.0, 1.0) for name in ("t_a", "t_b", "t_c_i", "t_c_ii")}
    ci["t_d_trend_up"] = (0.0, 1.0)
    ci["in_ci"] = lambda value, bounds: value is not None and bounds[0] <= value <= bounds[1]
    monkeypatch.setattr(fv, "bootstrap_ci", lambda *args, **kwargs: ci)
    stats = {"t_a": .5, "t_b": 0.0, "t_c_i": .5, "t_c_ii": .0,
             "t_d": {"trend_up": .2, "flat": .4}, "undefined": set()}
    stats[key] = 2.0
    result = fv.judge_validation(stats, live_rows())
    assert result["targets"][key]["passed"] is False
    assert result["passed"] is False


def test_judge_t_b_none_and_t_c_i_zero_rules(monkeypatch):
    ci = {name: (0.0, 1.0) for name in ("t_a", "t_b", "t_c_i", "t_c_ii")}
    ci["t_c_i"] = (0.2, 0.8)
    ci["t_d_trend_up"] = (0.0, 1.0)
    ci["in_ci"] = lambda value, bounds: value is not None and bounds[0] <= value <= bounds[1]
    monkeypatch.setattr(fv, "bootstrap_ci", lambda *args, **kwargs: ci)
    stats = {"t_a": .5, "t_b": None, "t_c_i": 0.0, "t_c_ii": .0,
             "t_d": {"trend_up": .2}, "undefined": set()}
    result = fv.judge_validation(stats, live_rows())
    assert result["targets"]["t_b"]["passed"] is False
    assert result["targets"]["t_c_i"]["passed"] is False


@pytest.mark.parametrize("trend_values, expected", [
    ({"trend_up": .2, "flat": .4}, True),
    ({"trend_up": .2, "flat": .2}, True),
    ({"trend_up": .4, "flat": .2}, False),
    ({"flat": .2}, False),
])
def test_judge_t_d_requires_ci_and_lowest(monkeypatch, trend_values, expected):
    ci = {name: (0.0, 1.0) for name in ("t_a", "t_b", "t_c_i", "t_c_ii", "t_d_trend_up")}
    ci["in_ci"] = lambda value, bounds: value is not None and bounds[0] <= value <= bounds[1]
    monkeypatch.setattr(fv, "bootstrap_ci", lambda *args, **kwargs: ci)
    stats = {"t_a": .5, "t_b": 0.0, "t_c_i": .5, "t_c_ii": .0,
             "t_d": trend_values, "undefined": set()}
    result = fv.judge_validation(stats, live_rows())
    assert result["targets"]["t_d"]["passed"] is expected
    assert result["passed"] is expected


def test_fit_symmetric_k_tie_small_and_zero_rng(monkeypatch):
    calls = []

    def fake_replay(*args, **kwargs):
        calls.append(kwargs)
        return ([{"exit_reason": "stop_loss", "trigger_mark_pnl_pct": -.1,
                   "minutes_held": 10, "regime": "trend_up"}], {})

    monkeypatch.setattr(fv, "replay", fake_replay)
    episodes = [{"exit_reason": "stop_loss", "trigger_profit_pct": -.1,
                 "minutes_held": 10, "regime": "trend_up"}]
    result = fv.fit_symmetric_k(episodes, {}, {}, pd.DataFrame(), r=0.0, seed=4)
    assert result["k"] == 0.0
    assert calls[0]["k"] == 0.0 and "rng" not in calls[0]
    assert all(call.get("rng") is not None for call in calls[1:])
    assert len({id(call["rng"]) for call in calls[1:]}) == len(calls) - 1


def test_run_controls_uses_a_or_and_c_t_c_ii(monkeypatch):
    ci = {name: (0.0, 1.0) for name in ("t_a", "t_b", "t_c_i", "t_c_ii")}
    ci["in_ci"] = lambda value, bounds: value is not None and bounds[0] <= value <= bounds[1]
    monkeypatch.setattr(fv, "bootstrap_ci", lambda *args, **kwargs: ci)
    replay_calls = []
    stats = iter([
        {"t_a": 2.0, "t_b": 0.0, "t_c_i": .5, "t_c_ii": .0, "t_d": {"trend_up": .5}, "undefined": set()},
        {"t_a": .5, "t_b": 0.0, "t_c_i": .5, "t_c_ii": 2.0, "t_d": {"trend_up": .5}, "undefined": set()},
    ])

    monkeypatch.setattr(fv, "target_stats", lambda rows: next(stats))
    monkeypatch.setattr(fv, "fit_symmetric_k", lambda *args, **kwargs: {"k": .75, "diff_by_k": {}})
    monkeypatch.setattr(fv, "replay", lambda *args, **kwargs: (replay_calls.append(kwargs) or ([], {})))
    episode = {"exit_reason": "stop_loss", "minutes_held": 30,
               "trigger_profit_pct": -.1, "regime": "trend_up"}
    result = fv.run_controls([episode], [episode], {}, {}, pd.DataFrame(), r=0.0, n_resamples=2)
    assert result["A"]["passed"] is True and result["C"]["passed"] is True
    assert replay_calls[0]["beta"] == 0.0 and replay_calls[0]["h_mult"] == 0.0
    assert replay_calls[1]["beta"] == 0.0 and replay_calls[1]["h_mult"] == 1.0
    assert replay_calls[1]["k"] == .75


@pytest.mark.parametrize("a_stats, c_stats, a_passed, c_passed", [
    ({"t_a": 2.0, "t_c_i": .5}, {"t_c_ii": 0.0}, True, False),
    ({"t_a": .5, "t_c_i": .5}, {"t_c_ii": 2.0}, False, True),
    ({"t_a": .5, "t_c_i": 2.0}, {"t_c_ii": 0.0}, True, False),
    ({"t_a": 2.0, "t_c_i": .5}, {"t_c_ii": 2.0}, True, True),
])
def test_run_controls_covers_each_control_branch(monkeypatch, a_stats, c_stats,
                                                  a_passed, c_passed):
    ci = {name: (0.0, 1.0) for name in ("t_a", "t_b", "t_c_i", "t_c_ii")}
    ci["in_ci"] = lambda value, bounds: value is not None and bounds[0] <= value <= bounds[1]
    monkeypatch.setattr(fv, "bootstrap_ci", lambda *args, **kwargs: ci)
    base = {"t_b": 0.0, "t_c_ii": 0.0, "t_d": {"trend_up": .5}, "undefined": set()}
    stats = iter([{**base, **a_stats}, {**base, **c_stats}])
    monkeypatch.setattr(fv, "target_stats", lambda rows: next(stats))
    monkeypatch.setattr(fv, "fit_symmetric_k", lambda *args, **kwargs: {"k": .75, "diff_by_k": {}})
    calls = []
    monkeypatch.setattr(fv, "replay", lambda *args, **kwargs:
                        (calls.append(kwargs) or ([], {})))
    episode = {"exit_reason": "stop_loss", "minutes_held": 30,
               "trigger_profit_pct": -.1, "regime": "trend_up"}
    result = fv.run_controls([episode], [episode], {}, {}, pd.DataFrame(), r=0.0,
                             n_resamples=2)
    assert result["A"]["passed"] is a_passed
    assert result["C"]["passed"] is c_passed
    assert result["passed"] is (a_passed and c_passed)
    assert calls[1]["rng"] is not None


def test_sensitivity_passes_fit_beta_and_h_mult_to_replay(monkeypatch):
    boundary = pd.Timestamp("2026-01-01", tz="UTC")
    episode = {"entry_ts": boundary, "kind": "retry", "exit_reason": "stop_loss",
               "minutes_held": 30, "trigger_profit_pct": -.1, "regime": "trend_up"}
    captured = {}
    monkeypatch.setattr(fv, "replay", lambda selected, *args, **kwargs:
                        (captured.update(kwargs) or ([{"exit_reason": "stop_loss",
                          "trigger_mark_pnl_pct": -.1, "minutes_held": 30,
                          "regime": "trend_up"}], {})))
    monkeypatch.setattr(fv, "bootstrap_ci", lambda *args, **kwargs: {
        "t_a": (0.0, 1.0), "t_c_i": (0.0, 1.0),
        "in_ci": lambda value, bounds: True})
    monkeypatch.setattr(fv, "target_stats", lambda rows: {
        "t_a": .5, "t_b": 0.0, "t_c_i": .5, "t_c_ii": 0.0,
        "t_d": {"trend_up": .5}, "undefined": set()})
    fv.sensitivity_all_kinds([episode], boundary, {}, {}, pd.DataFrame(),
                             fit_beta_value=.73, r=0.0, n_resamples=2)
    assert captured["beta"] == .73
    assert captured["h_mult"] == 1.0


def test_judge_t_d_fails_when_lowest_but_outside_ci(monkeypatch):
    ci = {name: (0.0, 1.0) for name in ("t_a", "t_b", "t_c_i", "t_c_ii")}
    ci["t_d_trend_up"] = (0.0, .1)
    ci["in_ci"] = lambda value, bounds: value is not None and bounds[0] <= value <= bounds[1]
    monkeypatch.setattr(fv, "bootstrap_ci", lambda *args, **kwargs: ci)
    stats = {"t_a": .5, "t_b": 0.0, "t_c_i": .5, "t_c_ii": 0.0,
             "t_d": {"trend_up": .2, "flat": .3}, "undefined": set()}
    result = fv.judge_validation(stats, live_rows())
    assert result["targets"]["t_d"]["lowest"] is True
    assert result["targets"]["t_d"]["in_ci"] is False
    assert result["targets"]["t_d"]["passed"] is False


@pytest.mark.parametrize("fit, judgement, controls, sensitivity, grade, stop", [
    ({"model_insufficient": True}, {"passed": True, "targets": {}}, {"A": {"passed": True}, "C": {"passed": True}}, {"t_a": True, "t_c_i": True}, "FAIL", None),
    ({"model_insufficient": False}, {"passed": False, "targets": {"t_b": {"passed": False}}}, {"A": {"passed": True}, "C": {"passed": True}}, {"t_a": True, "t_c_i": True}, "FAIL", None),
    ({"model_insufficient": False}, {"passed": True, "targets": {}}, {"A": {"passed": False}, "C": {"passed": True}}, {"t_a": True, "t_c_i": True}, "FAIL", None),
    ({"model_insufficient": False}, {"passed": True, "targets": {}}, {"A": {"passed": True}, "C": {"passed": True}}, {"t_a": True, "t_c_i": True}, "PASS", -.10),
    ({"model_insufficient": False}, {"passed": True, "targets": {}}, {"A": {"passed": True}, "C": {"passed": True}}, {"t_a": False, "t_c_i": True}, "PASS_CONDITIONAL", -.05),
    ({"model_insufficient": False}, {"passed": True, "targets": {}}, {"A": {"passed": True}, "C": {"passed": True}}, {"t_a": True, "t_c_i": False}, "PASS_CONDITIONAL", -.05),
])
def test_grade_calibration_truth_table(fit, judgement, controls, sensitivity, grade, stop):
    result = fv.grade_calibration(fit, judgement, controls, sensitivity)
    assert result["grade"] == grade
    assert result["probation_stop_pct"] == stop
    if grade == "FAIL":
        assert result["probation_stop_pct"] is None
        assert result["reasons"]


def test_sensitivity_all_kinds_includes_boundary_and_non_clean(monkeypatch):
    boundary = pd.Timestamp("2026-01-02", tz="UTC")
    episodes = [{"entry_ts": pd.Timestamp("2026-01-01", tz="UTC"), "kind": "clean",
                 "exit_reason": "stop_loss", "minutes_held": 30, "trigger_profit_pct": -.1,
                 "regime": "trend_up"},
                {"entry_ts": boundary, "kind": "single_limit", "exit_reason": "stop_loss",
                 "minutes_held": 30, "trigger_profit_pct": -.1, "regime": "trend_up"},
                {"entry_ts": pd.Timestamp("2026-01-03", tz="UTC"), "kind": "retry",
                 "exit_reason": "profit_target", "minutes_held": 75, "trigger_profit_pct": .5,
                 "regime": "trend_up"}]
    seen = []
    monkeypatch.setattr(fv, "replay", lambda selected, *args, **kwargs:
                        (seen.append(selected) or ([{"exit_reason": "stop_loss",
                          "trigger_mark_pnl_pct": -.1, "minutes_held": 10,
                          "regime": "trend_up"}], {})))
    result = fv.sensitivity_all_kinds(episodes, boundary, {}, {}, pd.DataFrame(),
                                      fit_beta_value=.5, r=0.0, n_resamples=2)
    assert result["n"] == 2 and [item["kind"] for item in seen[0]] == ["single_limit", "retry"]
    with pytest.raises(ValueError):
        fv.sensitivity_all_kinds(episodes[:1], boundary, {}, {}, pd.DataFrame(),
                                 fit_beta_value=.5, r=0.0, n_resamples=2)


def test_calibrate_contract_with_monkeypatched_pipeline(monkeypatch):
    clean = [{"kind": "clean", "entry_ts": pd.Timestamp(f"2026-01-{day:02d}", tz="UTC"),
              "limit_credit": .01 if day == 7 else .1, "exit_reason": "stop_loss",
              "minutes_held": 30, "trigger_profit_pct": -.1, "regime": "trend_up"}
             for day in range(1, 9)]
    extras = [{**clean[0], "kind": "single_limit", "entry_ts": pd.Timestamp("2026-01-09", tz="UTC")},
              {**clean[0], "kind": "retry", "entry_ts": pd.Timestamp("2026-01-10", tz="UTC")}]
    fit = {"beta": .5, "model_insufficient": False, "h_mult": 1.0}
    pess = {"beta": .5, "model_insufficient": False, "h_mult": 1.5}
    sim_row = {"exit_reason": "stop_loss", "trigger_mark_pnl_pct": -.1,
               "minutes_held": 30, "regime": "trend_up"}
    monkeypatch.setattr(fv, "fit_beta", lambda *args, **kwargs: pess if kwargs["h_mult"] == 1.5 else fit)
    monkeypatch.setattr(fv, "replay", lambda *args, **kwargs: ([sim_row] * len(args[0]), {}))
    monkeypatch.setattr(fv, "bootstrap_ci", lambda rows, **kwargs: {
        "t_a": (0.0, 1.0), "t_b": (-1.0, 0.0), "t_c_i": (0.0, 1.0),
        "t_c_ii": (-1.0, 1.0), "t_d_trend_up": (0.0, 1.0),
        "in_ci": lambda value, bounds: value is not None and bounds[0] <= value <= bounds[1],
    })
    monkeypatch.setattr(fv, "run_controls", lambda *args, **kwargs: {
        "A": {"passed": True}, "C": {"passed": True}, "passed": True})
    monkeypatch.setattr(fv, "sensitivity_all_kinds", lambda *args, **kwargs: {
        "t_a": {"in_ci": True}, "t_c_i": {"in_ci": True}, "n": 4})
    result = fv.calibrate(clean + extras, {}, {}, pd.DataFrame(), r=0.0, n_resamples=2)
    assert set(result) == {"boundary_ts", "n_train", "n_validation", "fit", "fit_pessimistic",
                           "validation", "controls", "sensitivity", "low_credit_sensitivity",
                           "grade", "skip_counts_validation"}
    assert result["n_train"] + result["n_validation"] == 8
    assert result["fit_pessimistic"]["h_mult"] == 1.5
    assert result["low_credit_sensitivity"]["n_excluded"] == 1
    assert result["grade"]["grade"] in {"PASS", "PASS_CONDITIONAL", "FAIL"}


def test_calibrate_uses_validation_beta_and_credit_boundary(monkeypatch):
    train = [{"kind": "clean", "entry_ts": pd.Timestamp("2026-01-01", tz="UTC"),
              "limit_credit": .1}]
    validation = [
        {"kind": "clean", "entry_ts": pd.Timestamp("2026-01-02", tz="UTC"),
         "limit_credit": .02, "exit_reason": "stop_loss", "minutes_held": 30,
         "trigger_profit_pct": -.1, "regime": "trend_up"},
        {"kind": "clean", "entry_ts": pd.Timestamp("2026-01-03", tz="UTC"),
         "limit_credit": .0200001, "exit_reason": "profit_target", "minutes_held": 75,
         "trigger_profit_pct": .5, "regime": "trend_up"},
    ]
    boundary = validation[0]["entry_ts"]
    monkeypatch.setattr(fv, "split_train_validation", lambda clean: (train, validation, boundary))
    fit = {"beta": .73, "model_insufficient": False, "h_mult": 1.0}
    pess = {"beta": .73, "model_insufficient": False, "h_mult": 1.5}
    monkeypatch.setattr(fv, "fit_beta", lambda *args, **kwargs:
                        pess if kwargs["h_mult"] == 1.5 else fit)
    replay_calls = []
    sim_row = {"exit_reason": "stop_loss", "trigger_mark_pnl_pct": -.1,
               "minutes_held": 30, "regime": "trend_up"}
    monkeypatch.setattr(fv, "replay", lambda selected, *args, **kwargs:
                        (replay_calls.append((selected, kwargs)) or ([sim_row] * len(selected), {})))
    monkeypatch.setattr(fv, "target_stats", lambda rows: {
        "t_a": .5, "t_b": 0.0, "t_c_i": .5, "t_c_ii": 0.0,
        "t_d": {"trend_up": .5}, "undefined": set()})
    judged_rows = []
    monkeypatch.setattr(fv, "judge_validation", lambda stats, rows, **kwargs:
                        (judged_rows.append(rows) or {"passed": True, "targets": {}}))
    monkeypatch.setattr(fv, "run_controls", lambda *args, **kwargs:
                        {"A": {"passed": True}, "C": {"passed": True}, "passed": True})
    monkeypatch.setattr(fv, "sensitivity_all_kinds", lambda *args, **kwargs:
                        {"t_a": {"in_ci": True}, "t_c_i": {"in_ci": True}, "n": 2})
    result = fv.calibrate(train + validation, {}, {}, pd.DataFrame(), r=0.0, n_resamples=2)
    assert replay_calls[0][0] == validation
    assert replay_calls[0][1]["beta"] == .73
    assert replay_calls[0][1]["h_mult"] == 1.0
    assert judged_rows == [fv.live_rows(validation)]
    assert result["low_credit_sensitivity"]["n_excluded"] == 1
    assert len(replay_calls[-1][0]) == 1
    assert replay_calls[-1][0][0]["limit_credit"] == .0200001
