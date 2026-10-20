"""Pre-run lane-health gate for ``mini_ork``.

A run that dispatches its DAG against a starved lane burns budget and returns
zero artifacts (kickoff lane-health-gate). The cost circuit used to trip per
LLM call *inside* the run, so the failure looked like any other model failure
— indistinguishable from one that tried. Refuse before the run starts:

1. Reuse the dispatcher's own expressions to resolve every *LLM-dispatching*
   node's lane to a real model: alias → routed lane (routing policy) → family
   (``resolve_lane_family``). Pinned nodes short-circuit. Nodes that never
   dispatch an LLM — ``publisher`` / ``rollback``, and script verifiers whose
   workflow entry sets ``verifier_ref`` without a ``prompt_ref`` — are dropped
   from the resolve set, so a dead lane they are merely *configured* on can no
   longer block a run (RB-23). Dynamically spawned children are still guarded
   per dispatch, not statically.
2. Batch-key ``providers.preflight`` over the deduped model set with the same
   scoped env :func:`dispatch_model` builds (``provider_environment`` — the
   persisted secret store merged under a user-exported env), so key detection
   matches dispatch exactly and a credential written by
   ``mini-ork providers configure`` is visible to the gate.
3. For every unhealthy model, emit one ``LaneFailure`` per waiting node naming
   the suggested replacement from ``lane_suggest.suggest``.

The public seam is three functions:

* :func:`gate` — pure decision: which lanes are *dead*. No DB, no printing.
  Tests assert on this surface to prove the resolve chain without a live DB.
* :func:`unverified` — pure decision: which lanes the gate could **not** check
  (``LaneHealth.verified`` is false, or the probe returned no result at all).
  A lane that is merely unverified is not known-bad, so it never blocks by
  itself — the native-lane blind spot this pair exists to surface.
* :func:`enforce` — both decisions plus the abort side effects (stderr message,
  ``<run_dir>/lane-preflight.json``, ``task_runs`` failure row, ``run_events``
  insert, stdout machine lines). Returns ``""`` to proceed or the
  blocked_reason string (``"lane_unhealthy"``), or ``"lane_unverified"`` when
  ``MO_LANE_REQUIRE_VERIFICATION=1`` turns an unverifiable lane into an abort.
  With verification not required, a run with only-unverified lanes writes the
  artifact (``verdict="proceed"``), warns on stderr, and proceeds.

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

from mini_ork import run_status as _run_status

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


@dataclass(frozen=True)
class LaneUnverified:
    """A lane the gate could not check — ``ok`` but no probe actually answered.

    The native-lane blind spot: ``LaneHealth.ok`` is True for an
    ``anthropic-native`` lane whose credential the CLI owns, so a run could
    dispatch against a lane nobody had ever asked a question of. This record
    names every node waiting on such a lane. It is deliberately NOT a
    :class:`LaneFailure` — "not known bad" is not "dead" — so the two lists
    :func:`_evaluate` returns stay disjoint, and a run with only these proceeds
    unless the operator opts into ``MO_LANE_REQUIRE_VERIFICATION=1``.
    """
    lane: str
    role: str
    node_id: str
    reason: str


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


#: Workflow node types that never call an LLM: they publish or unwind a run.
#: RB-23: a run was blocked ``lane_unhealthy`` on a publisher lane that makes
#: no LLM call, and on ``code-fix``'s script ``typecheck`` routed to kimi (401).
_NON_LLM_NODE_TYPES = frozenset({"publisher", "rollback"})


def _node_name_variants(node_id: str) -> tuple[str, ...]:
    """The name spellings a workflow entry may use for one dispatch node id.

    Plan decomposition ids and workflow ``name:`` values can differ only by
    separator (``code_impact_lens`` vs ``code-impact-lens``); mirror
    ``execute.nodes_from_plan``'s ``_wf_lookup`` so a node is recognised
    whichever spelling the caller carries.
    """
    return (node_id, node_id.replace("-", "_"), node_id.replace("_", "-"))


def _non_llm_workflow_nodes(workflow_path: str) -> set[str]:
    """Names of workflow nodes that cannot dispatch an LLM.

    The workflow entry — not the dispatch field tuple — is the authority the
    kickoff names. A node is non-LLM when its ``type`` is ``publisher`` or
    ``rollback``, or when it declares a ``verifier_ref`` and no ``prompt_ref``
    (the deterministic-script-verifier signature used by ``code-fix``'s
    ``typecheck``/``test`` and ``framework-edit``'s verifiers).

    Best-effort by contract: an empty path, an unreadable file, malformed
    YAML or an unexpected shape all return the empty set, and the caller keeps
    today's behaviour (probe every node). A preflight gate that cannot read its
    workflow must not silently shrink the probed set.
    """
    if not workflow_path:
        return set()
    try:
        import yaml
    except ImportError:  # pragma: no cover — PyYAML is a hard dependency
        return set()
    try:
        with open(workflow_path, encoding="utf-8") as fh:
            document = yaml.safe_load(fh)
    except (OSError, ValueError, yaml.YAMLError):
        # ValueError covers a non-UTF-8 workflow; YAMLError is not one.
        return set()
    if not isinstance(document, Mapping):
        return set()
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        return set()
    out: set[str] = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        name = str(node.get("name") or "")
        if not name:
            continue
        node_type = str(node.get("type") or "")
        # ``prompt_ref: null`` parses to None; never ``str(None)`` (truthy).
        prompt_ref = str(node.get("prompt_ref") or "")
        verifier_ref = str(node.get("verifier_ref") or "")
        if node_type in _NON_LLM_NODE_TYPES or (verifier_ref and not prompt_ref):
            out.add(name)
    return out


def resolve_dispatch_lanes(
    fields: Sequence[Sequence[str]],
    *,
    root: str | os.PathLike[str] | None,
    home: str | os.PathLike[str] | None,
    task_class: str = "",
    environ: Mapping[str, str] | None = None,
    workflow_path: str | None = None,
) -> list[DispatchLane]:
    """For every LLM-dispatching node, decide the lane+model the dispatcher uses.

    Field tuple layout matches ``WorkflowNode.dispatch_fields`` (the legacy
    8-tuple ``execute.py`` builds at the recovery-filter step), so field
    index ``6`` is ``model_lane or type`` and index ``1`` is the node type.

    The routing policy decides (``policy_route_lane`` is *the* single hook the
    dispatcher uses); family resolution (``resolve_lane_family``) unwinds
    ``*_lens`` aliases so the preflight keys on a real provider model. The
    model is the dedup key for the probe, but every waiting node survives on
    the returned list so the message names each one.

    Nodes that cannot dispatch an LLM are dropped first (RB-23): see
    :func:`_non_llm_workflow_nodes`. The workflow is read from
    ``workflow_path`` when given, else from ``MINI_ORK_WORKFLOW`` (set by the
    run lifecycle); when no workflow can be read every node is kept, matching
    the pre-RB-23 behaviour. A node whose id is absent from the workflow is
    likewise kept — "not in this workflow" is not proof it cannot dispatch.
    """
    non_llm = _non_llm_workflow_nodes(
        workflow_path or _read_env(environ, "MINI_ORK_WORKFLOW", "")
    )
    from mini_ork.dispatch import llm_dispatch, routing

    out: list[DispatchLane] = []
    for f in fields or ():
        if not f:
            continue
        # ``f`` may be a tuple/list of length 1..8; pad so position access is
        # always safe (the executor's own code uses ``(e.split(_SEP) + [""]*8)[:8]``).
        t = tuple(f) + ("",) * (8 - len(f))
        node_id = str(t[0] or "")
        if non_llm and any(v in non_llm for v in _node_name_variants(node_id)):
            continue
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


def _evaluate(
    fields: Sequence[Sequence[str]],
    *,
    root: str | os.PathLike[str] | None,
    home: str | os.PathLike[str] | None,
    task_class: str = "",
    environ: Mapping[str, str] | None = None,
) -> tuple[list[LaneFailure], list[LaneUnverified]]:
    """Pure decision: ``(failures, unverified)`` for the dispatch-set.

    ``fields`` is the post-recovery-filter dispatch-set (the same list the
    executor would loop over). ``failures`` holds one :class:`LaneFailure` per
    (unhealthy lane × waiting node) — a multi-role starvation surfaces every
    role that depends on the starved model. ``unverified`` holds one
    :class:`LaneUnverified` per (unverifiable lane × waiting node), applied in
    the same ``sorted_models`` order. The two lists are disjoint: a lane whose
    ``LaneHealth.ok`` is false is a failure, a lane that came back ok without a
    probe having answered (or with no result at all) is unverified.

    Pure by contract: no DB, no printing, no side effects. Tests assert
    on the same input twice and on ``capsys.readouterr()`` being empty.
    ``MO_LANE_HEALTH_GATE=0`` short-circuits to ``([], [])`` before any probe,
    so both public wrappers return empty without touching the registry.

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
    lane and must not start. Nothing was probed in that state, but the lane is
    reported as failed rather than unverified: the run is refused either way,
    and one reason beats two.
    """
    if _read_env(environ, "MO_LANE_HEALTH_GATE", "1") != "1":
        return [], []
    lanes = resolve_dispatch_lanes(
        fields, root=root, home=home, task_class=task_class, environ=environ,
    )
    if not lanes:
        return [], []
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
        ], []
    preflight = providers.preflight(sorted_models, root, environment=pre_env)

    # Index waiting nodes by model so one record per (lane, role, node)
    # is the deterministic iteration order. Preserve the resolution order
    # rather than re-sorting — operators expect the message to read in the
    # planner's order, not alphabetical.
    by_model: dict[str, list[DispatchLane]] = {m: [] for m in sorted_models}
    for dl in lanes:
        by_model[dl.model].append(dl)

    failures: list[LaneFailure] = []
    unverified: list[LaneUnverified] = []
    for model in sorted_models:
        health = preflight.get(model)
        if health is None:
            # The probe produced no entry for this lane at all: unknown, and
            # nobody checked — the same posture as an unprobed lane.
            for dl in by_model[model]:
                unverified.append(LaneUnverified(
                    lane=model, role=_node_role(dl.node_type),
                    node_id=dl.node_id,
                    reason="no preflight result for this lane",
                ))
            continue
        if not health.ok:
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
            continue
        if not health.verified:
            # ok=True on the strength of something other than a probe (a
            # native lane the CLI authenticates, a probe turned off, an
            # endpoint that never answered). Not dead — but not checked.
            for dl in by_model[model]:
                unverified.append(LaneUnverified(
                    lane=model, role=_node_role(dl.node_type),
                    node_id=dl.node_id, reason=health.reason,
                ))
    return failures, unverified


def gate(
    fields: Sequence[Sequence[str]],
    *,
    root: str | os.PathLike[str] | None,
    home: str | os.PathLike[str] | None,
    task_class: str = "",
    environ: Mapping[str, str] | None = None,
) -> list[LaneFailure]:
    """Pure decision: which lanes are dead, and which nodes are about to starve.

    The failure half of :func:`_evaluate` — see it for the resolution, purity
    and configuration-error contracts. Returns ``[]`` to proceed or one
    :class:`LaneFailure` per (unhealthy lane × waiting node).
    """
    return _evaluate(
        fields, root=root, home=home, task_class=task_class, environ=environ,
    )[0]


def unverified(
    fields: Sequence[Sequence[str]],
    *,
    root: str | os.PathLike[str] | None,
    home: str | os.PathLike[str] | None,
    task_class: str = "",
    environ: Mapping[str, str] | None = None,
) -> list[LaneUnverified]:
    """Pure decision: which lanes nobody actually checked.

    New **pure** function with the same keyword shape as :func:`gate`, over the
    same single probe (:func:`_evaluate`). One :class:`LaneUnverified` per
    (unverifiable lane × waiting node) in ``sorted_models`` order, where
    "unverifiable" means ``LaneHealth.ok and not LaneHealth.verified`` (or the
    probe returned no result for the lane). Same purity contract: no DB, no
    printing, no side effects. ``MO_LANE_HEALTH_GATE=0`` returns ``[]``.
    """
    return _evaluate(
        fields, root=root, home=home, task_class=task_class, environ=environ,
    )[1]


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
    gate, just skips the artifact.

    Written on the warn-and-proceed path too (``verdict="proceed"``), so the
    unverified lanes of a run that was allowed to start are still recorded
    where rubric/verify/reflect can read them."""
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
      appended with the gate's reason (read from ``payload["blocked_reason"]``,
      falling back to ``lane_unhealthy``, so both block reasons land correctly);
    * guarded by ``AND status NOT IN <run_status.SQL_TERMINAL>`` so a run that
      already settled is never overwritten;
    * one ``run_events`` row ``event_type='lane_preflight_blocked'`` — the same
      event type for both reasons, so no new type and no schema change.

    Swallows ``sqlite3.Error`` because the gate runs before the executor and a
    missing/locked DB must never escalate past the abort message.
    """
    if not db or not run_id or not os.path.isfile(db):
        return
    reason = str(payload.get("blocked_reason") or "lane_unhealthy")
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
                f"WHERE id=? AND status NOT IN {_run_status.SQL_TERMINAL}",
                (now, now, f"lane preflight: {reason}", run_id),
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
    rubric/verify/reflect can read it without re-running preflight. The reason
    is read from the payload (falling back to ``lane_unhealthy``) so both block
    reasons print a correct line. Called only on the block paths — a proceeding
    run prints no machine line at all.
    """
    print(f"lane_preflight={json.dumps(dict(payload), separators=(',', ':'))}")
    print(f"blocked_reason={payload.get('blocked_reason') or 'lane_unhealthy'}")
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
            f"MO_LANE_PROBE=0 leaves the gate on key presence and reports "
            f"those lanes as unverified)\n"
        )


