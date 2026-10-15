"""LF-R1: the tiered verified-theme selector and the two prompt readers.

Both readers used to select ``task_class = ? OR task_class = '*'`` ordered by
``n_runs DESC``, so the wildcard themes won every slot; ``_refresh_theme``
wrote NULL for a theme seen in exactly two classes; and the rendered headers
called these lessons "verified" as if their effect had been measured.

One shared selector (``themes.select_verified_themes``) now serves both:
tier 0 own class, tier 1 member of a comma-joined set, tier 2 the legacy ``'*'``
wildcard only for leftover slots, with a near-duplicate drop. These tests are
hermetic — a temp SQLite DB per test, no live DB, no network.
"""
from __future__ import annotations

import sqlite3

from mini_ork.learning import themes

# ── hermetic fixtures ───────────────────────────────────────────────────────

_GRADIENT_DDL = """
CREATE TABLE IF NOT EXISTS gradient_records (
    gradient_id   TEXT PRIMARY KEY,
    target        TEXT NOT NULL,
    signal        TEXT NOT NULL,
    suggested_change TEXT NOT NULL,
    evidence      TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0.0,
    created_at    INTEGER NOT NULL,
    task_class    TEXT
);
"""

# Nine wildcard lessons, pairwise token overlap <= 0.25 (checked against
# themes._tokenize below) so the near-duplicate drop cannot reorder them;
# n_runs 12..20 sits inside the kickoff's 10-20 window.
_WILDCARD_LESSONS = [
    "prefer drafting the plan outline before requesting any lens research",
    "cap the reviewer fan-out at three independent lenses per chapter",
    "cache retrieved prior-run evidence between sibling lens calls",
    "summarize transcript excerpts inline instead of pasting whole sessions",
    "ask the judge for pairwise verdicts rather than global scores",
    "keep per-chapter word budgets visible during synthesis drafting",
    "deduplicate cited sources before formatting the final bibliography",
    "collapse finished subtasks in the outline before the writing pass",
    "record explicit acceptance probes next to every planned chapter",
]

_FE_NEWER = ("Register new gate evaluators before dispatch so later nodes "
             "resolve them")
_FE_OLDER = ("Write the artifact manifest atomically after the synthesizer "
             "finishes")

# The expected page for ('framework_edit', 5): the two own-class themes first
# (tier 0, within-tier last_seen DESC), then tier-2 '*' by n_runs DESC.
_EXPECTED_ORDER = ["th-fe-newer", "th-fe-older", "th-w-8", "th-w-7", "th-w-6"]


def _build_db(tmp_path, name="state.db"):
    db = tmp_path / name
    themes.ensure_schema(str(db))
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    con.executescript(_GRADIENT_DDL)
    con.commit()
    return db, con


def _add_theme(con, theme_id, *, kind="task", task_class=None, lesson,
               n_runs=1, last_seen=0, status="verified"):
    con.execute(
        "INSERT INTO lesson_themes (theme_id, kind, role, task_class,"
        " representative, centroid, n_gradients, n_runs, n_at_refresh,"
        " first_seen, last_seen, lesson_text, status, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (theme_id, kind, None, task_class, lesson[:120],
         themes._centroid_to_blob({}), 0, n_runs, 0, last_seen, last_seen,
         lesson, status, last_seen),
    )
    con.commit()


def _add_member(con, theme_id, gradient_id, task_class, signal=None):
    con.execute(
        "INSERT INTO gradient_records (gradient_id, target, signal,"
        " suggested_change, evidence, confidence, created_at, task_class)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (gradient_id, "workflow.node.researcher",
         signal or f"signal prose for {gradient_id}", "change",
         f"tr-{gradient_id}", 0.5, 1, task_class),
    )
    con.execute(
        "INSERT INTO gradient_theme (gradient_id, theme_id, similarity)"
        " VALUES (?,?,?)",
        (gradient_id, theme_id, 1.0),
    )
    con.commit()


def _scenario_a_db(tmp_path):
    db, con = _build_db(tmp_path)
    for i, lesson in enumerate(_WILDCARD_LESSONS):
        _add_theme(con, f"th-w-{i}", task_class="*", lesson=lesson,
                   n_runs=12 + i, last_seen=1000 + i)
    _add_theme(con, "th-fe-older", task_class="framework_edit",
               lesson=_FE_OLDER, n_runs=3, last_seen=100)
    _add_theme(con, "th-fe-newer", task_class="framework_edit",
               lesson=_FE_NEWER, n_runs=3, last_seen=200)
    return db, con


