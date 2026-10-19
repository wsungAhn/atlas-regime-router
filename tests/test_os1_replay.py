import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import os1_replay
from os1_replay import ETF9, cap_replay, episode_structure, render_report, subset_stats, unit_risk


PYTHON = "/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python"


def occ(symbol="SPY", expiry="261120", typ="P", strike=450.0):
    strike_code = int(round(float(strike) * 1000))
    return f"{symbol}{expiry}{typ}{strike_code:08d}"


def ep(symbol="SPY", ts="2026-10-01T10:00:00+00:00", qty=1, credit=0.3, legs=None):
    return {
        "symbol": symbol,
        "entry_ts": datetime.fromisoformat(ts),
        "qty": qty,
        "limit_credit": credit,
        "legs": legs or [occ(symbol, typ="P", strike=450), occ(symbol, typ="P", strike=452)],
        "regime": "range",
    }


def row(e, exit_ts="2026-10-01T11:00:00", pnl=10):
    return {
        "symbol": e["symbol"],
        "entry_ts": e["entry_ts"].replace(tzinfo=None),
        "exit_ts": datetime.fromisoformat(exit_ts),
        "realized_pnl": pnl,
    }


def test_etf9_literal():
    assert ETF9 == ("SPY", "QQQ", "GLD", "TLT", "SLV", "IWM", "XLE", "XLF", "DIA")


def test_episode_structure_all_branches_and_verification():
    put = ep(legs=[occ(typ="P", strike=450), occ(typ="P", strike=451.5)])
    call = ep(legs=[occ(typ="C", strike=450), occ(typ="C", strike=451.5)])
    verified = ep(credit=0.5, legs=[occ(typ="P", strike=448), occ(typ="P", strike=450),
                                    occ(typ="C", strike=452), occ(typ="C", strike=454)])
    expiry_mismatch = ep(legs=[occ(expiry="261120", typ="P", strike=448), occ(expiry="261121", typ="P", strike=450),
                               occ(expiry="261120", typ="C", strike=452), occ(expiry="261120", typ="C", strike=454)])
    overlap = ep(legs=[occ(typ="P", strike=450), occ(typ="P", strike=452),
                       occ(typ="C", strike=451), occ(typ="C", strike=454)])

    assert episode_structure(put) == {"side": "bull_put", "widths": (1.5,), "credit": 0.3, "verified": False}
    assert episode_structure(call)["side"] == "bear_call"
    assert episode_structure(verified) == {"side": "condor", "widths": (2.0, 2.0), "credit": 0.5, "verified": True}
    assert episode_structure(expiry_mismatch)["verified"] is False
    assert episode_structure(overlap)["verified"] is False


def test_unit_risk_verified_and_unverified_condor():
    assert unit_risk({"side": "bull_put", "widths": (1.5,), "credit": 0.3, "verified": False}) == 120
    assert unit_risk({"side": "condor", "widths": (2.0, 1.5), "credit": 0.5, "verified": True}) == 150
    assert unit_risk({"side": "condor", "widths": (1.0, 2.0), "credit": 0.5, "verified": False}) == 250
    assert unit_risk({"side": "condor", "widths": (3.0, 2.0), "credit": 0.5, "verified": False}) == 450


def test_cap_replay_accepts_nonoverlapping_and_scales_pnl():
    episodes = [ep("SPY", "2026-10-01T10:00:00+00:00"),
                ep("QQQ", "2026-10-01T11:00:00+00:00"),
                ep("GLD", "2026-10-01T12:00:00+00:00")]
    result = cap_replay([row(episodes[2], "2026-10-01T13:00:00", 30),
                         row(episodes[0], "2026-10-01T11:00:00", 10),
                         row(episodes[1], "2026-10-01T12:00:00", 20)], episodes, 50000)
    assert result["n_candidates"] == result["n_accepted"] == 3
    assert result["uncapped_total_pnl"] == 60
    assert result["capped_total_pnl"] == 300
    assert result["capped_mean_pnl"] == 100


