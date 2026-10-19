import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, "src")
import friction_compare as fc


def row(symbol, entry, pnl, regime="range", reason="expiry", minutes=30, **extra):
    return {"symbol": symbol, "entry_ts": pd.Timestamp(entry), "realized_pnl": pnl,
            "regime": regime, "exit_reason": reason, "minutes_held": minutes, **extra}


def test_variants_are_literal_contract():
    assert fc.VARIANTS == {
        "X0": {"policy": "V0", "pt": 0.5, "sl": -1.0},
        "X1": {"policy": "V0", "pt": None, "sl": -1.0},
        "X2": {"policy": "V3", "pt": None, "sl": None},
        "A1": {"policy": "V5", "pt": None, "sl": None, "stop_account_pct": 0.01},
        "A2": {"policy": "V5", "pt": None, "sl": None, "stop_account_pct": 0.02},
        "A5": {"policy": "V5", "pt": None, "sl": None, "stop_account_pct": 0.05},
        "C2": {"policy": "V6", "pt": None, "sl": None, "stop_underlying_pct": 0.02},
        "C3": {"policy": "V6", "pt": None, "sl": None, "stop_underlying_pct": 0.03},
        "C5": {"policy": "V6", "pt": None, "sl": None, "stop_underlying_pct": 0.05},
    }


def test_run_variants_calls_replay_for_each_scenario_and_variant(monkeypatch):
    calls = []
    def fake(*args, **kwargs):
        calls.append(kwargs)
        return [row("SPY", "2026-01-01", 1.0)], {}
    monkeypatch.setattr(fc, "replay", fake)
    episode = {"symbol": "SPY", "entry_ts": "2026-01-01", "legs": ["SPY260117P00400000", "SPY260117P00390000"], "limit_credit": 1.0, "qty": 1}
    result = fc.run_variants([episode], {}, {}, pd.DataFrame(), scenarios={"central": {"beta": 1, "h_mult": 1}, "pessimistic": {"beta": 2, "h_mult": 3}}, r=0, s=.5, equity=100)
    assert len(calls) == 18 and set(result) == {"central", "pessimistic"}
    assert calls[0]["beta"] == 1 and calls[9]["beta"] == 2
    assert set(calls[0]) >= {"policy", "pt", "sl", "r", "s", "h_mult"}
    for call, variant_id in zip(calls[:9], fc.VARIANTS):
        spec = fc.VARIANTS[variant_id]
        assert call["policy"] == spec["policy"]
        assert call["pt"] == spec["pt"] and call["sl"] == spec["sl"]
        assert call["r"] == 0 and call["s"] == .5 and call["h_mult"] == 1
        if spec["policy"] == "V5":
            assert call["account_equity"] == 100
            assert call["stop_account_pct"] == spec["stop_account_pct"]
            assert "stop_underlying_pct" not in call
        elif spec["policy"] == "V6":
            assert call["stop_underlying_pct"] == spec["stop_underlying_pct"]
            assert "account_equity" not in call and "stop_account_pct" not in call
        else:
            assert "account_equity" not in call and "stop_account_pct" not in call
            assert "stop_underlying_pct" not in call
    assert all(call["beta"] == 2 and call["h_mult"] == 3 for call in calls[9:])


def test_summarize_intersection_and_statistics():
    base = [row("SPY", "2026-01-01", 2, regime="up", minutes=10), row("SPY", "2026-01-02", -1, minutes=20)]
    alt = [row("SPY", "2026-01-01", 1, regime="up", minutes=30, reason="stop_loss")]
    summary = fc.summarize({"X0": {"rows": base}, "A1": {"rows": alt}}, n_resamples=20, seed=1)
    assert summary["n_dropped"] == 1
    assert summary["variants"]["X0"]["mean_pnl"] == 2
    assert summary["variants"]["X0"]["total_pnl"] == 2
    assert summary["variants"]["X0"]["win_rate"] == 1
    assert summary["variants"]["X0"]["worst_pnl"] == 2
    assert summary["variants"]["X0"]["median_minutes_held"] == 10
    assert summary["variants"]["A1"]["stop_rate"] == 1
    assert summary["variants"]["A1"]["paired"]["mean"] == -1
    assert summary["variants"]["X0"]["paired"]["mean"] == 0
    assert summary["variants"]["X0"]["by_regime"]["up"]["n"] == 1
    zero_summary = fc.summarize({"X0": {"rows": [row("SPY", "2026-01-03", 0)]},
                                 "A1": {"rows": [row("SPY", "2026-01-03", -1)]}}, n_resamples=10)
    assert zero_summary["variants"]["X0"]["win_rate"] == 0


def test_regime_means_use_each_regime_denominator():
    rows = [row("SPY", "2026-01-01", 2, regime="up"), row("SPY", "2026-01-02", 4, regime="up"),
            row("SPY", "2026-01-03", -2, regime="down")]
    result = fc.summarize({"X0": {"rows": rows}, "A1": {"rows": rows}}, n_resamples=10)
    assert result["variants"]["X0"]["by_regime"] == {
        "down": {"mean_pnl": -2.0, "n": 1}, "up": {"mean_pnl": 3.0, "n": 2}}