def _task_class_of(db, theme_id):
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT task_class FROM lesson_themes WHERE theme_id = ?",
            (theme_id,)).fetchone()
    finally:
        con.close()
    return row[0] if row else "<missing>"


# ── tier precedence ─────────────────────────────────────────────────────────


def test_fixture_wildcards_do_not_overlap(tmp_path):
    """Guard on the fixture: the nine wildcard lessons are pairwise below the
    0.8 near-duplicate bar, so only the tier rule can order them."""
    for i in range(len(_WILDCARD_LESSONS)):
        for j in range(i + 1, len(_WILDCARD_LESSONS)):
            a = set(themes._tokenize(_WILDCARD_LESSONS[i]))
            b = set(themes._tokenize(_WILDCARD_LESSONS[j]))
            ratio = len(a & b) / min(len(a), len(b))
            assert ratio < 0.8, (i, j, ratio)


def test_own_class_themes_rank_above_wildcards(tmp_path):
    """Nine '*' themes (n_runs 12-20) and two framework_edit themes (n_runs 3):
    the two framework_edit themes come first, despite the wildcards' n_runs."""
    db, con = _scenario_a_db(tmp_path)
    out = themes.select_verified_themes(con, "framework_edit", 5)
    ids = [r["theme_id"] for r in out]
    assert ids == _EXPECTED_ORDER, ids
    con.close()


def test_both_readers_share_the_selector_ordering(tmp_path):
    """``context_v2.verified_themes`` and ``_verified_theme_lessons`` return the
    same theme ids in the same order as the shared selector."""
    import mini_ork.context_assembler as context_assembler
    import mini_ork.context_v2 as context_v2

    db, con = _scenario_a_db(tmp_path)
    selector_ids = [r["theme_id"]
                    for r in themes.select_verified_themes(con, "framework_edit", 5)]
    con.close()
    assert selector_ids == _EXPECTED_ORDER

    v2 = context_v2.verified_themes("framework_edit", db=str(db), limit=5)
    v2_ids = [item["id"] for item in v2]
    assert v2_ids == [f"t:{tid}" for tid in _EXPECTED_ORDER], v2_ids

    sources: list[dict] = []
    block = context_assembler._verified_theme_lessons(
        str(db), "framework_edit", 5, sources=sources)
    assert block
    assert [s["id"] for s in sources] == _EXPECTED_ORDER
    assert [s["id"] for s in sources] == [i[2:] for i in v2_ids] == selector_ids
    assert all(s["kind"] == "theme" for s in sources)


def test_selector_row_shape(tmp_path):
    """The selector returns {theme_id, lesson_text, n_runs, task_class}."""
    db, con = _scenario_a_db(tmp_path)
    out = themes.select_verified_themes(con, "framework_edit", 1)
    assert len(out) == 1
    row = out[0]
    assert row["theme_id"] == "th-fe-newer"
    assert row["task_class"] == "framework_edit"
    assert row["n_runs"] == 3
    assert "Register new gate evaluators" in row["lesson_text"]
    con.close()


def test_tier_order_beats_n_runs(tmp_path):
    """Tier 0 (exact) leads tier 1 (member of a set) leads tier 2 ('*'),
    regardless of n_runs."""
    db, con = _build_db(tmp_path)
    _add_theme(con, "th-exact", task_class="code_fix",
               lesson="pin every artifact reference to its producing node before publish",
               n_runs=1, last_seen=1)
    _add_theme(con, "th-set", task_class="code_fix,framework_edit",
               lesson="write acceptance probes beside each planned recipe change",
               n_runs=50, last_seen=2)
    _add_theme(con, "th-star", task_class="*",
               lesson="trim stale registry keys during the nightly garden pass",
               n_runs=98, last_seen=3)
    ids = [r["theme_id"] for r in themes.select_verified_themes(con, "code_fix", 5)]
    assert ids == ["th-exact", "th-set", "th-star"], ids
    con.close()


def test_member_class_set_served_to_members_only(tmp_path):
    """A 'code_fix,framework_edit' theme is returned for both members (tier 1)
    and never for an unrelated class."""
    db, con = _build_db(tmp_path)
    _add_theme(con, "th-set", task_class="code_fix,framework_edit",
               lesson="write acceptance probes beside each planned recipe change",
               n_runs=50, last_seen=2)
    _add_theme(con, "th-research", task_class="research_synthesis",
               lesson="freeze the diff once the reviewer signs the scope check",
               n_runs=10, last_seen=4)
    for cls in ("code_fix", "framework_edit"):
        ids = [r["theme_id"]
               for r in themes.select_verified_themes(con, cls, 5)]
        assert "th-set" in ids, (cls, ids)
    ids = [r["theme_id"]
           for r in themes.select_verified_themes(con, "research_synthesis", 5)]
    assert "th-set" not in ids, ids
    assert "th-research" in ids, ids
    con.close()


