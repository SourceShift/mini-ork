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
    MO_APPLY_MODE=append|replace  append = idempotent directive block (default,
                                  F3 2026-09-12); replace = legacy whole-file
    MO_APPLY_DRY_RUN=1            skip file write + version_registry write
    MO_APPLY_NONREGRESSION_DELTA  default 0.0
    MO_APPLY_REGRESSION_TOLERANCE default 0 (strict per-task no-regression)
    MO_APPLY_PERTASK_JSON         optional {"before":[...],"after":[...]}
    MO_APPLY_MIN_EXAMPLES         default 1
    MO_APPLY_SCORER               probe (default) | code | mock | gepa. `code`
                                  scores a PATCH against the framework tree
                                  (MO_APPLY_CODE_PATCH) instead of a directive.
                                  mock/gepa are
                                  TEST-ONLY: they fabricate utility and can
                                  never promote, regardless of env
    MO_APPLY_MOCK_BASELINE        mock baseline (score: 0.5; gate: 0.0)
    MO_APPLY_MOCK_DELTA           mock delta (default 0.05)
    MO_APPLY_PROBE_MAX_TASKS      probe scorer: max probes per eval (default 2)
    MO_APPLY_PROBE_BUDGET_USD     probe scorer: spend ceiling (default 2.0)
    MO_APPLY_PROBE_TIMEOUT_S      probe scorer: per-launch timeout (default 600)
    MO_APPLY_FORCE_REGRESSION=1   test seam: forces a regression score

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

# Scorers that invent a utility number instead of measuring one. They exist as
# test seams only: a promote on fabricated numbers is indistinguishable in the
# audit trail from a measured improvement, and once the human approval gate is
# gone there is no longer any reviewer to catch it. Not configurable by env —
# that is the point.
FABRICATING_SCORERS = ("mock", "gepa")

# ─────────────────────────────────────────────────────────────────────────────
# Help text — verbatim copy of bash's `cat <<'EOF' … EOF` block in _usage().
# The heredoc body ends with a blank line before EOF, so the emitted output
# ends with "...default off)\n\n".
# ─────────────────────────────────────────────────────────────────────────────
USAGE_TEXT = (
    "Usage: bin/mini-ork apply --task-class <name> --target <file>\n"
    "                          [--target-kind prompt_file|agent_prompt|workflow_node|workflow_edge]\n"
    "                          [--dry-run] [--scorer probe|mock|gepa] [--enable]\n"
    "\n"
    "Close the apply loop: turn the highest-confidence proposed prompt change\n"
    "into a scored workflow_candidate, gated by a non-regression rule, and\n"
    "either rewrite the prompt file (on promote) or quarantine with reason\n"
    "(on regression).\n"
    "\n"
    "Scorers:\n"
    "  probe          frozen probe-set held-out evaluation (real runs,\n"
    "                 recipes/<recipe>/probes/*.md) — the only promotable\n"
    "                 scorer; without a measurement the candidate is quarantined\n"
    "  mock           TEST-ONLY deterministic placeholder (fabricates utility,\n"
    "                 never promotes)\n"
    "  gepa           TEST-ONLY neutral placeholder (never promotes)\n"
    "\n"
    "Defaults:\n"
    "  --target-kind  prompt_file\n"
    "  --scorer       probe\n"
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
                                             'gradient_records','synthesis_gate_verdict','none')),
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

        # Priority 3: gradient_records fallback — only as last resort. The
        # gradient's `target` (e.g. agent.reviewer.prompt) must match the
        # requested target_name exactly: without the filter, any gradient of
        # the task_class wins and a workflow.node.* directive could be
        # appended to an agent prompt file.
        row = con.execute("""
            SELECT gradient_id AS id, suggested_change, signal, confidence,
                   row_number() OVER (ORDER BY confidence DESC) AS rank
            FROM gradient_records
            WHERE task_class=? AND target=?
            ORDER BY confidence DESC
            LIMIT 1
        """, (task_class, target_name)).fetchone()
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
    except sqlite3.OperationalError as exc:
        # A home that was never `mini-ork init`'d carries none of the source
        # tables, so the first query raises "no such table: pattern_records".
        # That is "nothing qualifies" — the caller's no_candidate path — not a
        # crash out of a command documented to exit 0. Any OTHER OperationalError
        # is a real schema or program fault and must still surface.
        if "no such table" not in str(exc):
            raise
        return ""
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# apply_score_candidate
# ─────────────────────────────────────────────────────────────────────────────
def score_candidate(candidate_id: str, scorer: str | None = None) -> str:
    """Score a candidate on a held-out set.

    Returns "<avg_utility_score> <n_examples>" (two floats on one line, like
    bash's stdout). ``mock`` is a deterministic placeholder and ``gepa`` a
    neutral placeholder pending the P2 real-execution evaluator
    (mini_ork.gepa.backends); both FABRICATE utility and can never promote (see
    ``FABRICATING_SCORERS``), so they exist only as test seams. An unknown
    scorer returns a neutral "0.5 1".
    """
    if scorer is None:
        scorer = os.environ.get("MO_APPLY_SCORER", "probe")

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
        # Neutral placeholder. The real GEPA scorer is the bring-your-own
        # evaluator seam in mini_ork.gepa.backends (a RunBackend returning a
        # fail-loud {score, feedback}); wiring it into the apply-loop gate is the
        # P2 real-execution-evaluator step. Until then this returns a neutral
        # constant so the non-regression gate is a no-op (before ≈ after) rather
        # than promoting on a fabricated gradient.
        return "0.5 1"

    return "0.5 1"  # unknown scorer → neutral


