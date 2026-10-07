"""Tests for the Memory tab P7b rewrite (kickoff ``learn-memory-tab``).

The tab is driven by three real data sources:

- ``user_preference_memory`` + ``lesson_injections`` for Preferences & constraints.
- ``execution_traces`` for Lane fit by task class.
- ``semantic_memory`` + ``semantic_memory_uses`` for Memories to review.

Those tables are created lazily by the runtime (migrations + ``ledger.ensure_schema``);
the test fixtures build only what the migration does not.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page, learn
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def _ledger_table(conn: sqlite3.Connection) -> None:
    """lesson_injections is bootstrapped lazily — make it exist before we INSERT."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lesson_injections (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       TEXT    NOT NULL,
            node_id      TEXT    NOT NULL,
            node_type    TEXT,
            lane         TEXT,
            task_class   TEXT,
            attempt      INTEGER,
            source_kind  TEXT    NOT NULL,
            source_id    TEXT    NOT NULL,
            held_out     INTEGER NOT NULL DEFAULT 0,
            ts           INTEGER NOT NULL
        );
        """
    )


def _memory_uses_table(conn: sqlite3.Connection) -> None:
    """semantic_memory_uses is bootstrapped lazily — make it exist before INSERT."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_memory_uses (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id    INTEGER NOT NULL,
            scope        TEXT    NOT NULL,
            run_id       TEXT    NOT NULL DEFAULT '',
            task_class   TEXT    NOT NULL DEFAULT '',
            lane         TEXT    NOT NULL DEFAULT '',
            node_id      TEXT    NOT NULL DEFAULT '',
            retrieved_at REAL    NOT NULL,
            outcome      TEXT    NOT NULL DEFAULT 'pending'
        );
        """
    )


def _ensure_semantic_memory_cols(conn: sqlite3.Connection) -> None:
    """Add the lazily-added semantic_memory columns the review table reads."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_memory)")}
    for col, decl in (("uses", "INTEGER NOT NULL DEFAULT 0"),
                      ("wins", "INTEGER NOT NULL DEFAULT 0"),
                      ("retired_at", "REAL NOT NULL DEFAULT 0"),
                      ("retire_reason", "TEXT NOT NULL DEFAULT ''"),
                      ("retire_evidence", "TEXT NOT NULL DEFAULT ''")):
        if col not in cols:
            conn.execute(f"ALTER TABLE semantic_memory ADD COLUMN {col} {decl}")


def _add_uses(con: sqlite3.Connection, memory_id: int, scope: str,
              outcome: str, count: int) -> None:
    """Insert ``count`` resolved/pending ledger rows for one (memory, scope)."""
    for _ in range(count):
        con.execute(
            "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
            "VALUES (?, ?, 'r', ?, ?)",
            (memory_id, scope, int(time.time()), outcome),
        )


def test_preferences_db_pref_shows_injection_count(home: Path) -> None:
    """A DB pref with 2 injections in the last 7 days reads 'given to 2 node(s)';
    a 3rd injection outside the window is ignored."""
    now = int(time.time())
    db_path = str(home / "state.db")
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO user_preference_memory "
            "(user_id, preference_key, preference_value, scope, scope_target, set_at) "
            "VALUES ('default', 'tone', 'Keep summaries short', 'global', '', '2026-10-01T00:00:00.000Z')"
        )
        _ledger_table(con)
        # Three injections — node_id varies so the unique key dedupes correctly:
        # 2 within 7 days, 1 older than 30 days (windowed out).
        for offset_s, node in ((1, "n1"), (2, "n2"), (30 * 86400, "n3")):
            con.execute(
                "INSERT INTO lesson_injections "
                "(run_id, node_id, node_type, lane, task_class, attempt, source_kind, source_id, held_out, ts) "
                "VALUES ('r', ?, 'implementer', 'glm', 'code_fix', 1, 'preference', "
                "'pref:global::tone', 0, ?)",
                (node, now - offset_s),
            )
        con.commit()
    finally:
        con.close()

    page = learn.build(home, "memory", {})
    prefs = _section(page, "Preferences & constraints")
    assert prefs["type"] == "list"
    item = prefs["items"][0]
    assert "given to 2 node(s) in 7 days" in item["sub"]
    assert "global" in item["sub"]
    # Remove button is wired to the right CLI invocation.
    remove = next(a for a in item["acts"] if a["label"] == "Remove")
    assert remove["kind"] == "ghost"
    cli = remove["do"]["cli"]
    assert cli[:2] == ["prefs", "rm"]
    assert cli[2] == "tone"
    assert "--scope" in cli and "global" in cli
    assert "--target" in cli and "" in cli


