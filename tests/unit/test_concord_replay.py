"""Unit tests: ``scripts/concord_replay.py`` (Concord P0.5 offline replay).

The script is stdlib-only and reads ``~/.claude/projects/*/*.jsonl``
transcripts, so every test builds a synthetic projects dir under ``tmp_path``
and drives ``concord_replay.main(argv)`` directly. Timestamps are fixed
absolute instants (a pinned base plus ``timedelta`` offsets), never
``datetime.now()``, and every run passes ``--since`` explicitly so the
file-mtime filter cannot drop a fixture. The worktree-resolution rule is
exercised with a synthetic ``.git`` file whose ``gitdir:`` points at a
synthetic main repo.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import concord_replay as cr  # noqa: E402  # pyright: ignore[reportMissingImports] — scripts/ added to sys.path above

BASE = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ts(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _tool(name: str, file_path: str) -> dict:
    return {"type": "tool_use", "name": name, "input": {"file_path": file_path}}


def _record(ts: datetime, session_id: str, cwd: str, tool_use: dict) -> dict:
    return {"type": "assistant", "timestamp": _ts(ts), "sessionId": session_id,
            "cwd": cwd, "message": {"content": [tool_use]}}


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _run(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    # tmp_path lives under /private/var/folders on macOS — a default scratch
    # exclusion — so fixtures opt out of the defaults explicitly.
    rc = cr.main(["--projects-dir", str(tmp_path / "projects"),
                  "--out", str(out), "--since", "3650d", "--no-default-excludes"])
    assert rc == 0
    return out


def _incidents(out: Path) -> list[dict]:
    p = out / "incidents.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_concurrent_write_within_window_not_two_hours(tmp_path):
    repo = _repo(tmp_path)
    proj = tmp_path / "projects" / "p1"
    _write_jsonl(proj / "a.jsonl", [_record(BASE, "A", str(repo), _tool("Write", str(repo / "f.py")))])
    _write_jsonl(proj / "b.jsonl", [_record(BASE + timedelta(minutes=5), "B", str(repo), _tool("Write", str(repo / "f.py")))])
    _write_jsonl(proj / "c.jsonl", [_record(BASE, "C", str(repo), _tool("Write", str(repo / "g.py")))])
    _write_jsonl(proj / "d.jsonl", [_record(BASE + timedelta(hours=2), "D", str(repo), _tool("Write", str(repo / "g.py")))])
    incs = _incidents(_run(tmp_path))
    assert [i["category"] for i in incs] == ["concurrent_write"]
    assert [i["path"] for i in incs] == [str(repo / "f.py")]


def test_stale_read_sequence(tmp_path):
    repo = _repo(tmp_path)
    proj = tmp_path / "projects" / "p1"
    _write_jsonl(proj / "a.jsonl", [
        _record(BASE, "A", str(repo), _tool("Read", str(repo / "f.py"))),
        _record(BASE + timedelta(minutes=6), "A", str(repo), _tool("Write", str(repo / "other.py"))),
    ])
    _write_jsonl(proj / "b.jsonl", [
        _record(BASE + timedelta(minutes=5), "B", str(repo), _tool("Write", str(repo / "f.py"))),
    ])
    incs = _incidents(_run(tmp_path))
    assert [i["category"] for i in incs] == ["stale_read"]
    assert incs[0]["path"] == str(repo / "f.py")


def test_stale_read_suppressed_when_a_rereads_after_b(tmp_path):
    # A re-read f.py after B's write, so A's later write rests on a fresh
    # premise: not a stale read.
    repo = _repo(tmp_path)
    proj = tmp_path / "projects" / "p1"
    _write_jsonl(proj / "a.jsonl", [
        _record(BASE, "A", str(repo), _tool("Read", str(repo / "f.py"))),
        _record(BASE + timedelta(minutes=6), "A", str(repo), _tool("Read", str(repo / "f.py"))),
        _record(BASE + timedelta(minutes=7), "A", str(repo), _tool("Write", str(repo / "other.py"))),
    ])
    _write_jsonl(proj / "b.jsonl", [
        _record(BASE + timedelta(minutes=5), "B", str(repo), _tool("Write", str(repo / "f.py"))),
    ])
    assert _incidents(_run(tmp_path)) == []


def test_since_filters_events_by_time_not_only_file_mtime(tmp_path):
    # The transcript file is fresh (just written), but its events are years
    # old: --since must drop them.
    repo = _repo(tmp_path)
    proj = tmp_path / "projects" / "p1"
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    _write_jsonl(proj / "a.jsonl", [_record(old, "A", str(repo), _tool("Write", str(repo / "f.py")))])
    _write_jsonl(proj / "b.jsonl", [_record(old, "B", str(repo), _tool("Write", str(repo / "f.py")))])
    out = tmp_path / "out"
    assert cr.main(["--projects-dir", str(tmp_path / "projects"), "--out", str(out), "--since", "14d"]) == 0
    assert _incidents(out) == []


def test_scratch_paths_excluded_by_default(tmp_path):
    scratch = "/tmp/concord-replay-test-scratch.json"
    proj = tmp_path / "projects" / "p1"
    _write_jsonl(proj / "a.jsonl", [_record(BASE, "A", "/tmp", _tool("Write", scratch))])
    _write_jsonl(proj / "b.jsonl", [_record(BASE + timedelta(minutes=1), "B", "/tmp", _tool("Write", scratch))])
    default_out = tmp_path / "out-default"
    assert cr.main(["--projects-dir", str(tmp_path / "projects"), "--out", str(default_out),
                    "--since", "3650d"]) == 0
    assert _incidents(default_out) == []
    assert [i["category"] for i in _incidents(_run(tmp_path))] == ["concurrent_write"]


def test_stale_read_absent_when_a_writes_before_b(tmp_path):
    repo = _repo(tmp_path)
    proj = tmp_path / "projects" / "p1"
    _write_jsonl(proj / "a.jsonl", [
        _record(BASE, "A", str(repo), _tool("Read", str(repo / "f.py"))),
        _record(BASE + timedelta(minutes=3), "A", str(repo), _tool("Write", str(repo / "other.py"))),
    ])
    _write_jsonl(proj / "b.jsonl", [
        _record(BASE + timedelta(minutes=5), "B", str(repo), _tool("Write", str(repo / "f.py"))),
    ])
    assert _incidents(_run(tmp_path)) == []


def test_logical_overlap_worktree_resolves_main_repo(tmp_path):
    main = _repo(tmp_path, "main")
    wt = tmp_path / "wt"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    proj = tmp_path / "projects" / "p1"
    _write_jsonl(proj / "a.jsonl", [_record(BASE, "A", str(main), _tool("Write", str(main / "shared.py")))])
    _write_jsonl(proj / "b.jsonl", [_record(BASE + timedelta(minutes=5), "B", str(wt), _tool("Write", str(wt / "shared.py")))])
    incs = _incidents(_run(tmp_path))
    assert [i["category"] for i in incs] == ["logical_overlap"]
    assert incs[0]["repo_root"] == str(main)
    assert incs[0]["path"] == "shared.py"


def test_hot_write_reported_with_base_category(tmp_path):
    repo = _repo(tmp_path)
    hot = repo / ".mini-ork" / "config" / "agents.yaml"
    hot.parent.mkdir(parents=True)
    proj = tmp_path / "projects" / "p1"
    _write_jsonl(proj / "a.jsonl", [_record(BASE, "A", str(repo), _tool("Write", str(hot)))])
    _write_jsonl(proj / "b.jsonl", [_record(BASE + timedelta(minutes=5), "B", str(repo), _tool("Write", str(hot)))])
    incs = _incidents(_run(tmp_path))
    assert {i["category"] for i in incs} == {"concurrent_write", "hot_write"}
    hot_inc = next(i for i in incs if i["category"] == "hot_write")
    assert hot_inc["base_category"] == "concurrent_write"
    assert hot_inc["path"] == ".mini-ork/config/agents.yaml"


def test_malformed_lines_skipped_and_counted(tmp_path, capsys):
    repo = _repo(tmp_path)
    proj = tmp_path / "projects" / "p1"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "a.jsonl").write_text(
        json.dumps(_record(BASE, "A", str(repo), _tool("Write", str(repo / "f.py")))) + "\n"
        + '{"type":"tool_use", not valid json\n'
        + json.dumps(_record(BASE + timedelta(minutes=5), "B", str(repo), _tool("Write", str(repo / "f.py")))) + "\n"
        + "\n"
    )
    out = tmp_path / "out"
    rc = cr.main(["--projects-dir", str(tmp_path / "projects"), "--out", str(out), "--since", "3650d",
                  "--no-default-excludes"])
    assert rc == 0
    incs = _incidents(out)
    assert [i["category"] for i in incs] == ["concurrent_write"]
    assert "1 malformed line(s) skipped" in capsys.readouterr().err


def test_sample_deterministic_for_fixed_seed(tmp_path):
    repo = _repo(tmp_path)
    proj = tmp_path / "projects" / "p1"
    for i in range(3):
        f = repo / f"f{i}.py"
        _write_jsonl(proj / f"a{i}.jsonl", [_record(BASE, f"A{i}", str(repo), _tool("Write", str(f)))])
        _write_jsonl(proj / f"b{i}.jsonl", [_record(BASE + timedelta(minutes=5), f"B{i}", str(repo), _tool("Write", str(f)))])
    base_args = ["--projects-dir", str(tmp_path / "projects"), "--since", "3650d", "--seed", "7"]
    out1, out2 = tmp_path / "out1", tmp_path / "out2"
    assert cr.main(base_args + ["--out", str(out1)]) == 0
    assert cr.main(base_args + ["--out", str(out2)]) == 0
    assert (out1 / "label-sample.csv").read_text() == (out2 / "label-sample.csv").read_text()
    assert (out1 / "incidents.jsonl").read_text() == (out2 / "incidents.jsonl").read_text()


def test_score_prints_precision_per_category(tmp_path, capsys):
    csv_path = tmp_path / "labels.csv"
    csv_path.write_text(
        "incident_id,category,path,session_a,session_b,ts,label,notes\n"
        "inc-0001,concurrent_write,/a/f.py,A,B,2026-10-01T12:00:00Z,real,\n"
        "inc-0002,concurrent_write,/a/f.py,C,D,2026-10-01T12:00:00Z,real,\n"
        "inc-0003,concurrent_write,/a/f.py,E,F,2026-10-01T12:00:00Z,noise,\n"
        "inc-0004,stale_read,/a/f.py,A,B,2026-10-01T12:00:00Z,real,\n"
        "inc-0005,stale_read,/a/f.py,C,D,2026-10-01T12:00:00Z,noise,\n"
    )
    assert cr.main(["--score", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert "concurrent_write: 0.666667" in out
    assert "stale_read: 0.500000" in out


def test_same_session_never_forms_incident_with_itself(tmp_path):
    repo = _repo(tmp_path)
    proj = tmp_path / "projects" / "p1"
    _write_jsonl(proj / "a.jsonl", [
        _record(BASE, "A", str(repo), _tool("Write", str(repo / "f.py"))),
        _record(BASE + timedelta(minutes=5), "A", str(repo), _tool("Write", str(repo / "f.py"))),
        _record(BASE, "A", str(repo), _tool("Read", str(repo / "h.py"))),
        _record(BASE + timedelta(minutes=6), "A", str(repo), _tool("Write", str(repo / "h.py"))),
    ])
    assert _incidents(_run(tmp_path)) == []


def test_notebook_edit_uses_notebook_path(tmp_path):
    repo = _repo(tmp_path)
    proj = tmp_path / "projects" / "p1"
    nb_tool = {"type": "tool_use", "name": "NotebookEdit", "input": {"notebook_path": str(repo / "nb.ipynb")}}
    _write_jsonl(proj / "a.jsonl", [_record(BASE, "A", str(repo), nb_tool)])
    _write_jsonl(proj / "b.jsonl", [_record(BASE + timedelta(minutes=5), "B", str(repo), nb_tool)])
    incs = _incidents(_run(tmp_path))
    assert [i["category"] for i in incs] == ["concurrent_write"]
    assert [i["path"] for i in incs] == [str(repo / "nb.ipynb")]
