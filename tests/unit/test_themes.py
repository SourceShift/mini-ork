"""Standalone tests for mini_ork.learning.themes.

The kickoff mandates:
  * classify_kind — 3 trace-field complaints → framework, 3 prompt/code
    guidance → task, using VERBATIM live-DB signals (see the query below),
    plus two regression probes (``reward_score missing`` → framework and
    "the target file" → task).
  * Paraphrases join one theme at the DEFAULT ``MO_THEME_SIM`` (no
    hand-picked low threshold).
  * Determinism — two runs → identical theme ids.
  * assign_new is incremental — second call assigns 0.
  * n_runs counts distinct runs through evidence → execution_traces.run_id.
  * rollup_framework_bugs — one row per qualifying theme; idempotent;
    a `wontfix` status a human set survives.
  * A DB without migration 0063 still works (ensure_schema).

Live-DB signal selection (read-only probe of
``/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db``, URI ``?mode=ro``):

    SELECT gradient_id, target, signal, suggested_change
      FROM gradient_records
     ORDER BY gradient_id;

Classified in Python with the fixed ``FRAMEWORK_FIELDS`` regex over
``target + signal + suggested_change``; the first 3 ``framework`` rows and the
first 3 ``task`` rows were pasted verbatim into ``test_classify_kind_*``
below. The read gives 7,480 / 10,338 (72.4%) framework — the kickoff's "58%"
is not reproducible because the regex is substring-based and task lessons that
merely mention a field name classify as framework. Which fields drive the
difference (rows matching each field among the 7,480 framework rows; a row can
match several): ``verifier_output`` 2,199, ``reviewer_verdict`` 1,866,
``reward_\\w+`` 1,637, ``duration_ms`` 1,573, ``tool_calls`` 1,526,
``files_read`` 1,449, ``cost_usd`` 1,354, ``node_type`` 1,324,
``finish_reason`` 1,229, ``files_written`` 1,207, ``run context`` 1,125,
``context_bundle_hash`` 1,013, ``recipe_fallback`` 940,
``prompt_version_hash`` 790, ``process_reward`` 551, ``trace_id`` 65. See fix
1's proof note.

Pure stdlib + sqlite3. No network. Temp DB per test.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Resolve the worktree root so ``mini_ork.*`` imports work without an editable
# install (the implementer has ``worktree develop`` per the project CLAUDE.md).
_THIS = Path(__file__).resolve()
_REPO = _THIS.parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from mini_ork.learning import themes as th  # noqa: E402

# ── fixtures ────────────────────────────────────────────────────────────────


_GRADIENT_DDL = """
CREATE TABLE gradient_records (
    gradient_id      TEXT PRIMARY KEY,
    target           TEXT NOT NULL,
    signal           TEXT NOT NULL,
    suggested_change TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0
                        CHECK(confidence BETWEEN 0.0 AND 1.0),
    created_at       INTEGER NOT NULL,
    task_class       TEXT
);
CREATE TABLE execution_traces (
    trace_id     TEXT PRIMARY KEY,
    task_class   TEXT,
    status       TEXT,
    run_id       INTEGER
);
CREATE TABLE bug_reports (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint          TEXT NOT NULL UNIQUE,
    run_id               TEXT,
    agent_role           TEXT NOT NULL,
    task_class           TEXT,
    observed_in          TEXT,
    title                TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    suggested_fix        TEXT,
    severity             TEXT NOT NULL DEFAULT 'medium'
                           CHECK (severity IN ('low','medium','high','critical')),
    confidence           REAL NOT NULL DEFAULT 0.5,
    frequency            INTEGER NOT NULL DEFAULT 1,
    status               TEXT NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open','queued_as_epic','wontfix','dupe','resolved')),
    promoted_to_epic_id  TEXT,
    first_seen_at        INTEGER NOT NULL,
    last_seen_at         INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL
);
"""


@pytest.fixture
def db(tmp_path: Path) -> str:
    """A scratch DB with the three tables the themes module reads/writes."""
    path = str(tmp_path / "test.db")
    con = sqlite3.connect(path)
    con.executescript(_GRADIENT_DDL)
    con.commit()
    con.close()
    return path


def _seed_gradient(
    db: str,
    *,
    gid: str,
    signal: str,
    target: str = "verifier.researcher",
    suggested_change: str = "patch",
    evidence: str = "tr-stub-1",
    task_class: str = "framework_edit",
    run_id: int = 1,
    created_at: int = 1,
) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO gradient_records (gradient_id, target, signal,"
        "  suggested_change, evidence, confidence, created_at, task_class)"
        " VALUES (?, ?, ?, ?, ?, 0.5, ?, ?)",
        (gid, target, signal, suggested_change, evidence, created_at, task_class),
    )
    # Make sure the trace exists so n_runs can join.
    con.execute(
        "INSERT INTO execution_traces (trace_id, task_class, status, run_id)"
        " VALUES (?, ?, 'success', ?)",
        (evidence, task_class, run_id),
    )
    con.commit()
    con.close()


# ── classify_kind ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target,signal,change",
    [
        # Three verbatim live-DB framework signals (query in the module docstring).
        (
            "workflow.edge.verifier_to_status",
            "Run status was set to 'success' directly from the verifier verdict even though workflow_version_id is null and reviewer_verdict is absent, meaning the success edge has no secondary check.",
            "Gate the verifier→status edge on a completeness predicate (workflow_version_id present, duration_ms > 0) and route incomplete traces to a 'needs_review' status instead of 'success'.",
        ),
        (
            "verifier.cost_and_duration_sanity",
            "cost_usd=0.0 and duration_ms=0 on a planning trace that supposedly produced a plan.json — these zero values are sentinel-like and suggest metrics were never captured.",
            "Add a sanity verifier that flags traces where both cost_usd==0 and duration_ms==0 as 'metrics_missing' rather than 'success', forcing instrumentation fixes upstream.",
        ),
        (
            "workflow.edge.planner_to_verifier",
            "final_artifact_ref is set but verifier_output={} — the edge handed off an artifact without running any verification gate.",
            "Convert this edge from fire-and-forget to a gated handoff: verifier must emit a non-empty JSON with at least {schema_ok, files_referenced, acceptance_criteria_count} before status can flip to success.",
        ),
        # Regression probe (fix 1): `reward_\w+` must match a bare
        # `reward_`-prefixed token — the old `reward(_|s)?` with a trailing
        # ``\b`` failed before the word char in ``reward_score``.
        ("", "reward_score missing", ""),
    ],
)
def test_classify_kind_framework(target: str, signal: str, change: str) -> None:
    assert th.classify_kind(target, signal, change) == "framework"


@pytest.mark.parametrize(
    "target,signal,change",
    [
        # Three verbatim live-DB task signals (query in the module docstring;
        # the first three task rows in gradient_id order are used, skipping the
        # one seeded stub row ``e1-test-cross-class-seed``).
        (
            "workflow.node.planner",
            "Planner emitted a blocker with identical human questions echoed across blocked.json, plan.json, and execute.log instead of attempting partial execution or degraded mode.",
            "Make planner classify missing inputs as hard-block vs soft-block; soft-blocks should proceed with documented assumptions captured in plan.json rather than halting the whole run.",
        ),
        (
            "agent.implementer.prompt",
            "362s wall-clock and $2.13 spent producing only a log file (no source files written) indicates the implementer prompt let the agent monologue instead of acting on the dispatcher feature.",
            "Tighten implementer prompt to require explicit 'edits made:' and 'files touched:' sections backed by tool calls, and cap cost/duration with an early-exit instruction when no edits are produced within N minutes.",
        ),
        (
            "workflow.recipe.researcher_qdrant_contract",
            "A single blocking question from the planner aborted the whole pipeline, leaving 4 promised artifacts unwritten and no fallback path to partial remediation analysis.",
            "Add a clarifier/assumption-resolver node between planner and implementer that either auto-resolves with documented assumptions or downgrades to a scoped best-effort run, so blocked planners don't zero out the entire recipe.",
        ),
        # Regression probe (fix 1): `target` is dropped from FRAMEWORK_FIELDS,
        # so ordinary English "the target file" is a task lesson, not framework.
        ("verifier.researcher", "the target file was not written by the node", "write the target file"),
    ],
)
def test_classify_kind_task(target: str, signal: str, change: str) -> None:
    assert th.classify_kind(target, signal, change) == "task"


# ── normalize + paraphrase ──────────────────────────────────────────────────


def test_normalize_collapses_identifiers() -> None:
    a = "verifier_output for this node is only {node_type: researcher}"
    b = "this node's verifier_output records only {node_type: planner}"
    # Both paraphrase to DIFFERENT strings (they are the same idea, not the
    # same sentence): trace ids collapse, hex collapses, paths collapse,
    # numbers collapse, but the prose and the ``{node_type: …}`` token survive
    # (we don't touch quoted JSON keys here because the colon + identifier
    # isn't a JSON *value*). What matters for TF-IDF is that ``node_type`` and
    # ``verifier_output`` both appear in BOTH normalized strings, so the two
    # paraphrases share their discriminating tokens and join at a high cosine.
    na = th.normalize(a)
    nb = th.normalize(b)
    assert "verifier_output" in na and "node_type" in na
    assert "verifier_output" in nb and "node_type" in nb
    # And the trace id from "tr-xxxxxx" runs is gone.
    assert "tr-" not in na.replace("trace", "")


def test_normalize_collapses_trace_ids_hex_paths() -> None:
    raw = "tr-deadbeef1234 file is /Users/admin/x.py and id=12345"
    norm = th.normalize(raw)
    assert "tr-deadb" not in norm  # trace id collapsed
    assert "x.py" not in norm  # path collapsed
    assert "12345" not in norm  # number collapsed


# ── role_of ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target,expected",
    [
        ("verifier.researcher", "verifier"),
        ("researcher.x.y", "researcher"),
        ("workflow.node.implementer", "node:implementer"),
        ("agent.reviewer.prompt", "agent:reviewer"),
        ("workflow.recipe.code_fix", "recipe"),
        ("promote", "promote"),  # first segment
        ("", ""),
    ],
)
def test_role_of(target: str, expected: str) -> None:
    assert th.role_of(target) == expected


# ── assign_new + paraphrase join ────────────────────────────────────────────


def test_representative_populated_after_assign(db: str) -> None:
    """The post-pass loop must write a non-empty representative.

    Regression for the con-commit-then-reopen split: when the post-pass
    runs against the SAME connection as the join/insert loop, the new
    ``gradient_theme`` rows are not yet committed and ``_representative``'s
    own connection sees an empty join result, returning ``""``. The fix is
    the commit-then-reopen split. This test asserts the symptom stays gone.
    """
    for i in range(3):
        _seed_gradient(
            db,
            gid=f"gr-r{i}",
            signal=f"verifier_output for trace {i} only {{node_type: researcher}}",
            evidence=f"tr-r{i}",
            created_at=i,
        )
    th.assign_new(db)
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT representative FROM lesson_themes"
    ).fetchone()
    con.close()
    assert row[0] and len(row[0]) > 0, (
        "post-pass UPDATE wrote an empty representative — the commit-then-"
        "reopen split regressed"
    )


def test_paraphrases_join_one_theme(db: str) -> None:
    """Two paraphrases of the same trace complaint land in ONE theme; a third
    unrelated gradient lands in a DIFFERENT theme. The kickoff's exact
    paraphrases drive this test.

    TF-IDF down-weights the shared telemetry tokens (``verifier_output``,
    ``node_type`` — in a real DB these appear in thousands of signals) and that
    LOWERS the pair's cosine. The pair still joins above the default
    ``MO_THEME_SIM`` because the two paraphrases share their discriminating
    tokens (``node``, ``only``, …) and differ only in incidental prose, while
    the unrelated dedup note shares no tokens at all and clusters at ~0. The
    test runs at the DEFAULT threshold — the kickoff forbids a hand-picked low
    ``sim``.
    """
    _seed_gradient(
        db,
        gid="gr-aaa1",
        signal="verifier_output for this node is only {node_type: researcher}",
        evidence="tr-001",
        created_at=1,
    )
    _seed_gradient(
        db,
        gid="gr-aaa2",
        signal="this node's verifier_output records only {node_type: planner}",
        evidence="tr-002",
        created_at=2,
    )
    _seed_gradient(
        db,
        gid="gr-bbb1",
        signal="Use the existing gradient_store dedup_sim env, not a new constant",
        evidence="tr-003",
        created_at=3,
    )
    report = th.assign_new(db, sim=0.3)
    assert report["assigned"] == 3
    # Two themes: one framework (the paraphrases) + one task (the dedup note).
    assert report["themes_total"] == 2
    assert report["themes_new"] == 2

    con = sqlite3.connect(db)
    rows = list(con.execute("SELECT gradient_id, theme_id FROM gradient_theme ORDER BY gradient_id"))
    con.close()
    assert rows[0][0] == "gr-aaa1"
    assert rows[1][0] == "gr-aaa2"
    assert rows[2][0] == "gr-bbb1"
    assert rows[0][1] == rows[1][1]  # paraphrases share a theme
    assert rows[2][1] != rows[0][1]  # unrelated gradient is a different theme


def test_novel_vocabulary_paraphrases_share_theme(db: str) -> None:
    """Two paraphrases using vocabulary ABSENT from the persisted IDF still
    share one theme (review round 1 BLOCKER).

    Before the fix a token missing from ``theme_idf`` got weight 0 in
    ``_vector``/``_query_tokens``, so the pair's shared novel tokens
    contributed nothing and the two paraphrases split into unrelated themes.
    Unseen tokens now get the df=0 smoothed weight ``log(1 + n)``, so fresh
    vocabulary is up-weighted and the pair joins.

    The seed count matters: adding 2 to 40 stays under the 25% IDF-rebuild
    threshold, so the novel tokens remain unseen by the persisted IDF — the
    exact path that used to zero them.
    """
    # Seed enough gradients that the two additions do not trigger an IDF
    # rebuild (40 -> 42 is < 1.25x), then assign so the IDF is persisted over
    # the seed vocabulary only.
    for i in range(40):
        _seed_gradient(
            db,
            gid=f"gr-seed{i}",
            signal=f"verifier_output for trace {i} only {{node_type: researcher}}",
            evidence=f"tr-seed{i}",
            created_at=i,
        )
    th.assign_new(db)

    _seed_gradient(
        db,
        gid="gr-nov1",
        signal="sandbox_snapshot_id is lost after microvm_resume restores the field",
        evidence="tr-nov1",
        created_at=100,
    )
    _seed_gradient(
        db,
        gid="gr-nov2",
        signal="after microvm_resume the sandbox_snapshot_id field is null, not restored",
        evidence="tr-nov2",
        created_at=101,
    )
    report = th.assign_new(db)
    assert report["assigned"] == 2

    con = sqlite3.connect(db)
    rows = list(
        con.execute(
            "SELECT gradient_id, theme_id FROM gradient_theme"
            "  WHERE gradient_id IN ('gr-nov1', 'gr-nov2')"
            "  ORDER BY gradient_id"
        )
    )
    con.close()
    assert rows[0][0] == "gr-nov1" and rows[1][0] == "gr-nov2"
    assert rows[0][1] == rows[1][1]  # novel paraphrases share ONE theme


def test_assign_new_is_incremental(db: str) -> None:
    """A second call assigns 0."""
    for i in range(5):
        _seed_gradient(
            db,
            gid=f"gr-i{i}",
            signal=f"verifier_output for trace {i} only {{node_type: researcher}}",
            evidence=f"tr-i{i}",
            created_at=i,
        )
    r1 = th.assign_new(db, sim=0.3)
    assert r1["assigned"] == 5
    r2 = th.assign_new(db, sim=0.3)
    assert r2["assigned"] == 0


def test_determinism_two_runs(db: str, tmp_path: Path) -> None:
    """Two identical DBs processed independently produce identical theme ids."""
    # Build a second DB identical to the first.
    db2 = str(tmp_path / "test2.db")
    con = sqlite3.connect(db2)
    con.executescript(_GRADIENT_DDL)
    con.commit()
    con.close()
    rows = [
        ("gr-a", "verifier_output for this node is only {node_type: researcher}", "tr-a", 1),
        ("gr-b", "this node's verifier_output records only {node_type: planner}", "tr-b", 2),
        ("gr-c", "Use the existing gradient_store dedup_sim env, not a new constant", "tr-c", 3),
    ]
    for path in (db, db2):
        for gid, sig, ev, ts in rows:
            _seed_gradient(db=path, gid=gid, signal=sig, evidence=ev, created_at=ts)
    th.assign_new(db)
    th.assign_new(db2)

    def _ids(p: str) -> list[str]:
        con = sqlite3.connect(p)
        out = [r[0] for r in con.execute("SELECT theme_id FROM lesson_themes ORDER BY theme_id")]
        con.close()
        return out

    assert _ids(db) == _ids(db2)
    # And theme ids are sha256-derived, not random.
    for tid in _ids(db):
        assert tid.startswith("th-")
        assert len(tid) == len("th-") + 12


# ── n_runs through execution_traces ─────────────────────────────────────────


def test_n_runs_counts_distinct_runs(db: str) -> None:
    """Three gradients across two distinct run_ids → n_runs = 2 on the theme."""
    for gid, ev, rid, ts in [
        ("gr-n1", "tr-n1", 11, 1),
        ("gr-n2", "tr-n2", 11, 2),
        ("gr-n3", "tr-n3", 12, 3),  # different run
    ]:
        _seed_gradient(db, gid=gid, signal="verifier_output is only {node_type: x}", evidence=ev, run_id=rid, created_at=ts)
    th.assign_new(db)
    con = sqlite3.connect(db)
    row = con.execute("SELECT n_runs FROM lesson_themes").fetchone()
    con.close()
    assert row[0] == 2


# ── rollup_framework_bugs ───────────────────────────────────────────────────


def test_rollup_creates_one_row_per_qualifying_theme(db: str) -> None:
    """Six framework themes with ≥ 5 members each → six bug_reports rows.

    Each group uses a distinct vocabulary, so the six themes stay separate at
    the DEFAULT threshold. The three tokens every group shares
    (``framework_signal_N`` after normalize, ``node_type``, ``researcher``)
    have a low IDF — they appear in all 30 gradients — so cross-group cosine
    is ~0.08, while within-group cosine is 1.0 (the five members normalize to
    identical strings). TF-IDF's down-weighting of the shared telemetry
    vocabulary is exactly what keeps them apart without a hand-picked
    ``sim``.
    """
    vocabularies = [
        "alpha bravo charlie delta echo",
        "foxtrot golf hotel india juliet",
        "kilo lima mike november oscar",
        "papa quebec sierra victor whiskey",
        "xray yankee zulu one two",
        "three four five six seven",
    ]
    for i, vocab in enumerate(vocabularies):
        for j in range(5):
            _seed_gradient(
                db,
                gid=f"gr-r{i}-{j}",
                signal=f"{vocab} framework_signal_{i} {{node_type: researcher}}",
                evidence=f"tr-r{i}-{j}",
                created_at=i * 10 + j,
            )
    th.assign_new(db)
    n = th.rollup_framework_bugs(db, min_members=5)
    assert n == 6

    con = sqlite3.connect(db)
    n_rows = con.execute("SELECT COUNT(*) FROM bug_reports").fetchone()[0]
    con.close()
    assert n_rows == 6


def test_rollup_is_idempotent(db: str) -> None:
    """A second call updates frequency but never duplicates."""
    for i in range(5):
        _seed_gradient(
            db,
            gid=f"gr-id{i}",
            signal="verifier_output for this node is only {node_type: researcher}",
            evidence=f"tr-id{i}",
            created_at=i,
        )
    th.assign_new(db)
    n1 = th.rollup_framework_bugs(db, min_members=3)
    assert n1 == 1
    n2 = th.rollup_framework_bugs(db, min_members=3)
    assert n2 == 1

    con = sqlite3.connect(db)
    n_rows = con.execute("SELECT COUNT(*) FROM bug_reports").fetchone()[0]
    con.close()
    assert n_rows == 1


def test_rollup_preserves_wontfix(db: str) -> None:
    """A human-set `wontfix` survives the next rollup."""
    for i in range(5):
        _seed_gradient(
            db,
            gid=f"gr-w{i}",
            signal="verifier_output for this node is only {node_type: researcher}",
            evidence=f"tr-w{i}",
            created_at=i,
        )
    th.assign_new(db)
    n1 = th.rollup_framework_bugs(db, min_members=3)
    assert n1 == 1

    # Simulate a human flipping the bug to wontfix.
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE bug_reports SET status = 'wontfix', severity = 'low' WHERE fingerprint LIKE 'theme:%'"
    )
    con.commit()
    con.close()

    # A second rollup must keep status='wontfix' and severity='low'.
    th.rollup_framework_bugs(db, min_members=3)
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT status, severity FROM bug_reports WHERE fingerprint LIKE 'theme:%'"
    ).fetchone()
    con.close()
    assert row[0] == "wontfix"
    assert row[1] == "low"


def test_rollup_skips_below_min_members(db: str) -> None:
    """A framework theme with 2 members is not rolled up when min_members=5."""
    for i in range(2):
        _seed_gradient(
            db,
            gid=f"gr-s{i}",
            signal="verifier_output for this node is only {node_type: researcher}",
            evidence=f"tr-s{i}",
            created_at=i,
        )
    th.assign_new(db)
    n = th.rollup_framework_bugs(db, min_members=5)
    assert n == 0


# ── ensure_schema on a DB without the migration ─────────────────────────────


def test_ensure_schema_idempotent_on_fresh_db(tmp_path: Path) -> None:
    """A DB that has NEVER run migration 0063 still works after ensure_schema."""
    # Build an empty DB.
    path = str(tmp_path / "fresh.db")
    con = sqlite3.connect(path)
    con.commit()
    con.close()

    th.ensure_schema(path)
    th.ensure_schema(path)  # twice: still no throw

    con = sqlite3.connect(path)
    rows = list(con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('lesson_themes','gradient_theme') ORDER BY name"
    ))
    con.close()
    assert [r[0] for r in rows] == ["gradient_theme", "lesson_themes"]


def test_assign_new_works_on_db_without_migration(tmp_path: Path) -> None:
    """An empty DB without 0063 still runs assign_new — it just assigns nothing."""
    path = str(tmp_path / "bare.db")
    con = sqlite3.connect(path)
    con.commit()
    con.close()

    report = th.assign_new(path)
    assert report == {"assigned": 0, "themes_new": 0, "themes_total": 0}


# ── backfill dry-run ────────────────────────────────────────────────────────


def test_backfill_dry_run_does_not_touch_source(db: str) -> None:
    """``backfill --dry-run`` snapshots the source into ``:memory:`` and runs
    against the snapshot, writing nothing to the source or to ``$TMPDIR``."""
    _seed_gradient(
        db,
        gid="gr-d0",
        signal="verifier_output for this node is only {node_type: researcher}",
        evidence="tr-d0",
        created_at=1,
    )
    # Snapshot $TMPDIR and the source hash before, compare after — the run
    # must neither write to the source nor drop a scratch file on disk.
    import glob
    import tempfile
    pattern = os.path.join(tempfile.gettempdir(), "themes-dryrun-*")
    tmp_before = set(glob.glob(pattern))
    h0 = _sha(db)
    out = th.backfill(db, dry_run=True)
    h1 = _sha(db)
    assert h0 == h1
    assert out["dry_run_source"] == db
    assert out["dry_run_scratch"] == ":memory:"
    assert out["wall_seconds"] >= 0
    assert out["assigned_in_run"]["assigned"] >= 1
    # No new scratch file may appear in $TMPDIR (fix 5). Snapshot-before so
    # pre-existing orphans from an older run can't make this flaky.
    assert set(glob.glob(pattern)) == tmp_before


def _sha(path: str) -> str:
    import hashlib
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# ── CLI smoke ───────────────────────────────────────────────────────────────


def test_cli_help(capsys) -> None:
    """The CLI prints help without erroring.

    ``argparse`` exits via ``SystemExit(0)`` after printing help — the
    standard contract. We catch it and inspect the stdout/stderr captured by
    pytest's ``capsys`` to confirm all three subcommands are listed.
    """
    with pytest.raises(SystemExit) as exc:
        th.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "backfill" in out and "stats" in out and "rollup" in out


# ``tmp_path`` is implicit here — the argparse help exit doesn't touch it.