def test_preferences_file_pref_has_open_not_remove(home: Path, monkeypatch) -> None:
    """A legacy file-sourced pref surfaces an Open button and never a Remove."""
    config_dir = home / "config"
    config_dir.mkdir()
    upath = config_dir / "user_preferences.json"
    upath.write_text(json.dumps({"tone": "Keep summaries short"}), encoding="utf-8")
    # list_prefs() reads user_preferences.json from $MINI_ORK_HOME/config/, not
    # from the home/ tree. Point it at the test home so this stays isolated.
    monkeypatch.setenv("MINI_ORK_HOME", str(home))

    page = learn.build(home, "memory", {})
    prefs = _section(page, "Preferences & constraints")
    item = next(i for i in prefs["items"] if i["t"] == "Keep summaries short")
    labels = [a["label"] for a in item["acts"]]
    assert labels == ["Open"]
    assert "not given to any node in 7 days" in item["sub"]
    assert "from " in item["sub"] and str(upath) in item["sub"]


def test_preferences_empty_state(home: Path) -> None:
    page = learn.build(home, "memory", {})
    prefs = _section(page, "Preferences & constraints")
    assert prefs["items"][0]["t"] == "No preferences yet"
    assert "mini-ork prefs set tone" in prefs["items"][0]["sub"]
    # Section-level actions still expose the Add button.
    assert any(a["label"] == "Add preference" for a in prefs["actions"])


def _seed_traces(con: sqlite3.Connection, lane: str, role: str, cls: str,
                 n_success: int, n_failure: int, *,
                 cost_usd: float = 0.05, validity: str = "valid",
                 days_ago: float = 0.0) -> None:
    """Insert ``n_success`` success + ``n_failure`` failure execution_traces rows
    for one (lane, role, class) group, stamped ``days_ago`` days before now.

    ``created_at`` is written in the same ``%Y-%m-%dT%H:%M:%S.%fZ`` shape the
    runtime uses (``db/migrations/0014``), so the 28-day window compare in
    ``_lane_fit`` sees it the same way the live DB does.
    """
    created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")
    vo = json.dumps({"node_type": role})
    seq = 0
    for status, n in (("success", n_success), ("failure", n_failure)):
        for _ in range(n):
            con.execute(
                "INSERT INTO execution_traces "
                "(trace_id, agent_version_id, task_class, verifier_output, cost_usd, "
                "status, created_at, validity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"tr-{role}-{lane}-{seq}", lane, cls, vo, cost_usd, status, created, validity),
            )
            seq += 1


def test_lane_fit_hides_rows_below_three_runs(home: Path) -> None:
    """Lanes with fewer than 3 runs are filtered out — kickoff floor."""
    con = sqlite3.connect(home / "state.db")
    _seed_traces(con, "v1", "implementer", "code_fix", 9, 1)  # 10 runs, 90% — visible
    _seed_traces(con, "v2", "implementer", "code_fix", 2, 0)  # 2 runs → hidden
    _seed_traces(con, "v3", "implementer", "code_fix", 1, 3)  # 4 runs, 25% — visible
    con.commit()
    con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Lane fit by task class")
    lanes_in_rows = [r["cells"][2]["t"] for r in table["rows"]]
    # v2 has only 2 runs → must NOT appear.
    assert not any("v2" in t for t in lanes_in_rows)
    assert any("v1" in t for t in lanes_in_rows)
    assert any("v3" in t for t in lanes_in_rows)


