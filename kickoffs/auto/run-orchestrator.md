# Per-run orchestrator — full loop scaffold (default off)

> **Status update (2026-10-11):** the orchestrator shipped default-off as this
> kickoff specifies, and the default has since been **flipped to ON**. Current
> contract: enabled unless `MO_RUN_ORCHESTRATOR=0/false/no/off` (see
> `docs/CONFIG.md` §Run behavior and
> `mini_ork/orchestration/runner_orchestrator.py`). Everything below is the
> original spec and reads as-of shipping.

## Goal

Give every mini-ork run an **orchestrator observer** that sits at each DAG node
boundary and decides, from the node's typed outcome, ONE of five actions:
`advance · help · repair · mutate · promote`, and emits a learnable trace.
Ships behind `MO_RUN_ORCHESTRATOR=1`, **default off** — with the flag unset the
run loop behaves byte-identically to today.

This is a *composition* task: every channel already exists in the tree. Wire
them; do not rebuild them.

## What already exists (reuse — do NOT re-implement)

| Need | Live surface |
|---|---|
| Typed failure classes | `mini_ork/learning/failure_classifier.py` (`classify()`, 5 classes) |
| failure_class → (surface, edit-kind) | `mini_ork/learning/harness_operator.py:_TYPED_MAPPING` |
| Per-node reward (outcome-gated) | `mini_ork/learning/process_reward.py:score_trace` → `execution_traces.process_reward` |
| Node outcome events | `mini_ork/observability/node_events.py:mo_node_emit` → `run_events` |
| STEER channel (write) | `mini_ork/web/control.py:steer_run(db, run_id, msg, role_target=, severity=, confidence=, ttl_secs=)` |
| REPAIR/revise machinery | `execute.py:_write_revise_feedback` (:3190), `_archive_revise_round` (:3229), `_control_descendants` (:937) |
| PROMOTE target | `mini_ork/cli/apply.py:apply_mutation`, `learning/harness_operator.py:materialize_mutation` |
| Loop insertion point | `mini_ork/cli/execute.py:1009` (`outcomes = _parallel(batch)`) |

## Files in scope (touch ONLY these)

- `mini_ork/orchestration/runner_orchestrator.py` (**new**) — the module.
- `mini_ork/cli/execute.py` — the wiring only (see §Wiring). Nothing else in the file.
- `tests/unit/test_runner_orchestrator.py` (**new**) — unit tests.

Do NOT touch: any `recipes/**`, any `.mini-ork/config/**`, `db/migrations/**`,
`Cargo.*`, or any file outside the three above. Do NOT rebase, merge, or reset
during the run.

## Required module contract — `mini_ork/orchestration/runner_orchestrator.py`

Pure decision core + a thin, side-effectful adapter. No DB/lane/model access in
`decide()`. Stdlib only (`dataclasses`, `os`, `json`, `typing`).

```python
ADVANCE, HELP, REPAIR, MUTATE, PROMOTE = "advance", "help", "repair", "mutate", "promote"

@dataclass(frozen=True)
class Action:
    kind: str                      # one of the five above
    reason: str                    # one line, human-readable
    payload: dict = field(default_factory=dict)
    requires_llm: bool = False     # True only when a rule was ambiguous
    budget_usd: float = 0.0

def enabled() -> bool:
    """MO_RUN_ORCHESTRATOR truthy ('1','true','yes','on', case-insensitive)."""

def classify_outcome(node_type, rc, finish_reason="", *, attempts=0, max_turns_hit=False) -> tuple[str, str, str]:
    """(failure_class, locus, scope). Rules-first, no model.
    - delegate the class to failure_classifier.classify(reason=finish_reason, max_turns_hit=...)
    - locus: map via harness_operator._TYPED_MAPPING -> {"prompt","recovery","stage_order","verifier"}
      plus "env" for infra_interrupt; default "node".
    - scope: rc==0 -> "none"; edit-arg-level stop -> "edit-args";
      repeated failure (attempts>=2) -> "replace-node"; else "replace-subgraph".
    """

def decide(*, node_id, node_type, rc, finish_reason="", lane="", attempts=0,
           process_reward=None, has_retries_edge=False, stall=False) -> Action:
    """Pure. Deterministic. Order of precedence:
    1. rc == 0 and not stall            -> ADVANCE
    2. stall (repeat failure, no progress) -> HELP  (bounded content-half nudge)
    3. rc != 0 and locus in {node,model} and has_retries_edge -> REPAIR
    4. rc != 0 and locus in {harness,stage_order,verifier,prompt} -> MUTATE (proposal)
    5. rc != 0 and locus == env (infra) -> PROMOTE is NOT used; -> REPAIR with retry_policy
    6. otherwise                        -> ADVANCE (fail-open: never wedge a run)
    """

def steer_message(action: Action) -> str:
    """Render a HELP Action into a bounded steering message. MUST pass the guard."""

def guard_steer(message: str) -> bool:
    """Reject any nudge that would mutate the node's CONTRACT. Return False if the
    message contains any of (case-insensitive): 'finish_reason', 'exit_code',
    'schema', 'unset MO_', 'MO_RUN_ORCHESTRATOR', 'git reset', 'rm -rf'.
    Content-half-only: a nudge may add information, never change routing,
    termination, or schema."""

def budget_ok(spent_usd: float) -> bool:
    """True while spent_usd < cap. Cap = float(MO_ORCH_BUDGET_USD or '1.0')."""

def on_node_boundary(ctx, *, spent_usd=0.0) -> Action | None:
    """Adapter. Return None when disabled (flag off) — the loop then does nothing.
    Otherwise read the node's trace (execution_traces row for run_id+node_id when
    db available) for lane/process_reward/attempts, build the decide() inputs,
    call decide(), emit ONE mo_node_emit(run_id, node_id, node_type,
    f"orchestrator.{action.kind}", json.dumps(payload)), and return the Action.
    Any internal exception -> return None (the orchestrator must never fail a run)."""
```