# ─────────────────────────────────────────────────────────────────────────────
# apply_evaluate_gate
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_gate(candidate_id: str, utility_before: float,
                  utility_after: float, pertask_json: str = "") -> str:
    """Apply the non-regression gate to a candidate with two utility numbers.

    Returns a JSON line: {"decision":"promoted"|"quarantined"|"rejected",
    "rationale":"...", ...}. There is no human gate: every non-promote is a
    quarantine or a rejection, recorded with its reason.

    Candidates are only PROMOTED when utility_after >= utility_before (no
    regression), with a configurable delta threshold. Below threshold →
    quarantined with a recorded reason (auditable) rather than rejected
    outright. When per-task before/after vectors are supplied
    (MO_APPLY_PERTASK_JSON), previously-PASSING held-out tasks that now FAIL
    block promotion past MO_APPLY_REGRESSION_TOLERANCE regardless of the
    aggregate (arXiv 2607.14004).

    Per-task vectors also raise the promote bar from "no regression" to "a
    measured improvement": with a real held-out measurement in hand, a delta of
    exactly the threshold means the candidate is indistinguishable from the
    baseline, and equality is not evidence. The scalar-only path (no vectors)
    keeps the historical ``delta >= dt`` rule.
    """
    del candidate_id  # bash accepted it positionally but never used it
    ub = float(utility_before)
    ua = float(utility_after)
    dt = float(os.environ.get("MO_APPLY_NONREGRESSION_DELTA", "0.0"))
    me = int(os.environ.get("MO_APPLY_MIN_EXAMPLES", "1"))
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
    measured = regressed >= 0  # per-task vectors present == a real held-out run happened

    # For a MEASURED candidate, equality is not evidence. delta == dt means the
    # candidate's publish rate over the probe set is indistinguishable from the
    # baseline's — and "indistinguishable" is exactly what a directive that does
    # nothing produces, as does a probe harness where every probe fails in both
    # arms. Promoting there is a promote on no evidence, recorded in the audit
    # trail identically to a measured improvement. The scalar-only path
    # (regressed < 0, no per-task data) keeps its historical delta >= dt rule.
    improved = delta > dt if measured else delta >= dt

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
    elif improved:
        decision = "promoted"
        rationale = (f"non-regression cleared: utility_after={ua:.4f} >= "
                     f"utility_before={ub:.4f} (delta={delta:+.4f} >= threshold={dt:+.4f})")
        if regressed == 0:
            rationale += "; 0 per-task regressions"
        delta_margin = 0.0
    elif measured:
        # A real held-out measurement ran and it found no gain. Quarantined: there
        # is nothing for a human to adjudicate — the measurement answered the
        # question, and the answer was "no difference".
        decision = "quarantined"
        rationale = (f"no measured improvement: utility_after={ua:.4f} == "
                     f"utility_before={ub:.4f} over the held-out probe set "
                     f"(delta={delta:+.4f} <= threshold={dt:+.4f}, 0 regressions); "
                     f"a measured candidate must strictly beat the baseline to promote")
        delta_margin = 0.0
    elif abs(delta - dt) < 0.02:
        # Scalar path only: utility is within measurement noise of the baseline
        # and there is no per-task vector to resolve it either way. An ambiguous
        # measurement is simply not promotable — there is no human in this loop
        # to break the tie (see the module docstring on the RSI posture). This
        # feeds _previously_failed, so an ambiguous directive is never re-proposed.
        decision = "quarantined"
        rationale = (f"ambiguous delta={delta:+.4f} (threshold={dt:+.4f}): within "
                     f"measurement noise of the baseline and no per-task vector to "
                     f"resolve it; not promotable without a measured improvement")
        delta_margin = 0.0
    else:
        decision = "quarantined"
        rationale = (f"regression: utility_after={ua:.4f} < utility_before={ub:.4f} "
                     f"(delta={delta:+.4f} < threshold={dt:+.4f})")
        delta_margin = 0.0

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
def _directive_block(new_prompt: str, *, source_ref: str = "",
                     context: str = "") -> str:
    """Render one appended learning directive. The HTML-comment marker line
    carries the idempotency key (source_ref) and is also the audit anchor a
    human greps for; the observation/directive pair stays visible so the
    executing model gets the rationale, GEPA-style."""
    marker = f"<!-- applied:{source_ref} -->" if source_ref else "<!-- applied -->"
    lines = [marker]
    if context:
        lines.append(f"- Observation: {context.strip()}")
    lines.append(f"- Directive: {new_prompt.strip()}")
    return "\n".join(lines)