def test_lane_fit_star_only_on_best_lane_with_at_least_five_runs(home: Path) -> None:
    """★ marks the lane with the highest pass rate AND runs >= 5."""
    con = sqlite3.connect(home / "state.db")
    # code_fix: v1 9/10 = 90% (★), v2 4/5 = 80% (eligible but lower), v3 3/3 = 100% (no ★ — runs<5).
    _seed_traces(con, "v1", "implementer", "code_fix", 9, 1)
    _seed_traces(con, "v2", "implementer", "code_fix", 4, 1)
    _seed_traces(con, "v3", "implementer", "code_fix", 3, 0)
    # review: v4 8/9 = 89% → eligible and best → ★
    _seed_traces(con, "v4", "implementer", "review", 8, 1)
    con.commit()
    con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Lane fit by task class")
    # Rows are emitted class-first: the first cell carries the class name on the
    # first row of each class group, and an empty string for the rest. Group
    # by walking in sequence.
    by_class: dict[str, list[dict]] = {}
    current = ""
    for r in table["rows"]:
        cls_cell = r["cells"][0]["t"]
        if cls_cell:
            current = cls_cell
            by_class.setdefault(current, []).append(r)
        else:
            by_class.setdefault(current, []).append(r)
    # code_fix: only v1 should carry ★
    cf = by_class["code_fix"]
    stars = [r for r in cf if "★" in r["cells"][2]["t"]]
    assert len(stars) == 1
    assert "v1" in stars[0]["cells"][2]["t"]
    # review: v4 carries ★
    assert any("★" in r["cells"][2]["t"] and "v4" in r["cells"][2]["t"] for r in by_class["review"])


def test_lane_fit_pass_rate_colours(home: Path) -> None:
    """green ≥ 70%, red < 40%, yellow in between — checked on the rate cell."""
    con = sqlite3.connect(home / "state.db")
    _seed_traces(con, "va", "implementer", "g_class", 9, 1)  # 90% → green
    _seed_traces(con, "vb", "implementer", "g_class", 2, 3)  # 40% → yellow (boundary)
    _seed_traces(con, "vc", "implementer", "g_class", 1, 4)  # 20% → red
    con.commit()
    con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Lane fit by task class")
    # Group rows by class (first cell carries the class; later rows in the
    # same group leave it blank).
    rows: list[dict] = []
    current = ""
    for r in table["rows"]:
        cls_cell = r["cells"][0]["t"]
        if cls_cell:
            current = cls_cell
        if current == "g_class":
            rows.append(r)
    colours = {r["cells"][2]["t"].replace(" ★", ""): r["cells"][4]["c"] for r in rows}
    assert colours["va"] == "green"
    assert colours["vb"] == "yellow"
    assert colours["vc"] == "red"