def test_cap_replay_symbol_open_boundary_and_ordering():
    first = ep("SPY", "2026-10-01T10:00:00+00:00")
    overlapping = ep("SPY", "2026-10-01T10:30:00+00:00")
    boundary = ep("SPY", "2026-10-01T11:00:00+00:00")
    after_boundary = ep("DIA", "2026-10-01T12:00:00+00:00")
    result = cap_replay([row(after_boundary, "2026-10-01T13:00:00", 40),
                         row(boundary, "2026-10-01T12:00:00", 30),
                         row(overlapping, "2026-10-01T11:30:00", 20),
                         row(first, "2026-10-01T11:00:00", 10)],
                        [first, overlapping, boundary, after_boundary], 50000)
    assert result["n_accepted"] == 3
    assert result["skip_counts"]["symbol_open"] == 1
    assert result["skip_counts"]["count_cap"] == 0
    assert result["capped_total_pnl"] == 400


def test_cap_replay_count_risk_credit_skips_and_zero_summary():
    symbols = ("SPY", "QQQ", "GLD", "TLT", "SLV", "IWM")
    episodes = [ep(symbol, f"2026-10-01T{10 + index:02d}:00:00+00:00", credit=1.9,
                   legs=[occ(symbol, typ="C", strike=450), occ(symbol, typ="C", strike=452)] if index % 2 else None)
                for index, symbol in enumerate(symbols)]
    rows = [row(item, "2026-10-02T10:00:00", 1) for item in episodes]
    result = cap_replay(rows, episodes, 50000)
    assert result["n_accepted"] == 5
    assert result["skip_counts"]["count_cap"] == 1

    risk_eps = [ep("SPY", "2026-10-01T10:00:00+00:00"), ep("QQQ", "2026-10-01T11:00:00+00:00"),
                ep("GLD", "2026-10-01T12:00:00+00:00"), ep("TLT", "2026-10-01T13:00:00+00:00"),
                ep("SLV", "2026-10-01T14:00:00+00:00"), ep("IWM", "2026-10-01T15:00:00+00:00")]
    risk_rows = [row(item, "2026-10-02T10:00:00", 1) for item in risk_eps]
    risk = cap_replay(risk_rows, risk_eps, 50000, max_open_positions=100)
    assert risk["skip_counts"]["risk_cap"] >= 1

    bad = ep("XLE", "2026-10-03T10:00:00+00:00", credit=2.0)
    low = ep("XLF", "2026-10-03T11:00:00+00:00", credit=0.199999)
    skipped = cap_replay([row(bad), row(low)], [bad, low], 50000)
    assert skipped["n_accepted"] == 0
    assert skipped["skip_counts"]["bad_credit"] == 1
    assert skipped["skip_counts"]["low_credit_ratio"] == 1
    assert skipped["capped_mean_pnl"] is None and skipped["capped_worst_pnl"] is None
    assert set(skipped["skip_counts"]) == {"symbol_open", "count_cap", "risk_cap", "bad_credit", "low_credit_ratio"}
    with pytest.raises(ValueError, match="no episode"):
        cap_replay([row(ep())], [], 50000)


def test_cap_replay_concurrent_exposure_metrics_are_literal():
    episodes = [ep(symbol, f"2026-10-01T{10 + index:02d}:00:00+00:00") for index, symbol in enumerate(("SPY", "QQQ", "GLD"))]
    result = cap_replay([row(item, "2026-10-02T10:00:00") for item in episodes], episodes, 50000)
    assert result["max_concurrent_count"] == 3
    assert result["max_concurrent_L_over_equity"] == pytest.approx(2550 / 50000)
    assert result["median_concurrent_L_over_equity"] == pytest.approx(1700 / 50000)


def test_subset_stats_filters_etf_ratio_boundary_and_pair_direction():
    e_spy = ep("SPY", "2026-10-01T10:00:00+00:00", credit=0.2)
    e_aapl = ep("AAPL", "2026-10-01T11:00:00+00:00", credit=0.19)
    episodes = [e_spy, e_aapl]

    def variant(x2, x0, stop):
        return {"rows": [dict(row(e_spy, pnl=x2), realized_pnl=x2),
                          dict(row(e_aapl, pnl=stop), realized_pnl=stop)]}

    results = {"X0": variant(1, 1, 2), "X2": variant(3, 1, 4),
               "A1": variant(2, 1, 4), "A2": variant(2, 1, 4), "A5": variant(2, 1, 4),
               "C2": variant(2, 1, 4), "C3": variant(2, 1, 4), "C5": variant(2, 1, 4)}
    stats = subset_stats(results, episodes, alpha=.1, n_resamples=10, seed=1)
    assert stats["etf9"]["n"] == 1 and stats["etf9"]["mean"] == 2
    assert stats["ratio_ge_010"]["n"] == 1
    assert stats["x2_vs"]["A1"]["n"] == 2
    assert stats["x2_vs"]["A1"]["mean"] == -0.5


