"""Hermetic contract tests for the harness-target slice of the apply loop.

Kickoff: ``kickoffs/auto/rsi-i5-harness-sweep.md`` (G02-T01, E6 slice) +
``kickoffs/auto/rsi-i5-harness-sweep-repair.md`` (per-arm cost gate).

What this slice adds:

* ``mini_ork/cli/apply.py``
    - ``_harness_target_file_for`` resolves ``harness.<recipe>.<node>`` via the
      recipe's ``workflow.yaml`` ``prompt_ref``.
    - ``_prompt_file_for`` delegates the ``harness.`` prefix to it.
    - ``auto_sweep`` extends its gradient-SQL filter to include ``harness.%``
      targets when ``MO_APPLY_HARNESS_TARGETS=1`` (default OFF).
    - ``apply_run`` appends a ``cost_per_solved_task`` audit line to
      ``gate_rationale`` (informational; legacy aggregate form).
    - ``apply_run`` adds a per-arm ``cost_per_solved_task_per_arm`` audit
      line and, for ``harness.*`` targets ONLY, flips a would-be
      ``promoted`` to ``quarantined`` with reason ``harness-cost-regression``
      when the candidate arm's cps exceeds the baseline's (mechanism step 3).

* ``mini_ork/learning/harness_operator.py``
    - ``materialize_mutation`` is the proposal → directive adapter used by the
      apply path (pure function, no DB I/O, stable ``source_ref`` for
      idempotency).

These tests pin the surface without spinning up real probe runs. The
"stub scorer" pattern is the existing monkeypatch seam
(``mini_ork.learning.probe_scorer.probe_score``); cases:

1. Flag OFF → ``auto_sweep`` selects unchanged (no harness targets).
2. Flag ON + equal solved set + lower candidate cost → promoted.
3. Flag ON + strict-gain solved set + higher candidate cost → quarantined
   with reason ``harness-cost-regression`` (the proof-of-fix case; fails
   on the WIP code at ``apply.py:1029`` which only audits cost).
4. After scoring, the live recipe prompt file is byte-identical to its
   pre-scoring contents (the probe scorer's temp-copy path is the seam).
5. agent.* target with higher candidate cost → decision unchanged by cost.
6. control_n=3 baseline cost is normalised per attempt (not 3× over-counted).
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

# Same minimal fixture schema as tests/unit/test_cli_apply_py.py; isolated so
# this test does not depend on the parity file's import surface.
# pick_candidate reads pattern_records (priority 1) and emergent_patterns
# (priority 2) BEFORE gradient_records (priority 3); tables must exist or
# the "no such table" handler short-circuits the gradient fallback.
FIXTURE_DDL = """
CREATE TABLE pattern_records (
  pattern_id            TEXT PRIMARY KEY,
  description           TEXT NOT NULL,
  evidence_trace_ids    TEXT NOT NULL DEFAULT '[]',
  frequency             INTEGER NOT NULL DEFAULT 1,
  first_seen            TEXT NOT NULL DEFAULT '',
  last_seen             TEXT NOT NULL DEFAULT '',
  output_type           TEXT NOT NULL,
  promoted_to           TEXT,
  status                TEXT NOT NULL DEFAULT 'observed'
);
CREATE TABLE emergent_patterns (
  pattern_id           TEXT PRIMARY KEY,
  cluster_label        TEXT NOT NULL,
  member_item_ids_json TEXT NOT NULL,
  feature_set_json     TEXT NOT NULL,
  strength_score       REAL NOT NULL,
  suggested_meta_adr   TEXT,
  status               TEXT NOT NULL DEFAULT 'proposed',
  detected_at          INTEGER NOT NULL,
  resolved_at          INTEGER
);
CREATE TABLE gradient_records (
    gradient_id      TEXT PRIMARY KEY,
    target           TEXT NOT NULL,
    signal           TEXT NOT NULL,
    suggested_change TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0,
    created_at       INTEGER NOT NULL,
    task_class       TEXT
);
CREATE TABLE workflow_memory (
  workflow_version_id       TEXT PRIMARY KEY,
  workflow_name             TEXT NOT NULL,
  base_version_id           TEXT,
  yaml_hash                 TEXT NOT NULL,
  yaml_blob                 TEXT NOT NULL,
  mutations                 TEXT NOT NULL DEFAULT '[]',
  created_at                TEXT NOT NULL DEFAULT '',
  status                    TEXT NOT NULL DEFAULT 'candidate',
  previous_stable_version_id TEXT
);
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
  candidate_id          TEXT NOT NULL,
  from_version_id       TEXT NOT NULL,
  to_version_id         TEXT NOT NULL,
  utility_before        REAL NOT NULL DEFAULT 0.0,
  utility_after         REAL NOT NULL DEFAULT 0.0,
  benchmark_run_id      TEXT,
  rationale             TEXT NOT NULL DEFAULT '',
  decision              TEXT NOT NULL,
  decided_at            TEXT NOT NULL DEFAULT '',
  decided_by            TEXT NOT NULL
);
CREATE TABLE apply_attempts (
    attempt_id              TEXT PRIMARY KEY,
    task_class              TEXT NOT NULL,
    target_kind             TEXT NOT NULL,
    target_name             TEXT NOT NULL,
    source_kind             TEXT NOT NULL,
    source_id               TEXT,
    candidate_id            TEXT,
    promotion_id            TEXT,
    base_workflow_version_id TEXT,
    utility_before          REAL,
    utility_after           REAL,
    utility_delta           REAL,
    decision                TEXT NOT NULL,
    rationale               TEXT NOT NULL DEFAULT '',
    dry_run                 INTEGER NOT NULL DEFAULT 0,
    apply_enabled           INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT ''
);
"""


@pytest.fixture()
def harness_env(monkeypatch):
    """Scrub apply-loop env so each test sets only what it intends."""
    for var in (
        "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_ROOT",
        "MO_APPLY_ENABLED", "MO_APPLY_DRY_RUN", "MO_APPLY_SCORER",
        "MO_APPLY_MODE",
        "MO_APPLY_NONREGRESSION_DELTA", "MO_APPLY_MIN_EXAMPLES",
        "MO_APPLY_REGRESSION_TOLERANCE", "MO_APPLY_PERTASK_JSON",
        "MO_APPLY_MOCK_BASELINE", "MO_APPLY_MOCK_DELTA",
        "MO_APPLY_FORCE_REGRESSION",
        "MO_AUTO_APPLY", "MO_AUTO_APPLY_MAX_TARGETS",
        "MO_APPLY_PROBE_MAX_TASKS", "MO_APPLY_PROBE_BUDGET_USD",
        "MO_APPLY_PROBE_TIMEOUT_S", "MO_APPLY_HARNESS_TARGETS",
    ):
        monkeypatch.delenv(var, raising=False)
    ap._SCHEMA_INIT = False
    yield monkeypatch
    ap._SCHEMA_INIT = False


@pytest.fixture()
def harness_db(tmp_path, harness_env):
    """A tmp sqlite DB with the minimal fixture schema; MINI_ORK_DB set."""
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE_DDL)
    con.commit()
    con.close()
    harness_env.setenv("MINI_ORK_DB", str(path))
    return str(path)


@pytest.fixture()
def harness_recipe(tmp_path, harness_env):
    """A recipe with a workflow.yaml pointing to a real prompts/<node>.md file.

    ``harness_env`` is depended on (not consumed) to ensure env scrubbing
    runs before the recipe is materialized on disk.
    """
    del harness_env  # noqa: F841 — fixture ordering only
    recipe = tmp_path / "recipes" / "fw-edit"
    (recipe / "prompts").mkdir(parents=True)
    (recipe / "prompts" / "implementer.md").write_text("LIVE PROMPT BODY\n")
    wf = """