def test_lane_fit_excludes_infra_vacuous_and_stale(home: Path) -> None:
    """Exact cells for the kickoff recipe, and infra/vacuous/stale rows excluded.

    The infra_failed and vacuous rows are seeded ON the 'good'/'mid' lanes (same
    role and class), not parked on their own <3-run lanes — so letting either
    through its filter would change the asserted 90%/40% and fail the test.
    """
    con = sqlite3.connect(home / "state.db")
    _seed_traces(con, "good", "researcher", "verified_artifact", 9, 1, cost_usd=0.09)  # 90% → ★
    _seed_traces(con, "mid", "researcher", "verified_artifact", 2, 3, cost_usd=0.20)   # 40%
    # infra_failed ON the good lane — if the validity filter dropped it, good
    # becomes 9/11 ≈ 82%, not 90%. (Manual INSERT: _seed_traces would reuse
    # 'tr-researcher-good-0' and trip the trace_id UNIQUE constraint.)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    con.execute(
        "INSERT INTO execution_traces "
        "(trace_id, agent_version_id, task_class, verifier_output, cost_usd, "
        "status, created_at, validity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("tr-researcher-good-infra-0", "good", "verified_artifact",
         json.dumps({"node_type": "researcher"}), 0.09, "failure", created, "infra_failed"),
    )
    # vacuous ON the mid lane — if the status filter dropped it, mid becomes
    # 2/6 ≈ 33%, not 40%.
    con.execute(
        "INSERT INTO execution_traces "
        "(trace_id, agent_version_id, task_class, verifier_output, cost_usd, "
        "status, created_at, validity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("tr-researcher-mid-vac-0", "mid", "verified_artifact",
         json.dumps({"node_type": "researcher"}), 0.20, "vacuous", created, "valid"),
    )
    _seed_traces(con, "old", "researcher", "verified_artifact", 5, 0,
                 days_ago=30.0)                                                 # stale → excluded
    con.commit()
    con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Lane fit by task class")
    assert len(table["rows"]) == 2
    good, mid = table["rows"]
    # Assert the exact cells: class | role | lane | runs | pass rate | cost / run.
    assert [c["t"] for c in good["cells"]] == \
        ["verified_artifact", "researcher", "good ★", "10", "90%", "$0.09"]
    assert [c["t"] for c in mid["cells"]] == \
        ["", "researcher", "mid", "5", "40%", "$0.20"]
    assert good["cells"][4]["c"] == "green"
    assert mid["cells"][4]["c"] == "yellow"
    # The excluded rows must not surface as their own lanes.
    lane_texts = [r["cells"][2]["t"] for r in table["rows"]]
    assert not any("infra" in t or "vac" in t or "old" in t for t in lane_texts)


def test_lane_fit_star_one_role_per_lane(home: Path) -> None:
    """One lane with two roles in the same class: only the best (role, lane) row
    gets the ★ — the star is keyed on the row, not the lane name alone."""
    con = sqlite3.connect(home / "state.db")
    # Same lane 'shared', two roles in one class. researcher 9/10 = 90% (★);
    # implementer 4/5 = 80% (eligible but lower). Both ≥ 5 runs, same lane.
    _seed_traces(con, "shared", "researcher", "code_fix", 9, 1)
    _seed_traces(con, "shared", "implementer", "code_fix", 4, 1)
    con.commit()
    con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Lane fit by task class")
    by_role = {r["cells"][1]["t"]: r for r in table["rows"]}
    assert set(by_role) == {"researcher", "implementer"}
    stars = [r for r in table["rows"] if "★" in r["cells"][2]["t"]]
    assert len(stars) == 1
    assert stars[0]["cells"][1]["t"] == "researcher"
    # Same lane, other role: no star.
    assert "★" not in by_role["implementer"]["cells"][2]["t"]


def test_lane_fit_role_from_trace_id_prefix(home: Path) -> None:
    """Role falls back to the ``tr-<type>-`` prefix; a trace_id with no second
    dash renders '?', never the stray '-' the old substr produced."""
    con = sqlite3.connect(home / "state.db")
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # verifier_output has no node_type → role must come from the trace_id prefix.
    rows = [
        ("tr-verifier-x-0", "v1", "cls", "{}", "success"),
        ("tr-verifier-x-1", "v1", "cls", "{}", "success"),
        ("tr-verifier-x-2", "v1", "cls", "{}", "success"),
        # No second dash → '?' role (not '-'). Same class so both lanes surface.
        ("tr-3efc7149cd804818", "v2", "cls", "{}", "success"),
        ("tr-3efc7149cd804819", "v2", "cls", "{}", "success"),
        ("tr-3efc7149cd804820", "v2", "cls", "{}", "success"),
    ]
    for tid, lane, cls, vo, status in rows:
        con.execute(
            "INSERT INTO execution_traces "
            "(trace_id, agent_version_id, task_class, verifier_output, cost_usd, "
            "status, created_at, validity) VALUES (?, ?, ?, ?, 0.05, ?, ?, 'valid')",
            (tid, lane, cls, vo, status, created),
        )
    con.commit()
    con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Lane fit by task class")
    roles = {r["cells"][1]["t"] for r in table["rows"]}
    assert roles == {"verifier", "?"}