def test_size_os1_consistency_for_accepted_structures():
    from os1_sizing import size_os1

    for structure in (
        {"side": "bull_put", "widths": (1.5,), "credit": .3, "verified": False},
        {"side": "bear_call", "widths": (2.0,), "credit": .5, "verified": False},
        {"side": "condor", "widths": (2.0, 3.0), "credit": .5, "verified": True},
        {"side": "condor", "widths": (2.0, 3.0), "credit": .5, "verified": False},
    ):
        qty, reason = size_os1(50000, 50000, structure["widths"] if structure["side"] == "condor" else structure["widths"][0],
                               structure["credit"], structure["side"], [], 5, structure["verified"])
        assert reason == "ok"
        assert qty * unit_risk(structure) <= .02 * 50000


def test_cap_replay_uses_mark_free_e_now_equity(monkeypatch):
    episode = ep()
    calls = []
    real_sizer = os1_replay.size_os1

    def checked_sizer(e0, e_now, *args, **kwargs):
        calls.append((e0, e_now))
        return real_sizer(e0, e_now, *args, **kwargs)

    monkeypatch.setattr(os1_replay, "size_os1", checked_sizer)
    cap_replay([row(episode)], [episode], 50000)
    assert calls == [(50000, 50000)]


def test_render_report_contract_and_determinism():
    e = ep()
    stats = subset_stats({"X0": {"rows": []}, "X2": {"rows": []}}, [e], alpha=.1, n_resamples=10, seed=1)
    empty = cap_replay([], [e], 50000)
    result = {"central": {"cap": empty, "subsets": stats}, "pessimistic": {"cap": empty, "subsets": stats}}
    meta = {"date": "2026-10-19"}
    text = render_report(result, meta)
    headings = ["## Interpretation limits", "## Cap-applied X2 (central)", "## Cap-applied X2 (pessimistic)",
                "## Subsets", "## X2 vs stops", "## Inputs"]
    assert [text.index(item) for item in headings] == sorted(text.index(item) for item in headings)
    assert "시뮬레이터 보정 등급은 FAIL(T-d)." in text
    assert "n/a" in text
    assert text == render_report(result, meta)


def _mock_main(monkeypatch, tmp_path):
    episode = ep()
    variants = {}
    for name in ("X0", "X2", "A1", "A2", "A5", "C2", "C3", "C5"):
        variants[name] = {"rows": [dict(row(episode), realized_pnl=1)]}
    monkeypatch.setattr(os1_replay, "load_live_episodes", lambda _: ([episode], {}))
    monkeypatch.setattr(os1_replay, "load_inputs", lambda *_: ({}, {}, []))
    monkeypatch.setattr(os1_replay, "run_variants", lambda *args, **kwargs: {"central": variants, "pessimistic": variants})
    monkeypatch.setattr(os1_replay, "data_period", lambda _: ("start", "end"))
    args = ["--decisions", "decisions.jsonl", "--table", "table.csv", "--bars-dir", "bars",
            "--calibration-json", str(tmp_path / "calibration.json"), "--out-dir", str(tmp_path / "out"),
            "--date", "2026-10-19", "--r", "0.01", "--equity", "50000", "--resamples", "10"]
    (tmp_path / "calibration.json").write_text(json.dumps({"fit": {"beta": 1, "h_mult": 1},
                                                             "fit_pessimistic": {"beta": 2, "h_mult": 2}}))
    (tmp_path / "table.csv").write_text("class,bucket,h\nindex_etf,<0.10,0.1\n")
    args[3] = str(tmp_path / "table.csv")
    return args


def test_main_generates_outputs_and_refuses_overwrite(monkeypatch, tmp_path):
    args = _mock_main(monkeypatch, tmp_path)
    assert os1_replay.main(args) == 0
    md_path = tmp_path / "out" / "os1-replay-2026-10-19.md"
    json_path = tmp_path / "out" / "os1-replay-2026-10-19.json"
    before = (md_path.read_bytes(), json_path.read_bytes())
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("run_variants must not be called")

    monkeypatch.setattr(os1_replay, "run_variants", fail_if_called)
    with pytest.raises(FileExistsError):
        os1_replay.main(args)
    assert called is False and (md_path.read_bytes(), json_path.read_bytes()) == before


