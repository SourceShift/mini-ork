"""Hermetic tests for the diversity / pass@k collapse floor (RSI Tier-1 #12a).

Three layers, matching ``mini_ork/learning/collapse_detector.py`` (the pure
floor), ``mini_ork/learning/collapse_floor.py`` (the read-only DB reader) and
``mini_ork/cli/collapse.py`` (``--floor``). Every DB test builds a temp sqlite
via ``mig.init_db``; no network, no shared state.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

import pytest

from mini_ork.cli import collapse as collapse_cli
from mini_ork.learning import collapse_detector as cd
from mini_ork.stores import migrate as mig

INS_RUN = (
    "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, workflow_version,"
    " created_at, updated_at, status) VALUES (?,?,?,?,'latest',?,?,?)"
)
INS_TRACE = (
    "INSERT INTO execution_traces (trace_id, run_id, task_class, agent_version_id,"
    " status, verifier_output, created_at)"
    " VALUES (?,?,?,?, 'success', '{\"node_type\": \"implementer\"}', ?)"
)


def _repo_root():
    import mini_ork

    return os.path.dirname(os.path.dirname(os.path.abspath(mini_ork.__file__)))


def _rows(passes, lanes, keys=None):
    """Kickoff helper: rows with run_id=f"r{i}" and task_key=keys[i] or f"k{i}"."""
    return [
        {
            "run_id": f"r{i}",
            "passed": bool(p),
            "task_key": keys[i] if keys is not None else f"k{i}",
            "lanes": list(lanes[i]),
        }
        for i, p in enumerate(passes)
    ]


def _halt_runs(recent_lanes):
    """Prior 20 rows (first 10 passed, lanes a/b), recent 20 (first 2 passed)."""
    prior = _rows(
        [True] * 10 + [False] * 10,
        [["a"] if i % 2 == 0 else ["b"] for i in range(20)],
    )
    recent = _rows([True] * 2 + [False] * 18, recent_lanes)
    return prior + recent


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "state.db"
    rc, _out, err = mig.init_db(str(path), root=_repo_root())
    assert rc == 0, f"init_db failed: {err}"
    return path


def _seed(con, runs, task_class="code_fix"):
    """Seed task_runs + execution_traces.

    Each spec: {"status": str, "lanes": [str] (default no lanes),
    "kickoff": str (default kf-{i}), "traces": [(trace_id, lane)] override}.
    """
    for i, spec in enumerate(runs):
        rid = f"run-{i:04d}"
        created = 1700000000 + i
        con.execute(
            INS_RUN,
            (rid, task_class, task_class, spec.get("kickoff", f"kf-{i:04d}"),
             created, created, spec["status"]),
        )
        traces = spec.get("traces")
        if traces is None:
            traces = [
                (f"tr-implementer-implementer-{i:08x}-{j:04x}", lane)
                for j, lane in enumerate(spec.get("lanes") or [])
            ]
        for tid, lane in traces:
            con.execute(INS_TRACE, (tid, rid, task_class, lane, str(created)))
    con.commit()


def _halt_specs():
    """The kickoff's halt pattern: 40 runs — prior half first 10 published with
    alternating a/b lanes, recent half first 2 published, all lane a."""
    specs = []
    for i in range(20):
        specs.append({
            "status": "published" if i < 10 else "failed",
            "lanes": [["a", "b"][i % 2]],
            "kickoff": f"kf-{i:04d}",
        })
    for i in range(20):
        specs.append({
            "status": "published" if i < 2 else "failed",
            "lanes": ["a"],
            "kickoff": f"kf-{20 + i:04d}",
        })
    return specs


# ---------------------------------------------------------------------------
# Pure floor()
# ---------------------------------------------------------------------------

def test_insufficient_history():
    report = cd.floor(_rows([True] * 39, [["a"]] * 39))
    assert report["n"] == 39
    assert report["window"] == 20
    assert report["prior"] is None
    assert report["recent"] is None
    assert report["pass_at_k_drop"] is None
    assert report["fell"] is False
    assert report["narrowed"] is False
    assert report["breach"] is False
    assert report["recommendation"] == "none"
    assert report["reason"] == "insufficient history (n < 40)"


def test_halt():
    report = cd.floor(_halt_runs([["a"]] * 20))
    assert report["n"] == 40
    assert report["prior"]["pass_at_k"] == pytest.approx(0.5)
    assert report["recent"]["pass_at_k"] == pytest.approx(0.1)
    assert report["pass_at_k_drop"] == pytest.approx(0.4)
    assert report["fell"] is True
    assert report["narrowed"] is True
    assert report["breach"] is True
    assert report["recommendation"] == "halt"
    assert report["reason"] == (
        "pass@k fell while lane diversity narrowed (diversity floor breached)"
    )


def test_watch_pass_fell_only():
    report = cd.floor(
        _halt_runs([["a"] if i % 2 == 0 else ["b"] for i in range(20)])
    )
    assert report["fell"] is True
    assert report["narrowed"] is False
    assert report["breach"] is False
    assert report["recommendation"] == "watch"
    assert report["reason"] == "pass@k fell but lane diversity did not narrow"


def test_watch_narrowing_only():
    prior = _rows(
        [True] * 10 + [False] * 10,
        [["a"] if i % 2 == 0 else ["b"] for i in range(20)],
    )
    recent = _rows([True] * 10 + [False] * 10, [["a"]] * 20)
    report = cd.floor(prior + recent)
    assert report["pass_at_k_drop"] == pytest.approx(0.0)
    assert report["fell"] is False
    assert report["narrowed"] is True
    assert report["breach"] is False
    assert report["recommendation"] == "watch"
    assert report["reason"] == "lane diversity narrowed but pass@k did not fall"


def test_strict_drop_boundary():
    prior = _rows([True] * 5 + [False] * 5, [["x"]] * 10)
    recent = _rows([True] * 3 + [False] * 7, [["x"]] * 10)
    report = cd.floor(prior + recent, window=10)
    assert report["prior"]["pass_at_k"] == pytest.approx(0.5)
    assert report["recent"]["pass_at_k"] == pytest.approx(0.3)
    assert report["pass_at_k_drop"] == pytest.approx(0.2)
    assert report["fell"] is False
    assert report["narrowed"] is False
    assert report["breach"] is False
    assert report["recommendation"] == "none"
    assert report["reason"] == "no floor breach"


def test_no_narrowing_without_diversity():
    prior = _rows([True] * 10, [["x"]] * 10)
    recent = _rows([False] * 10, [["x"]] * 10)
    report = cd.floor(prior + recent, window=10)
    assert report["prior"]["lane_count"] == 1
    assert report["recent"]["lane_count"] == 1
    assert report["narrowed"] is False
    assert report["recommendation"] == "watch"

    prior = _rows([True] * 10, [[]] * 10)
    recent = _rows([False] * 10, [[]] * 10)
    report = cd.floor(prior + recent, window=10)
    assert report["prior"]["lane_count"] == 0
    assert report["recent"]["lane_count"] == 0
    assert report["narrowed"] is False


def test_pass_at_k_estimator():
    prior = _rows([True, True], [["a"], ["a"]])
    recent = _rows([False, True], [["a"], ["a"]], keys=["t", "t"])
    runs = prior + recent
    r3 = cd.floor(runs, window=2, k=3)["recent"]
    assert r3["pass_at_1"] == pytest.approx(0.5)
    assert r3["pass_at_k"] == pytest.approx(1.0)
    r1 = cd.floor(runs, window=2, k=1)["recent"]
    assert r1["pass_at_1"] == pytest.approx(0.5)
    assert r1["pass_at_k"] == pytest.approx(0.0)


def test_group_key_fallbacks():
    prior = _rows([True, True], [["a"], ["a"]])
    recent = [
        {"run_id": "x1", "passed": False, "task_key": "", "lanes": ["a"]},
        {"run_id": "x1", "passed": True, "task_key": "", "lanes": ["a"]},
    ]
    report = cd.floor(prior + recent, window=2, k=3)
    assert report["recent"]["pass_at_k"] == pytest.approx(1.0)
    assert report["recent"]["pass_at_1"] == pytest.approx(0.5)

    recent = [
        {"run_id": "", "passed": False, "task_key": "", "lanes": ["a"]},
        {"run_id": "", "passed": True, "task_key": "", "lanes": ["a"]},
    ]
    report = cd.floor(prior + recent, window=2, k=3)
    assert report["recent"]["pass_at_k"] == pytest.approx(0.5)


def test_detect_unchanged():
    assert cd.detect([])["reason"] == "insufficient history (n < 4)"
    assert cd.MIN_STEPS == 4
    assert cd.summarize(cd.detect([])).startswith("n=0")


# ---------------------------------------------------------------------------
# DB reader
# ---------------------------------------------------------------------------

def test_floor_runs_terminal_only_oldest_first(db_path):
    from mini_ork.learning import collapse_floor

    con = sqlite3.connect(db_path)
    _seed(con, [
        {"status": "published", "lanes": ["a"], "kickoff": "kf-0"},
        {"status": "superseded", "lanes": ["a"], "kickoff": "kf-1"},
        {"status": "executing", "lanes": ["a"], "kickoff": "kf-2"},
        {"status": "failed", "lanes": ["a"], "kickoff": "kf-3"},
    ])
    rows = collapse_floor.floor_runs(con, "code_fix", limit=10)
    con.close()
    assert [r["run_id"] for r in rows] == ["run-0000", "run-0003"]
    assert rows[0]["passed"] is True
    assert rows[1]["passed"] is False


def test_floor_runs_limit_keeps_newest(db_path):
    from mini_ork.learning import collapse_floor

    con = sqlite3.connect(db_path)
    _seed(con, [{"status": "published", "lanes": ["a"]} for _ in range(5)])
    rows = collapse_floor.floor_runs(con, "code_fix", limit=3)
    con.close()
    assert [r["run_id"] for r in rows] == ["run-0002", "run-0003", "run-0004"]


def test_floor_runs_lanes_implementer_only(db_path):
    from mini_ork.learning import collapse_floor

    con = sqlite3.connect(db_path)
    _seed(con, [
        {"status": "published", "kickoff": "kf-0", "traces": [
            ("tr-reviewer-reviewer-00000001", "zlane"),
            ("tr-implementer-implementer-00000001", ""),
            ("tr-implementer-implementer-00000002", " b "),
            ("tr-implementer-implementer-00000003", "a"),
        ]},
        {"status": "published", "kickoff": "kf-1", "lanes": []},
    ])
    rows = collapse_floor.floor_runs(con, "code_fix", limit=10)
    con.close()
    assert rows[0]["lanes"] == ["a", "b"]
    assert rows[1]["lanes"] == []


def test_floor_runs_missing_tables():
    from mini_ork.learning import collapse_floor

    con = sqlite3.connect(":memory:")
    assert collapse_floor.floor_runs(con, "code_fix", limit=5) == []
    con.close()


def test_resolve_window(monkeypatch):
    from mini_ork.learning import collapse_floor

    monkeypatch.setenv("MO_CB_FLOOR_WINDOW", "5")
    assert collapse_floor.resolve_window() == 5
    for bad in ("abc", "0", "-3", "2.5"):
        monkeypatch.setenv("MO_CB_FLOOR_WINDOW", bad)
        assert collapse_floor.resolve_window() == cd.FLOOR_WINDOW
    monkeypatch.delenv("MO_CB_FLOOR_WINDOW", raising=False)
    assert collapse_floor.resolve_window() == 20


def test_evaluate_env_window(db_path, monkeypatch):
    from mini_ork.learning import collapse_floor

    monkeypatch.setenv("MO_CB_FLOOR_WINDOW", "2")
    con = sqlite3.connect(db_path)
    _seed(con, [
        {"status": "published" if i % 2 == 0 else "failed", "lanes": ["a"]}
        for i in range(5)
    ])
    report = collapse_floor.evaluate(con, "code_fix")
    con.close()
    assert report["window"] == 2
    assert report["n"] == 4
    assert report["task_class"] == "code_fix"


def test_evaluate_env_invalid_falls_back(db_path, monkeypatch):
    from mini_ork.learning import collapse_floor

    monkeypatch.setenv("MO_CB_FLOOR_WINDOW", "abc")
    con = sqlite3.connect(db_path)
    _seed(con, [{"status": "published", "lanes": ["a"]}])
    report = collapse_floor.evaluate(con, "code_fix")
    con.close()
    assert report["window"] == 20


def test_evaluate_halt_pattern(db_path, monkeypatch):
    from mini_ork.learning import collapse_floor

    monkeypatch.delenv("MO_CB_FLOOR_WINDOW", raising=False)
    con = sqlite3.connect(db_path)
    _seed(con, _halt_specs(), task_class="code_fix")
    report = collapse_floor.evaluate(con, "code_fix")
    con.close()
    assert report["recommendation"] == "halt"
    assert report["n"] == 40


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@pytest.fixture()
def halt_db(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    rc, _out, err = mig.init_db(str(path), root=_repo_root())
    assert rc == 0, f"init_db failed: {err}"
    con = sqlite3.connect(str(path))
    _seed(con, _halt_specs(), task_class="code_fix")
    con.close()
    monkeypatch.delenv("MO_CB_FLOOR_WINDOW", raising=False)
    monkeypatch.setenv("MINI_ORK_DB", str(path))
    return path


def test_cli_json(halt_db, capsys):
    rc = collapse_cli.main(["--floor", "code_fix", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.endswith("\n")
    report = json.loads(out)
    assert report["recommendation"] == "halt"
    assert report["task_class"] == "code_fix"
    assert out == json.dumps(report, indent=2, sort_keys=True) + "\n"


def test_cli_plain_single_line(halt_db, capsys):
    assert collapse_cli.main(["--floor", "code_fix"]) == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert out == json.dumps(report, sort_keys=True) + "\n"


def test_cli_read_only(halt_db, capsys):
    before = hashlib.sha256(halt_db.read_bytes()).hexdigest()
    assert collapse_cli.main(["--floor", "code_fix", "--json"]) == 0
    capsys.readouterr()
    after = hashlib.sha256(halt_db.read_bytes()).hexdigest()
    assert before == after


def test_cli_missing_db(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "gone" / "state.db"
    monkeypatch.setenv("MINI_ORK_DB", str(missing))
    assert collapse_cli.main(["--floor", "code_fix"]) == 0
    assert "no state.db at" in capsys.readouterr().err


def test_cli_missing_task_class(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "none.db"))
    assert collapse_cli.main(["--floor"]) == 0
    assert "--floor requires a task class" in capsys.readouterr().err


def test_cli_positional_with_floor_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "none.db"))
    assert collapse_cli.main(["--floor", "code_fix", "history.json"]) == 0
    assert "unexpected argument:" in capsys.readouterr().err


def test_cli_existing_history_mode(tmp_path, capsys):
    history = [
        {"step": 0, "score": 0.10, "anchor": 0.90, "directives": []},
        {"step": 1, "score": 0.20, "anchor": 0.85, "directives": ["d1"]},
        {"step": 2, "score": 0.30, "anchor": 0.80, "directives": ["d1", "d2"]},
        {"step": 3, "score": 0.40, "anchor": 0.75, "directives": ["d2"]},
    ]
    path = tmp_path / "history.json"
    path.write_text(json.dumps(history))
    assert collapse_cli.main([str(path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["n"] == 4
    assert "task_class" not in report