def test_memories_to_review_lists_below_baseline_with_retire(home: Path) -> None:
    """A 60% memory (10 resolved + 3 pending) in a 75%-baseline scope appears with
    n=10 and Δ -15pt; pending uses are ignored."""
    db_path = str(home / "state.db")
    con = sqlite3.connect(db_path)
    try:
        # semantic_memory table needs the lazily-added columns for _memory_counts_kv.
        cols = {r[1] for r in con.execute("PRAGMA table_info(semantic_memory)")}
        for col, decl in (("uses", "INTEGER NOT NULL DEFAULT 0"),
                          ("wins", "INTEGER NOT NULL DEFAULT 0"),
                          ("retired_at", "REAL NOT NULL DEFAULT 0"),
                          ("retire_reason", "TEXT NOT NULL DEFAULT ''"),
                          ("retire_evidence", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:
                con.execute(f"ALTER TABLE semantic_memory ADD COLUMN {col} {decl}")
        # Two memories in scope 'code_fix': m1 (60% / listed), m2 (80% / NOT listed).
        con.execute(
            "INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
            "VALUES ('code_fix', 'm1: do not write tests inline', x'00', ?, 10, 6, 0)",
            (int(time.time()),),
        )
        con.execute(
            "INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
            "VALUES ('code_fix', 'm2: keep responses terse', x'00', ?, 10, 8, 0)",
            (int(time.time()),),
        )
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS semantic_memory_uses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id    INTEGER NOT NULL,
                scope        TEXT    NOT NULL,
                run_id       TEXT    NOT NULL DEFAULT '',
                task_class   TEXT    NOT NULL DEFAULT '',
                lane         TEXT    NOT NULL DEFAULT '',
                node_id      TEXT    NOT NULL DEFAULT '',
                retrieved_at REAL    NOT NULL,
                outcome      TEXT    NOT NULL DEFAULT 'pending'
            );
            """
        )
        # Scope baseline must read 75%: 21 wins / 28 entries = 75%. Recipe below:
        # m1: 6w/4l, m2: 8w/2l, baseline (mem 999): 7w/1l  → 21w / 28 = 75%.
        # m1 = 60%, Δ -15pt → listed.  m2 = 80%, Δ +5pt → hidden.
        for _ in range(6):
            con.execute(
                "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
                "VALUES (1, 'code_fix', 'r', ?, 'win')",
                (int(time.time()),),
            )
        for _ in range(4):
            con.execute(
                "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
                "VALUES (1, 'code_fix', 'r', ?, 'loss')",
                (int(time.time()),),
            )
        # 3 pending uses for m1 — pending must NOT count toward n (resolved only).
        for _ in range(3):
            con.execute(
                "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
                "VALUES (1, 'code_fix', 'r', ?, 'pending')",
                (int(time.time()),),
            )
        for _ in range(8):
            con.execute(
                "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
                "VALUES (2, 'code_fix', 'r', ?, 'win')",
                (int(time.time()),),
            )
        for _ in range(2):
            con.execute(
                "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
                "VALUES (2, 'code_fix', 'r', ?, 'loss')",
                (int(time.time()),),
            )
        for _ in range(7):
            con.execute(
                "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
                "VALUES (999, 'code_fix', 'r', ?, 'win')",
                (int(time.time()),),
            )
        con.execute(
            "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
            "VALUES (999, 'code_fix', 'r', ?, 'loss')",
            (int(time.time()),),
        )
        con.commit()
    finally:
        con.close()

    page = learn.build(home, "memory", {})
    sections_by_titles = {s["title"]: s for s in page["sections"]}
    # Kv line above the table.
    counts = sections_by_titles["Memories to review · counts"]
    assert any(it["k"] == "Baseline" and it["v"].endswith("%") for it in counts["items"])
    # The table is named "Memories to review".
    table = sections_by_titles["Memories to review"]
    assert table["type"] == "table"
    flagged_rows = table["rows"]
    # Find the m1 row (text begins with "m1:") and assert Δ and action.
    m1_row = next(r for r in flagged_rows if r["cells"][0]["t"].startswith("m1"))
    assert m1_row["cells"][2]["t"] == "60%"
    assert m1_row["cells"][3]["t"] == "75%"
    assert m1_row["cells"][4]["t"] == "-15pt"
    assert m1_row["cells"][4]["c"] == "red"
    # n counts resolved uses only: 6 wins + 4 losses = 10, the 3 pending are ignored.
    assert m1_row["cells"][5]["t"] == "10"
    # m2 must NOT be listed (Δ = +5pt > -5).
    assert not any(r["cells"][0]["t"].startswith("m2") for r in flagged_rows)
    # Retire action: cli = memory-lifecycle --retire 1 --reason <text>.
    retire = m1_row["do"]
    assert retire["cli"][:3] == ["memory-lifecycle", "--retire", "1"]
    assert "--reason" in retire["cli"]


def test_memories_to_review_retired_row_shows_reactivate(home: Path) -> None:
    """A retired memory surfaces a Reactivate action (no Retire, no --reason)."""
    db_path = str(home / "state.db")
    con = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(semantic_memory)")}
        for col, decl in (("uses", "INTEGER NOT NULL DEFAULT 0"),
                          ("wins", "INTEGER NOT NULL DEFAULT 0"),
                          ("retired_at", "REAL NOT NULL DEFAULT 0"),
                          ("retire_reason", "TEXT NOT NULL DEFAULT ''"),
                          ("retire_evidence", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:
                con.execute(f"ALTER TABLE semantic_memory ADD COLUMN {col} {decl}")
        # A retired memory (retired_at != 0) under the candidate rules: utility > RETIRE_EXIT_UTILITY.
        # wins/uses ≥ 0.45 - ε with uses ≥ 4 → e.g. wins=4 uses=4 → utility = (4+1)/(4+2) = 0.83.
        con.execute(
            "INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at, "
            "retire_reason) VALUES ('code_fix', 'retired m: useful pattern', x'00', ?, 4, 4, ?, 'old')",
            (int(time.time()), int(time.time())),
        )
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS semantic_memory_uses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id    INTEGER NOT NULL,
                scope        TEXT    NOT NULL,
                run_id       TEXT    NOT NULL DEFAULT '',
                task_class   TEXT    NOT NULL DEFAULT '',
                lane         TEXT    NOT NULL DEFAULT '',
                node_id      TEXT    NOT NULL DEFAULT '',
                retrieved_at REAL    NOT NULL,
                outcome      TEXT    NOT NULL DEFAULT 'pending'
            );
            """
        )
        # 4 wins, 1 loss = 80% baseline in this scope.
        for _ in range(4):
            con.execute(
                "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
                "VALUES (1, 'code_fix', 'r', ?, 'win')",
                (int(time.time()),),
            )
        con.execute(
            "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
            "VALUES (1, 'code_fix', 'r', ?, 'loss')",
            (int(time.time()),),
        )
        # Bring the baseline down so the row is also delta-flagged (utility > baseline → reactivate).
        for _ in range(2):
            con.execute(
                "INSERT INTO semantic_memory_uses (memory_id, scope, run_id, retrieved_at, outcome) "
                "VALUES (999, 'code_fix', 'r', ?, 'loss')",
                (int(time.time()),),
            )
        con.commit()
    finally:
        con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Memories to review")
    rows = [r for r in table["rows"] if r["cells"][0]["t"].startswith("retired m")]
    assert rows, table["rows"]
    reactivate = rows[0]["do"]
    assert reactivate["cli"] == ["memory-lifecycle", "--reactivate", "1"]
    assert rows[0]["cells"][6]["t"] == "retired"


