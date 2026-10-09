"""Contract tests for the only-verified-learnings rule
(``mini_ork/learning/prompt_directives.py`` + the ``--revert-unverified`` branch
of ``mini_ork/cli/apply.py``).

The rule (user decision, 2026-10-07): a learned directive may sit in a recipe
prompt file only if the apply loop MEASURED it and promoted it. This suite pins:

  1. ``scan`` finds every applied block with the exact line range of a fixture
     prompt carrying two blocks and surrounding text.
  2. ``revert_unverified`` removes those lines and leaves the surrounding bytes
     identical — byte-exact, because the blocks were written by
     ``existing.rstrip("\\n") + "\\n\\n" + block + "\\n"``.
  3. ``verification`` is the honesty predicate: an UNVETTED/mock promote is NOT
     verified, a probe promote IS, a quarantined-only source is not.
  4. ``revert_unverified`` writes one ``rejected``/``human`` row (the
     CHECK-legal encoding — see the module docstring) and quarantines the
     suffix-matched version_registry row; a second call is a no-op.
  5. ``apply_mutation`` writes a sidecar entry on a real promote.
  6. The real-repo guard: no applied marker survives without a sidecar entry
     that names a real scorer.

The temp DB mirrors the REAL ``db/migrations/0011_evolution.sql`` DDL *including
the CHECK constraints*. The minimal fixture in ``test_cli_apply_py.py`` drops
them, which is exactly how a schema-invalid write (``decision='reverted'`` /
``decided_by='operator'``) would pass tests and then crash the operator's
post-merge ``--db-only`` run against the live DB.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import apply as ap  # noqa: E402
from mini_ork.learning import prompt_directives as pd  # noqa: E402

# Canonical 0011_evolution.sql + 0048_apply_attempts.sql shapes, CHECKs included.
REAL_SCHEMA_DDL = """
CREATE TABLE workflow_candidates (
  candidate_id              TEXT PRIMARY KEY,
  base_workflow_version_id  TEXT NOT NULL,
  mutations                 TEXT NOT NULL DEFAULT '[]',
  status                    TEXT NOT NULL DEFAULT 'candidate',
  benchmark_summary_id      TEXT,
  utility_delta             REAL NOT NULL DEFAULT 0.0,
  created_by                TEXT NOT NULL DEFAULT 'evolution_engine',
  created_at                TEXT NOT NULL DEFAULT ''
);
CREATE TABLE promotion_records (
  promotion_id          TEXT PRIMARY KEY,
  candidate_id          TEXT NOT NULL REFERENCES workflow_candidates(candidate_id),
  from_version_id       TEXT NOT NULL,
  to_version_id         TEXT NOT NULL,
  utility_before        REAL NOT NULL DEFAULT 0.0,
  utility_after         REAL NOT NULL DEFAULT 0.0,
  benchmark_run_id      TEXT,
  rationale             TEXT NOT NULL DEFAULT '',
  decision              TEXT NOT NULL
                        CHECK (decision IN ('promoted','quarantined','rejected','pending_human_approval')),
  decided_at            TEXT NOT NULL DEFAULT '',
  decided_by            TEXT NOT NULL CHECK (decided_by IN ('gate','human'))
);
CREATE TABLE apply_attempts (
    attempt_id              TEXT PRIMARY KEY,
    task_class              TEXT NOT NULL,
    target_kind             TEXT NOT NULL CHECK (target_kind IN
                            ('workflow_node','workflow_edge','agent_prompt','prompt_file')),
    target_name             TEXT NOT NULL,
    source_kind             TEXT NOT NULL CHECK (source_kind IN
                            ('pattern_records','emergent_patterns','gradient_records',
                             'synthesis_gate_verdict','none')),
    source_id               TEXT,
    candidate_id            TEXT REFERENCES workflow_candidates(candidate_id),
    promotion_id            TEXT REFERENCES promotion_records(promotion_id),
    base_workflow_version_id TEXT,
    utility_before          REAL,
    utility_after           REAL,
    utility_delta           REAL,
    decision                TEXT NOT NULL CHECK (decision IN
                            ('promoted','quarantined','rejected',
                             'pending_human_approval','dry_run','no_candidate')),
    rationale               TEXT NOT NULL DEFAULT '',
    dry_run                 INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
    apply_enabled           INTEGER NOT NULL DEFAULT 0 CHECK (apply_enabled IN (0,1)),
    created_at              TEXT NOT NULL DEFAULT ''
);
CREATE TABLE version_registry (
    version_id               TEXT PRIMARY KEY,
    kind                     TEXT NOT NULL CHECK(kind IN ('workflow','agent')),
    name                     TEXT NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'candidate'
                                 CHECK(status IN ('candidate','stable','quarantined','retired')),
    payload                  TEXT NOT NULL DEFAULT '{}',
    previous_stable_version  TEXT,
    quarantine_reason        TEXT,
    quarantine_cleared_by    TEXT,
    utility_score            REAL DEFAULT 0.0,
    promoted_at              INTEGER,
    quarantined_at           INTEGER,
    created_at               INTEGER NOT NULL
);
"""

_ENV = ["MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_ROOT", "MO_APPLY_ENABLED",
        "MO_APPLY_DRY_RUN", "MO_APPLY_MODE", "MO_APPLY_SCORER"]


@pytest.fixture()
def envscrub(monkeypatch):
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture()
def db(tmp_path, envscrub):
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript(REAL_SCHEMA_DDL)
    con.commit()
    con.close()
    envscrub.setenv("MINI_ORK_DB", str(path))
    ap._SCHEMA_INIT = False
    yield str(path)
    ap._SCHEMA_INIT = False


FIXTURE_PROMPT = (
    "# Prompt\n"
    "\n"
    "Intro stays.\n"
    "\n"
    "<!-- applied:gradient_records:gr-aaa -->\n"
    "- Observation: obs a\n"
    "- Directive: dir a\n"
    "\n"
    "Middle stays.\n"
    "\n"
    "<!-- applied:gradient_records:gr-bbb -->\n"
    "- Observation: obs b\n"
    "- Directive: dir b\n"
)


def _make_repo(tmp_path, prompt: str = FIXTURE_PROMPT) -> Path:
    repo = tmp_path / "repo"
    (repo / "recipes" / "demo" / "prompts").mkdir(parents=True)
    (repo / "recipes" / "demo" / "prompts" / "agent.md").write_text(prompt)
    return repo


def _seed(db_path, *, source_id, candidate_id, decision, rationale,
          decided_by="gate", target_name="prompts/agent.md"):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO workflow_candidates (candidate_id, base_workflow_version_id)"
        " VALUES (?,?)", (candidate_id, "wf-synthetic-baseline"))
    con.execute(
        "INSERT INTO apply_attempts"
        " (attempt_id, task_class, target_kind, target_name, source_kind,"
        "  source_id, candidate_id, decision, rationale)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (f"a-{candidate_id}", "framework_edit", "prompt_file", target_name,
         "gradient_records", source_id, candidate_id, decision, rationale))
    con.execute(
        "INSERT INTO promotion_records"
        " (promotion_id, candidate_id, from_version_id, to_version_id, rationale,"
        "  decision, decided_at, decided_by)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (f"pr-{candidate_id}", candidate_id, "wf-synthetic-baseline",
         "wf-synthetic-baseline", rationale, decision, "2026-09-12T00:00:00.000Z",
         decided_by))
    con.commit()
    con.close()


def _rows(db_path, sql, args=()):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


# ── 1. scan ──────────────────────────────────────────────────────────────────

def test_scan_finds_blocks_with_exact_line_ranges(tmp_path):
    repo = _make_repo(tmp_path)
    blocks = pd.scan(str(repo))
    assert [(b["source_id"], b["start_line"], b["end_line"]) for b in blocks] == [
        ("gr-aaa", 4, 7),   # blank line 4 + marker/obs/dir 5-7
        ("gr-bbb", 10, 13),
    ]
    assert blocks[0]["file"] == "recipes/demo/prompts/agent.md"
    assert blocks[0]["marker_ref"] == "gradient_records:gr-aaa"
    assert blocks[0]["text"] == (
        "\n<!-- applied:gradient_records:gr-aaa -->\n"
        "- Observation: obs a\n- Directive: dir a")


# ── 2. byte-exact removal ────────────────────────────────────────────────────

def test_revert_removes_blocks_and_leaves_surrounding_bytes(tmp_path, db):
    repo = _make_repo(tmp_path)
    # Both sources unverified (nothing in the DB -> verification False).
    result = pd.revert_unverified(str(repo), db, files=True, record=False)
    assert result["files_changed"] == ["recipes/demo/prompts/agent.md"]
    assert result["removed"] == ["gr-aaa", "gr-bbb"]
    # The append path was `text.rstrip("\n") + "\n\n" + block + "\n"` per block;
    # reversing it leaves the original prose with no trailing blanks.
    assert (repo / "recipes" / "demo" / "prompts" / "agent.md").read_text() == (
        "# Prompt\n\nIntro stays.\n\nMiddle stays.\n")


def test_revert_removes_only_the_unverified_block(tmp_path, db):
    repo = _make_repo(tmp_path)
    _seed(db, source_id="gr-aaa", candidate_id="c-mock", decision="promoted",
          rationale="UNVETTED promote (scorer=mock fabricates utility; "
                    "operator-enabled via MO_APPLY_UNVETTED)")
    _seed(db, source_id="gr-bbb", candidate_id="c-probe", decision="promoted",
          rationale="probe: n=2 before=0.50 after=0.70 control_n=1; improved")
    result = pd.revert_unverified(str(repo), db, files=True, record=False)
    assert result["removed"] == ["gr-aaa"]
    assert result["kept_verified"] == ["gr-bbb"]
    text = (repo / "recipes" / "demo" / "prompts" / "agent.md").read_text()
    assert "gr-aaa" not in text
    assert "<!-- applied:gradient_records:gr-bbb -->" in text
    assert "- Directive: dir b" in text


# ── 3. verification — the honesty predicate ──────────────────────────────────

def test_verification_rejects_mock_promote(db):
    _seed(db, source_id="gr-mock", candidate_id="c-mock", decision="promoted",
          rationale="UNVETTED promote (scorer=mock fabricates utility; "
                    "operator-enabled via MO_APPLY_UNVETTED)")
    out = pd.verification(db, "gr-mock")
    assert out["verified"] is False
    assert out["candidate_id"] == "c-mock"


def test_verification_accepts_probe_promote(db):
    _seed(db, source_id="gr-probe", candidate_id="c-probe", decision="promoted",
          rationale="probe: n=2 before=0.50 after=0.70 control_n=1; improved")
    assert pd.verification(db, "gr-probe")["verified"] is True


def test_verification_rejects_quarantine_only_source(db):
    _seed(db, source_id="gr-quar", candidate_id="c-quar", decision="quarantined",
          rationale="probe scorer measured nothing (no frozen probe set)")
    assert pd.verification(db, "gr-quar")["verified"] is False


def test_verification_tolerates_missing_db(tmp_path):
    assert pd.verification(str(tmp_path / "nope.db"), "gr-anything")["verified"] is False


# ── 4. revert_unverified: record + quarantine, idempotent ────────────────────

def test_revert_records_rejected_row_and_quarantines_version(tmp_path, db):
    repo = _make_repo(tmp_path)
    _seed(db, source_id="gr-aaa", candidate_id="c-mock", decision="promoted",
          rationale="UNVETTED promote (scorer=mock fabricates utility; "
                    "operator-enabled via MO_APPLY_UNVETTED)")
    _seed(db, source_id="gr-bbb", candidate_id="c-probe", decision="promoted",
          rationale="probe: n=2 before=0.50 after=0.70 control_n=1; improved")

    # A legacy version_registry agent row promoted by an old worktree: same
    # repo-relative suffix, no candidate_id in the payload -> suffix match.
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO version_registry (version_id, kind, name, status, payload, created_at)"
        " VALUES ('v-age-1','agent',?, 'stable', ?, 0)",
        ("/old/worktree/recipes/demo/prompts/agent.md",
         json.dumps({"name": "recipes/demo/prompts/agent.md", "status": "stable"})))
    con.commit()
    con.close()

    result = pd.revert_unverified(str(repo), db)
    assert result["recorded"] == 1
    assert result["quarantined"] == ["v-age-1"]

    reverts = _rows(db, "SELECT * FROM promotion_records WHERE decision='rejected'")
    assert len(reverts) == 1
    assert reverts[0]["decided_by"] == "human"           # CHECK-legal encoding
    assert reverts[0]["candidate_id"] == "c-mock"
    assert reverts[0]["rationale"].startswith("unverified:")

    vrow = _rows(db, "SELECT * FROM version_registry WHERE version_id='v-age-1'")[0]
    assert vrow["status"] == "quarantined"
    assert vrow["quarantine_reason"] == pd.REVERT_RATIONALE
    assert vrow["quarantined_at"]

    text = (repo / "recipes" / "demo" / "prompts" / "agent.md").read_text()
    assert "gr-aaa" not in text
    assert "gr-bbb" in text

    # Idempotent: a second run removes nothing and records nothing.
    before = len(_rows(db, "SELECT * FROM promotion_records"))
    again = pd.revert_unverified(str(repo), db)
    assert again["removed"] == []
    assert again["files_changed"] == []
    assert again["recorded"] == 0
    assert again["quarantined"] == []
    assert len(_rows(db, "SELECT * FROM promotion_records")) == before


def test_db_only_discovery_finds_unverified_sources(tmp_path, db):
    _seed(db, source_id="gr-legacy", candidate_id="c-legacy", decision="promoted",
          rationale="UNVETTED promote (scorer=mock fabricates utility; "
                    "operator-enabled via MO_APPLY_UNVETTED)",
          target_name="prompts/agent.md")
    # Markers already gone from disk (post-merge): discovery must come from DB.
    repo = tmp_path / "repo"
    (repo / "recipes").mkdir(parents=True)
    assert pd._discover_unverified_source_ids(db) == ["gr-legacy"]
    result = pd.revert_unverified(str(repo), db, files=False, record=True)
    assert result["removed"] == ["gr-legacy"]
    assert result["recorded"] == 1
    assert _rows(db, "SELECT * FROM promotion_records WHERE decision='rejected'")


def _seed_version_row(db_path, version_id, *, candidate_id, status="stable",
                      cleared_by=None):
    """A version_registry agent row carrying ``candidate_id`` in its payload.

    ``--db-only`` has no on-disk blocks, so ``_quarantine`` matches these rows
    by ``payload.candidate_id`` (the suffix branch needs ``files=True``).
    """
    payload = {"name": "recipes/demo/prompts/agent.md",
               "candidate_id": candidate_id}
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO version_registry (version_id, kind, name, status, payload,"
        " quarantine_cleared_by, created_at) VALUES (?, 'agent', ?, ?, ?, ?, 0)",
        (version_id, "/old/wt/recipes/demo/prompts/agent.md", status,
         json.dumps(payload), cleared_by))
    con.commit()
    con.close()


def test_db_only_second_run_does_not_requarantine(tmp_path, db):
    """The replay hazard lives in ``--db-only`` mode only.

    ``files`` mode rebuilds the work list from disk (clean after the first run),
    so ``_quarantine`` is never reached a second time. ``--db-only`` rebuilds it
    from the DB, where the original promote rows persist, so the second run
    still calls ``_quarantine`` — which must not rewrite ``quarantined_at``.
    """
    _seed(db, source_id="gr-legacy", candidate_id="c-legacy", decision="promoted",
          rationale="UNVETTED promote (scorer=mock fabricates utility; "
                    "operator-enabled via MO_APPLY_UNVETTED)",
          target_name="prompts/agent.md")
    _seed_version_row(db, "v-age-1", candidate_id="c-legacy")
    repo = tmp_path / "repo"
    (repo / "recipes").mkdir(parents=True)

    first = pd.revert_unverified(str(repo), db, files=False, record=True)
    assert first["recorded"] == 1
    assert first["quarantined"] == ["v-age-1"]
    stamp = _rows(db, "SELECT quarantined_at FROM version_registry"
                      " WHERE version_id='v-age-1'")[0]["quarantined_at"]
    assert stamp

    second = pd.revert_unverified(str(repo), db, files=False, record=True)
    assert second["recorded"] == 0
    assert second["quarantined"] == []            # nothing re-quarantined
    after = _rows(db, "SELECT status, quarantined_at FROM version_registry"
                      " WHERE version_id='v-age-1'")[0]
    assert after["status"] == "quarantined"
    assert after["quarantined_at"] == stamp       # not rewritten


def test_db_only_leaves_human_cleared_row_alone(tmp_path, db):
    """A row a human un-quarantined (status back to 'candidate' with
    ``quarantine_cleared_by`` set) must not be swept up again."""
    _seed(db, source_id="gr-legacy", candidate_id="c-legacy", decision="promoted",
          rationale="UNVETTED promote (scorer=mock fabricates utility; "
                    "operator-enabled via MO_APPLY_UNVETTED)",
          target_name="prompts/agent.md")
    _seed_version_row(db, "v-age-1", candidate_id="c-legacy",
                      status="candidate", cleared_by="alice")
    repo = tmp_path / "repo"
    (repo / "recipes").mkdir(parents=True)

    result = pd.revert_unverified(str(repo), db, files=False, record=True)
    assert result["quarantined"] == []
    row = _rows(db, "SELECT status, quarantine_cleared_by FROM version_registry"
                    " WHERE version_id='v-age-1'")[0]
    assert row["status"] == "candidate"
    assert row["quarantine_cleared_by"] == "alice"


# ── 5. sidecar on a real promote ─────────────────────────────────────────────

def test_apply_mutation_writes_sidecar(tmp_path, db, envscrub):
    target = tmp_path / "agent.md"
    target.write_text("ORIGINAL\n")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    vid = ap.apply_mutation(
        "c-probe", str(target), "emit a ledger", db=db,
        source_ref="gradient_records:gr-probe", source_id="gr-probe",
        scorer="probe", n=2, before=0.5, after=0.7)
    assert vid

    sidecar = tmp_path / pd.SIDECAR_NAME
    assert sidecar.is_file()
    entries = json.loads(sidecar.read_text())
    assert len(entries) == 1
    e = entries[0]
    assert e["source_id"] == "gr-probe"
    assert e["source_ref"] == "gradient_records:gr-probe"
    assert e["scorer"] == "probe"
    assert e["n"] == 2 and e["before"] == 0.5 and e["after"] == 0.7
    assert e["decided_at"]


# ── 6. the guard ─────────────────────────────────────────────────────────────

def test_guard_flags_marker_without_verified_sidecar(tmp_path):
    repo = _make_repo(tmp_path, prompt="# P\n\n<!-- applied:gradient_records:gr-x -->\n- Directive: d\n")
    assert pd.unverified_markers(str(repo)) == ["recipes/demo/prompts/agent.md"]

    prompts_dir = repo / "recipes" / "demo" / "prompts"
    prompts_dir.joinpath(pd.SIDECAR_NAME).write_text(
        json.dumps([{"source_id": "gr-x", "source_ref": "gradient_records:gr-x",
                     "scorer": "mock", "n": 1}]))
    assert pd.unverified_markers(str(repo)) == ["recipes/demo/prompts/agent.md"]

    prompts_dir.joinpath(pd.SIDECAR_NAME).write_text(
        json.dumps([{"source_id": "gr-x", "source_ref": "gradient_records:gr-x",
                     "scorer": "probe", "n": 2}]))
    assert pd.unverified_markers(str(repo)) == []


def test_real_repo_has_no_unverified_markers():
    """The rule, enforced against the actual repo tree: every applied block
    that survives must be explained by a sidecar entry naming a real scorer.
    After the revert this is vacuously true (no markers remain).

    Runs also read the project overlay (``<MINI_ORK_HOME>/recipes``) FIRST, so
    when a home is set the guard must walk it too — otherwise an overlay prompt
    edit is injected yet invisible.
    """
    assert pd.unverified_markers(REPO) == []
    assert pd.unverified_markers(REPO, pd.home_recipe_dirs()) == []


def test_home_recipe_dirs(monkeypatch):
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    assert pd.home_recipe_dirs() == []
    monkeypatch.setenv("MINI_ORK_HOME", str(Path("/tmp/x-home")))
    assert pd.home_recipe_dirs() == [str(Path("/tmp/x-home") / "recipes")]
    assert pd.home_recipe_dirs("/other") == [str(Path("/other") / "recipes")]


def test_guard_flags_overlay_marker_only_when_roots_passed(tmp_path):
    """A marker in the project overlay is invisible to a repo-only scan and
    visible once the overlay roots are supplied — the read-root/scan-root gap."""
    repo = _make_repo(tmp_path, prompt="# P\n")  # repo recipes are clean
    overlay_target = (tmp_path / "home" / "recipes" / "ovl" / "prompts" / "agent.md")
    overlay_target.parent.mkdir(parents=True)
    overlay_target.write_text(
        "# P\n\n<!-- applied:gradient_records:gr-ovl -->\n- Directive: d\n")

    assert pd.unverified_markers(str(repo)) == []  # repo-only: blind to overlay
    overlay = pd.home_recipe_dirs(str(tmp_path / "home"))
    assert pd.unverified_markers(str(repo), overlay) == [str(overlay_target)]


def test_revert_unverified_cleans_overlay(tmp_path, db):
    """The repair path reaches the same overlay roots the guard checks."""
    repo = _make_repo(tmp_path, prompt="# P\n")
    target = tmp_path / "home" / "recipes" / "ovl" / "prompts" / "agent.md"
    target.parent.mkdir(parents=True)
    target.write_text("# P\n\n<!-- applied:gradient_records:gr-ovl -->\n"
                      "- Observation: o\n- Directive: d\n")
    overlay = pd.home_recipe_dirs(str(tmp_path / "home"))

    res = pd.revert_unverified(str(repo), db, files=True, record=False,
                               extra_recipe_dirs=overlay)
    assert "gr-ovl" in res["removed"]
    assert res["files_changed"] == [str(target)]
    assert "applied:gradient_records" not in target.read_text()
    assert pd.unverified_markers(str(repo), overlay) == []


def test_cli_revert_include_home(tmp_path, db, envscrub, capsys):
    """``--revert-unverified --include-home`` cleans the overlay too."""
    repo = _make_repo(tmp_path, prompt="# P\n")
    target = tmp_path / "home" / "recipes" / "ovl" / "prompts" / "agent.md"
    target.parent.mkdir(parents=True)
    target.write_text("# P\n\n<!-- applied:gradient_records:gr-ovl -->\n"
                      "- Directive: d\n")
    envscrub.setenv("MINI_ORK_HOME", str(tmp_path / "home"))

    rc = ap._revert_unverified_main(
        ["--revert-unverified", "--files-only", "--include-home"], str(repo))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "gr-ovl" in out["removed"]
    assert "applied:gradient_records" not in target.read_text()
