"""A frozen-path publish refusal (and override) is a safety event, and R1f.

``land_patch`` is the single publish choke point: a patch touching a frozen path
is refused there with ``publish-frozen-path``. That refusal used to be silent,
and ``diagnose``'s R1 classified every ``publish_abstain`` with proven levels as
an ENGINE fault — so the self-driving loop opened a fix epic to "fix" the engine
that correctly refused a frozen edit.

These tests pin the two halves of the fix: the refusal/override write a
``safety_events`` row (best effort, never changing the verdict), and a
frozen-path abstain is diagnosed as an owner decision (``R1f``) that wins over
R1/R1b.

Hermetic: a throwaway git repo per test through the real ``land_patch``, a
throwaway sqlite DB seeded from the frozen migration, and a temp ``home`` for
diagnose. No network, no provider credentials.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mini_ork.cli import publisher_authored_patch as pap
from mini_ork.cli import publisher as pub
from mini_ork.recovery import auto_repair
from mini_ork.recovery import diagnose as dx
from mini_ork.stores import migrate as mig
from mini_ork.stores import safety_events

REPO = Path(__file__).resolve().parents[2]

GIT_IDENTITY = ("-c", "user.name=mo-test", "-c", "user.email=mo-test@example.invalid")

FIXTURE_REL = "recipes/code-fix/probes/fixtures/probe-1/tally.py"
ORDINARY_REL = "docs/note.txt"

FIXTURE_BASE = "TALLY_BASE = 1\n"
FIXTURE_EDITED = "TALLY_BASE = 2\n"
NOTE_BASE = "ordinary note\n"
NOTE_EDITED = "ordinary note, edited\n"

# A minimal kernel-frozen document declaring only the fixtures glob.
FIXTURE_ONLY_YAML = "version: 1\npaths:\n  - recipes/code-fix/probes/fixtures/**\n"

_SHA40 = re.compile(r"^[0-9a-f]{40}$")


# ── land_patch harness (verbatim from test_frozen_paths_enforced.py) ─────────


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """Run git in the throwaway repo with the index/dir env sanitized, so an
    ambient lane value can never make an assertion read a different tree than
    ``land_patch`` did."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")}
    proc = subprocess.run(["git", *GIT_IDENTITY, *args], cwd=repo, env=env,
                          capture_output=True, text=True, check=check, timeout=60)
    return proc.stdout


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_repo(tmp_path: Path, frozen_yaml: str | None = FIXTURE_ONLY_YAML) -> Path:
    """A throwaway git repo holding the files the tests' patches touch.

    ``frozen_yaml=None`` leaves ``config/kernel-frozen.yaml`` absent (the
    fail-closed case); a garbage string writes an unparsable document."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, FIXTURE_REL, FIXTURE_BASE)
    _write(repo, ORDINARY_REL, NOTE_BASE)
    if frozen_yaml is not None:
        _write(repo, "config/kernel-frozen.yaml", frozen_yaml)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _patch(repo: Path, edits: dict[str, tuple[str, str]]) -> str:
    """Real ``git diff`` output applying each ``rel: (old, new)`` edit.

    The tree is restored afterwards, so the repo is clean and the patch is
    exactly what a run hands the publisher."""
    for rel, (_old, new) in edits.items():
        _write(repo, rel, new)
    diff = _git(repo, "diff", "--", *edits)
    for rel, (old, _new) in edits.items():
        _write(repo, rel, old)
    assert diff.strip(), "harness bug: generated an empty patch"
    return diff


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture(autouse=True)
def _no_frozen_override(monkeypatch):
    """Every test deletes MO_ALLOW_FROZEN_EDIT unless it sets it."""
    monkeypatch.delenv("MO_ALLOW_FROZEN_EDIT", raising=False)


def _make_db(tmp_path: Path) -> Path:
    """A temp state.db carrying the real safety_events schema (frozen migration)."""
    dbp = tmp_path / "state.db"
    con = sqlite3.connect(str(dbp))
    try:
        con.executescript((REPO / "db/migrations/0036_safety_events.sql").read_text())
        con.commit()
    finally:
        con.close()
    return dbp


def _rows(dbp: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(dbp))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM safety_events").fetchall()]
    finally:
        con.close()


# ── a refusal / override writes one row ─────────────────────────────────────


def test_refusal_writes_one_row(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    dbp = _make_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_DB", str(dbp))
    head0 = _head(repo)
    patch = _patch(repo, {FIXTURE_REL: (FIXTURE_BASE, FIXTURE_EDITED)})

    got = pap.land_patch(str(repo), patch, "", run_id="run-x")

    assert isinstance(got, pap.Abstain)
    assert got.reason == "publish-frozen-path"
    assert _head(repo) == head0
    rows = _rows(dbp)
    assert len(rows) == 1, f"expected exactly one safety_events row, got {rows}"
    row = rows[0]
    assert row["tripwire_id"] == "frozen-kernel-edit"
    assert row["severity"] == "high"
    assert row["run_id"] == "run-x"
    assert row["status"] == "open"
    assert json.loads(row["evidence_json"]) == {
        "run_id": "run-x", "paths": [FIXTURE_REL], "boundary": "publish"}


def test_ordinary_patch_writes_nothing(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    dbp = _make_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_DB", str(dbp))
    patch = _patch(repo, {ORDINARY_REL: (NOTE_BASE, NOTE_EDITED)})

    got = pap.land_patch(str(repo), patch, "", run_id="run-o")

    assert isinstance(got, str)
    assert _SHA40.match(got), f"expected a 40-hex sha, got {got!r}"
    assert _rows(dbp) == []


def test_override_is_recorded(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    dbp = _make_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_DB", str(dbp))
    monkeypatch.setenv("MO_ALLOW_FROZEN_EDIT", "1")
    patch = _patch(repo, {FIXTURE_REL: (FIXTURE_BASE, FIXTURE_EDITED)})

    got = pap.land_patch(str(repo), patch, "", run_id="run-ov")

    assert isinstance(got, str)
    assert _SHA40.match(got), f"expected a 40-hex sha, got {got!r}"
    rows = _rows(dbp)
    assert len(rows) == 1, f"expected exactly one safety_events row, got {rows}"
    row = rows[0]
    assert row["tripwire_id"] == "frozen-kernel-override"
    assert row["severity"] == "medium"
    assert json.loads(row["evidence_json"])["boundary"] == "publish"


def test_no_run_id_is_fine(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    dbp = _make_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_DB", str(dbp))
    patch = _patch(repo, {FIXTURE_REL: (FIXTURE_BASE, FIXTURE_EDITED)})

    got = pap.land_patch(str(repo), patch, "")

    assert isinstance(got, pap.Abstain)
    assert got.reason == "publish-frozen-path"
    rows = _rows(dbp)
    assert len(rows) == 1
    assert rows[0]["run_id"] is None  # NULL
    assert json.loads(rows[0]["evidence_json"])["run_id"] == ""


# ── the refusal never depends on the event landing ──────────────────────────


def test_refusal_survives_missing_table(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")  # a valid, empty sqlite file: no tables
    monkeypatch.setenv("MINI_ORK_DB", str(empty))
    head0 = _head(repo)
    patch = _patch(repo, {FIXTURE_REL: (FIXTURE_BASE, FIXTURE_EDITED)})

    got = pap.land_patch(str(repo), patch, "", run_id="run-x")

    assert isinstance(got, pap.Abstain)
    assert got.reason == "publish-frozen-path"
    assert _head(repo) == head0


def test_refusal_survives_emit_raise(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    dbp = _make_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_DB", str(dbp))
    head0 = _head(repo)

    def _boom(*args, **kwargs):
        raise RuntimeError("safety_events store is on fire")

    monkeypatch.setattr(safety_events, "emit", _boom)
    patch = _patch(repo, {FIXTURE_REL: (FIXTURE_BASE, FIXTURE_EDITED)})

    got = pap.land_patch(str(repo), patch, "", run_id="run-x")

    assert isinstance(got, pap.Abstain)
    assert got.reason == "publish-frozen-path"
    assert _head(repo) == head0
    assert pap._record_frozen_event("frozen-kernel-edit", "high", ["a"],
                                    run_id="r") is False


# ── the publisher passes the run id through ─────────────────────────────────


def test_publisher_passes_run_id(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "review-verdict.json").write_text(
        json.dumps({"verdict": "approve"}), encoding="utf-8")

    seen: dict[str, Any] = {}

    def fake_land_patch(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return pap.Abstain("publish-frozen-path", "d")

    monkeypatch.setattr(
        pap, "resolve_authored_patch",
        lambda run_dir, repo=None: pap.AuthoredPatch(
            patch_text="diff --git a/x b/x\n", source="carry-patch"))
    monkeypatch.setattr(pap, "report_foreign_hunks", lambda *a, **k: [])
    monkeypatch.setattr(pap, "land_patch", fake_land_patch)

    got = pub._publisher_try_commit_files(
        str(tmp_path), str(tmp_path), str(run_dir), "", "", "code-fix",
        "implementer", "run-y")

    assert got is False
    assert seen.get("kwargs", {}).get("run_id") == "run-y"
    assert "message" in seen.get("kwargs", {})  # the existing kwarg survives
    abstain = json.loads((run_dir / "publish-abstain.json").read_text())
    assert abstain["reason"] == "publish-frozen-path"


# ── R1f — a frozen-path refusal is an owner decision ────────────────────────

RUN = "run-1791914481-48337"
T0 = 1_791_914_481


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, *, run_id: str = RUN, status: str = "failed",
          recipe: str = "framework-edit", notes: str = "") -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": recipe}))
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
        "updated_at, ended_at, task_class, kickoff_path, workflow_version, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, 0.5, T0, T0 + 100, T0 + 80, "framework_edit",
         "", "latest", notes or None),
    )
    con.commit()
    con.close()
    return run_dir


def _event(home: Path, run_id: str, seq: int, event_type: str, payload: dict[str, Any]) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"evt-{run_id}-{event_type}-{seq}", run_id, event_type,
         json.dumps(payload), T0 + seq),
    )
    con.commit()
    con.close()


def _assert_contract(result: dict[str, Any]) -> None:
    """The wire contract every result — matched or fall-through — must obey."""
    assert result["layer"] in dx.LAYERS, result
    assert result["routes_to"] in dx.ROUTES, result
    for item in result["evidence"]:
        assert set(item) == {"kind", "ref", "excerpt"}, item
    if result["rule_id"] == dx.RULE_UNCLASSIFIED:
        assert result["source"] == ""
    else:
        assert result["source"] == "rule", result


def _seed_abstain(home: Path, run_id: str = RUN, *, levels_ok: bool = True,
                  reason: str = "publish_abstain") -> Path:
    """The live shape: every node done, the publisher withheld the publish."""
    run_dir = _seed(home, run_id=run_id)
    (run_dir / "publish-abstain.json").write_text(json.dumps({
        "reason": "authored patch did not apply",
        "detail": "the implementer's patch no longer applies to the target tree",
    }))
    (run_dir / "verdict.json").write_text(json.dumps({
        "verdict": "pass",
        "source": "execute@run-level",
        "levels": {"applies": "PROVEN", "executes": "PROVEN", "target": "PROVEN",
                   "preserve": "PROVEN", "contract": "PROVEN"},
        "levels_required": ["applies", "target"],
        "levels_ok": levels_ok,
        "levels_decision": "publish" if levels_ok else "abstain",
    }))
    _event(home, run_id, 1, "node_start", {"node_id": "implementer"})
    _event(home, run_id, 2, "node_end",
           {"node_id": "implementer", "finish_reason": "done"})
    _event(home, run_id, 3, "node_start", {"node_id": "publisher", "node_type": "publisher"})
    _event(home, run_id, 4, "node_end",
           {"node_id": "publisher", "node_type": "publisher", "finish_reason": reason})
    return run_dir


def _overwrite_frozen_abstain(run_dir: Path) -> None:
    (run_dir / "publish-abstain.json").write_text(json.dumps({
        "reason": "publish-frozen-path",
        "detail": "refusing to publish: mini_ork/recovery/auto_repair.py is frozen",
    }))


def test_r1f_owner_decision(home: Path) -> None:
    run_dir = _seed_abstain(home)
    _overwrite_frozen_abstain(run_dir)

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R1f"
    assert (result["layer"], result["stage"], result["node"]) == \
        ("kickoff", "deliver", "publisher")
    assert result["routes_to"] == "owner"
    assert result["source"] == "rule"
    assert result["confidence"] == 0.95
    refs = [item.get("ref") for item in result["evidence"]]
    assert "publish-abstain.json" in refs, f"evidence refs: {refs}"
    _assert_contract(result)
    assert auto_repair._escalatable(result) is False


def test_r1f_wins_over_r1b(home: Path) -> None:
    run_dir = _seed_abstain(home, levels_ok=False)
    _overwrite_frozen_abstain(run_dir)

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R1f"
    assert result["routes_to"] == "owner"


def test_r1_unchanged(home: Path) -> None:
    _seed_abstain(home)

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R1"
    assert result["layer"] == "engine"
    assert result["routes_to"] == "fix_epic"