def _unverified_payload(unverified: Sequence[LaneUnverified]) -> list[dict[str, Any]]:
    """The JSON shape of the unverified list, shared by both payload paths."""
    return [
        {"lane": u.lane, "role": u.role, "node_id": u.node_id, "reason": u.reason}
        for u in unverified
    ]


def _stderr_unverified(
    unverified: Sequence[LaneUnverified], *, blocking: bool,
) -> None:
    """Operator-facing line per unverifiable lane, then the toggle for the mode.

    ``blocking`` is the ``MO_LANE_REQUIRE_VERIFICATION=1`` abort: the same
    ``lane=… role=… node=… cannot verify: …`` shape, with the fix that turns the
    abort back into a warning. Otherwise the line is a ``warn:`` naming the
    lane, and the fix is the opt-in that would have blocked the run.
    """
    if not unverified:
        return
    for u in unverified:
        prefix = "" if blocking else "warn: "
        sys.stderr.write(
            f"{prefix}lane={u.lane}  role={u.role}  node={u.node_id}  "
            f"cannot verify: {u.reason}\n"
        )
    if blocking:
        sys.stderr.write(
            "fix: set MO_LANE_REQUIRE_VERIFICATION=0 to warn and proceed "
            f"instead of blocking (or verify lane={unverified[0].lane} — e.g. "
            "set its credential so the probe can run)\n"
        )
    else:
        sys.stderr.write(
            "warn: set MO_LANE_REQUIRE_VERIFICATION=1 to refuse a run whose "
            "lanes could not be verified\n"
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

    Returns ``""`` to proceed, ``"lane_unhealthy"`` when a lane is dead, or
    ``"lane_unverified"`` when lanes could not be verified and the operator
    opted into ``MO_LANE_REQUIRE_VERIFICATION=1``. The string only signals
    which ``blocked_reason`` to write into the lifecycle sink; the caller
    (execute.main) emits the rc and the lifecycle tail does the rest.

    Order is the kickoff's: failures win outright, so a dead lane blocks as
    ``lane_unhealthy`` even when unverifiable lanes are present and
    verification is required. Otherwise a lane nobody could check either
    blocks (opt-in) or warns while the run proceeds — with the artifact
    recording which lanes were never verified, and no ``blocked_reason=`` /
    ``verdict=`` machine line, so the lifecycle never scrapes a proceeding run
    as blocked.
    """
    failures, unverified_lanes = _evaluate(
        fields, root=root, home=home, task_class=task_class, environ=environ,
    )
    # The captured stderr is what the lifecycle scrapes for blocked_reason,
    # so direct ``sys.stderr.write`` (not a redirected print). The lifecycle
    # captures this module's writes via the redirect in _run_lifecycle_impl.
    if failures:
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
            "unverified": _unverified_payload(unverified_lanes),
            "checked_at": int(time.time()),
        }
        _emit_blocked_artifacts(run_dir, payload)
        _persist_blocked(
            _db_path(db, environ),
            run_id or _read_env(environ, "MINI_ORK_RUN_ID", ""),
            payload, environ=environ,
        )
        _print_machine_lines(payload)
        return "lane_unhealthy"
    if unverified_lanes:
        require = _read_env(environ, "MO_LANE_REQUIRE_VERIFICATION", "0") == "1"
        _stderr_unverified(unverified_lanes, blocking=require)
        payload = {
            "verdict": "blocked" if require else "proceed",
            "blocked_reason": "lane_unverified" if require else "",
            "failures": [],
            "unverified": _unverified_payload(unverified_lanes),
            "checked_at": int(time.time()),
        }
        _emit_blocked_artifacts(run_dir, payload)
        if not require:
            # Warn-and-proceed: the artifact records the unchecked lanes, and
            # nothing else changes — no DB write, no stdout machine line.
            return ""
        _persist_blocked(
            _db_path(db, environ),
            run_id or _read_env(environ, "MINI_ORK_RUN_ID", ""),
            payload, environ=environ,
        )
        _print_machine_lines(payload)
        return "lane_unverified"
    return ""
