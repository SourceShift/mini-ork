"""Python port of ``bin/mini-ork-apply`` + ``lib/apply.sh`` — close the apply
loop (IMPL-3).

Consumes the highest-confidence pattern_records / emergent_patterns /
gradient_records row for a (task_class, target_kind, target_name) tuple,
materializes it as a workflow_candidates row, scores it on a held-out set,
and ONLY if the non-regression gate clears does it rewrite the target prompt
file + write a version_registry entry. On regression it quarantines the
candidate and writes a promotion_records row explaining the decision.

Strangler-fig parity port. The bash implementation is already mostly inline
``python3 - <<'PY'`` heredocs; this module lifts those blocks verbatim into
functions and replaces the bash control flow (arg parsing, env exports,
command substitution) with equivalent Python. CLI surface (flags, stdout/
stderr contract, exit codes) matches ``bin/mini-ork-apply``.

Public surface (mirrors the bash API one-for-one):

    help_text()                          → bash `_usage` heredoc
    ensure_tables(db=None)               → _apply_ensure_tables
    pick_candidate(tc, tk, tn, db=None)  → apply_pick_candidate
    score_candidate(cid, scorer=None)    → apply_score_candidate
    evaluate_gate(cid, ub, ua,
                  pertask_json="")       → apply_evaluate_gate
    materialize_candidate(...)           → apply_materialize_candidate
    apply_mutation(cid, target_file,
                   new_prompt, db=None)  → apply_apply_mutation
    attempt_record(...)                  → apply_attempt_record
    record_promotion(...)                → apply_record_promotion
    apply_run(tc, tk, tn,
              target_file="", db=None)   → apply_run
    main(argv=None)                      → bin/mini-ork-apply dispatcher

Env contract (identical to bash):
    MO_APPLY_ENABLED=1            master gate (default OFF)
    MO_APPLY_DRY_RUN=1            skip file write + version_registry write
    MO_APPLY_NONREGRESSION_DELTA  default 0.0
    MO_APPLY_REGRESSION_TOLERANCE default 0 (strict per-task no-regression)
    MO_APPLY_PERTASK_JSON         optional {"before":[...],"after":[...]}
    MO_APPLY_MIN_EXAMPLES         default 1
    MO_APPLY_SCORER               mock (default) | gepa
    MO_APPLY_MOCK_BASELINE        mock baseline (score: 0.5; gate: 0.0)
    MO_APPLY_MOCK_DELTA           mock delta (default 0.05)
    MO_APPLY_FORCE_REGRESSION=1   test seam: forces a regression score
    MINI_ORK_REQUIRE_HUMAN_APPROVAL=true  force pending_human_approval

Exit code mapping (mirrors bash exactly):
    0  success (promote, quarantine, and no_candidate all exit 0 — a
       gate-driven quarantine is success: the gate ENFORCED itself)
    1  missing flag value (bash `${2:?msg}` aborts with rc 1)
    2  usage error (unknown flag, unexpected argument, missing --task-class
       or --target)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid

__all__ = [
    "help_text",
    "ensure_tables",
    "pick_candidate",
    "score_candidate",
    "evaluate_gate",
    "materialize_candidate",
    "apply_mutation",
    "attempt_record",
    "record_promotion",
    "apply_run",
    "main",
]

_VALID_TARGET_KINDS = (
    "prompt_file", "agent_prompt", "workflow_node", "workflow_edge",
)

# ─────────────────────────────────────────────────────────────────────────────
# Help text — verbatim copy of bash's `cat <<'EOF' … EOF` block in _usage().
# The heredoc body ends with a blank line before EOF, so the emitted output
# ends with "...default off)\n\n".
# ─────────────────────────────────────────────────────────────────────────────
USAGE_TEXT = (
    "Usage: bin/mini-ork apply --task-class <name> --target <file>\n"
    "                          [--target-kind prompt_file|agent_prompt|workflow_node|workflow_edge]\n"
    "                          [--dry-run] [--scorer mock|gepa] [--enable]\n"
    "\n"
    "Close the apply loop: turn the highest-confidence proposed prompt change\n"
    "into a scored workflow_candidate, gated by a non-regression rule, and\n"
    "either rewrite the prompt file (on promote) or quarantine with reason\n"
    "(on regression).\n"
    "\n"
    "Defaults:\n"
    "  --target-kind  prompt_file\n"
    "  --scorer       mock\n"
    "  --dry-run      off unless MO_APPLY_DRY_RUN=1\n"
    "\n"
    "Flags:\n"
    "  --enable        Set MO_APPLY_ENABLED=1 for this call (master gate; default off)\n"
    "\n"
)


def help_text() -> str:
    """Return the bash `_usage` heredoc body verbatim."""
    return USAGE_TEXT


# ─────────────────────────────────────────────────────────────────────────────
# DB plumbing (mirrors `${MINI_ORK_DB:?MINI_ORK_DB unset}` + _apply_ensure_tables)
# ─────────────────────────────────────────────────────────────────────────────
_SCHEMA_INIT = False

_APPLY_ATTEMPTS_DDL = """
    CREATE TABLE IF NOT EXISTS apply_attempts (
        attempt_id              TEXT PRIMARY KEY,
        task_class              TEXT NOT NULL,
        target_kind             TEXT NOT NULL CHECK (target_kind IN
                                            ('workflow_node','workflow_edge','agent_prompt','prompt_file')),
        target_name             TEXT NOT NULL,
        source_kind             TEXT NOT NULL CHECK (source_kind IN
                                            ('pattern_records','emergent_patterns',
                                             'gradient_records','synthesis_gate_verdict')),
        source_id               TEXT,
        candidate_id            TEXT REFERENCES workflow_candidates(candidate_id) ON DELETE SET NULL,
        promotion_id            TEXT REFERENCES promotion_records(promotion_id) ON DELETE SET NULL,
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
        created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    );