def test_memories_to_review_empty_state(home: Path) -> None:
    """No semantic_memory_uses → 'Nothing to review' empty-state row, no errors."""
    page = learn.build(home, "memory", {})
    table = _section(page, "Memories to review")
    assert "Nothing to review" in table["rows"][0]["cells"][0]["t"]
    assert page["errors"] == {}


def test_preferences_injection_window_frozen_now(home: Path) -> None:
    """Frozen ``now`` pins the 7-day window: one injection inside, one outside."""
    frozen = 2_000_000_000
    db_path = str(home / "state.db")
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO user_preference_memory "
            "(user_id, preference_key, preference_value, scope, scope_target, set_at) "
            "VALUES ('default', 'tone', 'Keep summaries short', 'global', '', '2026-10-01T00:00:00.000Z')"
        )
        _ledger_table(con)
        # 1 day ago (inside the 7-day window) vs 8 days ago (outside).
        for node, offset_days in (("in", 1), ("out", 8)):
            con.execute(
                "INSERT INTO lesson_injections "
                "(run_id, node_id, node_type, lane, task_class, attempt, source_kind, source_id, held_out, ts) "
                "VALUES ('r', ?, 'implementer', 'glm', 'code_fix', 1, 'preference', "
                "'pref:global::tone', 0, ?)",
                (node, frozen - offset_days * 86400),
            )
        con.commit()
    finally:
        con.close()

    sections = learn.memory.sections(home, {}, {}, now=frozen)
    prefs = next(s for s in sections if s["title"] == "Preferences & constraints")
    item = prefs["items"][0]
    assert "given to 1 node(s) in 7 days" in item["sub"]