def apply_mutation(candidate_id: str, target_file: str, new_prompt: str,
                   db: str | None = None, *, source_ref: str = "",
                   context: str = "") -> str:
    """On PROMOTED decisions, mutate the target prompt file and write a
    version_registry row. NO-OP (returns "") unless MO_APPLY_ENABLED=1 and
    MO_APPLY_DRY_RUN is unset/0. Returns the version_id ("" when skipped or
    when the version register call fails — bash's `|| true` swallows it).

    F3 semantics (2026-09-12): when the target file already exists and
    MO_APPLY_MODE is "append" (default), the change lands as an idempotent
    learning-directive block APPENDED to the prompt — gradient
    ``suggested_change`` text is a one-sentence directive, not a full prompt,
    so whole-file replacement would erase the prompt. ``source_ref`` (e.g.
    "gradient_records:gr-…") is the idempotency marker: a re-apply of the
    same source is skipped. MO_APPLY_MODE=replace restores the legacy
    whole-file rewrite for callers that pass a complete prompt.
    """
    apply_enabled = os.environ.get("MO_APPLY_ENABLED", "0")
    dry_run = os.environ.get("MO_APPLY_DRY_RUN", "0")

    if dry_run == "1" or apply_enabled != "1":
        sys.stderr.write(
            f"apply_apply_mutation: dry-run (apply_enabled={apply_enabled} "
            f"dry_run={dry_run}); no file write\n"
        )
        return ""

    mode = os.environ.get("MO_APPLY_MODE", "append")
    existing = ""
    if os.path.isfile(target_file):
        with open(target_file, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
        if mode == "append" and source_ref and source_ref in existing:
            sys.stderr.write(
                f"apply_apply_mutation: {source_ref} already applied to "
                f"{target_file}; skipping (idempotent)\n")
            return ""

    # Snapshot the previous file content BEFORE writing. It travels in the
    # registry payload (see below), not in a `<target>.apply-rollback-<pid>`
    # sidecar: the sidecar only survives as long as nobody cleans the worktree,
    # and version_registry.rollback() had no way to locate it anyway — which is
    # why a rollback used to move status columns and leave the promoted text on
    # disk. The hash is kept as a cheap integrity check on the stored content.
    prev_hash = ""
    if existing:
        prev_hash = hashlib.sha256(existing.encode("utf-8")).hexdigest()

    if existing and mode == "append":
        block = _directive_block(new_prompt, source_ref=source_ref,
                                 context=context)
        out = existing.rstrip("\n") + "\n\n" + block + "\n"
    else:
        out = f"{new_prompt}\n"

    try:
        with open(target_file, "w") as fh:
            fh.write(out)
    except OSError:
        sys.stderr.write(f"apply_apply_mutation: FAILED to write {target_file}\n")
        raise

    # Record the version. kind='agent' because prompt rewrites are agent-side
    # changes. The payload carries both file texts — ``content`` is what this
    # promotion wrote, ``baseline_content`` is what was there before — so
    # version_registry.rollback() can put real bytes back, and can mint the
    # first promotion's predecessor row from the same text. Bash sourced
    # lib/version_registry.sh with `|| true` and skipped the call when
    # unavailable — mirror by swallowing any failure and returning "".
    version_id = ""
    try:
        from mini_ork.registries import version_registry
        payload = json.dumps({
            "name": target_file,
            "version_id": None,
            "status": "stable",
            "utility_score": 0.0,
            "rollback_hash": prev_hash,
            "content": out,
            # None when the target did not exist before this mutation: there is
            # no predecessor state to restore, so no baseline row is minted and
            # a later rollback raises rather than guessing.
            "baseline_content": existing or None,
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
def _previously_failed(task_class: str, target_name: str, source_id: str,
                       db: str | None = None) -> bool:
    """Edit-memory lookup: has this exact (task_class, target, source_id)
    directive already failed a gate (quarantined or rejected)?"""
    if not source_id:
        return False
    try:
        con = sqlite3.connect(_db_path(db))
    except sqlite3.Error:
        return False
    try:
        row = con.execute(
            "SELECT 1 FROM apply_attempts WHERE task_class=? AND target_name=? "
            "AND source_id=? AND decision IN ('quarantined','rejected') LIMIT 1",
            (task_class, target_name, source_id)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False  # table missing → nothing remembered yet
    finally:
        con.close()


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

    # 1b. Outcome-tagged edit memory (GRAO 2604.20714 — task #19 prerequisite).
    #     A directive that failed a gate once is never re-proposed: without
    #     this memory the optimizer keeps re-proposing failed edits and
    #     performance collapses below baseline by iteration 4.
    if _previously_failed(task_class, target_name, source_id, db=db):
        aid = attempt_record(
            task_class, target_kind, target_name,
            source_kind, source_id, "", "", "",
            "", "", "", "rejected",
            "edit memory: (task_class, target, source) already has a "
            "quarantined/rejected apply_attempts row — never re-proposed "
            "(GRAO 2604.20714)",
            dry_flag, apply_enabled, db=db)
        sys.stdout.write(aid + "\n")
        sys.stdout.write(
            f'{{"decision":"rejected","task_class":"{task_class}",'
            f'"target":"{target_name}","source_id":"{source_id}"}}\n'
        )
        return 0

    # 2. Materialize the candidate.
    candidate_id = materialize_candidate(
        task_class, target_kind, target_name,
        source_kind, source_id, suggested_change, db=db)

    # 3. Score. utility_before = baseline utility of the CURRENT prompt so
    #    the non-regression gate compares against the real baseline.
    #    scorer=probe runs the real held-out evaluation (task #18, GRASP
    #    2605.29668): two arms over the frozen probe set, utilities from
    #    task_runs outcomes — never fabricated numbers.
    scorer = os.environ.get("MO_APPLY_SCORER", "probe")
    utility_before = "0.0"
    pertask_json = os.environ.get("MO_APPLY_PERTASK_JSON", "")
    if scorer == "mock":
        utility_before = os.environ.get("MO_APPLY_MOCK_BASELINE", "0.0")

    probe_result = None
    probe_unmeasured = False
    probe_dead_arms = False
    utility_after = ""
    if scorer in ("probe", "code"):
        from mini_ork.learning import probe_scorer as _ps  # deferred: cycle-safe
        try:
            if scorer == "code":
                # A code candidate is a PATCH against the framework tree, not a
                # directive appended to a prompt: the directive args do not
                # apply and are deliberately not passed. Both scorers share the
                # same result shape, so the gate below is unchanged.
                probe_result = _ps.probe_score_code(
                    task_class, os.environ.get("MO_APPLY_CODE_PATCH", ""))
            else:
                probe_result = _ps.probe_score(
                    task_class, target_file, suggested_change,
                    source_ref=f"{source_kind}:{source_id}" if source_id else "",
                    context=parsed.get("signal", ""))
        except RuntimeError as exc:
            sys.stderr.write(f"[probe-scorer] {exc}\n")
            probe_result = None
        if probe_result is not None and probe_result.get("n", 0) > 0:
            if probe_result["before"] <= 0.0 and probe_result["after"] <= 0.0:
                # Live-smoke finding (2026-09-16): every probe launch can fail
                # for an infra reason (needs_answers block, dead lane, bad env)
                # and n>0 with 0.0-vs-0.0 utilities then sails through the
                # scalar gate as a "non-regression" — promoting on a dead
                # harness, the exact fabrication this scorer retires. Both
                # arms entirely dead is NOT a measurement. (0→positive is a
                # genuine improvement and stays promotable.)
                probe_unmeasured = True
                probe_dead_arms = True
                utility_after = "0.0"
            else:
                utility_before = f"{probe_result['before']:.4f}"
                utility_after = f"{probe_result['after']:.4f}"
                if probe_result.get("pertask_json"):
                    pertask_json = probe_result["pertask_json"]
        else:
            # No frozen probe set (or budget exhausted before any pair
            # completed): NOTHING was measured. Fall through to no gate
            # promote — a neutral 0.5-vs-0.5 delta of 0 would otherwise
            # satisfy the scalar non-regression gate and promote on zero
            # evidence, which is exactly the fabrication this scorer exists
            # to retire.
            probe_unmeasured = True
            utility_after = "0.0"
    else:
        score_out = score_candidate(candidate_id)
        parts = score_out.split()
        utility_after = parts[0] if parts else ""

    # 4. Gate. Pass the optional per-task held-out vector so the in-loop
    #    no-regression gate can block a candidate that regresses a
    #    previously-solved task even when the aggregate improved (2607.14004).
    if probe_unmeasured:
        # quarantine, not pending_human_approval: there is no human in this loop
        # (see the module docstring on the RSI posture). An unmeasured candidate is
        # never promotable, and the reason travels with the row either way.
        # Consequence, and it is intended: a quarantine feeds _previously_failed,
        # so this directive is not re-proposed. That is the containment — a sweep
        # over a target with a dead harness would otherwise re-launch real probe
        # runs every cycle, and re-proposing never becomes a measurement.
        gate_decision = "quarantined"
        if probe_dead_arms:
            gate_rationale = ("probe scorer: BOTH arms failed every probe "
                              "(before=0.00 after=0.00) — the probe launches are "
                              "broken (needs_answers block, dead lane, bad env), not "
                              "the candidate; refusing to promote on a dead harness")
        else:
            gate_rationale = ("probe scorer measured nothing (no frozen probe set under "
                              "recipes/<recipe>/probes/, unresolvable target file, or budget "
                              "exhausted) — refusing to promote without held-out evaluation")
        utility_delta = 0.0
    else:
        gate_json = evaluate_gate(
            candidate_id, float(utility_before), float(utility_after),
            pertask_json)
        gate = json.loads(gate_json)
        gate_decision = gate["decision"]
        gate_rationale = gate["rationale"]
        utility_delta = gate["utility_delta"]
        if probe_result is not None:
            gate_rationale = (f"probe: n={probe_result['n']} "
                              f"before={probe_result['before']:.2f} "
                              f"after={probe_result['after']:.2f} "
                              f"cost=${probe_result.get('cost_usd', 0.0):.2f}; "
                              f"{gate_rationale}")

    # 4b. Evaluator honesty. The mock scorer and the gepa placeholder fabricate
    #     utility numbers (mock centers after≈0.55 against a 0.0 baseline → the
    #     scalar gate promotes EVERYTHING). A promote on fabricated numbers is
    #     indistinguishable in the audit trail from a measured improvement, so
    #     it is refused outright — there is no opt-in flag that restores it.
    #     The probe scorer is deliberately absent from this list: its utilities
    #     come from real held-out runs.
    if gate_decision == "promoted" and scorer in FABRICATING_SCORERS:
        gate_decision = "quarantined"
        gate_rationale = (f"refusing promote: scorer={scorer} fabricates utility "
                          f"(no real held-out measurement); {gate_rationale}")

    # 5. Promotion record (audit). For a non-promoted decision (quarantined /
    #    rejected) the promotion row still exists (it's the audit trail of why
    #    we did NOT promote).
    promotion_id = record_promotion(
        candidate_id, utility_before, utility_after,
        gate_decision, gate_rationale, db=db)

    # 6. Apply (only on PROMOTED + apply_enabled + !dry_run + target_file set).
    version_id = ""
    if gate_decision == "promoted" and target_file and suggested_change:
        try:
            version_id = apply_mutation(
                candidate_id, target_file, suggested_change, db=db,
                source_ref=f"{source_kind}:{source_id}" if source_id else "",
                context=parsed.get("signal", ""))
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
# Post-run auto-apply sweep (task #19, AutoSaddler 2608.23041).
# ─────────────────────────────────────────────────────────────────────────────

def _recipe_dir_for(task_class: str) -> str:
    root = _resolve_root()
    for name in (task_class, task_class.replace("_", "-"), task_class.replace("-", "_")):
        path = os.path.join(root, "recipes", name)
        if os.path.isdir(path):
            return path
    return ""


def _prompt_file_for(recipe_dir: str, target: str) -> str:
    """Gradient target → an existing recipe prompt path.

    ``agent.<role>.prompt`` (reflect's agent-targeted form) maps to
    ``prompts/<role>.md`` with ``_``/``-`` interchange; a ``prompts/<name>``
    target is already a recipe-relative path. No match → '' (target skipped,
    never guessed)."""
    if not recipe_dir:
        return ""
    if target.startswith("agent.") and target.endswith(".prompt"):
        role = target[len("agent."):-len(".prompt")]
        for name in sorted({role, role.replace("-", "_"), role.replace("_", "-")}):
            path = os.path.join(recipe_dir, "prompts", name + ".md")
            if os.path.isfile(path):
                return path
        return ""
    if target.startswith("prompts/"):
        path = os.path.join(recipe_dir, target)
        return path if os.path.isfile(path) else ""
    return ""


def auto_sweep(task_class: str, db: str | None = None,
               max_targets: int | None = None) -> list[dict]:
    """Bounded post-run apply sweep: top-1 gradient per agent-prompt target,
    every candidate still passes through the SAME gated apply_run — nothing
    promotes without the gate (dev-set filter, AutoSaddler's core rule).
    Returns per-target result dicts for the caller's log."""
    if max_targets is None:
        try:
            max_targets = max(1, int(os.environ.get("MO_AUTO_APPLY_MAX_TARGETS", "1")))
        except ValueError:
            max_targets = 1
    con = None
    try:
        con = sqlite3.connect(_db_path(db))
        rows = con.execute(
            "SELECT target, suggested_change FROM gradient_records "
            "WHERE task_class=? AND target LIKE 'agent.%' "
            "AND target NOT LIKE 'cross_class:%' "
            "ORDER BY confidence DESC", (task_class,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if con:
            con.close()
    targets: list[str] = []
    for target, change in rows:
        if not change or target in targets:
            continue  # top-1 per target only
        targets.append(target)
        if len(targets) >= max_targets:
            break
    print(f"[auto-apply] sweep task_class={task_class} targets={len(targets)} "
          f"(max={max_targets})")
    results = []
    recipe = _recipe_dir_for(task_class)
    for target in targets:
        target_file = _prompt_file_for(recipe, target)
        if not target_file:
            print(f"[auto-apply] {target}: skipped (no prompt file in recipe)")
            results.append({"target": target, "skipped": "no prompt file"})
            continue
        apply_run(task_class, "prompt_file", target, target_file, db=db)
        con = None
        try:
            con = sqlite3.connect(_db_path(db))
            row = con.execute(
                "SELECT decision, rationale FROM apply_attempts "
                "WHERE task_class=? AND target_name=? "
                "ORDER BY rowid DESC LIMIT 1", (task_class, target)).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            if con:
                con.close()
        decision = row[0] if row else "unknown"
        rationale = (row[1] if row else "")[:200]
        print(f"[auto-apply] {target}: {decision}")
        results.append({"target": target, "decision": decision,
                        "rationale": rationale})
    return results


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
    scorer = os.environ.get("MO_APPLY_SCORER", "probe")
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