def test_null_class_theme_never_returned(tmp_path):
    """A NULL-class verified theme is never selected, for any class."""
    db, con = _build_db(tmp_path)
    _add_theme(con, "th-null", task_class=None,
               lesson="archive unused prompts after the quarterly audit sweep",
               n_runs=99, last_seen=9)
    _add_theme(con, "th-ok", task_class="framework_edit",
               lesson=_FE_NEWER, n_runs=3, last_seen=1)
    _add_theme(con, "th-star", task_class="*",
               lesson="trim stale registry keys during the nightly garden pass",
               n_runs=5, last_seen=2)
    for cls in ("framework_edit", "code_fix", "bug_audit", "research_synthesis"):
        ids = [r["theme_id"]
               for r in themes.select_verified_themes(con, cls, 10)]
        assert "th-null" not in ids, (cls, ids)
    ids = [r["theme_id"]
           for r in themes.select_verified_themes(con, "framework_edit", 10)]
    assert ids == ["th-ok", "th-star"], ids
    con.close()


def test_only_verified_task_rows_with_lesson_text(tmp_path):
    """Only status='verified', kind='task', non-blank lesson_text rows count."""
    db, con = _build_db(tmp_path)
    _add_theme(con, "th-ok", task_class="framework_edit", status="verified",
               lesson=_FE_NEWER, n_runs=3)
    _add_theme(con, "th-candidate", task_class="framework_edit",
               status="candidate", lesson=_FE_OLDER, n_runs=40)
    _add_theme(con, "th-blank", task_class="framework_edit", status="verified",
               lesson="", n_runs=41)
    _add_theme(con, "th-whitespace", task_class="framework_edit",
               status="verified", lesson="   ", n_runs=42)
    _add_theme(con, "th-framework-kind", task_class="framework_edit",
               kind="framework", status="verified",
               lesson="collapse finished subtasks in the outline before the writing pass",
               n_runs=43)
    ids = [r["theme_id"]
           for r in themes.select_verified_themes(con, "framework_edit", 10)]
    assert ids == ["th-ok"], ids
    con.close()


def test_selector_cold_safe_missing_table_and_column(tmp_path):
    """Missing table/column -> []; never raises on the injection path."""
    no_table = sqlite3.connect(str(tmp_path / "no-table.db"))
    no_table.row_factory = sqlite3.Row
    assert themes.select_verified_themes(no_table, "framework_edit", 5) == []
    no_table.close()

    degenerate = sqlite3.connect(str(tmp_path / "no-column.db"))
    degenerate.row_factory = sqlite3.Row
    degenerate.execute("CREATE TABLE lesson_themes (theme_id TEXT PRIMARY KEY)")
    degenerate.commit()
    assert themes.select_verified_themes(degenerate, "framework_edit", 5) == []
    degenerate.close()


def test_selectors_without_row_factory(tmp_path):
    """The selector reads by index, so a caller's default tuple row_factory
    (the assembler's own connection) works too."""
    db, con = _build_db(tmp_path)
    _add_theme(con, "th-fe", task_class="framework_edit", lesson=_FE_NEWER,
               n_runs=3, last_seen=1)
    con.close()
    plain = sqlite3.connect(str(db))  # no row_factory -> tuple rows
    out = themes.select_verified_themes(plain, "framework_edit", 5)
    plain.close()
    assert [r["theme_id"] for r in out] == ["th-fe"]


# ── near-duplicate dropping ─────────────────────────────────────────────────


def test_near_duplicate_lens_lessons_yield_at_most_one(tmp_path):
    """Three lessons that overlap >= 0.8 by token set yield at most one theme —
    the strongest survives and cannot be dropped by a weaker sibling."""
    db, con = _build_db(tmp_path)
    base = ("route concurrent lens calls to the cheapest sibling lens first "
            "and escalate only after a lens fails")
    near = [base, base + " always", base + " today"]
    for i in range(len(near)):
        for j in range(i + 1, len(near)):
            a, b = set(themes._tokenize(near[i])), set(themes._tokenize(near[j]))
            assert len(a & b) / min(len(a), len(b)) >= 0.8, (i, j)
    for i, lesson in enumerate(near):
        _add_theme(con, f"th-near-{i}", task_class="framework_edit",
                   lesson=lesson, n_runs=30 - i, last_seen=30 - i)
    ids = [r["theme_id"]
           for r in themes.select_verified_themes(con, "framework_edit", 5)]
    assert ids == ["th-near-0"], ids
    con.close()