"""


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if not env:
        raise RuntimeError("MINI_ORK_DB unset")
    return env


def ensure_tables(db: str | None = None) -> None:
    """Mirror ``_apply_ensure_tables`` (idempotent; process-wide guard)."""
    global _SCHEMA_INIT
    if _SCHEMA_INIT:
        return
    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    # apply_attempts is already created by migration 0048 at db init. This
    # lib-only idempotent guard covers the case where the migration hasn't
    # been applied yet (e.g. mini-ork is sourced into a freshly cloned repo
    # before `mini-ork init`).
    con.executescript(_APPLY_ATTEMPTS_DDL)
    con.commit()
    con.close()
    _SCHEMA_INIT = True


def _now() -> str:
    """Bash used time.strftime('%Y-%m-%dT%H:%M:%fZ', time.gmtime()) — the %f
    is NOT a strftime directive, so it stays a literal '%f' in the output.
    Kept verbatim for parity."""
    return time.strftime("%Y-%m-%dT%H:%M:%fZ", time.gmtime())


# ─────────────────────────────────────────────────────────────────────────────
# apply_pick_candidate
# ─────────────────────────────────────────────────────────────────────────────
def pick_candidate(task_class: str, target_kind: str, target_name: str,
                   db: str | None = None) -> str:
    """Pick the highest-confidence source pattern for the tuple.

    Sources, in priority order: pattern_records (output_type='prompt_change',
    status='observed') → emergent_patterns (status='proposed') →
    gradient_records (fallback). Returns a single JSON line, or "" when
    nothing qualifies (bash echoes an empty line).
    """
    ensure_tables(db)
    con = sqlite3.connect(_db_path(db))
    con.row_factory = sqlite3.Row
    try:
        # Priority 1: pattern_records. The real schema has no suggested_change
        # column — the proposed change text IS the `description` field.
        row = con.execute("""
            SELECT pattern_id AS id, description, frequency,
                   CAST(frequency AS REAL) /
                     NULLIF((SELECT MAX(frequency) FROM pattern_records
                             WHERE output_type='prompt_change'), 0) AS confidence
            FROM pattern_records
            WHERE output_type='prompt_change'
              AND status='observed'
              AND (description LIKE ? OR description LIKE ?)
            ORDER BY frequency DESC, last_seen DESC
            LIMIT 1
        """, (f"%{target_name}%", f"%{task_class}%")).fetchone()
        if row is not None:
            return json.dumps({
                "source_kind": "pattern_records",
                "source_id": row["id"],
                "confidence": float(row["confidence"] or 0.0),
                "suggested_change": row["description"],
                "frequency": int(row["frequency"]),
            })

        # Priority 2: emergent_patterns. Column-of-record is cluster_label.
        row = con.execute("""
            SELECT pattern_id AS id, cluster_label, suggested_meta_adr, strength_score
            FROM emergent_patterns
            WHERE status='proposed'
              AND (cluster_label LIKE ? OR suggested_meta_adr LIKE ?
                   OR cluster_label LIKE ? OR suggested_meta_adr LIKE ?)
            ORDER BY strength_score DESC, detected_at DESC
            LIMIT 1
        """, (f"%{target_name}%", f"%{target_name}%",
              f"%{task_class}%", f"%{task_class}%")).fetchone()
        if row is not None:
            return json.dumps({
                "source_kind": "emergent_patterns",
                "source_id": row["id"],
                "confidence": float(row["strength_score"] or 0.0),
                "suggested_change": row["suggested_meta_adr"] or row["cluster_label"] or "",
                "cluster_label": row["cluster_label"],
            })

        # Priority 3: gradient_records fallback — only as last resort.
        row = con.execute("""
            SELECT gradient_id AS id, suggested_change, signal, confidence,
                   row_number() OVER (ORDER BY confidence DESC) AS rank
            FROM gradient_records
            WHERE task_class=?
            ORDER BY confidence DESC
            LIMIT 1
        """, (task_class,)).fetchone()
        if row is not None:
            return json.dumps({
                "source_kind": "gradient_records",
                "source_id": row["id"],
                "confidence": float(row["confidence"] or 0.0),
                "suggested_change": row["suggested_change"],
                "signal": row["signal"],
            })

        # Nothing picked.
        return ""
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# apply_score_candidate
# ─────────────────────────────────────────────────────────────────────────────
def score_candidate(candidate_id: str, scorer: str | None = None) -> str:
    """Score a candidate on a held-out set.

    Returns "<avg_utility_score> <n_examples>" (two floats on one line, like
    bash's stdout). Default scorer is a deterministic mock; ``gepa`` delegates
    to mini_ork.optimize.gepa (placeholder wiring, mirrors bash); an unknown
    scorer returns a neutral "0.5 1".
    """
    if scorer is None:
        scorer = os.environ.get("MO_APPLY_SCORER", "mock")

    if scorer == "mock":
        # Deterministic score derived from candidate_id hash so tests get
        # stable values without invoking a model.
        baseline = float(os.environ.get("MO_APPLY_MOCK_BASELINE", "0.5"))
        delta = float(os.environ.get("MO_APPLY_MOCK_DELTA", "0.05"))
        h = int(hashlib.sha256(candidate_id.encode()).hexdigest(), 16)
        score = baseline + delta + ((h % 1000) / 1000.0 - 0.5) * 0.05
        score = max(0.0, min(1.0, score))
        n = 5
        # Allow tests to force a regression by setting MO_APPLY_FORCE_REGRESSION=1
        if os.environ.get("MO_APPLY_FORCE_REGRESSION") == "1":
            score = max(0.0, baseline - 0.10 - delta)
        return f"{score:.4f} {n}"

    if scorer == "gepa":
        # Real evaluator (IMPL-2). Placeholder wiring identical to bash:
        # defer to held_out_score when the candidate has traces, otherwise
        # echo the mock scores so the gate still runs.
        try:
            from mini_ork.optimize.gepa import held_out_score  # type: ignore  # noqa: F401
            return "0.5 1"  # see IMPL-3 follow-up to wire real batch lookup
        except Exception:
            return "0.5 1"

    return "0.5 1"  # unknown scorer → neutral


# ─────────────────────────────────────────────────────────────────────────────
# apply_evaluate_gate
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_gate(candidate_id: str, utility_before: float,
                  utility_after: float, pertask_json: str = "") -> str:
    """Apply the non-regression gate to a candidate with two utility numbers.

    Returns a JSON line: {"decision":"promoted"|"quarantined"|
    "pending_human_approval"|"rejected", "rationale":"...", ...}.

    Candidates are only PROMOTED when utility_after >= utility_before (no
    regression), with a configurable delta threshold. Below threshold →
    quarantined with a recorded reason (auditable) rather than rejected
    outright. When per-task before/after vectors are supplied
    (MO_APPLY_PERTASK_JSON), previously-PASSING held-out tasks that now FAIL
    block promotion past MO_APPLY_REGRESSION_TOLERANCE regardless of the
    aggregate (arXiv 2607.14004).
    """
    del candidate_id  # bash accepted it positionally but never used it
    ub = float(utility_before)
    ua = float(utility_after)
    dt = float(os.environ.get("MO_APPLY_NONREGRESSION_DELTA", "0.0"))
    me = int(os.environ.get("MO_APPLY_MIN_EXAMPLES", "1"))
    ha = os.environ.get("MINI_ORK_REQUIRE_HUMAN_APPROVAL", "false").lower() == "true"
    regress_tol = int(os.environ.get("MO_APPLY_REGRESSION_TOLERANCE", "0"))

    delta = ua - ub

    # ── in-loop no-regression gate (RELAI-VCL / arXiv 2607.14004) ──────────
    # regressed == -1 means no per-task data was supplied, so we fall back to
    # the scalar aggregate rule (byte-identical to the legacy behavior).
    regressed = -1
    regressed_ids = []
    if pertask_json:
        try:
            pt = json.loads(pertask_json)
            before = pt.get("before", []) or []
            after = pt.get("after", []) or []
            ids = pt.get("ids", list(range(min(len(before), len(after)))))
            regressed = 0
            for i in range(min(len(before), len(after))):
                if before[i] and not after[i]:      # pass → fail == a regression
                    regressed += 1
                    if i < len(ids):
                        regressed_ids.append(ids[i])
        except Exception:
            regressed = -1  # malformed vector → treat as absent, never crash the gate

    has_pertask_regression = regressed > regress_tol

    # ── decision rule ───────────────────────────────────────────────────────
    if has_pertask_regression:
        # No-regression gate FIRES: block even when the aggregate improved.
        decision = "quarantined"
        _ids = f" [{','.join(map(str, regressed_ids))}]" if regressed_ids else ""
        rationale = (f"per-task no-regression gate: {regressed} previously-solved held-out "
                     f"task(s) now fail (tolerance={regress_tol}); aggregate delta was "
                     f"{delta:+.4f} but the candidate regresses solved work{_ids} "
                     f"(2607.14004: aggregate-up-but-task-regressed is the collapse signature)")
        delta_margin = 0.0
        needs_human = False
    elif delta >= dt:
        decision = "promoted"
        rationale = (f"non-regression cleared: utility_after={ua:.4f} >= "
                     f"utility_before={ub:.4f} (delta={delta:+.4f} >= threshold={dt:+.4f})")
        if regressed == 0:
            rationale += "; 0 per-task regressions"
        delta_margin = 0.0
        needs_human = False
    elif abs(delta - dt) < 0.02 and not ha:
        # Utility is within measurement noise of the baseline AND we are NOT
        # already in a human-approval-required mode → ask for human review.
        decision = "pending_human_approval"
        rationale = f"ambiguous delta={delta:+.4f} (threshold={dt:+.4f}); requesting human review"
        delta_margin = 0.0
        needs_human = True
    else:
        decision = "quarantined"
        rationale = (f"regression: utility_after={ua:.4f} < utility_before={ub:.4f} "
                     f"(delta={delta:+.4f} < threshold={dt:+.4f})")
        delta_margin = 0.0
        needs_human = False

    if needs_human or ha:
        # Honor human-approval override at the very end so override beats all rules.
        decision = "pending_human_approval"
        rationale = "human approval required (MINI_ORK_REQUIRE_HUMAN_APPROVAL=true)"

    result = {
        "decision": decision,
        "rationale": rationale,
        "utility_before": round(ub, 6),
        "utility_after": round(ua, 6),
        "utility_delta": round(delta - delta_margin, 6),
        "threshold": dt,
        "min_examples": me,
        "regressed_tasks": regressed,          # -1 = no per-task data (scalar-only path)
        "regression_tolerance": regress_tol,
        "needs_human": needs_human or ha,
    }
    return json.dumps(result)


# ─────────────────────────────────────────────────────────────────────────────
# apply_materialize_candidate
# ─────────────────────────────────────────────────────────────────────────────
def materialize_candidate(task_class: str, target_kind: str, target_name: str,
                          source_kind: str, source_id: str,
                          suggested_change: str, db: str | None = None) -> str:
    """Materialize a picked source pattern as a workflow_candidates row whose
    mutations JSON encodes the concrete prompt change. Returns the new
    candidate_id. Pure DB write — no file I/O."""
    ensure_tables(db)
    cid = f"cand-{uuid.uuid4().hex[:16]}"

    # Find the active base workflow version (if any). NULL is acceptable for
    # pure prompt_file targets where the lifecycle lives at the file level.
    con = sqlite3.connect(_db_path(db))
    try:
        base_row = con.execute("""
            SELECT workflow_version_id FROM workflow_memory
            WHERE status='stable'
            ORDER BY created_at DESC LIMIT 1
        """).fetchone()
        base_vid = base_row[0] if base_row else "wf-baseline-no-row"

        mutations = json.dumps([{
            "kind": "prompt_change",
            "node_name": target_name,
            "field": "system_prompt",
            "old_val": None,
            "new_val": suggested_change,
            "source_kind": source_kind,
            "source_id": source_id or None,
            "task_class": task_class,
        }])

        now = _now()
        try:
            con.execute("""
                INSERT INTO workflow_candidates
                    (candidate_id, base_workflow_version_id, mutations,
                     status, created_by, created_at)
                VALUES (?,?,?, 'candidate', 'evolution_engine', ?)
            """, (cid, base_vid, mutations, now))
            con.commit()
            return cid
        except sqlite3.IntegrityError:
            # base_vid doesn't actually exist in workflow_memory (FK
            # violation). Retry with the null-equivalent synthetic id so the
            # apply loop can still run for prompt-only targets without a
            # workflow row.
            base_vid = "wf-synthetic-baseline"
            # workflow_memory uses workflow_name (not name) per migration 0009.
            con.execute("""
                INSERT OR IGNORE INTO workflow_memory
                    (workflow_version_id, workflow_name, yaml_hash, yaml_blob, status, created_at)
                VALUES (?, 'synthetic_baseline', 'sha256-synthetic', 'synthetic', 'retired', ?)
            """, (base_vid, now))
            con.execute("""
                INSERT INTO workflow_candidates
                    (candidate_id, base_workflow_version_id, mutations,
                     status, created_by, created_at)
                VALUES (?,?,?, 'candidate', 'evolution_engine', ?)
            """, (cid, base_vid, mutations, now))
            con.commit()
            return cid
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# apply_apply_mutation
# ─────────────────────────────────────────────────────────────────────────────
def apply_mutation(candidate_id: str, target_file: str, new_prompt: str,
                   db: str | None = None) -> str:
    """On PROMOTED decisions, rewrite the target prompt file and write a
    version_registry row. NO-OP (returns "") unless MO_APPLY_ENABLED=1 and
    MO_APPLY_DRY_RUN is unset/0. Returns the version_id ("" when skipped or
    when the version register call fails — bash's `|| true` swallows it)."""
    apply_enabled = os.environ.get("MO_APPLY_ENABLED", "0")
    dry_run = os.environ.get("MO_APPLY_DRY_RUN", "0")

    if dry_run == "1" or apply_enabled != "1":
        sys.stderr.write(
            f"apply_apply_mutation: dry-run (apply_enabled={apply_enabled} "
            f"dry_run={dry_run}); no file write\n"
        )
        return ""

    # Snapshot the previous file content into a rollback handle BEFORE writing.
    prev_hash = ""
    if os.path.isfile(target_file):
        with open(target_file, "rb") as fh:
            prev_hash = hashlib.sha256(fh.read()).hexdigest()
        shutil.copyfile(target_file, f"{target_file}.apply-rollback-{os.getpid()}")

    # Write the new prompt content (bash: printf '%s\n' > file).
    try:
        with open(target_file, "w") as fh:
            fh.write(f"{new_prompt}\n")
    except OSError:
        sys.stderr.write(f"apply_apply_mutation: FAILED to write {target_file}\n")
        raise

    # Record the version. kind='agent' because prompt rewrites are agent-side
    # changes. The payload carries the prior hash + target path so
    # version_rollback can recover. Bash sourced lib/version_registry.sh with
    # `|| true` and skipped the call when unavailable — mirror by swallowing
    # any failure and returning "".
    version_id = ""
    try:
        from mini_ork.registries import version_registry
        payload = json.dumps({
            "name": target_file,
            "version_id": None,
            "status": "stable",
            "utility_score": 0.0,
            "rollback_hash": prev_hash,
            "candidate_id": candidate_id,
            "target_path": target_file,
        })
        version_id = version_registry.register("agent", payload, db=db)
    except Exception:
        version_id = ""

    return version_id


# ─────────────────────────────────────────────────────────────────────────────
# apply_attempt_record
# ─────────────────────────────────────────────────────────────────────────────
def attempt_record(task_class: str, target_kind: str, target_name: str,
                   source_kind: str, source_id: str,
                   candidate_id: str, promotion_id: str, base_wf_version: str,
                   utility_before, utility_after, utility_delta,
                   decision: str, rationale: str,
                   dry_run, apply_enabled, db: str | None = None) -> str:
    """Persist an apply_attempts row. Returns the attempt_id (bash prints it
    on stdout; apply_run suppresses it in the normal path but not in the
    no_candidate path)."""
    ensure_tables(db)
    aid = f"apply-{uuid.uuid4().hex[:16]}"
    now = _now()
    con = sqlite3.connect(_db_path(db))
    try:
        con.execute("""
            INSERT INTO apply_attempts
                (attempt_id, task_class, target_kind, target_name,
                 source_kind, source_id, candidate_id, promotion_id,
                 base_workflow_version_id,
                 utility_before, utility_after, utility_delta,
                 decision, rationale, dry_run, apply_enabled, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (aid, task_class, target_kind, target_name,
              source_kind, source_id or None,
              candidate_id or None, promotion_id or None, base_wf_version or None,
              float(utility_before) if utility_before not in (None, "") else None,
              float(utility_after) if utility_after not in (None, "") else None,
              float(utility_delta) if utility_delta not in (None, "") else None,
              decision, rationale, int(dry_run), int(apply_enabled), now))
        con.commit()
    finally:
        con.close()
    return aid


# ─────────────────────────────────────────────────────────────────────────────
# apply_record_promotion
# ─────────────────────────────────────────────────────────────────────────────
def record_promotion(candidate_id: str, utility_before, utility_after,
                     decision: str, rationale: str, db: str | None = None) -> str:
    """Record a workflow_candidates promotion decision via the existing
    promotion_records audit table. Returns the promotion_id."""
    ensure_tables(db)
    con = sqlite3.connect(_db_path(db))
    con.row_factory = sqlite3.Row
    try:
        base_vid = "wf-synthetic-baseline"
        row = con.execute(
            "SELECT base_workflow_version_id FROM workflow_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if row and row["base_workflow_version_id"]:
            base_vid = row["base_workflow_version_id"]
        pid = f"pr-{uuid.uuid4().hex[:16]}"
        now = _now()
        con.execute("""
            INSERT INTO promotion_records
                (promotion_id, candidate_id, from_version_id, to_version_id,
                 utility_before, utility_after, benchmark_run_id,
                 rationale, decision, decided_at, decided_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (pid, candidate_id, base_vid, base_vid,
              float(utility_before), float(utility_after), None,
              rationale, decision, now, 'gate'))
        con.commit()
    finally:
        con.close()
    return pid


# ─────────────────────────────────────────────────────────────────────────────
# apply_run — top-level orchestrator
# ─────────────────────────────────────────────────────────────────────────────
def apply_run(task_class: str, target_kind: str, target_name: str,
              target_file: str = "", db: str | None = None) -> int:
    """Run pick → materialize → score → gate → write (or quarantine).

    Writes a JSON summary line on stdout (suitable for log capture). Returns
    0 on a no-op run and on a gate-driven quarantine (quarantine is success —
    the whole point is the gate ENFORCED itself).
    """
    ensure_tables(db)

    apply_enabled = os.environ.get("MO_APPLY_ENABLED", "0")
    dry_run = os.environ.get("MO_APPLY_DRY_RUN", "0")
    dry_flag = "1" if dry_run == "1" else "0"

    # 1. Pick the source pattern.
    picked = pick_candidate(task_class, target_kind, target_name, db=db)
    if not picked or picked == "null":
        # bash lets this attempt_record's stdout (the attempt id) through —
        # only the normal path below is redirected to /dev/null.
        aid = attempt_record(
            task_class, target_kind, target_name,
            "none", "", "", "", "", "", "", "", "no_candidate",
            f"no qualifying source pattern for ({target_kind}, {target_name})",
            dry_flag, apply_enabled, db=db)
        sys.stdout.write(aid + "\n")
        sys.stdout.write(
            f'{{"decision":"no_candidate","task_class":"{task_class}",'
            f'"target":"{target_name}"}}\n'
        )
        return 0

    parsed = json.loads(picked)
    source_kind = parsed["source_kind"]
    source_id = parsed.get("source_id", "")
    confidence = parsed.get("confidence", 0.0)
    suggested_change = parsed.get("suggested_change", "")

    # 2. Materialize the candidate.
    candidate_id = materialize_candidate(
        task_class, target_kind, target_name,
        source_kind, source_id, suggested_change, db=db)

    # 3. Score. utility_before = baseline utility of the CURRENT prompt so
    #    the non-regression gate compares against the real baseline.
    utility_before = "0.0"
    if os.environ.get("MO_APPLY_SCORER", "mock") == "mock":
        utility_before = os.environ.get("MO_APPLY_MOCK_BASELINE", "0.0")
    score_out = score_candidate(candidate_id)
    parts = score_out.split()
    utility_after = parts[0] if parts else ""

    # 4. Gate. Pass the optional per-task held-out vector so the in-loop
    #    no-regression gate can block a candidate that regresses a
    #    previously-solved task even when the aggregate improved (2607.14004).
    gate_json = evaluate_gate(
        candidate_id, float(utility_before), float(utility_after),
        os.environ.get("MO_APPLY_PERTASK_JSON", ""))
    gate = json.loads(gate_json)
    gate_decision = gate["decision"]
    gate_rationale = gate["rationale"]
    utility_delta = gate["utility_delta"]

    # 5. Promotion record (audit). For quarantined / pending_human_approval
    #    decisions the promotion row still exists (it's the audit trail of
    #    why we did NOT promote).
    promotion_id = record_promotion(
        candidate_id, utility_before, utility_after,
        gate_decision, gate_rationale, db=db)

    # 6. Apply (only on PROMOTED + apply_enabled + !dry_run + target_file set).
    version_id = ""
    if gate_decision == "promoted" and target_file and suggested_change:
        try:
            version_id = apply_mutation(candidate_id, target_file, suggested_change, db=db)
        except OSError:
            # bash: `version_id=$(apply_apply_mutation ... || true)` swallows
            # the write-failure rc; the flow continues with an empty id.
            version_id = ""

    # 7. Apply-attempt audit row (stdout suppressed in bash via > /dev/null).
    attempt_record(
        task_class, target_kind, target_name,
        source_kind, source_id, candidate_id, promotion_id, "",
        utility_before, utility_after, utility_delta,
        gate_decision, gate_rationale,
        dry_flag, apply_enabled, db=db)

    sys.stdout.write(
        f'{{"decision":"{gate_decision}","candidate_id":"{candidate_id}",'
        f'"promotion_id":"{promotion_id}","version_id":"{version_id}",'
        f'"confidence":{confidence},"dry_run":"{dry_run}"}}\n'
    )
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# bin/mini-ork-apply dispatcher — mirrors bash arg parsing + env flow exactly.
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_root() -> str:
    root = os.environ.get("MINI_ORK_ROOT") or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    os.environ["MINI_ORK_ROOT"] = root
    return root


def _resolve_target_name(target: str, root: str) -> str:
    """Bash `_resolve_target_name`: strip the MINI_ORK_ROOT/ prefix when the
    target lives under the engine root so the picker resolves the same row
    regardless of cwd."""
    prefix = root + "/"
    if target.startswith(prefix):
        return target[len(prefix):]
    return target


def main(argv: list[str] | None = None) -> int:
    """CLI dispatcher. Returns the exit code (mirrors bin/mini-ork-apply)."""
    if argv is None:
        argv = sys.argv[1:]

    root = _resolve_root()

    # ── arg parsing (bash `while/case` loop) ────────────────────────────────
    task_class = ""
    target = ""
    target_kind = "prompt_file"
    dry_run = os.environ.get("MO_APPLY_DRY_RUN", "0")
    scorer = os.environ.get("MO_APPLY_SCORER", "mock")
    enable_now = False

    def _missing_value(flag: str) -> int:
        # bash `${2:?--flag requires a value}` aborts the script with rc 1.
        sys.stderr.write(f"{flag} requires a value\n")
        return 1

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            sys.stdout.write(USAGE_TEXT)
            return 0
        if arg in ("--task-class", "--target", "--target-kind", "--scorer"):
            if i + 1 >= len(argv) or argv[i + 1] == "":
                return _missing_value(arg)
            value = argv[i + 1]
            if arg == "--task-class":
                task_class = value
            elif arg == "--target":
                target = value
            elif arg == "--target-kind":
                target_kind = value
            else:
                scorer = value
            i += 2
            continue
        if arg == "--dry-run":
            dry_run = "1"
            i += 1
            continue
        if arg == "--enable":
            enable_now = True
            i += 1
            continue
        if arg.startswith("-"):
            sys.stderr.write(f"Unknown flag: {arg}\n")
            sys.stderr.write(USAGE_TEXT)
            return 2
        sys.stderr.write(f"Unexpected argument: {arg}\n")
        return 2

    if not task_class:
        sys.stderr.write("--task-class is required\n")
        sys.stderr.write(USAGE_TEXT)
        return 2
    if not target:
        sys.stderr.write("--target is required\n")
        sys.stderr.write(USAGE_TEXT)
        return 2

    # Honor --enable / --dry-run precedence: --enable lifts the master gate,
    # --dry-run keeps the file write off regardless.
    if enable_now:
        os.environ["MO_APPLY_ENABLED"] = "1"
    if dry_run == "1":
        os.environ["MO_APPLY_DRY_RUN"] = "1"
    os.environ["MO_APPLY_SCORER"] = scorer

    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    os.environ["MINI_ORK_HOME"] = home
    os.environ["MINI_ORK_DB"] = db

    target_name = _resolve_target_name(target, root)

    sys.stdout.write("=== mini-ork apply ===\n")
    sys.stdout.write(f"    task_class: {task_class}\n")
    sys.stdout.write(f"    target:     {target}\n")
    sys.stdout.write(f"    target_kind:{target_kind}\n")
    sys.stdout.write(f"    scorer:     {scorer}\n")
    sys.stdout.write(f"    apply_enabled: {os.environ.get('MO_APPLY_ENABLED', '0')}\n")
    sys.stdout.write(f"    dry_run:    {os.environ.get('MO_APPLY_DRY_RUN', '0')}\n")
    sys.stdout.write("\n")

    return apply_run(task_class, target_kind, target_name, target, db=db)


if __name__ == "__main__":
    raise SystemExit(main())