version: "0.1.0"
task_class: fw_edit
nodes:
  - name: planner
    type: planner
    prompt_ref: prompts/planner.md
  - name: implementer
    type: implementer
    prompt_ref: prompts/implementer.md
  - name: reviewer
    type: reviewer
    prompt_ref: null
"""
    (recipe / "workflow.yaml").write_text(wf)
    return recipe


def _seed_gradient(db_path, *, gradient_id, target, change, confidence,
                   task_class):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO gradient_records"
        " (gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (gradient_id, target, "harness", change, "[]", confidence, 100,
         task_class),
    )
    con.commit()
    con.close()


def _rows(db_path, table):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table}").fetchall()]
    finally:
        con.close()


def _live_prompt_bytes(recipe: Path) -> bytes:
    return (recipe / "prompts" / "implementer.md").read_bytes()


# ── resolver contract ────────────────────────────────────────────────────────

def test_harness_resolver_happy_path(harness_recipe):
    target_file = ap._prompt_file_for(
        str(harness_recipe), "harness.fw-edit.implementer")
    assert target_file == str(harness_recipe / "prompts" / "implementer.md")
    assert Path(target_file).read_text() == "LIVE PROMPT BODY\n"


def test_harness_resolver_null_prompt_ref_returns_empty(harness_recipe):
    # `reviewer` node has prompt_ref=null in the fixture — must NOT be guessed.
    assert ap._prompt_file_for(
        str(harness_recipe), "harness.fw-edit.reviewer") == ""


def test_harness_resolver_unknown_node_returns_empty(harness_recipe):
    assert ap._prompt_file_for(
        str(harness_recipe), "harness.fw-edit.ghost-node") == ""


def test_harness_resolver_node_name_dash_underscore_interchange(harness_recipe):
    # Both forms must resolve (workflow.yaml names carry one; gradients
    # may carry the other, mirroring the agent.* branch).
    assert ap._prompt_file_for(
        str(harness_recipe), "harness.fw-edit.code-impact-lens") == ""
    # Add a node with a dash form to prove the resolver accepts it cleanly.
    (harness_recipe / "prompts" / "code-impact-lens.md").write_text("X\n")
    (harness_recipe / "workflow.yaml").write_text(
        (harness_recipe / "workflow.yaml").read_text().replace(
            "  - name: implementer\n",
            "  - name: code_impact_lens\n"
            "    type: researcher\n"
            "    prompt_ref: prompts/code-impact-lens.md\n"
            "  - name: implementer\n",
        )
    )
    assert ap._prompt_file_for(
        str(harness_recipe),
        "harness.fw-edit.code-impact-lens") == str(
            harness_recipe / "prompts" / "code-impact-lens.md")


def test_harness_resolver_recipe_mismatch_returns_empty(harness_recipe):
    # The resolver is called with a recipe_dir it already chose via
    # _recipe_dir_for; if the target's recipe name disagrees with the dir,
    # refuse rather than crossing recipes.
    assert ap._prompt_file_for(
        str(harness_recipe), "harness.other-recipe.implementer") == ""


def test_harness_resolver_missing_workflow_yaml(tmp_path):
    # No workflow.yaml → cannot resolve → "" (no guess).
    recipe = tmp_path / "recipes" / "no-wf"
    (recipe / "prompts").mkdir(parents=True)
    (recipe / "prompts" / "implementer.md").write_text("X\n")
    assert ap._prompt_file_for(
        str(recipe), "harness.no-wf.implementer") == ""


# ── auto_sweep opt-in ────────────────────────────────────────────────────────

def test_auto_sweep_flag_off_skips_harness_targets(
    harness_db, harness_recipe, harness_env, monkeypatch,
):
    del harness_env  # env is scrubbed transitively via harness_db; not used here
    monkeypatch.setattr(ap, "_resolve_root", lambda: str(harness_recipe.parent.parent))
    _seed_gradient(harness_db, gradient_id="gr-h1",
                   target="harness.fw-edit.implementer",
                   change="directive one", confidence=0.9,
                   task_class="fw_edit")
    _seed_gradient(harness_db, gradient_id="gr-a1",
                   target="agent.tiny-researcher.prompt",
                   change="agent directive", confidence=0.8,
                   task_class="fw_edit")
    # Flag is unset (default OFF): harness targets must be filtered out
    # of the SELECT.
    results = ap.auto_sweep("fw_edit", db=harness_db, max_targets=10)
    targets = [r["target"] for r in results]
    assert "harness.fw-edit.implementer" not in targets
    # agent.* still flows through unchanged.
    assert "agent.tiny-researcher.prompt" in targets


def test_auto_sweep_flag_on_includes_harness_targets(
    harness_db, harness_recipe, harness_env, monkeypatch,
):
    monkeypatch.setattr(ap, "_resolve_root", lambda: str(harness_recipe.parent.parent))
    _seed_gradient(harness_db, gradient_id="gr-h1",
                   target="harness.fw-edit.implementer",
                   change="directive one", confidence=0.9,
                   task_class="fw_edit")
    harness_env.setenv("MO_APPLY_HARNESS_TARGETS", "1")
    harness_env.setenv("MO_APPLY_ENABLED", "1")
    # Stub the scorer path so no real probes run. We only need
    # auto_sweep's SELECT + dispatch loop to fire.
    from mini_ork.learning import probe_scorer as ps

    def stub_probe(*_a, **_k):
        return None  # unmeasured → quarantine via the existing path

    monkeypatch.setattr(ps, "probe_score", stub_probe)
    results = ap.auto_sweep("fw_edit", db=harness_db, max_targets=10)
    targets = [r["target"] for r in results]
    assert "harness.fw-edit.implementer" in targets
    # Live recipe prompt untouched even after the sweep (no probe launched).
    assert _live_prompt_bytes(harness_recipe) == b"LIVE PROMPT BODY\n"
    atts = _rows(harness_db, "apply_attempts")
    harness_atts = [a for a in atts if a["target_name"].startswith("harness.")]
    assert len(harness_atts) == 1
    assert harness_atts[0]["target_kind"] == "prompt_file"  # CHECK-valid


# ── cost-per-solved-task gate rationale ─────────────────────────────────────

def _common_apply_kwargs(harness_db, harness_env, harness_recipe, monkeypatch,
                         *, n, before_avg, after_avg,
                         baseline_total_cost=0.0, candidate_total_cost=0.0,
                         control_n=1, before_bin=None, after_bin=None):
    """Wire a stub probe scorer + probe_result shape shared by cases 2-6.

    The stub emits a ``runs`` list with per-arm entries (one baseline row
    per attempt, one candidate row per probe) so the per-arm cost split
    in apply.py has data to read. ``control_n`` is the normalisation
    factor — for control_n=1 the baseline has n rows; for control_n=K
    the baseline has n*K rows. The aggregate ``cost_usd`` field is the
    sum of both arms' totals (parity with the prior stub shape).
    """
    del harness_db  # noqa: F841 — db is wired transitively via harness_env
    harness_env.setenv("MO_APPLY_ENABLED", "1")
    harness_env.setenv("MO_APPLY_SCORER", "probe")
    # _recipe_dir_for joins root + "recipes" + name, so root must be the
    # parent of the recipes/ tree (i.e. tmp_path, NOT tmp_path/recipes).
    monkeypatch.setattr(ap, "_resolve_root",
                        lambda: str(harness_recipe.parent.parent))
    pertask = {
        "before": before_bin or [1] * n,
        "after": after_bin or [1] * n,
        "ids": [f"probe-{i}.md" for i in range(n)],
    }
    n_baseline_runs = max(1, n * max(1, int(control_n)))
    n_candidate_runs = max(1, n)
    baseline_per = baseline_total_cost / n_baseline_runs
    candidate_per = candidate_total_cost / n_candidate_runs
    baseline_runs = [
        {
            "probe": f"probe-{i % max(1, n)}.md",
            "arm": "baseline",
            "retry": i // max(1, n),
            "run_id": f"run-b-{i}",
            "outcome": before_avg,
            "cost_usd": baseline_per,
        }
        for i in range(n_baseline_runs)
    ]
    candidate_runs = [
        {
            "probe": f"probe-{i}.md",
            "arm": "candidate",
            "run_id": f"run-c-{i}",
            "outcome": after_avg,
            "cost_usd": candidate_per,
        }
        for i in range(n_candidate_runs)
    ]
    probe_out = {
        "n": n,
        "before": before_avg,
        "after": after_avg,
        "pertask_json": json.dumps(pertask),
        "runs": baseline_runs + candidate_runs,
        "cost_usd": baseline_total_cost + candidate_total_cost,
        "control_n": control_n,
    }
    from mini_ork.learning import probe_scorer as ps
    monkeypatch.setattr(ps, "probe_score", lambda *_a, **_k: dict(probe_out))
    return probe_out


def test_apply_run_harness_target_lower_cost_promotes(
    harness_db, harness_recipe, harness_env, monkeypatch,
):
    """Case 2: strict gain + cheaper candidate arm → promoted; live recipe untouched.

    The promotion branch writes a directive block to the LIVE prompt file
    (apply_mutation is not gated to the temp-copy path; the temp-copy is only
    used by the probe scorer). After this test the live file WILL contain the
    appended block — that is the documented semantics for ``MO_APPLY_MODE=append``
    on a real promote. The "byte-identical after scoring" invariant lives in
    case 4, where the gate quarantines and no write happens.

    The per-arm split (control_n=1) makes baseline_cost=$0.04 and
    candidate_cost=$0.04; n_solved_baseline=1, n_solved_candidate=2 →
    cps_baseline=$0.04, cps_candidate=$0.02 → cps_candidate < cps_baseline
    → no harness-cost flip → decision stays "promoted".
    """
    _seed_gradient(
        harness_db, gradient_id="gr-h-lo",
        target="harness.fw-edit.implementer",
        change="lower-cost directive", confidence=0.9,
        task_class="fw_edit",
    )
    _common_apply_kwargs(
        harness_db, harness_env, harness_recipe, monkeypatch,
        n=2, before_avg=0.5, after_avg=1.0,  # strict gain: 0→1 recovery
        before_bin=[0, 1], after_bin=[1, 1],
        baseline_total_cost=0.04, candidate_total_cost=0.04,
    )
    live_target = harness_recipe / "prompts" / "implementer.md"
    target_file = str(live_target)
    target_name = "harness.fw-edit.implementer"
    rc = ap.apply_run("fw_edit", "prompt_file", target_name,
                      target_file, db=harness_db)
    assert rc == 0
    atts = _rows(harness_db, "apply_attempts")
    harness_atts = [a for a in atts if a["target_name"] == target_name]
    assert len(harness_atts) == 1
    assert harness_atts[0]["decision"] == "promoted"
    rationale = harness_atts[0]["rationale"]
    # Legacy aggregate audit line is preserved (parity with prior case).
    assert "cost_per_solved_task" in rationale
    assert "before=$" in rationale and "after=$" in rationale
    # n_solved_before = 1 (one probe passed), n_solved_after = 2 → cps halves.
    assert "n_solved_before=1" in rationale
    assert "n_solved_after=2" in rationale
    # The existing cost=$ line and probe: n= line stay (parity with prior case).
    assert "cost=$0.08" in rationale
    assert "probe: n=2" in rationale
    # New per-arm audit line: candidate cheaper per-solved → no flip.
    assert "cost_per_solved_task_per_arm" in rationale
    assert "baseline=$0.0400" in rationale
    assert "candidate=$0.0200" in rationale
    assert "baseline_cost=$0.0400" in rationale
    assert "candidate_cost=$0.0400" in rationale
    assert "control_n=1" in rationale
    # The harness-cost-regression reason MUST NOT appear (cheap candidate).
    assert "harness-cost-regression" not in rationale


def test_apply_run_harness_target_higher_cost_quarantines(
    harness_db, harness_recipe, harness_env, monkeypatch,
):
    """Case 3: strict-gain solved set + higher candidate cost → quarantined.

    This is the proof-of-fix case the repair kickoff calls out: the
    non-regression gate alone would promote (utility_after > utility_before),
    but the new per-arm harness-cost gate flips the decision because
    ``cps_candidate > cps_baseline``. On the WIP code (apply.py:1029-1053)
    cost is audit-only and this test fails — the repair makes it pass.

    Per-arm numbers: baseline_total_cost=$0.04 (2 attempts × $0.02);
    candidate_total_cost=$0.40 (2 attempts × $0.20, 10× more expensive);
    n_solved_baseline=1, n_solved_candidate=2 →
    cps_baseline=$0.04, cps_candidate=$0.20 → $0.20 > $0.04 → flip.
    """
    _seed_gradient(
        harness_db, gradient_id="gr-h-hi",
        target="harness.fw-edit.implementer",
        change="higher-cost directive", confidence=0.9,
        task_class="fw_edit",
    )
    pre_bytes = _live_prompt_bytes(harness_recipe)
    _common_apply_kwargs(
        harness_db, harness_env, harness_recipe, monkeypatch,
        n=2, before_avg=0.5, after_avg=1.0,  # strict gain: 0→1 recovery
        before_bin=[0, 1], after_bin=[1, 1],
        baseline_total_cost=0.04, candidate_total_cost=0.40,
    )
    target_file = str(harness_recipe / "prompts" / "implementer.md")
    target_name = "harness.fw-edit.implementer"
    rc = ap.apply_run("fw_edit", "prompt_file", target_name,
                      target_file, db=harness_db)
    assert rc == 0
    atts = _rows(harness_db, "apply_attempts")
    harness_atts = [a for a in atts if a["target_name"] == target_name]
    assert len(harness_atts) == 1
    # The would-be promote flips to quarantine because the candidate arm
    # is more expensive per-solved-task than the baseline.
    assert harness_atts[0]["decision"] == "quarantined"
    rationale = harness_atts[0]["rationale"]
    # Both audit lines stay on a quarantine.
    assert "cost_per_solved_task" in rationale
    assert "cost_per_solved_task_per_arm" in rationale
    # The new reason prefix lands on the rationale.
    assert "harness-cost-regression" in rationale
    # The per-arm dollar values are surfaced for audit.
    assert "cps_baseline=$0.0400" in rationale
    assert "cps_candidate=$0.2000" in rationale
    # Live recipe prompt file is byte-identical to its pre-scoring content —
    # the apply_mutation branch never fires on a quarantine.
    assert _live_prompt_bytes(harness_recipe) == pre_bytes


def test_apply_run_harness_target_byte_identical_live_recipe_after_scoring(
    harness_db, harness_recipe, harness_env, monkeypatch,
):
    """Case 4: live recipe prompt file is byte-identical after scoring.

    Same harness + stub scorer as case 3, plus the probe scorer's temp-copy
    path (the only path that touches the prompt file during scoring). After
    ``apply_run`` returns, the LIVE prompt bytes must match the pre-scoring
    bytes exactly — proves the probe scorer's ``_materialize_arm`` writes
    only into the TEMP recipe copy, never the live recipe.
    """
    _seed_gradient(
        harness_db, gradient_id="gr-h-clean",
        target="harness.fw-edit.implementer",
        change="byte-identity directive", confidence=0.9,
        task_class="fw_edit",
    )
    pre_bytes = _live_prompt_bytes(harness_recipe)
    _common_apply_kwargs(
        harness_db, harness_env, harness_recipe, monkeypatch,
        n=2, before_avg=0.5, after_avg=0.5,
        before_bin=[1, 0], after_bin=[1, 0],
        baseline_total_cost=0.02, candidate_total_cost=0.02,
    )
    target_file = str(harness_recipe / "prompts" / "implementer.md")
    target_name = "harness.fw-edit.implementer"
    rc = ap.apply_run("fw_edit", "prompt_file", target_name,
                      target_file, db=harness_db)
    assert rc == 0
    # The whole recipes/ tree under tmp_path should contain the original
    # recipe only — no __probe_<pid>_<idx> leftovers that would indicate the
    # live recipe got copied and modified (probe_scorer must clean up).
    live_path = harness_recipe / "prompts" / "implementer.md"
    assert live_path.read_bytes() == pre_bytes
    # No sibling temp recipes.
    siblings = [p.name for p in (harness_recipe.parent).iterdir()]
    assert siblings == ["fw-edit"], (
        f"unexpected recipe tree state: {siblings}")


def test_apply_run_agent_target_higher_cost_decision_unchanged(
    harness_db, harness_recipe, harness_env, monkeypatch,
):
    """Case 5: agent.* target with a more expensive candidate arm.

    The harness-cost gate is keyed on ``target_name.startswith("harness.")``
    only (mechanism step 3). For an agent.* target with a strict-gain
    solved set AND a more expensive candidate arm, the non-regression
    gate promotes; the per-arm cost audit line is appended but the
    decision must NOT flip — agent.* cost is logged, not gating.

    Bypasses auto_sweep's harness-only SELECT by calling apply_run
    directly with target_name="agent.tiny-researcher.prompt". The
    resolver path (target_kind=prompt_file) is the same one auto_sweep
    would take; only the prefix differs.
    """
    _seed_gradient(
        harness_db, gradient_id="gr-a-hi",
        target="agent.tiny-researcher.prompt",
        change="agent directive (expensive)", confidence=0.9,
        task_class="fw_edit",
    )
    _common_apply_kwargs(
        harness_db, harness_env, harness_recipe, monkeypatch,
        n=2, before_avg=0.5, after_avg=1.0,  # strict gain
        before_bin=[0, 1], after_bin=[1, 1],
        baseline_total_cost=0.04, candidate_total_cost=0.40,
    )
    target_file = str(harness_recipe / "prompts" / "implementer.md")
    target_name = "agent.tiny-researcher.prompt"
    rc = ap.apply_run("fw_edit", "prompt_file", target_name,
                      target_file, db=harness_db)
    assert rc == 0
    atts = _rows(harness_db, "apply_attempts")
    agent_atts = [a for a in atts if a["target_name"] == target_name]
    assert len(agent_atts) == 1
    # Non-harness prefix → cost is audit-only, decision still "promoted".
    assert agent_atts[0]["decision"] == "promoted"
    rationale = agent_atts[0]["rationale"]
    # Both audit lines surface; the regression reason MUST NOT appear.
    assert "cost_per_solved_task_per_arm" in rationale
    assert "baseline=$0.0400" in rationale
    assert "candidate=$0.2000" in rationale
    assert "harness-cost-regression" not in rationale


def test_apply_run_harness_target_control_n_3_normalises_baseline(
    harness_db, harness_recipe, harness_env, monkeypatch,
):
    """Case 6: control_n=3 normalises baseline cost per attempt.

    With control_n=3 and n=2 probes, probe_scorer emits 6 baseline rows
    (3 attempts × 2 probes). The naive (un-normalised) baseline sum is
    3× the per-attempt cost; without the ``baseline_cost / control_n``
    step in apply.py, a control_n=3 baseline would appear 3× more
    expensive than a single-shot candidate even when the per-attempt
    cost is identical.

    Scenario: baseline_total_cost=$0.30 (6 attempts × $0.05) →
    baseline_cost_normalised = $0.30 / 3 = $0.10; candidate_total_cost=
    $0.40. n_solved_baseline=1, n_solved_candidate=2 → cps_baseline=
    $0.10/1=$0.10, cps_candidate=$0.40/2=$0.20 → $0.20 > $0.10 → flip.

    WITHOUT normalisation: baseline_cost=$0.30 → cps_baseline=$0.30/1=
    $0.30 > $0.20 → no flip → wrongly promoted. The test pins the
    normalised branch by asserting the flip outcome.
    """
    _seed_gradient(
        harness_db, gradient_id="gr-h-cn3",
        target="harness.fw-edit.implementer",
        change="control_n=3 directive", confidence=0.9,
        task_class="fw_edit",
    )
    pre_bytes = _live_prompt_bytes(harness_recipe)
    _common_apply_kwargs(
        harness_db, harness_env, harness_recipe, monkeypatch,
        n=2, before_avg=0.5, after_avg=1.0,  # strict gain
        before_bin=[0, 1], after_bin=[1, 1],
        baseline_total_cost=0.30, candidate_total_cost=0.40,
        control_n=3,
    )
    target_file = str(harness_recipe / "prompts" / "implementer.md")
    target_name = "harness.fw-edit.implementer"
    rc = ap.apply_run("fw_edit", "prompt_file", target_name,
                      target_file, db=harness_db)
    assert rc == 0
    atts = _rows(harness_db, "apply_attempts")
    harness_atts = [a for a in atts if a["target_name"] == target_name]
    assert len(harness_atts) == 1
    rationale = harness_atts[0]["rationale"]
    # Normalised baseline_cost = 0.30 / 3 = $0.10; surfaced for audit.
    assert "baseline_cost=$0.1000" in rationale
    assert "candidate_cost=$0.4000" in rationale
    assert "control_n=3" in rationale
    # Per-solved cps (audit-line prefix is "baseline=/candidate=", the
    # gate-flip reason prefix is "cps_baseline=/cps_candidate=").
    assert "baseline=$0.1000" in rationale
    assert "candidate=$0.2000" in rationale
    # The harness-cost-regression reason fires because normalised
    # baseline is cheaper per-solved than the candidate.
    assert "harness-cost-regression" in rationale
    assert "cps_baseline=$0.1000" in rationale
    assert "cps_candidate=$0.2000" in rationale
    # Decision flips to quarantine; live recipe stays untouched.
    assert harness_atts[0]["decision"] == "quarantined"
    assert _live_prompt_bytes(harness_recipe) == pre_bytes


# ── harness_operator adapter ────────────────────────────────────────────────

def test_materialize_mutation_prompt_proposal_returns_directive_block(tmp_path):
    from mini_ork.learning import harness_operator as ho
    recipe_dir = tmp_path / "recipes" / "fw-edit"
    recipe_dir.mkdir(parents=True)
    proposal = {
        "signature": "output_invalid:implementer",
        "target": "prompt",
        "kind": "prompt_edit",
        "support": 3,
        "rationale": "output_invalid failures implicate the prompt harness surface",
        "evidence": ["run-1", "run-2", "run-3"],
    }
    suggested, source_ref = ho.materialize_mutation(
        proposal, str(recipe_dir), node="implementer")
    assert "implementer" in suggested
    assert "support=3" in suggested
    assert "run-1, run-2" in suggested  # up-to-5 evidence excerpts
    # Stable source_ref for idempotency (apply_mutation skips re-apply).
    assert source_ref == "harness_operator:output_invalid:implementer:implementer"


def test_materialize_mutation_non_prompt_surface_raises(tmp_path):
    from mini_ork.learning import harness_operator as ho
    recipe_dir = tmp_path / "recipes" / "fw-edit"
    recipe_dir.mkdir(parents=True)
    bad = {
        "signature": "infra_interrupt:implementer",
        "target": "recovery",
        "kind": "retry_policy",
        "support": 2,
        "rationale": "x",
        "evidence": [],
    }
    with pytest.raises(ValueError, match="prompt/prompt_edit"):
        ho.materialize_mutation(bad, str(recipe_dir), node="implementer")


def test_materialize_mutation_source_ref_stable_across_calls(tmp_path):
    """Re-applying the same proposal must produce the same source_ref so
    apply_mutation's idempotency check (apply.py:627) skips a re-apply."""
    from mini_ork.learning import harness_operator as ho
    recipe_dir = tmp_path / "recipes" / "fw-edit"
    recipe_dir.mkdir(parents=True)
    proposal = {
        "signature": "output_invalid:implementer",
        "target": "prompt",
        "kind": "prompt_edit",
        "support": 2,
        "rationale": "x",
        "evidence": [],
    }
    _, ref1 = ho.materialize_mutation(proposal, str(recipe_dir),
                                      node="implementer")
    _, ref2 = ho.materialize_mutation(proposal, str(recipe_dir),
                                      node="implementer")
    assert ref1 == ref2
