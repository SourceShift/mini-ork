"""Hermetic tests for the scheduled reflect cadence (Gap S2).

No LLM, no network: a tmp home, a tmp DB initialised through
``mini_ork.stores.migrate.init_db`` (the lease tables), and a recording
``reflect_fn`` stand-in for ``mini_ork.cli.reflect.main``.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mini_ork.learning import reflection_pipeline
from mini_ork.learning import reflect_schedule as rs
from mini_ork.stores.lease import acquire_lease, release_lease
from mini_ork.stores.migrate import init_db

REPO_ROOT = Path(__file__).resolve().parents[2]

_ENV_KEYS = (
    "MO_REFLECTION_UNTIL_TS",
    "MO_REFLECT_DEADLINE_S",
    "MINI_ORK_REFLECT_RUN_ID",
    "MO_REFLECT_POST_RUN",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def home_db(tmp_path: Path) -> tuple[Path, str]:
    home = tmp_path / "home"
    home.mkdir()
    db = str(tmp_path / "state.db")
    rc, _out, _err = init_db(db=db, root=str(REPO_ROOT))
    assert rc == 0, "init_db must succeed for the hermetic fixture"
    return home, db


class RecordingReflect:
    """Records argv and the env visible at call time."""

    def __init__(self, rc: int = 0):
        self.calls: list[dict] = []
        self.rc = rc

    def __call__(self, argv):
        self.calls.append({"argv": list(argv), "env": dict(os.environ)})
        return self.rc


def _write_config(home: Path, name: str, body: str) -> str:
    path = home / name
    path.write_text(body)
    return str(path)


def _enabled_cfg(home: Path) -> dict:
    return rs.load_config(
        str(home),
        _write_config(home, "enabled.yaml", "reflect_schedule:\n  enabled: true\n"),
    )


def _state(home: Path) -> dict:
    return json.loads((home / "state" / "reflect-schedule.json").read_text())


# ── 1. off by default ───────────────────────────────────────────────────────

def test_no_config_file_is_disabled(home_db, capsys):
    home, db = home_db
    cfg = rs.load_config(str(home), None)
    stub = RecordingReflect()

    assert rs.run_once(str(home), db, cfg, reflect_fn=stub) == 0
    assert "reflect schedule: disabled" in capsys.readouterr().out
    assert stub.calls == []
    assert acquire_lease(db, rs.LEASE_KEY, owner_token="probe") is not None


# ── 2. a named config that does not exist fails closed ─────────────────────

def test_named_missing_config_exits_2(home_db, capsys):
    home, _db = home_db
    with pytest.raises(SystemExit) as exc:
        rs.load_config(str(home), str(home / "nope.yaml"))
    assert exc.value.code == 2
    assert capsys.readouterr().err.strip()


def test_named_unparseable_config_exits_2(home_db):
    home, _db = home_db
    bad = _write_config(home, "bad.yaml", "reflect_schedule: [oops, {")
    with pytest.raises(SystemExit) as exc:
        rs.load_config(str(home), bad)
    assert exc.value.code == 2


# ── 3. first pass: window, env, state ──────────────────────────────────────

def test_first_pass_window_env_and_state(home_db):
    home, db = home_db
    cfg = _enabled_cfg(home)
    stub = RecordingReflect()

    assert rs.run_once(str(home), db, cfg, now=10_000_000, reflect_fn=stub) == 0

    assert len(stub.calls) == 1
    argv = stub.calls[0]["argv"]
    assert argv == ["--since", "9395200"]          # now - max_window_s
    env = stub.calls[0]["env"]
    assert env["MO_REFLECTION_UNTIL_TS"] == "9998200"  # now - settle_s
    assert env["MO_REFLECT_DEADLINE_S"] == "900"

    assert _state(home) == {"last_ok_until": 9_998_200, "at": 10_000_000}
    # atomic write leaves no tmp file behind
    assert os.listdir(home / "state") == ["reflect-schedule.json"]
    # the scoped env does not leak
    assert "MO_REFLECTION_UNTIL_TS" not in os.environ
    assert "MO_REFLECT_DEADLINE_S" not in os.environ


# ── 4. a second pass resumes from the recorded window ─────────────────────

def test_second_pass_resumes_from_state(home_db):
    home, db = home_db
    cfg = _enabled_cfg(home)
    stub = RecordingReflect()

    assert rs.run_once(str(home), db, cfg, now=10_000_000, reflect_fn=stub) == 0
    assert rs.run_once(str(home), db, cfg, now=10_003_600, reflect_fn=stub) == 0

    assert stub.calls[1]["argv"] == ["--since", "9998200"]
    assert _state(home) == {"last_ok_until": 10_001_800, "at": 10_003_600}


# ── 5. a failing pass keeps the state ──────────────────────────────────────

def test_failing_pass_keeps_state(home_db):
    home, db = home_db
    state_dir = home / "state"
    state_dir.mkdir(parents=True)
    prior = {"last_ok_until": 9_998_200, "at": 9_999_999}
    (state_dir / "reflect-schedule.json").write_text(json.dumps(prior))
    cfg = _enabled_cfg(home)

    rc = rs.run_once(str(home), db, cfg, now=10_000_000,
                     reflect_fn=RecordingReflect(rc=1))

    assert rc == 1
    assert _state(home) == prior


def test_nothing_settled_does_not_run(home_db, capsys):
    home, db = home_db
    state_dir = home / "state"
    state_dir.mkdir(parents=True)
    prior = {"last_ok_until": 9_999_000, "at": 9_999_000}
    (state_dir / "reflect-schedule.json").write_text(json.dumps(prior))
    stub = RecordingReflect()

    assert rs.run_once(str(home), db, _enabled_cfg(home), now=10_000_000,
                       reflect_fn=stub) == 0
    assert stub.calls == []
    assert "reflect schedule: nothing settled yet" in capsys.readouterr().out
    assert _state(home) == prior


# ── 6. the lease excludes a second pass ────────────────────────────────────

def test_lease_held_elsewhere_is_a_noop(home_db, capsys):
    home, db = home_db
    token = acquire_lease(db, rs.LEASE_KEY, owner_token="holder")
    assert token is not None
    stub = RecordingReflect()

    assert rs.run_once(str(home), db, _enabled_cfg(home), now=10_000_000,
                       reflect_fn=stub) == 0
    assert stub.calls == []
    assert "another scheduled pass holds the lease" in capsys.readouterr().out
    release_lease(db, rs.LEASE_KEY, token)


def test_lease_released_when_reflect_raises(home_db):
    home, db = home_db

    def boom(_argv):
        raise RuntimeError("reflect exploded")

    with pytest.raises(RuntimeError):
        rs.run_once(str(home), db, _enabled_cfg(home), now=10_000_000,
                    reflect_fn=boom)
    probe = acquire_lease(db, rs.LEASE_KEY, owner_token="probe-after-raise")
    assert probe is not None
    release_lease(db, rs.LEASE_KEY, probe)


# ── 7. the reflection window's upper bound ─────────────────────────────────

def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def traced_db(tmp_path: Path) -> str:
    db = str(tmp_path / "traces.db")
    rc, _out, _err = init_db(db=db, root=str(REPO_ROOT))
    assert rc == 0
    con = sqlite3.connect(db)
    for trace_id, ts in (("t-old", 9_000_000), ("t-new", 9_999_999)):
        con.execute(
            "INSERT INTO execution_traces(trace_id, run_id, task_class, status,"
            " created_at) VALUES (?,?,?,?,?)",
            (trace_id, 1, "code-fix", "success", _iso(ts)),
        )
    con.commit()
    con.close()
    return db


def test_until_bound_excludes_newer_traces(traced_db, monkeypatch):
    monkeypatch.setenv("MO_REFLECTION_UNTIL_TS", "9998200")
    got = reflection_pipeline._extract_trace_ids_sql(traced_db, 0, 100)
    assert got == ["t-old"]


def test_until_bound_unset_returns_all(traced_db):
    got = reflection_pipeline._extract_trace_ids_sql(traced_db, 0, 100)
    assert set(got) == {"t-old", "t-new"}


@pytest.mark.parametrize("value", ["not-a-number", "0", "-5"])
def test_invalid_until_bound_is_ignored(traced_db, monkeypatch, value):
    monkeypatch.setenv("MO_REFLECTION_UNTIL_TS", value)
    got = reflection_pipeline._extract_trace_ids_sql(traced_db, 0, 100)
    assert set(got) == {"t-old", "t-new"}


# ── 8. the service loop ────────────────────────────────────────────────────

def test_run_forever_stops_on_keyboard_interrupt(home_db):
    home, db = home_db
    stub = RecordingReflect()
    sleeps: list[int] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    rc = rs.run_forever(str(home), db, _enabled_cfg(home),
                        sleep_fn=fake_sleep, reflect_fn=stub)

    assert rc == 0
    assert len(stub.calls) == 2
    assert sleeps == [3600, 3600]


def test_unmigrated_db_fails_closed_instead_of_reading_as_a_held_lease(tmp_path, capsys):
    """acquire_lease answers None on a missing table; without this check an
    unmigrated DB would print the lease-held line and never run (review fix)."""
    import sqlite3

    home = tmp_path / "home"
    home.mkdir()
    bare = str(tmp_path / "bare.db")
    sqlite3.connect(bare).close()
    reflect = RecordingReflect()
    rc = rs.run_once(str(home), bare, _enabled_cfg(home), now=10_000_000,
                     reflect_fn=reflect)
    assert rc == 2
    assert reflect.calls == []
    err = capsys.readouterr().err
    assert "no run_leases table" in err
    assert rs.LEASE_HELD_MESSAGE not in err


# ── review 156 ──────────────────────────────────────────────────────────────


def _add_traces(db: str, epochs: list[int]) -> None:
    con = sqlite3.connect(db)
    for i, ts in enumerate(epochs):
        con.execute(
            "INSERT INTO execution_traces(trace_id, run_id, task_class, status,"
            " created_at) VALUES (?,?,?,?,?)",
            (f"t-{i}", 1, "code-fix", "success", _iso(ts)),
        )
    con.commit()
    con.close()


def test_window_is_capped_at_one_batch_so_no_trace_is_skipped(home_db, monkeypatch):
    home, db = home_db
    monkeypatch.setenv("MO_REFLECTION_BATCH", "3")
    _add_traces(db, [9_500_000, 9_500_100, 9_500_200, 9_500_300, 9_500_400])
    reflect = RecordingReflect()
    rc = rs.run_once(str(home), db, _enabled_cfg(home), now=10_000_000,
                     reflect_fn=reflect)
    assert rc == 0
    # The 4th trace (9_500_300) is beyond the batch: the window ends just before it.
    assert reflect.calls[0]["env"]["MO_REFLECTION_UNTIL_TS"] == str(9_500_299)
    assert _state(home)["last_ok_until"] == 9_500_299
    # The next tick starts there and reaches the rest.
    rs.run_once(str(home), db, _enabled_cfg(home), now=10_000_000, reflect_fn=reflect)
    assert reflect.calls[1]["argv"] == ["--since", str(9_500_299)]


def test_a_window_within_one_batch_is_not_capped(home_db, monkeypatch):
    home, db = home_db
    monkeypatch.setenv("MO_REFLECTION_BATCH", "10")
    _add_traces(db, [9_500_000, 9_500_100])
    reflect = RecordingReflect()
    rs.run_once(str(home), db, _enabled_cfg(home), now=10_000_000, reflect_fn=reflect)
    assert reflect.calls[0]["env"]["MO_REFLECTION_UNTIL_TS"] == str(10_000_000 - 1800)


def test_a_dry_run_never_advances_the_window(home_db, monkeypatch):
    home, db = home_db
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    reflect = RecordingReflect()
    rc = rs.run_once(str(home), db, _enabled_cfg(home), now=10_000_000,
                     reflect_fn=reflect)
    assert rc == 0
    assert not (home / "state" / "reflect-schedule.json").exists()


@pytest.mark.parametrize("flags", [["--dry-run"], ["--since", "1"], ["--run-id", "r"],
                                   ["--task-class", "x"], ["--lane", "glm"]])
def test_scheduled_refuses_filters_it_would_ignore(flags, capsys):
    from mini_ork.cli import reflect as reflect_cli

    rc = reflect_cli.main(["--scheduled", *flags])
    assert rc == 2
    assert "--scheduled does not take" in capsys.readouterr().err


def test_the_deadline_stops_gradient_extraction(monkeypatch, tmp_path):
    """The per-trace handler must let the pass deadline through (review 156)."""
    calls = []

    def _extract(tid):
        calls.append(tid)
        raise reflection_pipeline.ReflectDeadline()

    monkeypatch.setattr(reflection_pipeline, "_extract_trace_ids_sql",
                        lambda *a, **k: ["t1", "t2", "t3"])
    monkeypatch.setattr(reflection_pipeline, "_gradient_extract", _extract)
    from mini_ork.learning import gradient_extractor as _ge
    monkeypatch.setattr(_ge, "has_watermark", lambda tid: False)
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "x.db"))
    with pytest.raises(reflection_pipeline.ReflectDeadline) as exc:
        reflection_pipeline.reflection_extract_gradients(0)
    assert exc.value.code == 124
    assert calls == ["t1"]


def test_other_system_exits_are_still_counted_per_trace(monkeypatch, tmp_path):
    def _extract(tid):
        raise SystemExit(1)

    monkeypatch.setattr(reflection_pipeline, "_extract_trace_ids_sql",
                        lambda *a, **k: ["t1", "t2"])
    monkeypatch.setattr(reflection_pipeline, "_gradient_extract", _extract)
    from mini_ork.learning import gradient_extractor as _ge
    monkeypatch.setattr(_ge, "has_watermark", lambda tid: False)
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "x.db"))
    reflection_pipeline.reflection_extract_gradients(0)  # does not raise
