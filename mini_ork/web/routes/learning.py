"""The brain, exposed.

An audit of state.db against the router surface found that 28 of 37 populated
tables were DARK — queried by no endpoint. Among them the learning loop's most
load-bearing artifacts: the contextual bandit's learned lane policy
(lane_domain_advantage / lane_region_advantage), GEPA's outcomes
(prompt_win_rates / promotion_records), and the failure memory (2,009 failure_links).

Gradients were visible; whether a gradient ever WON or got PROMOTED was not. That
is the difference between watching a loop run and watching it learn.

Every SELECT below names columns read off `PRAGMA table_info` — none are guessed.
Every handler is has_table-guarded, so a fresh state.db returns [] rather than 500.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..db import StateDB
from ..deps import get_db
from ..repositories import LearningRepository

router = APIRouter(prefix="/api/v1/learning", tags=["learning"])


# ── the contextual bandit — what the router actually learned ─────────────────


@router.get("/bandit")
def bandit_policy(db: StateDB = Depends(get_db), limit: int = 200) -> dict[str, Any]:
    """The learned lane policy.

    `relative_advantage` is the group-relative advantage per
    (agent_version, task_class, node_type, objective_domain[, code_region]).
    This is the policy the router reads at dispatch — i.e. the thing that decides
    whether a cheap lane is trusted with a task.

    `region` rows are the finer partition (they add code_region); `domain` rows are
    the coarse fallback used when a region has too few samples.
    """
    domain: list[dict[str, Any]] = []
    region: list[dict[str, Any]] = []

    if db.has_table("lane_domain_advantage"):
        domain = db.rows(
            """
            SELECT agent_version_id, task_class, node_type, objective_domain,
                   relative_advantage, runs_count, success_count, last_updated
            FROM lane_domain_advantage
            ORDER BY relative_advantage DESC
            LIMIT ?
            """,
            (limit,),
        )

    if db.has_table("lane_region_advantage"):
        region = db.rows(
            """
            SELECT agent_version_id, task_class, node_type, objective_domain,
                   code_region, relative_advantage, runs_count, success_count,
                   last_updated
            FROM lane_region_advantage
            ORDER BY relative_advantage DESC
            LIMIT ?
            """,
            (limit,),
        )

    return {"domain": domain, "region": region}


# ── GEPA — not just the gradients, but whether they won ──────────────────────


@router.get("/gepa")
def gepa_state(db: StateDB = Depends(get_db), limit: int = 100) -> dict[str, Any]:
    """Reflective prompt evolution: proposals, their win rates, and promotions.

    `gradient_records` (already exposed at /trajectory/gradients) is the PROPOSAL
    stream. What was missing is the OUTCOME: which prompt versions won head-to-head
    (prompt_win_rates) and which were actually promoted into the live agent
    (promotion_records, with utility_before/after). A loop that proposes but never
    promotes is not learning — and until now you could not tell the difference.
    """
    win_rates: list[dict[str, Any]] = []
    promotions: list[dict[str, Any]] = []

    if db.has_table("prompt_win_rates"):
        win_rates = db.rows(
            """
            SELECT prompt_version_hash, task_class, node_type, agent_role,
                   wins, losses, ties, win_rate, sample_size, last_updated
            FROM prompt_win_rates
            ORDER BY sample_size DESC, win_rate DESC
            LIMIT ?
            """,
            (limit,),
        )

    if db.has_table("promotion_records"):
        promotions = db.rows(
            """
            SELECT promotion_id, candidate_id, from_version_id, to_version_id,
                   utility_before, utility_after, benchmark_run_id, rationale,
                   decision, decided_at, decided_by
            FROM promotion_records
            ORDER BY decided_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    gradient_count = LearningRepository(db).gradient_count()

    return {
        "gradient_count": gradient_count,
        "win_rates": win_rates,
        "promotions": promotions,
    }


# ── failure memory — the largest dark dataset in the db ──────────────────────