def test_binding_boundary_and_condor_width():
    episodes = [{"symbol": "SPY", "entry_ts": "2026-01-01", "legs": ["SPY260117P00400000", "SPY260117P00390000", "SPY260117C00450000", "SPY260117C00460000"], "limit_credit": 2.0, "qty": 1}]
    meta = fc._binding_metadata(episodes, 100)
    assert meta[("SPY", pd.Timestamp("2026-01-01"))]["max_loss_over_equity"] == 8.0

    rows = [row("SPY", "2026-01-01", -1, max_loss_over_equity=0.009),
            row("SPY", "2026-01-02", -1, max_loss_over_equity=0.01)]
    result = fc.summarize({"X0": {"rows": rows}, "A1": {"rows": rows}}, n_resamples=10)
    binding = result["variants"]["A1"]["binding"]
    assert binding["max"] == .01 and binding["median"] == .0095
    assert binding["structurally_untriggerable_rate"] == .5


def test_summarize_worst_pnl_is_the_minimum_realized_pnl():
    rows = [row("SPY", "2026-01-01", 3), row("SPY", "2026-01-02", -2),
            row("SPY", "2026-01-03", 1)]
    result = fc.summarize({"X0": {"rows": rows}, "A1": {"rows": rows}}, n_resamples=10)
    assert result["variants"]["X0"]["worst_pnl"] == -2


def test_binding_condor_uses_the_larger_wing_width():
    episodes = [{"symbol": "SPY", "entry_ts": "2026-01-01",
                 "legs": ["SPY260117P00400000", "SPY260117P00390000",
                           "SPY260117C00450000", "SPY260117C00470000"],
                 "limit_credit": 2.0, "qty": 1}]
    meta = fc._binding_metadata(episodes, 100)
    assert meta[("SPY", pd.Timestamp("2026-01-01"))]["max_loss_over_equity"] == 18.0


def test_summarize_stop_rate_is_stop_rows_over_common_rows():
    rows = [row("SPY", "2026-01-01", -1, reason="stop_loss"),
            row("SPY", "2026-01-02", 2), row("SPY", "2026-01-03", 4)]
    result = fc.summarize({"X0": {"rows": rows}, "A1": {"rows": rows}}, n_resamples=10)
    assert result["variants"]["A1"]["stop_rate"] == 1 / 3


def test_main_loads_live_episodes_and_writes_both_outputs(tmp_path, monkeypatch):
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text("not used\n", encoding="utf-8")
    table = tmp_path / "table.csv"
    table.write_text("class,bucket,n,h,fallback\n", encoding="utf-8")
    episodes = [{"symbol": "SPY", "entry_ts": pd.Timestamp("2026-01-01", tz="UTC")}]
    monkeypatch.setattr(fc, "load_live_episodes", lambda path: (episodes, {"crypto": 2}))
    monkeypatch.setattr(fc, "load_inputs", lambda rows, path: ({"SPY": pd.DataFrame(index=pd.date_range("2026-01-01", periods=1))}, {}, ["QQQ"]))
    monkeypatch.setattr(fc, "data_period", lambda bars: (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")))
    fake_results = {scenario: {variant: {"rows": [], "skip_counts": {}}
                              for variant in fc.VARIANTS} for scenario in ("central", "pessimistic")}
    monkeypatch.setattr(fc, "run_variants", lambda *args, **kwargs: fake_results)
    monkeypatch.setattr(fc, "summarize", lambda *args, **kwargs: {"n_common": 0, "n_dropped": 0, "variants": {}})
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"fit": {"beta": 1, "h_mult": 2},
                                       "fit_pessimistic": {"beta": 3, "h_mult": 4}}), encoding="utf-8")
    args = ["--decisions", str(decisions), "--table", str(table), "--bars-dir", str(tmp_path),
            "--calibration-json", str(calibration), "--out-dir", str(tmp_path / "out"),
            "--date", "2026-01-10", "--r", "0.04", "--equity", "100"]
    assert fc.main(args) == 0
    assert (tmp_path / "out/compare-2026-01-10.md").exists()
    payload = json.loads((tmp_path / "out/compare-2026-01-10.json").read_text())
    assert payload["meta"]["missing_symbols"] == ["QQQ"]
    assert payload["meta"]["exclusion_counts"] == {"crypto": 2}


def test_main_rejects_existing_output_before_running_variants(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "compare-2026-01-10.md").write_text("keep", encoding="utf-8")
    called = []
    monkeypatch.setattr(fc, "run_variants", lambda *a, **k: called.append(True))
    with pytest.raises(FileExistsError):
        fc.main(["--decisions", "missing", "--table", "missing", "--bars-dir", "missing",
                 "--calibration-json", "missing", "--out-dir", str(out), "--date", "2026-01-10",
                 "--r", "0.04", "--equity", "100"])
    assert not called and (out / "compare-2026-01-10.md").read_text() == "keep"