def test_memories_to_review_zero_delta_muted(home: Path) -> None:
    """A candidate whose resolved rate equals its baseline renders a muted +0pt Δ."""
    db_path = str(home / "state.db")
    con = sqlite3.connect(db_path)
    try:
        _ensure_semantic_memory_cols(con)
        # Active candidate: uses=4, wins=0 → utility (0+1)/(4+2) < enter → retire.
        con.execute(
            "INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
            "VALUES ('code_fix', 'c0: neutral memory', x'00', ?, 4, 0, 0)",
            (int(time.time()),),
        )
        _memory_uses_table(con)
        # mem 1 resolved 60% (3w/2l); baseline 60% (mem 999 3w/2l) → Δ = 0.
        _add_uses(con, 1, "code_fix", "win", 3)
        _add_uses(con, 1, "code_fix", "loss", 2)
        _add_uses(con, 999, "code_fix", "win", 3)
        _add_uses(con, 999, "code_fix", "loss", 2)
        con.commit()
    finally:
        con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Memories to review")
    row = next(r for r in table["rows"] if r["cells"][0]["t"].startswith("c0"))
    assert row["cells"][2]["t"] == "60%"
    assert row["cells"][4]["t"] == "+0pt"
    assert row["cells"][4]["c"] == "muted"