def test_main_input_errors_and_help(tmp_path):
    with pytest.raises(SystemExit):
        os1_replay.main([])
    missing = tmp_path / "missing.json"
    table = tmp_path / "table.csv"
    table.write_text("class,bucket,h\nindex_etf,<0.10,0.1\n")
    args = ["--decisions", "decisions.jsonl", "--table", str(table), "--bars-dir", "bars",
            "--calibration-json", str(missing), "--out-dir", str(tmp_path / "out"),
            "--date", "2026-10-19", "--r", "0.01", "--equity", "50000"]
    with pytest.raises(FileNotFoundError):
        os1_replay.main(args)
    missing.write_text(json.dumps({"fit": {"beta": 1, "h_mult": 1}}))
    with pytest.raises(KeyError):
        os1_replay.main(args)
    env = dict(__import__("os").environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    completed = subprocess.run([PYTHON, "-B", "-m", "os1_replay", "--help"], cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True, check=False, env=env)
    assert completed.returncode == 0 and "--calibration-json" in completed.stdout


def test_main_serialization_failure_leaves_both_outputs_absent(monkeypatch, tmp_path):
    args = _mock_main(monkeypatch, tmp_path)
    monkeypatch.setattr(os1_replay, "to_jsonable", lambda _: (_ for _ in ()).throw(TypeError("injected serialization failure")))
    with pytest.raises(TypeError, match="injected serialization failure"):
        os1_replay.main(args)
    assert not (tmp_path / "out" / "os1-replay-2026-10-19.md").exists()
    assert not (tmp_path / "out" / "os1-replay-2026-10-19.json").exists()


def test_main_atomic_replace_failure_leaves_both_outputs_absent(monkeypatch, tmp_path):
    args = _mock_main(monkeypatch, tmp_path)
    monkeypatch.setattr(os1_replay.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("injected replace failure")))
    with pytest.raises(OSError, match="injected replace failure"):
        os1_replay.main(args)
    assert not (tmp_path / "out" / "os1-replay-2026-10-19.md").exists()
    assert not (tmp_path / "out" / "os1-replay-2026-10-19.json").exists()


def test_cap_replay_excludes_equal_exit_from_exposure_metrics():
    first = ep("SPY", "2026-10-01T10:00:00+00:00")
    second = ep("QQQ", "2026-10-01T11:00:00+00:00")
    result = cap_replay([row(first, "2026-10-01T11:00:00", 10),
                         row(second, "2026-10-01T12:00:00", 20)], [first, second], 50000)
    assert result["max_concurrent_count"] == 1
    assert result["max_concurrent_L_over_equity"] == pytest.approx(850 / 50000)
    assert result["median_concurrent_L_over_equity"] == pytest.approx(850 / 50000)


def test_cap_replay_worst_pnl_is_minimum_accepted_pnl(monkeypatch):
    episodes = [ep(symbol, f"2026-10-01T{10 + index:02d}:00:00+00:00", qty=1)
                for index, symbol in enumerate(("SPY", "QQQ", "GLD"))]
    monkeypatch.setattr(os1_replay, "size_os1", lambda *args, **kwargs: (1, "ok"))
    result = cap_replay([row(episodes[0], "2026-10-01T11:00:00", 10),
                         row(episodes[1], "2026-10-01T12:00:00", -5),
                         row(episodes[2], "2026-10-01T13:00:00", 3)], episodes, 50000)
    assert result["capped_total_pnl"] == 8
    assert result["capped_worst_pnl"] == -5


def test_main_second_atomic_write_failure_rolls_back_first_output(monkeypatch, tmp_path):
    args = _mock_main(monkeypatch, tmp_path)
    real_atomic_write = os1_replay._atomic_write
    calls = 0

    def fail_on_json(path, content, *, json_value=False):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second write failure")
        real_atomic_write(path, content, json_value=json_value)

    monkeypatch.setattr(os1_replay, "_atomic_write", fail_on_json)
    with pytest.raises(OSError, match="injected second write failure"):
        os1_replay.main(args)
    assert calls == 2
    assert not (tmp_path / "out" / "os1-replay-2026-10-19.md").exists()
    assert not (tmp_path / "out" / "os1-replay-2026-10-19.json").exists()