## Wiring — `mini_ork/cli/execute.py`

Only two edits:

1. After `outcomes = _parallel(batch)` (:1009) and after the `statuses` update
   loop, insert a bounded block, guarded so the default path is untouched:

```python
# ── per-run orchestrator (MO_RUN_ORCHESTRATOR=1; default off) ──
try:
    from mini_ork.orchestration.runner_orchestrator import enabled as _orch_enabled
except Exception:
    _orch_enabled = lambda: False
if _orch_enabled():
    _orchestrator_act(live_run_dir, db, run_id, outcomes, retry_edges,
                      revision_state=locals())
```

2. Add module-level helper `_orchestrator_act(run_dir, db, run_id, outcomes,
   retry_edges, *, revision_state)` in `execute.py` that, for each
   `(field, rc, finish_reason)` with `rc != 0`:
   - calls `on_node_boundary(...)`;
   - `HELP`  → `from mini_ork.web.control import steer_run`; call it with the
     guarded message, `role_target` = the node's type, `severity="info"`;
     **skip the call if `guard_steer(msg)` is False** and emit an
     `orchestrator.help.rejected` event instead;
   - `REPAIR` → write the bounded brief via the existing `_write_revise_feedback`
     path (reuse, don't fork);
   - `MUTATE` → emit `orchestrator.mutate.proposed` and, only when
     `retry_edges` already declares a `retries` edge into this node, reset its
     descendants with `_control_descendants` (reuse the existing machinery);
     never invent edges;
   - `PROMOTE` → emit `orchestrator.promote.filed` with the failure-class +
     file anchor; do NOT call `apply_mutation` (that stays the post-run,
     held-out-gated path).
   Wrap the whole helper body in `try/except` that prints one line to stderr and
   returns — the orchestrator can never take a run down.

## Requirements

- **Default off, provably.** With `MO_RUN_ORCHESTRATOR` unset, no orchestrator
  function is called and `execute.py` behaves exactly as before.
- **Pure core.** `decide()` and `classify_outcome()` take no DB/lane/model and
  are deterministic — unit-testable with plain dicts.
- **Guard is load-bearing.** HELP messages pass `guard_steer`; a message that
  would change the node's contract is rejected, not sent.
- **Bounded.** One global budget gate (`budget_ok`); REPAIR briefs are bounded
  (≤ 40 lines); every action emits exactly one `run_events` row for audit.
- **No new dependencies.** Stdlib + existing `mini_ork` modules only.
- **Unknown stops fail-open** to `terminal` classification (via
  `failure_classifier`) but `decide` still returns `ADVANCE`, never a wedge.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains only the three scoped files.
- `${MINI_ORK_RUN_DIR}/verdict.json` has
  `{ "files_changed": 3, "tests_pass": true, "static_pass": true, "pass": true }`.
- In the isolated patched copy, `python3.11 -m pytest tests/unit/test_runner_orchestrator.py -q`
  passes with all of these (exact names):
  1. `test_flag_default_off` — `enabled()` False with env unset.
  2. `test_flag_on_truthy_variants` — `1/true/YES/on` all True, `0/off/""` False.
  3. `test_advance_on_success` — rc=0 → ADVANCE.
  4. `test_classify_infra_is_env_locus` — infra_interrupt → locus `env`.
  5. `test_classify_output_invalid_is_prompt` — output_invalid → locus `prompt`.
  6. `test_classify_terminal_is_verifier` — terminal → locus `verifier`.
  7. `test_repair_on_failed_with_retries_edge` — failed + any retries edge → REPAIR.
  8. `test_mutate_on_harness_locus` — harness locus → MUTATE.
  9. `test_help_on_stall` — stall=True, rc=0 → HELP.
  10. `test_guard_rejects_contract_mutation` — `guard_steer("change finish_reason")` False.
  11. `test_guard_allows_content_nudge` — a plain "consider X" nudge True.
  12. `test_budget_gate_blocks_over_cap` — `budget_ok(spent > cap)` False.
- `python3.11 -m pytest tests/unit/ -q -k orchestrator` shows the new tests
  collected and green, and no previously-passing test regresses.

## Prior art (evidence for the design — cite in the diff comments)

- Type-before-treat: 2607.28802; narrow-don't-broaden at failure: 2605.08717, 2608.11772.
- Scope-the-edit: 2609.34313; invariant-guarded mutation: 2607.20488.
- Execution-anchored gates only: 2609.02246; invisible orchestrators suppress
  protective behaviour: 2605.13851.