def test_memories_to_review_candidate_shows_resolved_rate(home: Path) -> None:
    """A candidate shows its resolved ledger win rate, not smoothed utility."""
    db_path = str(home / "state.db")
    con = sqlite3.connect(db_path)
    try:
        _ensure_semantic_memory_cols(con)
        # Active candidate: uses=4, wins=0 → utility = 1/6 ≈ 17% if shown.
        con.execute(
            "INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
            "VALUES ('code_fix', 'c1: resolved 80', x'00', ?, 4, 0, 0)",
            (int(time.time()),),
        )
        _memory_uses_table(con)
        # Resolved ledger: mem 1 = 8w/2l (80%); baseline mem 999 1w/1l → 9/12 = 75%.
        _add_uses(con, 1, "code_fix", "win", 8)
        _add_uses(con, 1, "code_fix", "loss", 2)
        _add_uses(con, 999, "code_fix", "win", 1)
        _add_uses(con, 999, "code_fix", "loss", 1)
        con.commit()
    finally:
        con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Memories to review")
    row = next(r for r in table["rows"] if r["cells"][0]["t"].startswith("c1"))
    assert row["cells"][2]["t"] == "80%"   # resolved, not the 17% utility
    assert row["cells"][5]["t"] == "10"    # 8+2 resolved, not semantic_memory.uses=4


def test_memories_to_review_positive_delta_rounds_to_green(home: Path) -> None:
    """Raw Δ ≈ +4.8pt renders '+5pt' green — the text and colour read the same
    rounded value instead of colouring the raw float below the +5 threshold."""
    db_path = str(home / "state.db")
    con = sqlite3.connect(db_path)
    try:
        _ensure_semantic_memory_cols(con)
        # Active candidate: uses=4, wins=0 → utility 1/6 < enter → retire candidate.
        con.execute(
            "INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
            "VALUES ('code_fix', 'g: rounds up to green', x'00', ?, 4, 0, 0)",
            (int(time.time()),),
        )
        _memory_uses_table(con)
        # Resolved ledger: mem 1 = 1w/2l (33.3%); baseline mem 999 = 1w/3l
        # → scope baseline = 2/7 ≈ 28.6% → raw Δ ≈ +4.76 → rounds to +5.
        _add_uses(con, 1, "code_fix", "win", 1)
        _add_uses(con, 1, "code_fix", "loss", 2)
        _add_uses(con, 999, "code_fix", "win", 1)
        _add_uses(con, 999, "code_fix", "loss", 3)
        con.commit()
    finally:
        con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Memories to review")
    row = next(r for r in table["rows"] if r["cells"][0]["t"].startswith("g:"))
    assert row["cells"][4]["t"] == "+5pt"
    assert row["cells"][4]["c"] == "green"


def test_memories_to_review_negative_fraction_renders_plus_zero(home: Path) -> None:
    """Raw Δ ≈ −0.48pt renders '+0pt' muted — round() normalises −0.x to 0,
    so the cell never shows the '−0pt' that f'{-0.3:+.0f}' would produce."""
    db_path = str(home / "state.db")
    con = sqlite3.connect(db_path)
    try:
        _ensure_semantic_memory_cols(con)
        con.execute(
            "INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
            "VALUES ('code_fix', 'z: negates to plus zero', x'00', ?, 4, 0, 0)",
            (int(time.time()),),
        )
        _memory_uses_table(con)
        # Resolved ledger: mem 1 = 13w/1l (92.9%); baseline mem 999 = 1w/0l
        # → scope baseline = 14/15 ≈ 93.3% → raw Δ ≈ −0.48 → rounds to 0.
        _add_uses(con, 1, "code_fix", "win", 13)
        _add_uses(con, 1, "code_fix", "loss", 1)
        _add_uses(con, 999, "code_fix", "win", 1)
        con.commit()
    finally:
        con.close()

    page = learn.build(home, "memory", {})
    table = _section(page, "Memories to review")
    row = next(r for r in table["rows"] if r["cells"][0]["t"].startswith("z:"))
    assert row["cells"][4]["t"] == "+0pt"
    assert row["cells"][4]["c"] == "muted"


def test_entrypoint_no_errors(home: Path) -> None:
    """All four tabs build without errors — empty home, no exceptions."""
    start = time.monotonic()
    for tab in ("overview", "lessons", "memory", "improve"):
        page = build_page(home, "learn", tab, {})
        assert page["ok"]
        assert page["errors"] == {}, (tab, page["errors"])
    assert time.monotonic() - start < 5