def test_distinct_same_theme_lessons_survive(tmp_path):
    """Two genuinely different lessons on the same class both come back — the
    dedup compares only against selected rows, not the whole pool."""
    db, con = _build_db(tmp_path)
    _add_theme(con, "th-a", task_class="framework_edit",
               lesson="Register new gate evaluators before dispatch resolves them",
               n_runs=5, last_seen=5)
    _add_theme(con, "th-b", task_class="framework_edit",
               lesson="Write the artifact manifest atomically after synthesis ends",
               n_runs=4, last_seen=4)
    ids = [r["theme_id"]
           for r in themes.select_verified_themes(con, "framework_edit", 5)]
    assert ids == ["th-a", "th-b"], ids
    con.close()


# ── reclassify_themes ───────────────────────────────────────────────────────


def _reclassify_db(tmp_path):
    db, con = _build_db(tmp_path)
    _add_theme(con, "th-r1", task_class="*", lesson="legacy wildcard one",
               n_runs=10, status="verified")
    _add_member(con, "th-r1", "g-r1a", "framework_edit")
    _add_member(con, "th-r1", "g-r1b", "code_fix")
    _add_theme(con, "th-r2", task_class=None, lesson="legacy null two",
               n_runs=11, status="candidate")
    _add_member(con, "th-r2", "g-r2a", "code_fix")
    _add_member(con, "th-r2", "g-r2b", "chapter_review")
    _add_theme(con, "th-r3", task_class="*", lesson="legacy wildcard three",
               n_runs=12, status="verified")
    _add_member(con, "th-r3", "g-r3a", "framework_edit")
    _add_member(con, "th-r3", "g-r3b", "chapter_review")
    _add_member(con, "th-r3", "g-r3c", "code_fix")
    _add_theme(con, "th-r4", task_class="code_fix", lesson="already settled",
               n_runs=13, status="verified")
    _add_member(con, "th-r4", "g-r4a", "code_fix")
    _add_theme(con, "th-r5", task_class="*", kind="framework",
               lesson="framework wildcard stays", n_runs=14)
    _add_member(con, "th-r5", "g-r5a", "code_fix")
    _add_member(con, "th-r5", "g-r5b", "framework_edit")
    return db, con


def test_reclassify_writes_sorted_sets_and_counts(tmp_path):
    """Two classes -> the pair, three -> the triple, count returned, second
    call 0 (idempotent)."""
    db, con = _reclassify_db(tmp_path)
    assert themes.reclassify_themes(db=str(db)) == 3
    assert _task_class_of(db, "th-r1") == "code_fix,framework_edit"
    assert _task_class_of(db, "th-r2") == "chapter_review,code_fix"
    assert _task_class_of(db, "th-r3") == "chapter_review,code_fix,framework_edit"
    assert themes.reclassify_themes(db=str(db)) == 0
    assert _task_class_of(db, "th-r3") == "chapter_review,code_fix,framework_edit"
    con.close()


def test_reclassify_touches_only_stale_task_rows(tmp_path):
    """Settled task rows and framework-kind rows are untouched."""
    db, con = _reclassify_db(tmp_path)
    themes.reclassify_themes(db=str(db))
    assert _task_class_of(db, "th-r4") == "code_fix"
    assert _task_class_of(db, "th-r5") == "*"       # kind='framework'
    con.close()


def test_reclassify_never_changes_stored_status(tmp_path):
    """The stored status value does not change — `verified` stays `verified`."""
    db, con = _reclassify_db(tmp_path)
    before = {t: _status_of(con, t) for t in ("th-r1", "th-r2", "th-r3")}
    themes.reclassify_themes(db=str(db))
    after = {t: _status_of(con, t) for t in ("th-r1", "th-r2", "th-r3")}
    assert after == before
    assert after["th-r1"] == "verified" and after["th-r2"] == "candidate"
    con.close()


def _status_of(con, theme_id):
    row = con.execute(
        "SELECT status FROM lesson_themes WHERE theme_id = ?", (theme_id,)
    ).fetchone()
    return row[0] if row else "<missing>"


def test_reclassify_cold_safe_missing_db_and_table(tmp_path):
    """Missing file and missing table both return 0 and never raise."""
    empty_db = tmp_path / "empty.db"
    empty_db.write_bytes(b"")
    assert themes.reclassify_themes(db=str(empty_db)) == 0
    assert themes.reclassify_themes(db=str(tmp_path / "does-not-exist.db")) == 0
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    bare.executescript(_GRADIENT_DDL)
    bare.commit()
    bare.close()
    assert themes.reclassify_themes(db=str(tmp_path / "bare.db")) == 0


