"""Pre-run lane-health gate for ``mini_ork``.

A run that dispatches its DAG against a starved lane burns budget and returns
zero artifacts (kickoff lane-health-gate). The cost circuit used to trip per
LLM call *inside* the run, so the failure looked like any other model failure
— indistinguishable from one that tried. Refuse before the run starts:

1. Reuse the dispatcher's own expressions to resolve every node's lane to a
   real model: alias → routed lane (routing policy) → family
   (``resolve_lane_family``). Pinned nodes short-circuit; submodes are unioned
   best-effort from the registry, but a dynamic child is still preflight-guarded
   on dispatch.
2. Batch-key ``providers.preflight`` over the deduped model set with the same
   scoped env :func:`dispatch_model` builds (``provider_environment`` — the
   persisted secret store merged under a user-exported env), so key detection
   matches dispatch exactly and a credential written by
   ``mini-ork providers configure`` is visible to the gate.
3. For every unhealthy model, emit one ``LaneFailure`` per waiting node naming
   the suggested replacement from ``lane_suggest.suggest``.

The public seam is two functions:

* :func:`gate` — pure decision. No DB, no printing. Tests assert on this
  surface to prove the resolve chain without a live DB.
* :func:`enforce` — ``gate`` plus the abort side effects (stderr message,
  ``<run_dir>/lane-preflight.json``, ``task_runs`` failure row, ``run_events``
  insert, stdout machine lines). Returns ``""`` to proceed or the
  blocked_reason string (``"lane_unhealthy"``).

The closed-listing run-never-started exit code is :data:`RC_BLOCKED` (75), so
the budget preflight, the lane gate, and the needs-answers execute gate all
share the same exit the lifecycle scrapes into ``sink["verdict"]="blocked"``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Same EX_TEMPFAIL the budget preflight and the execute-needs-answers gate
# use. Three call sites, one rc — the lifecycle only learns ``blocked`` once.
RC_BLOCKED = 75


@dataclass(frozen=True)
class DispatchLane:
    """One node resolved to the model the dispatch loop will actually pick."""
    node_id: str
    node_type: str
    workflow_lane: str
    routed_lane: str
    model: str


@dataclass(frozen=True)
class LaneFailure:
    """A starvation record: which lane died, for which role, naming every node
    that was about to dispatch on it, with the suggested replacement."""
    lane: str
    role: str
    node_id: str
    reason: str
    suggestions: tuple[dict, ...] = ()


def _read_env(environ: Mapping[str, str] | None, key: str, default: str = "") -> str:
    """Read from an optional env mapping, fall back to ``os.environ``.

    The gate runs at lifecycle entry before ``context_env_snapshot`` has been
    published for the dispatch loop; reading from ``os.environ`` matches the
    ``_execute_gate_check`` precedent (``os.environ.get("MINI_ORK_EXECUTE_GATE", ...)``)
    so the knob behaves consistently across both pre-dispatch gates.
    """
    if environ is not None and key in environ:
        return str(environ[key] or "")
    return os.environ.get(key, default)


def _preflight_env(environ: Mapping[str, str] | None) -> dict[str, str]:
    """The scoped env the gate probes lanes against.

    Builds the same dict :func:`dispatch_model` builds (``provider_environment``
    — persisted secret exports from :func:`read_secret_exports` merged with the
    caller's process env, user exports winning). This is the contract the kickoff
    pins: ``providers.preflight`` MUST see every credential dispatch would see,
    otherwise a lane the gate pronounces dead is one ``dispatch_model`` would
    have happily served (the gate then becomes a silent no-op — exactly the
    failure mode the kickoff exists to close).

    Tests passing ``environ=...`` get full control of the credential view:
    ``provider_environment(environment=environ)`` resolves the secret-store
    path from ``environ``'s ``MINI_ORK_HOME``/``MINI_ORK_SECRETS``, reads it
    (returning ``{}`` for a missing file, raising :class:`SecretStoreError`
    for a malformed one), and overlays ``environ`` on top. The operator's
    secret store therefore stays the source of truth in tests as well, with
    ``environ`` providing the override layer.

    Raises :class:`SecretStoreError` when the resolved credential store is
    unreadable; the gate treats that as a hard fail (a closed-listing run that
    can't read its own credentials must not start — same posture as
    ``dispatch_model``'s ``rc=2`` "provider credential store failed" branch).
    """
    # Lazy import keeps this module importable in unit tests that don't carry
    # the full dispatch graph into the test process yet, and so the
    # ``SecretStoreError`` surface is only imported on this code path.
    from mini_ork.dispatch.providers import provider_environment

    return provider_environment(environment=environ)


def _node_role(node_type: str) -> str:
    """The hint ``lane_suggest.suggest`` uses to decide code-lane filtering.

    Plain passthrough so suggest sees the workflow's own vocabulary; we don't
    synthesize a role — a planner on a starved lane still wants the planner
    family, not an ``implementer``-keyed suggestion.
    """
    return node_type or ""


def resolve_dispatch_lanes(
    fields: Sequence[Sequence[str]],
    *,
    root: str | os.PathLike[str] | None,
    home: str | os.PathLike[str] | None,
    task_class: str = "",
    environ: Mapping[str, str] | None = None,
) -> list[DispatchLane]:
    """For every dispatched node, decide the lane+model the dispatcher will use.

    Field tuple layout matches ``WorkflowNode.dispatch_fields`` (the legacy
    8-tuple ``execute.py`` builds at the recovery-filter step), so field
    index ``6`` is ``model_lane or type`` and index ``1`` is the node type.

    The routing policy decides (``policy_route_lane`` is *the* single hook the
    dispatcher uses); family resolution (``resolve_lane_family``) unwinds
    ``*_lens`` aliases so the preflight keys on a real provider model. The
    model is the dedup key for the probe, but every waiting node survives on
    the returned list so the message names each one.
    """
    from mini_ork.dispatch import llm_dispatch, routing

    out: list[DispatchLane] = []
    for f in fields or ():
        if not f:
            continue
        # ``f`` may be a tuple/list of length 1..8; pad so position access is
        # always safe (the executor's own code uses ``(e.split(_SEP) + [""]*8)[:8]``).
        t = tuple(f) + ("",) * (8 - len(f))
        node_id = str(t[0] or "")
        node_type = str(t[1] or "")
        workflow_lane = str(t[6] or node_type)
        routed = routing.policy_route_lane(
            node_type, workflow_lane, dry_run=False, root=root, task_class=task_class,
        )
        model = llm_dispatch.resolve_lane_family(
            routed, str(root) if root else "", str(home) if home else "",
        )
        out.append(DispatchLane(
            node_id=node_id, node_type=node_type,
            workflow_lane=workflow_lane, routed_lane=routed, model=model,
        ))
    return out


def _lane_suggest(home: str | os.PathLike[str] | None, *, failed_lane: str,
                  alias: str, node_types: Sequence[str], db: str = "") -> tuple[dict, ...]:
    """Best-effort replacement suggestions for one starved lane.

    Failures (missing home, malformed providers.yaml, DB drift) degrade to
    ``()`` so the gate stays green when the helper can't read its source data
    — never silently rewriting the operator's run.
    """
    if not failed_lane:
        return ()
    try:
        from mini_ork.recovery import lane_suggest

        args: dict[str, Any] = {
            "failed_lane": failed_lane, "alias": alias,
            "node_types": list(node_types),
        }
        if db:
            args["db"] = db
        rows = lane_suggest.suggest(home, **args)
    except Exception:  # noqa: BLE001 — helper stays best-effort
        return ()
    norm = []
    for row in rows or ():
        if isinstance(row, Mapping):
            ln = str(row.get("lane") or "").strip()
            if not ln:
                continue
            norm.append({"lane": ln, "reason": str(row.get("reason") or "")})
    return tuple(norm)


def gate(
    fields: Sequence[Sequence[str]],
    *,
    root: str | os.PathLike[str] | None,
    home: str | os.PathLike[str] | None,
    task_class: str = "",
    environ: Mapping[str, str] | None = None,
) -> list[LaneFailure]:
    """Pure decision: which lanes are dead, and which nodes are about to starve.

    ``fields`` is the post-recovery-filter dispatch-set (the same list the
    executor would loop over). Returns ``[]`` to proceed or one
    :class:`LaneFailure` per (unhealthy lane × waiting node) — a multi-role
    starvation surfaces every role that depends on the starved model.

    Pure by contract: no DB, no printing, no side effects. Tests assert
    on the same input twice and on ``capsys.readouterr()`` being empty.

    Configuration errors (an unknown lane in the registry, a missing API key
    on a ``*-compat`` lane) are filtered OUT — ``dispatch_model`` already
    refuses those per call with rc=2, so blocking here too would conflate
    two failure modes and break tests on bare workflow lane names. The
    gate's value is the case the dispatch loop cannot fix in-flight:
    a lane that has credit today and zero credit at minute three.

    A ``SecretStoreError`` from :func:`provider_environment` is treated as a
    closed-listing failure (one :class:`LaneFailure` per waiting node naming
    the credential store as the reason) — same shape as
    ``dispatch_model``'s ``rc=2`` "provider credential store failed" branch,
    because a run that can't read its own credentials cannot dispatch any
    lane and must not start.
    """
    if _read_env(environ, "MO_LANE_HEALTH_GATE", "1") != "1":
        return []
    lanes = resolve_dispatch_lanes(
        fields, root=root, home=home, task_class=task_class, environ=environ,
    )
    if not lanes:
        return []
    # Dedup models for the probe: opus and sonnet on one credential probe once
    # (the cache fingerprint rides on (url,key), so the dedup is a perf hint,
    # not a correctness step).
    seen: dict[str, DispatchLane] = {}
    for dl in lanes:
        seen.setdefault(dl.model, dl)
    sorted_models = sorted(seen)

    from mini_ork.dispatch import providers

    try:
        pre_env = _preflight_env(environ)
    except providers.SecretStoreError as exc:
        # The credential store is unreadable (malformed, symlink, wrong
        # permissions/owner). ``dispatch_model`` would refuse every dispatch
        # with rc=2 in this state — fail closed here too. Surface one failure
        # per waiting node so the operator's stderr names every lane they
        # were about to burn a dispatch on, with a clear credential-store
        # reason.
        return [
            LaneFailure(
                lane=dl.model, role=_node_role(dl.node_type),
                node_id=dl.node_id,
                reason=f"provider credential store failed: {exc}",
            )
            for dl in lanes
        ]
    preflight = providers.preflight(sorted_models, root, environment=pre_env)

    # Index waiting nodes by model so one LaneFailure per (lane, role, node)
    # is the deterministic iteration order. Preserve the resolution order
    # rather than re-sorting — operators expect the message to read in the
    # planner's order, not alphabetical.
    by_model: dict[str, list[DispatchLane]] = {m: [] for m in sorted_models}
    for dl in lanes:
        by_model[dl.model].append(dl)

    failures: list[LaneFailure] = []
    for model in sorted_models:
        health = preflight.get(model)
        if health is None or health.ok:
            continue
        if _is_configuration_error(health.reason):
            # Not a budget-burner: the dispatcher will rc=2 on the first
            # attempt. Avoid blocking the run on this — the operator gets
            # the per-dispatch refusal and the run may still succeed via
            # routing/fallback elsewhere.
            continue
        for dl in by_model[model]:
            suggestions = _lane_suggest(
                home, failed_lane=model, alias=dl.workflow_lane,
                node_types=[dl.node_type], db=pre_env.get("MINI_ORK_DB", ""),
            )
            failures.append(LaneFailure(
                lane=model, role=_node_role(dl.node_type),
                node_id=dl.node_id, reason=health.reason,
                suggestions=suggestions,
            ))
    return failures


def _is_configuration_error(reason: str) -> bool:
    """True when ``reason`` is a configuration failure rather than a dead lane.

    Mirrors the two cases :func:`lane_health` returns for non-dead failures:

      * ``unknown lane: '<name>'`` — the lane is not in the registry at
        all (a workflow or test fixture passed a name no ``providers.yaml``
        declares);
      * ``<lane>: $<API_KEY_ENV> is not set — lane would die silently`` —
        a compat lane whose credential env var is unset.

    Both are visible at dispatch time (``dispatch_model`` returns ok=False
    with rc=2) and recovered from via routing; blocking here would be the
    kind of "false positive this whole guard exists to avoid" the
    compat-probe contract promises.
    """
    if not reason:
        return False
    head = reason.split(":", 1)[0].strip()
    if head.startswith("unknown lane"):
        return True
    if "is not set" in reason and "would die silently" in reason:
        return True
    return False


# ── enforce (side-effect half) ────────────────────────────────────────────────
# The pure-vs-side-effect split is the only discipline gate() being unit-testable
# without a DB or a real engine. enforce() is the same gate() plus the abort
# surface; it is what execute.main calls.

def _emit_blocked_artifacts(
    run_dir: str, payload: Mapping[str, Any],
) -> None:
    """Write ``<run_dir>/lane-preflight.json`` with the same OSError-swallowing
    discipline ``blocked.json`` uses — a read-only run dir never crashes the
    gate, just skips the artifact."""
    if not run_dir or run_dir == ".":
        return
    try:
        path = os.path.join(run_dir, "lane-preflight.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(payload)) + "\n")
    except OSError:
        pass


def _db_path(db: str = "", environ: Mapping[str, str] | None = None) -> str:
    """Resolve the DB path: caller → env → home/state.db fallback.

    Same precedence the budget preflight uses so a missing MINI_ORK_DB is read
    from MINI_ORK_HOME (kickoff prior-art §1.4 verbatim). Empty result means
    "no DB" and the writes are skipped — never raises."""
    db = db or _read_env(environ, "MINI_ORK_DB", "")
    if db:
        return db
    home = _read_env(environ, "MINI_ORK_HOME", "")
    if home:
        return os.path.join(home, "state.db")
    return ""


def _persist_blocked(
    db: str, run_id: str, payload: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Mark ``task_runs`` failed/ESCALATE and append ``run_events``.

    Reuses the exact shape ``_execute_gate_check`` (execute.py:3435-3446) writes:

    * ``status='failed'``, ``verdict=COALESCE(verdict,'ESCALATE')``, notes
      appended with the gate's reason;
    * guarded by ``AND status NOT IN ('published','rolled_back','failed')``
      so a run that already settled is never overwritten;
    * one ``run_events`` row ``event_type='lane_preflight_blocked'``.

    Swallows ``sqlite3.Error`` because the gate runs before the executor and a
    missing/locked DB must never escalate past the abort message.
    """
    if not db or not run_id or not os.path.isfile(db):
        return
    now = int(time.time())
    try:
        con = sqlite3.connect(db, timeout=5.0)
        con.execute("PRAGMA busy_timeout = 5000")
        try:
            con.execute(
                "UPDATE task_runs SET status='failed', "
                "verdict=COALESCE(verdict,'ESCALATE'), updated_at=?, "
                "ended_at=COALESCE(ended_at,?), "
                "notes=COALESCE(notes || '; ','') || ? "
                "WHERE id=? AND status NOT IN "
                "('published','rolled_back','failed')",
                (now, now, "lane preflight: lane_unhealthy", run_id),
            )
            con.execute(
                "INSERT INTO run_events(event_id, run_id, event_type, "
                "payload_json, created_at) VALUES (?,?,?,?,?)",
                (
                    f"evt-lane_preflight_blocked-{now}", run_id,
                    "lane_preflight_blocked",
                    json.dumps(dict(payload)), now,
                ),
            )
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def _print_machine_lines(payload: Mapping[str, Any]) -> None:
    """Emit the lifecycle's stdout key/value lines.

    Mirrors the budget preflight's ``blocked_reason=cost_limit`` /
    ``verdict=blocked`` shape (main.py:1074) so downstream parsers learn one
    format across all three rc=75 call sites. Folds the gate JSON verbatim so
    rubric/verify/reflect can read it without re-running preflight.
    """
    print(f"lane_preflight={json.dumps(dict(payload), separators=(',', ':'))}")
    print("blocked_reason=lane_unhealthy")
    print("verdict=blocked")


def _stderr_blocked(failures: Sequence[LaneFailure]) -> None:
    """Operator-facing line per starvation, then a one-line fix and a bypass.

    The format mirrors ``_execute_gate_check``'s ``[blocked] …`` block so both
    pre-dispatch gates read the same in the captured ``execute_stderr``.
    """
    if not failures:
        return
    for f in failures:
        sys.stderr.write(
            f"lane={f.lane}  role={f.role}  node={f.node_id}  reason: {f.reason}\n",
        )
        for s in f.suggestions:
            sys.stderr.write(
                f"  suggest: lane={s.get('lane', '?')}  — {s.get('reason', '')}\n"
            )
    # One-line fix and the escape hatch the kickoff calls for — operators see
    # one path forward at the very bottom of the stderr capture.
    if failures:
        lane = failures[0].lane
        sys.stderr.write(
            f"fix: mini-ork recover <run_id> --lane {lane}=<replacement> "
            f"(or set MO_LANE_HEALTH_GATE=0 to skip enforcement; "
            f"MO_LANE_PROBE=0 downgrades the gate to key presence, which for "
            f"native lanes is a no-op)\n"
        )


def enforce(
    fields: Sequence[Sequence[str]],
    *,
    root: str | os.PathLike[str] | None,
    home: str | os.PathLike[str] | None,
    task_class: str = "",
    db: str = "",
    run_id: str = "",
    run_dir: str = "",
    environ: Mapping[str, str] | None = None,
) -> str:
    """Side-effect half of the gate.

    Returns ``""`` to proceed or ``"lane_unhealthy"`` when blocked. The string
    only signals which ``blocked_reason`` to write into the lifecycle sink;
    the caller (execute.main) emits the rc and the lifecycle tail does the
    rest.
    """
    failures = gate(
        fields, root=root, home=home, task_class=task_class, environ=environ,
    )
    if not failures:
        return ""
    # The captured stderr is what the lifecycle scrapes for blocked_reason,
    # so direct ``sys.stderr.write`` (not a redirected print). The lifecycle
    # captures this module's writes via the redirect in _run_lifecycle_impl.
    _stderr_blocked(failures)
    payload: dict[str, Any] = {
        "verdict": "blocked",
        "blocked_reason": "lane_unhealthy",
        "failures": [
            {
                "lane": f.lane, "role": f.role, "node_id": f.node_id,
                "reason": f.reason,
                "suggestions": [dict(s) for s in f.suggestions],
            }
            for f in failures
        ],
        "checked_at": int(time.time()),
    }
    _emit_blocked_artifacts(run_dir, payload)
    _persist_blocked(
        _db_path(db, environ), run_id or _read_env(environ, "MINI_ORK_RUN_ID", ""),
        payload, environ=environ,
    )
    _print_machine_lines(payload)
    return "lane_unhealthy"
