import json
import datetime as dt
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import friction_calibrate as fc


def _bars(start, count):
    index = pd.date_range(start, periods=count, freq="15min")
    return pd.DataFrame({"close": np.arange(count, dtype=float) + 100}, index=index)


def test_load_inputs_prepares_bars_and_tracks_missing(tmp_path, monkeypatch):
    symbol = "AAA"
    _bars("2026-01-01", 3).to_parquet(tmp_path / f"{symbol}_15min.parquet")
    daily = pd.DataFrame({"open": [1], "high": [2], "low": [0], "close": [1], "volume": [1]},
                         index=pd.date_range("2025-01-01", periods=1))
    daily.to_parquet(tmp_path / f"{symbol}_daily.parquet")
    seen = []
    monkeypatch.setattr(fc, "_rolling_iv_series", lambda frame: seen.append(frame) or pd.Series([0.2]))
    bars, iv, missing = fc.load_inputs([{"symbol": "CCC"}, {"symbol": "BBB"}, {"symbol": symbol}], tmp_path)
    assert missing == ["BBB", "CCC"]
    assert "BBB" not in bars and "CCC" not in bars and "BBB" not in iv and "CCC" not in iv
    assert list(bars[symbol].index) == list(_bars("2026-01-01", 3).index + pd.Timedelta(minutes=15))
    assert symbol in iv and seen[0].equals(daily)


def test_data_period():
    first = _bars("2026-01-02", 2)
    second = _bars("2026-01-01", 2)
    assert fc.data_period({"A": first, "B": second}) == (second.index.min(), first.index.max())
    with pytest.raises(ValueError):
        fc.data_period({})


def _result():
    return {
        "boundary_ts": pd.Timestamp("2026-01-05"), "n_train": 6, "n_validation": 4,
        "fit": {"beta": 0.5, "h_mult": 1.0, "extended": False, "model_insufficient": False,
                "loss_by_beta": {0.0: 2.0, 0.5: 1.0}, "skip_counts": {"no_bars": 0},
                "live_stats": {"t_a": 0.5, "t_b": -0.1, "t_c_i": 0.2, "t_c_ii": 0.1},
                "sim_stats": {"t_a": 0.4, "t_b": None, "t_c_i": 0.3, "t_c_ii": 0.2},
                "residuals_by_regime": {"low": {"sim_t_a": 0.4, "live_t_a": 0.5, "n": 3}}},
        "fit_pessimistic": {"beta": 0.4, "h_mult": 1.5},
        "validation": {"sim_stats": {"t_a": 0.5, "t_b": None, "t_c_i": 0.2, "t_c_ii": 0.1},
                        "judgement": {"targets": {
                            "t_a": {"sim": 0.5, "ci": (0.1, 0.9), "passed": True},
                            "t_b": {"sim": None, "ci": (None, None), "passed": False},
                            "t_c_i": {"sim": 0.2, "ci": (0.0, 0.4), "passed": True},
                            "t_c_ii": {"sim": 0.1, "ci": (0.0, 0.2), "passed": True},
                            "t_d": {"sim_trend_up": 0.3, "ci": (0.1, 0.5), "in_ci": True,
                                     "lowest": True, "passed": True}}, "undefined": [], "passed": False}},
        "controls": {"A": {"sim_stats": {"t_a": 1}, "outside_t_a": True,
                              "outside_t_c_i": False, "passed": True},
                      "C": {"sim_stats": {"t_c_ii": 2}, "k": 0.75,
                              "outside_t_c_ii": True, "passed": True},
                      "ci": {}, "passed": True},
        "sensitivity": {"t_a": {"sim": 0.1, "ci": (0.0, 0.2), "in_ci": True},
                        "t_c_i": {"sim": 0.2, "ci": (0.1, 0.3), "in_ci": True}, "n": 112},
        "low_credit_sensitivity": {"sim_t_b": None, "live_t_b": -0.1, "n_excluded": 2},
        "grade": {"grade": "B", "probation_stop_pct": -0.05, "reasons": ["reason"]},
    }


def test_render_report_is_complete_and_deterministic():
    meta = {"date": "2026-01-10", "n_episodes_total": 10, "exclusion_counts": {}, "missing_symbols": [],
            "data_period": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-10")), "r": 0.04, "s": 0.5,
            "seed": 1, "n_resamples": 10}
    report = fc.render_report(_result(), meta)
    headings = ["# Friction calibration", "## Summary", "## Fit", "## Validation", "## Controls",
                "## Sensitivity", "## Limitations", "## Inputs"]
    positions = [report.index(headings[0])] + [report.index(heading + "\n") for heading in headings[1:]]
    assert all(positions[i] < positions[i + 1] for i in range(len(positions) - 1))
    assert "B" in report and "0.5000" in report and "-0.0500" in report and "-0.1000" in report
    assert "β·h (median / pessimistic): 0.5000 / 0.6000" in report
    assert "n/a" in report and "0.0000" in report and "reason" in report
    assert "| t_b | n/a | (n/a, n/a) | False |" in report
    assert "t_d (trend_up)" in report and "outside_t_c_i" not in report
    assert "0.7500" in report and "Sensitivity n: 112" in report and "Low-credit excluded n: 2" in report
    assert report == fc.render_report(_result(), meta)
    assert "| 0.5000 | 1.0000 |" in report
    assert all(item in report for item in fc.LIMITATIONS)
    assert "- n/a" not in report.split("## Fit", 1)[0]
    assert "t_a=1; t_b=n/a; t_c_i=n/a; t_c_ii=n/a" in report
    assert "2026-01-01T00:00:00 – 2026-01-10T00:00:00" in report