def test_main_missing_required_numeric_args_and_bad_calibration(tmp_path):
    common = ["--decisions", "d", "--table", "t", "--bars-dir", "b", "--calibration-json", "c",
              "--out-dir", str(tmp_path), "--date", "2026-01-10"]
    with pytest.raises(SystemExit): fc.main(common)
    with pytest.raises(SystemExit): fc.main(common + ["--r", "0.04"])
    calibration = tmp_path / "bad.json"
    calibration.write_text(json.dumps({"fit": {"beta": 1, "h_mult": 2}}), encoding="utf-8")
    with pytest.raises(KeyError):
        fc.main(common + ["--r", "0.04", "--equity", "100", "--calibration-json", str(calibration)])
    assert not list(tmp_path.glob("compare-*"))


def test_main_serialization_failure_leaves_no_outputs(tmp_path, monkeypatch):
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"fit": {"beta": 1, "h_mult": 2},
                                       "fit_pessimistic": {"beta": 3, "h_mult": 4}}), encoding="utf-8")
    table = tmp_path / "table.csv"
    table.write_text("class,bucket,n,h,fallback\n", encoding="utf-8")
    monkeypatch.setattr(fc, "load_live_episodes", lambda path: ([], {}))
    monkeypatch.setattr(fc, "load_inputs", lambda rows, path: ({}, {}, []))
    monkeypatch.setattr(fc, "run_variants", lambda *a, **k: {s: {} for s in ("central", "pessimistic")})
    monkeypatch.setattr(fc, "summarize", lambda *a, **k: {"n_common": 0, "n_dropped": 0, "variants": {}})
    monkeypatch.setattr(fc, "to_jsonable", lambda value: (_ for _ in ()).throw(TypeError("bad type")))
    with pytest.raises(TypeError):
        fc.main(["--decisions", "d", "--table", str(table), "--bars-dir", "b", "--calibration-json", str(calibration),
                 "--out-dir", str(tmp_path / "out"), "--date", "2026-01-10", "--r", "0.04", "--equity", "100"])
    assert not list((tmp_path / "out").glob("compare-*") if (tmp_path / "out").exists() else [])


def test_real_replay_integration_reaches_summary():
    episode = {"symbol": "SPY", "entry_ts": pd.Timestamp("2026-01-01 10:00", tz="UTC"),
               "legs": ["SPY   260109P00450000", "SPY   260109P00455000"], "n_legs": 2,
               "qty": 1, "limit_credit": 3.0, "live_close_path": "market", "regime": "range"}
    bars = pd.DataFrame({"close": [452.0, 450.0, 440.0, 440.0]},
                        index=pd.to_datetime(["2026-01-01 09:45", "2026-01-01 10:15", "2026-01-01 11:00", "2026-01-09 10:00"]))
    iv = pd.Series([1.0], index=pd.to_datetime(["2026-01-01"]))
    table = pd.DataFrame({"class": ["single_stock"] * 4, "bucket": ["<0.10", "0.10-0.50", "0.50-2.00", ">2.00"], "h": [1.] * 4})
    results = fc.run_variants([episode], {"SPY": bars}, {"SPY": iv}, table,
                              scenarios={"central": {"beta": 0., "h_mult": 1.}, "pessimistic": {"beta": .5, "h_mult": 2.}},
                              r=0., s=.5, equity=100.)
    summary = {name: fc.summarize(value, n_resamples=10) for name, value in results.items()}
    assert set(summary) == {"central", "pessimistic"}
    assert set(summary["central"]["variants"]) == set(fc.VARIANTS)
    assert summary["central"]["variants"]["X0"]["paired"]["mean"] == 0


def test_render_report_is_deterministic_and_complete():
    empty = {"n_dropped": 0, "variants": {key: {"n": 0, "mean_pnl": None, "total_pnl": 0, "win_rate": None, "worst_pnl": None, "paired": {"mean": 0, "ci": (0, 0)}, "stop_rate": None, "exit_reason_counts": {}, "by_regime": {}} for key in fc.VARIANTS}}
    summary = {"central": empty, "pessimistic": empty}
    meta = {"date": "2026-01-01", "data_period": ("a", "b")}
    report = fc.render_report(summary, meta)
    assert report == fc.render_report(summary, meta)
    assert report.index("## Interpretation limits") < report.index("## Variants") < report.index("## Results (central)") < report.index("## Results (pessimistic)")
    for heading in ("## By regime", "## Stop-rule binding", "## Inputs"):
        assert heading in report
    for sentence in ("FAIL", "paired relative comparison", "Absolute dollar profit", "live promotion", "103+16"):
        assert sentence in report
    assert report.count("| X0 |") == 3 and "n/a" in report


def test_help_subprocess():
    result = subprocess.run([sys.executable, "src/friction_compare.py", "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "--equity" in result.stdout