# ── _refresh_theme task_class ───────────────────────────────────────────────


def _refresh(con, theme_id, n_members):
    t = {"theme_id": theme_id, "kind": "task", "centroid": {}, "n": n_members,
         "n_at_refresh": 0, "representative": "", "first_seen": 0,
         "last_seen": 0, "role": None, "task_class": "*"}
    themes._refresh_theme(con, t, {}, 1700000000, 0.0)
    con.commit()


def test_refresh_theme_two_classes_writes_pair_not_null(tmp_path):
    db, con = _build_db(tmp_path)
    _add_theme(con, "th-two", task_class="*", lesson="two class theme",
               n_runs=5, status="verified")
    _add_member(con, "th-two", "g-a", "framework_edit",
                signal="write the manifest atomically after synthesis")
    _add_member(con, "th-two", "g-b", "code_fix",
                signal="pin artifact references before publishing anything")
    _refresh(con, "th-two", 2)
    assert _task_class_of(db, "th-two") == "code_fix,framework_edit"
    assert _status_of(con, "th-two") == "verified"
    con.close()


def test_refresh_theme_single_and_triple_and_empty(tmp_path):
    db, con = _build_db(tmp_path)
    _add_theme(con, "th-one", task_class="*", lesson="single class", n_runs=5)
    _add_member(con, "th-one", "g-c", "framework_edit")
    _refresh(con, "th-one", 1)
    assert _task_class_of(db, "th-one") == "framework_edit"

    _add_theme(con, "th-three", task_class="*", lesson="three class", n_runs=5)
    for i, cls in enumerate(("framework_edit", "chapter_review", "code_fix")):
        _add_member(con, "th-three", f"g-t{i}", cls)
    _refresh(con, "th-three", 3)
    assert _task_class_of(db, "th-three") == (
        "chapter_review,code_fix,framework_edit")

    _add_theme(con, "th-empty", task_class="*", lesson="memberless", n_runs=5)
    _refresh(con, "th-empty", 0)
    assert _task_class_of(db, "th-empty") is None
    con.close()


# ── assign_new wiring ───────────────────────────────────────────────────────


def test_assign_new_repairs_legacy_star_theme(tmp_path):
    """assign_new repairs an untouched legacy '*' theme (fail-soft hook)."""
    db, con = _build_db(tmp_path)
    _add_theme(con, "th-legacy", task_class="*", lesson="legacy wildcard",
               n_runs=10, status="verified")
    _add_member(con, "th-legacy", "g-l1", "framework_edit")
    _add_member(con, "th-legacy", "g-l2", "chapter_review")
    _add_member(con, "th-legacy", "g-l3", "code_fix")
    # One unassigned gradient, vocabulary disjoint from th-legacy, so the loop
    # starts its own theme and leaves th-legacy for the repair pass.
    con.execute(
        "INSERT INTO gradient_records (gradient_id, target, signal,"
        " suggested_change, evidence, confidence, created_at, task_class)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("g-free", "workflow.node.planner",
         "quixotic zebra yodel balustrade marmalade compass",
         "change", "tr-g-free", 0.5, 2, "framework_edit"),
    )
    con.commit()
    result = themes.assign_new(str(db))
    assert isinstance(result, dict)
    assert _task_class_of(db, "th-legacy") == (
        "chapter_review,code_fix,framework_edit")
    assert _status_of(con, "th-legacy") == "verified"
    con.close()


# ── honest headers ──────────────────────────────────────────────────────────


def test_v2_render_carries_the_honest_header(tmp_path):
    import mini_ork.context_v2 as context_v2

    pack = {"constraints": [], "file_findings": [], "prior_attempts": [],
            "themes": [{"id": "t:th-fe-newer", "text": _FE_NEWER, "n_runs": 3}]}
    text = context_v2.render(pack)
    assert ("Lessons induced from earlier runs (guardrail-checked; "
            "effect not yet measured):") in text
    assert "Lessons verified on earlier runs of this task class:" not in text


def test_v1_block_carries_the_honest_header(tmp_path):
    import mini_ork.context_assembler as context_assembler

    db, con = _scenario_a_db(tmp_path)
    block = context_assembler._verified_theme_lessons(str(db), "framework_edit", 5)
    con.close()
    assert block
    assert ("--- Lessons induced from prior runs (framework_edit; "
            "guardrail-checked, effect not yet measured) ---") in block
    assert "--- /induced lessons ---" in block
    assert "--- Verified lessons from prior runs" not in block
    assert "--- /verified lessons ---" not in block