def test_render_report_empty_reasons_has_one_na():
    result = _result()
    result["grade"]["reasons"] = []
    report = fc.render_report(result, {"date": "2026-01-10", "data_period": None})
    assert report.count("  - n/a") == 1


def test_cli_help_runs_as_script():
    completed = subprocess.run([sys.executable, "src/friction_calibrate.py", "--help"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    assert "--decisions" in completed.stdout


def test_to_jsonable_is_json_serializable():
    value = fc.to_jsonable({"ts": pd.Timestamp("2026-01-01"), "datetime": dt.datetime(2026, 1, 1, 2),
                            "date": dt.date(2026, 1, 2), "delta": pd.Timedelta(minutes=15),
                            "array": np.array([1, 2]), "set": {"z", "a"}, "np": np.int64(3),
                            "tuple": (1, 2), "call": lambda: None})
    assert json.dumps(value)
    assert value["ts"] == "2026-01-01T00:00:00"
    assert value["set"] == ["a", "z"]
    assert value["np"] == 3
    assert value["datetime"] == "2026-01-01T02:00:00"
    assert value["date"] == "2026-01-02"
    assert value["delta"] == "0 days 00:15:00"
    assert value["array"] == [1, 2]


def test_to_jsonable_rejects_unknown_objects():
    with pytest.raises(TypeError):
        fc.to_jsonable(object())


def test_main_contracts(tmp_path, monkeypatch):
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text("", encoding="utf-8")
    table = tmp_path / "table.csv"
    table.write_text("class,bucket,n,h,fallback\n", encoding="utf-8")
    monkeypatch.setattr(fc, "load_live_episodes", lambda path: ([{"symbol": "AAA"}], {}))
    monkeypatch.setattr(fc, "load_inputs", lambda episodes, bars: ({}, {}, ["AAA"]))
    calls = []
    monkeypatch.setattr(fc, "calibrate", lambda *args, **kwargs: calls.append((args, kwargs)) or _result())
    args = ["--decisions", str(decisions), "--table", str(table), "--bars-dir", str(tmp_path),
            "--out-dir", str(tmp_path / "out"), "--date", "2026-01-10", "--r", "0.04",
            "--s", "0.2", "--seed", "7", "--resamples", "9"]
    assert fc.main(args) == 0
    assert calls[0][1] == {"r": 0.04, "s": 0.2, "seed": 7, "n_resamples": 9}
    assert calls[0][0][3].equals(pd.DataFrame(columns=["class", "bucket", "n", "h", "fallback"]))
    assert (tmp_path / "out/calibration-2026-01-10.md").exists()
    assert json.loads((tmp_path / "out/calibration-2026-01-10.json").read_text())["fit"]["beta"] == 0.5
    with pytest.raises(FileExistsError):
        fc.main(args)
    assert len(calls) == 1

    monkeypatch.setattr(fc, "calibrate", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad")))
    fresh = tmp_path / "fresh"
    assert fc.main([*args[:-4], "--out-dir", str(fresh), "--date", "2026-01-11", "--r", "0.04"]) == 1
    assert not fresh.exists() or not list(fresh.iterdir())


def test_main_cleans_partial_outputs_on_json_failure(tmp_path, monkeypatch):
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text("", encoding="utf-8")
    table = tmp_path / "table.csv"
    table.write_text("class,bucket,n,h,fallback\n", encoding="utf-8")
    out = tmp_path / "out"
    monkeypatch.setattr(fc, "load_live_episodes", lambda path: ([{"symbol": "AAA"}], {}))
    monkeypatch.setattr(fc, "load_inputs", lambda episodes, bars: ({}, {}, ["AAA"]))
    bad_result = _result()
    bad_result["unserializable"] = object()
    monkeypatch.setattr(fc, "calibrate", lambda *args, **kwargs: bad_result)
    args = ["--decisions", str(decisions), "--table", str(table), "--bars-dir", str(tmp_path),
            "--out-dir", str(out), "--date", "2026-01-12", "--r", "0.04"]
    with pytest.raises(TypeError):
        fc.main(args)
    assert not (out / "calibration-2026-01-12.md").exists()
    assert not (out / "calibration-2026-01-12.json").exists()


def test_main_requires_r():
    with pytest.raises(SystemExit):
        fc.main(["--decisions", "x", "--table", "x", "--bars-dir", "x", "--date", "2026-01-01"])