@router.get("/failures")
def failure_memory(db: StateDB = Depends(get_db), limit: int = 100) -> dict[str, Any]:
    """What the loop has failed at, and how often. 2k+ rows, previously unreadable."""
    recent: list[dict[str, Any]] = []
    by_category: list[dict[str, Any]] = []

    if db.has_table("failure_memory"):
        recent = db.rows(
            """
            SELECT failure_id, run_id, workflow_stage, failure_category,
                   error_message, occurred_at
            FROM failure_memory
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        by_category = db.rows(
            """
            SELECT failure_category, workflow_stage, COUNT(*) AS count
            FROM failure_memory
            GROUP BY failure_category, workflow_stage
            ORDER BY count DESC
            """
        )

    return {"recent": recent, "by_category": by_category}


# ── emergent patterns — the meta-learning layer ──────────────────────────────


@router.get("/patterns")
def emergent_patterns(db: StateDB = Depends(get_db), limit: int = 100) -> list[dict[str, Any]]:
    """Clusters the system found in its own behaviour, with a suggested meta-ADR."""
    if not db.has_table("emergent_patterns"):
        return []
    return db.rows(
        """
        SELECT pattern_id, cluster_label, strength_score, suggested_meta_adr,
               status, detected_at, resolved_at
        FROM emergent_patterns
        ORDER BY strength_score DESC
        LIMIT ?
        """,
        (limit,),
    )


# ── topology — the panel shape is learned too ────────────────────────────────


@router.get("/topology")
def topology(db: StateDB = Depends(get_db), limit: int = 100) -> dict[str, Any]:
    """Which review-panel topologies win, at what cost and latency."""
    win_rates: list[dict[str, Any]] = []
    role_evolution: list[dict[str, Any]] = []
    telemetry: list[dict[str, Any]] = []

    if db.has_table("topology_win_rates"):
        win_rates = db.rows(
            """
            SELECT topology_id, workflow_name, task_class, wins, losses, ties,
                   win_rate, sample_size, avg_cost_usd, avg_duration_ms, last_updated
            FROM topology_win_rates
            ORDER BY sample_size DESC, win_rate DESC
            LIMIT ?
            """,
            (limit,),
        )

    if db.has_table("role_evolver_log"):
        role_evolution = db.rows("SELECT * FROM role_evolver_log ORDER BY rowid DESC LIMIT ?", (limit,))

    if db.has_table("panel_topology_telemetry"):
        # rho / context_distance / inductive_distance place a panel in a quadrant —
        # this is the signal the topology chooser learns from.
        telemetry = db.rows(
            """
            SELECT telemetry_id, panel_run_id, recipe, rho, context_distance,
                   inductive_distance, agent_count, n_traces, target_topology,
                   quadrant, computed_at
            FROM panel_topology_telemetry
            ORDER BY computed_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    return {"win_rates": win_rates, "role_evolution": role_evolution, "telemetry": telemetry}


# ── memory — what the loop remembers about tasks and its own agents ──────────


@router.get("/memory")
def memory(db: StateDB = Depends(get_db), limit: int = 100) -> dict[str, Any]:
    """Task memory (per-run outcomes/cost) + per-agent performance memory."""
    tasks: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []

    if db.has_table("task_memory"):
        tasks = db.rows(
            """
            SELECT id, run_id, task_class, kickoff_hash, outcome,
                   artifacts_produced, duration_ms, cost_usd, created_at
            FROM task_memory
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    if db.has_table("agent_performance_memory"):
        agents = db.rows(
            """
            SELECT agent_version_id, role, model, task_class, runs_count,
                   success_count, avg_cost_usd, avg_duration_ms, top_failure_modes,
                   relative_advantage, last_updated
            FROM agent_performance_memory
            ORDER BY relative_advantage DESC
            LIMIT ?
            """,
            (limit,),
        )

    return {"tasks": tasks, "agents": agents}


# ── gates — what may veto a run, and what it actually vetoed ─────────────────


@router.get("/gates")
def gates(db: StateDB = Depends(get_db), limit: int = 100) -> dict[str, Any]:
    """Registered gates + the rejections they GROUNDED (with evidence trace ids).

    `grounded_rejections` is the anti-Goodhart record: a veto that cites evidence,
    not an opinion. It is the closest thing in the db to a correctness audit trail.
    """
    registry: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    if db.has_table("registered_gates"):
        registry = db.rows(
            """
            SELECT gate_id, gate_type, condition_script_path, task_class_filter,
                   is_safety, registered_at, description
            FROM registered_gates
            ORDER BY is_safety DESC, gate_id
            LIMIT ?
            """,
            (limit,),
        )
    elif db.has_table("gate_registry"):
        registry = db.rows(
            """
            SELECT gate_id, gate_type, condition, task_class_filter, safety,
                   active, registered_at
            FROM gate_registry
            ORDER BY safety DESC, gate_id
            LIMIT ?
            """,
            (limit,),
        )

    if db.has_table("grounded_rejections"):
        rejections = db.rows(
            """
            SELECT id, ts, run_id, gate_name, verdict, concern,
                   evidence_trace_ids, evidence_summary, suggestion,
                   consumed_by_reflector_ts
            FROM grounded_rejections
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        )

    return {"registry": registry, "rejections": rejections}


# ── review — the pre-push panel's findings ──────────────────────────────────


@router.get("/reviews")
def reviews(db: StateDB = Depends(get_db), limit: int = 50) -> dict[str, Any]:
    """Pre-push reviews and the issues each lens raised."""
    reviews_: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    bugs: list[dict[str, Any]] = []

    if db.has_table("pre_push_reviews"):
        reviews_ = db.rows(
            """
            SELECT id, reviewed_at, source_sha, target_branch, reviewer_mode,
                   files_changed, lines_added, lines_removed, verdict,
                   issues_open, issues_critical, fix_epic_id, cost_usd, rationale
            FROM pre_push_reviews
            ORDER BY reviewed_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    if db.has_table("pre_push_review_issues"):
        issues = db.rows(
            """
            SELECT id, review_id, lens, severity, file_path, line_no, title,
                   description, suggested_fix, status, bug_report_id
            FROM pre_push_review_issues
            ORDER BY
              CASE LOWER(severity)
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium' THEN 2 ELSE 3
              END,
              id DESC
            LIMIT ?
            """,
            (limit * 4,),
        )

    if db.has_table("bug_reports"):
        bugs = db.rows(
            """
            SELECT id, fingerprint, run_id, agent_role, task_class, title,
                   severity, confidence, frequency, status, promoted_to_epic_id,
                   first_seen_at, last_seen_at
            FROM bug_reports
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    return {"reviews": reviews_, "issues": issues, "bug_reports": bugs}


# ── conductor — the meta-policy: topology + recipe + lane, and what it got ───


@router.get("/conductor")
def conductor(db: StateDB = Depends(get_db), limit: int = 100) -> dict[str, Any]:
    """The conductor's choices, its PREDICTED score, and the score it REALIZED.

    predicted vs realized is the calibration signal — a conductor that predicts well
    can be trusted to spend budget; one that does not, cannot.
    """
    decisions: list[dict[str, Any]] = []
    spawns: list[dict[str, Any]] = []

    if db.has_table("conductor_decisions"):
        decisions = db.rows(
            """
            SELECT id, decided_at, epic_id, task_class, chosen_topology,
                   chosen_recipe, chosen_lane_hints, predicted_score,
                   budget_pct_used, rationale, outcome, realized_score
            FROM conductor_decisions
            ORDER BY decided_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    if db.has_table("run_spawns"):
        spawns = db.rows(
            """
            SELECT spawn_id, parent_run_id, child_run_id, root_run_id, depth,
                   recipe, authority_level, allow_child_spawn, status, created_at
            FROM run_spawns
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    return {"decisions": decisions, "spawns": spawns}


# ── workflow evolution — the loop rewriting its own workflow ────────────────


@router.get("/workflows")
def workflows(db: StateDB = Depends(get_db), limit: int = 100) -> dict[str, Any]:
    """Candidate workflow mutations, their utility delta, and the promoted versions."""
    candidates: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []

    if db.has_table("workflow_candidates"):
        candidates = db.rows(
            """
            SELECT candidate_id, base_workflow_version_id, mutations, status,
                   benchmark_summary_id, utility_delta, created_by, created_at
            FROM workflow_candidates
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    if db.has_table("workflow_memory"):
        # yaml_blob is deliberately NOT selected — it is large and the panel never
        # renders it. Ship the hash; fetch the blob only if someone asks for it.
        versions = db.rows(
            """
            SELECT workflow_version_id, workflow_name, base_version_id, yaml_hash,
                   mutations, created_at, status, previous_stable_version_id
            FROM workflow_memory
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    return {"candidates": candidates, "versions": versions}


# ── benchmarks — the held-out gate on self-modification ─────────────────────


@router.get("/benchmarks")
def benchmarks(db: StateDB = Depends(get_db), limit: int = 100) -> dict[str, Any]:
    """The benchmark suite that gates promotion, and the results it produced."""
    tasks: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    if db.has_table("benchmark_tasks"):
        tasks = db.rows(
            """
            SELECT benchmark_id, task_class, expected_criteria, success_verifiers,
                   baseline_utility_score, source, created_at
            FROM benchmark_tasks
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    if db.has_table("benchmark_results"):
        results = db.rows(
            """
            SELECT result_id, benchmark_id, candidate_id, run_id, pass,
                   utility_score, evidence_path, ran_at
            FROM benchmark_results
            ORDER BY ran_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    return {"tasks": tasks, "results": results}


# ── research — the arXiv-driven R&D loop ────────────────────────────────────


@router.get("/research")
def research(db: StateDB = Depends(get_db), limit: int = 100) -> list[dict[str, Any]]:
    """Papers the self-improve loop read, and whether the technique reached a patch.

    `used_in_patch` is the honest column: reading a paper is not learning from it.
    """
    if not db.has_table("self_improve_arxiv_refs"):
        return []
    return db.rows(
        """
        SELECT id, run_id, arxiv_id, title, technique, mapped_file, confidence,
               used_in_patch, created_at
        FROM self_improve_arxiv_refs
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


# ── epics — the dependency cascade ──────────────────────────────────────────


@router.get("/epic-dependencies")
def epic_dependencies(db: StateDB = Depends(get_db), limit: int = 500) -> list[dict[str, Any]]:
    """Edges of the epic DAG — what blocks what."""
    if not db.has_table("epic_dependencies"):
        return []
    return db.rows(
        """
        SELECT id, from_epic_id, to_epic_id, kind, created_at, resolved_at
        FROM epic_dependencies
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


# ── governance — the circuit breakers that halt the loop ─────────────────────


@router.get("/circuit-breakers")
def circuit_breakers(db: StateDB = Depends(get_db)) -> list[dict[str, Any]]:
    """Open breakers are why a run silently isn't happening. Make them visible."""
    if not db.has_table("circuit_breaker_state"):
        return []
    return db.rows(
        """
        SELECT scope_key, state, opened_at, last_run_id, last_reason,
               trip_count, updated_at
        FROM circuit_breaker_state
        ORDER BY (state = 'open') DESC, updated_at DESC
        """
    )


# ── one call for the panel header ────────────────────────────────────────────


@router.get("/summary")
def summary(db: StateDB = Depends(get_db)) -> dict[str, Any]:
    """Counts for the Orca panel — one round-trip instead of six."""

    def count(table: str, where: str = "") -> int:
        if not db.has_table(table):
            return 0
        rows = db.rows(f"SELECT COUNT(*) AS n FROM {table} {where}")  # noqa: S608 — fixed table names
        return int(rows[0]["n"]) if rows else 0

    open_breakers = count("circuit_breaker_state", "WHERE state = 'open'")

    promoted = 0
    if db.has_table("promotion_records"):
        rows = db.rows(
            "SELECT COUNT(*) AS n FROM promotion_records WHERE LOWER(decision) IN ('promote','promoted','accept','accepted')"
        )
        promoted = int(rows[0]["n"]) if rows else 0

    return {
        "bandit_arms": count("lane_domain_advantage") + count("lane_region_advantage"),
        "gradients": count("gradient_records"),
        "promotions": count("promotion_records"),
        "promoted": promoted,
        "prompt_versions_scored": count("prompt_win_rates"),
        "failures": count("failure_memory"),
        "patterns": count("emergent_patterns"),
        "topologies": count("topology_win_rates"),
        "open_circuit_breakers": open_breakers,
        "traces": count("execution_traces"),
        "llm_calls": count("llm_calls"),
    }
