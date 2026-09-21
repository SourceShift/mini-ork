"""Deterministic transforms for the goal-loop recipe.

`goal_state_eval` runs the units lister + predicate command once per wave
inside the target repo and materializes the per-unit pass/fail map. The
predicate is invoked as argv (NOT shell) so a unit id cannot escape its
argument position.

`sweep_dispatch_plan` reads the goal-state map, picks up to
MO_GOAL_MAX_CHILDREN_PER_WAVE failing units, and writes the wave's
fix-children plan. U4a only PLANS — actual child spawning arrives with the
U4b driver.

`goal_apply_deploy` CLOSES the loop: after the sweep node produces a fix in
the target worktree, this node deploys it (a caller-supplied commit+push
command), waits for the deploy to land, re-dispatches every swept unit, and
BLOCKS until each unit reaches a terminal state — so the downstream
goal_check verifier observes the true post-deploy result inside the same
wave. It is a no-op passthrough unless armed with MO_GOAL_APPLY=1.

All transforms are decorated with ``@register_transform`` so workflow.yaml
can name them in the ``transform:`` field of type:transform nodes. They run
inside the MiniOrk Python process (NOT inside a coding harness), keeping the
subprocess I/O reproducible and inspectable from the receipt layer.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from mini_ork.workflow.artifacts import ArtifactContractError, ArtifactLedger
from mini_ork.workflow.compiler import CompiledWorkflow
from mini_ork.workflow.transforms import register_transform

# Load the goal_state helpers by file path because ``recipes/`` is not a
# Python package. The recipe-local loader uses
# ``importlib.util.spec_from_file_location`` which gives this module a
# synthetic name without a parent package, so a normal
# ``from .goal_state import ...`` would fail with "attempted relative import
# with no known parent package". Mirror the recipe_register loader pattern
# at ``mini_ork/cli/recipe_register.py:67``.
_GOAL_STATE_PATH = Path(__file__).resolve().parent / "goal_state.py"
_goal_state_spec = importlib.util.spec_from_file_location(
    "goal_loop_goal_state", _GOAL_STATE_PATH,
)
if _goal_state_spec is None or _goal_state_spec.loader is None:
    raise ImportError(f"could not load goal_state helper from {_GOAL_STATE_PATH}")
_goal_state_module = importlib.util.module_from_spec(_goal_state_spec)
sys.modules.setdefault(_goal_state_spec.name, _goal_state_module)
_goal_state_spec.loader.exec_module(_goal_state_module)
evaluate_units = _goal_state_module.evaluate_units
list_units = _goal_state_module.list_units
harvest_evidence = _goal_state_module.harvest_evidence


def _slug(unit_id: str) -> str:
    """Filesystem-safe rendering of a unit id (unit ids are often file paths)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", unit_id)[:80] or "x"


def _evidence_sha(text: str) -> str:
    """Stable fingerprint of the exact evidence bundle a child was handed."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@register_transform("goal_state_eval")
def goal_state_eval(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Materialize ``goal-state.json`` from MO_GOAL_* env config.

    Inputs:
        ``MO_GOAL_TARGET_CWD`` — absolute path to the target repo.
        ``MO_GOAL_UNITS_CMD``  — command run inside target cwd, one unit id per line.
        ``MO_GOAL_PREDICATE_CMD`` — argv-prefix; ``<unit_id>`` appended per call.
        ``MO_GOAL_CONFIRM_RUNS`` — red-confirmation attempts (default "1").

    Output:
        ``<run_dir>/goal-state.json`` mapping
        ``unit_id -> {"pass": bool, "reason": str, "reproduced": bool, "attempts": int}``.
    """
    target_cwd = os.environ.get("MO_GOAL_TARGET_CWD")
    units_cmd = os.environ.get("MO_GOAL_UNITS_CMD")
    predicate_cmd = os.environ.get("MO_GOAL_PREDICATE_CMD")
    if not target_cwd or not units_cmd or not predicate_cmd:
        raise ArtifactContractError(
            "goal_state_eval requires MO_GOAL_TARGET_CWD, MO_GOAL_UNITS_CMD, "
            "and MO_GOAL_PREDICATE_CMD env vars",
        )

    try:
        confirm_runs = int(os.environ.get("MO_GOAL_CONFIRM_RUNS", "1"))
    except ValueError:
        confirm_runs = 1
    units = list_units(target_cwd, units_cmd)
    states = evaluate_units(target_cwd, predicate_cmd, units, confirm_runs=confirm_runs)
    node = workflow.nodes[node_id]
    if "goal_state" not in node.outputs:
        raise ArtifactContractError("goal_state_eval requires a goal_state output")
    out_path = ledger.output_path(workflow, node_id, "goal_state")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(states, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


def _unit_sort_key(unit_id: str) -> tuple[int, int, str]:
    """Order numeric unit ids numerically (``2`` before ``10``), non-numeric ids
    lexicographically after them. Chapter units are ``"1".."10"`` — plain
    ``sorted()`` string-orders them ``1,10,2,3,…`` so the freed child slot would
    rotate ch1→ch10; numeric ordering keeps rotation in natural ch1→ch2→ch3 order.
    """
    return (0, int(unit_id), "") if unit_id.isdigit() else (1, 0, unit_id)


def _quarantined_from_env() -> set[str]:
    """GRAO quarantine set the outer driver exports (newline-delimited).

    ``_default_run_wave_fn`` writes ``MO_GOAL_QUARANTINED_UNITS`` before shelling
    the wave recipe so this in-recipe selector can EXCLUDE units the loop has
    already given up on. Unset/blank → empty set (historical behavior).
    """
    return {
        u.strip()
        for u in (os.environ.get("MO_GOAL_QUARANTINED_UNITS") or "").splitlines()
        if u.strip()
    }


# Operator taxonomy — the CLASS of action a failure calls for. The loop's
# historical action set has size one: every wave spawns MO_GOAL_CHILD_RECIPE
# (code-fix) for the selected unit. That is a constant, not a policy, and it is
# why a failure the child cannot reach gets re-paid every wave with an
# identical patch: the loop can only fix DOWNSTREAM of its own configuration.
# Vocabulary: "code-fix" (default), "framework-edit", "dispatch-repair".
_DEFAULT_OPERATOR = "code-fix"

# A unit still `status=pending` with `attempts=0` never RAN. Nothing a child can
# patch inside the chapter-writing tree changes that — the cause is upstream, in
# the dispatcher that never started it. Observed live: ch4 held a stranded
# dispatch claim for 29 minutes while the loop spawned code-fix children against
# its prose, whose committed text was already 43KB.
_NEVER_DISPATCHED = re.compile(r"\bstatus=pending\b.*\battempts=0\b")

# Harness-shaped: a repair stage rejected the token/citation FORM an earlier
# stage emitted. Both stages are configured in the harness, so this is a
# stage-order conflict — patching the prose cannot fix a gate that rejects the
# form the pipeline was told to produce. Signature: a named repair/gate stage
# AND a citation-anchor complaint (the ch1 case, "0 of 10 edits anchored").
_HARNESS_STAGE = re.compile(r"GevalRepair|FinalReaderGevalGate", re.IGNORECASE)
_CITATION_FORM = re.compile(r"anchored|citation|\bcite\b", re.IGNORECASE)


def classify_failure(
    reason: str, history: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Name the operator class a unit's failure reason calls for.

    Returns ``(operator, rationale)``. Conservative by construction: an
    unambiguous signature, or the historical default ``code-fix``. Never raises
    — an unrecognized reason is ``code-fix``, which is today's behavior, so this
    can only ever ADD a diagnosis, never remove one.

    ``history`` is the unit's prior attempts. It is unused today and reserved so
    a repeated identical reason can escalate later without changing the contract.
    """
    text = reason or ""
    if _NEVER_DISPATCHED.search(text):
        return (
            "dispatch-repair",
            "status=pending with attempts=0 — the unit never ran, so the cause is "
            "upstream of anything a fix child can patch",
        )
    if _HARNESS_STAGE.search(text) and _CITATION_FORM.search(text):
        return (
            "framework-edit",
            "a repair/gate stage rejected the citation form an earlier stage "
            "emitted — a stage-order conflict in the harness, not the prose",
        )
    return _DEFAULT_OPERATOR, "no non-code signature; historical default"


def _operator_for(unit_id: str, goal_state: dict[str, Any]) -> tuple[str, str]:
    """``classify_failure`` for one unit, honoring ``MO_GOAL_OPERATOR_TYPING=0``.

    SHADOW: the result is RECORDED on the plan entry and in the loop's decision
    ledger. ``child_recipe`` — what actually spawns — is untouched, so a wave's
    behavior is byte-identical to today until the recorded operators have been
    graded against real outcomes.
    """
    if os.environ.get("MO_GOAL_OPERATOR_TYPING", "1").strip() == "0":
        return _DEFAULT_OPERATOR, "operator typing disabled"
    return classify_failure(str(goal_state.get(unit_id, {}).get("reason", "")))


def _select_units(
    goal_state: dict[str, Any],
    max_children: int,
    quarantined: set[str] | None = None,
) -> list[str]:
    """Pure wave-selection core (ledger-free, unit-testable).

    Picks the failing units (``pass`` is falsy) in numeric-aware order and caps
    at ``max_children``. Two exclusions apply:

    * GRAO quarantine — units the driver has already given up on are EXCLUDED,
      so a single-child-per-wave loop that would otherwise re-select the same
      stuck unit forever rotates its freed slot onto the next failing unit.
      This exclusion has a STARVATION GUARD: if EVERY failing unit is
      quarantined the exclusion is dropped, so the wave still dispatches and
      the driver's ``all_quarantined`` stop ends the loop cleanly rather than
      the selector silently returning nothing.

    * Null verdict (``reproduced`` is falsy) — a red the confirmation pass
      could NOT reproduce is EXCLUDED. This exclusion has DELIBERATELY NO
      starvation guard: if every remaining red is not reproducible,
      ``_select_units`` returns ``[]`` and an empty selection IS the honest
      verdict — there is nothing to fix, and dispatching a child anyway is the
      behaviour this cycle exists to remove. A legacy goal-state dict without a
      ``reproduced`` key defaults to ``True`` so pre-change artifacts select
      exactly as they do today.
    """
    failing = sorted(
        (uid for uid, state in goal_state.items()
         if not state.get("pass", False) and state.get("reproduced", True)),
        key=_unit_sort_key,
    )
    if quarantined:
        remaining = [uid for uid in failing if uid not in quarantined]
        if remaining:
            failing = remaining
    return failing[:max_children]


@register_transform("goal_sweep_plan")
def goal_sweep_plan(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Select failing units for the wave's fix children.

    Reads the upstream ``goal_state`` artifact written by ``goal_state_eval``,
    picks the failing units in numeric-aware order (``_select_units``) MINUS the
    driver's GRAO-quarantine set, and caps at ``MO_GOAL_MAX_CHILDREN_PER_WAVE``
    (default 3). The output ``sweep-plan.json`` lists
    ``{unit_id, child_recipe, kickoff_hint}`` entries — the outer U4b driver
    consumes this and dispatches the fix children. U4a only materializes the plan.
    """
    try:
        max_children = int(os.environ.get("MO_GOAL_MAX_CHILDREN_PER_WAVE", "3"))
    except ValueError:
        max_children = 3
    if max_children < 0:
        max_children = 0
    child_recipe = os.environ.get("MO_GOAL_CHILD_RECIPE", "")

    prepared = ledger.prepared_inputs(node_id)
    goal_state_paths = prepared.paths.get("goal_state", ())
    if not goal_state_paths:
        raise ArtifactContractError("goal_sweep_plan requires goal_state input")
    goal_state = json.loads(goal_state_paths[0].read_text(encoding="utf-8"))

    selected = _select_units(goal_state, max_children, _quarantined_from_env())

    # Deep-evidence harvest (optional): for ONLY the units we're about to
    # dispatch, run MO_GOAL_EVIDENCE_CMD to gather the rich failure signal the
    # one-line predicate reason can't carry. The full text is persisted to
    # <run_dir>/evidence/<slug>.md (so the fix child can re-read it whole) and a
    # copy is threaded into the plan so the spawn templater can inline it as
    # {{evidence}}. Unset MO_GOAL_EVIDENCE_CMD → the child falls back to reason.
    evidence_map = _harvest_selected_evidence(selected, goal_state)

    node = workflow.nodes[node_id]
    if "sweep_plan" not in node.outputs:
        raise ArtifactContractError("goal_sweep_plan requires a sweep_plan output")
    out_path = ledger.output_path(workflow, node_id, "sweep_plan")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # The operator CLASS this unit's reason calls for, recorded (not dispatched).
    operators = {unit_id: _operator_for(unit_id, goal_state) for unit_id in selected}

    plan = [
        {
            "unit_id": unit_id,
            "child_recipe": child_recipe,
            "operator": operators[unit_id][0],
            "operator_rationale": operators[unit_id][1],
            "kickoff_hint": {
                "unit_id": unit_id,
                "reason": goal_state[unit_id].get("reason", ""),
                "evidence": evidence_map.get(unit_id, {}).get("text", ""),
                "evidence_path": evidence_map.get(unit_id, {}).get("path", ""),
            },
            "evidence_sha": _evidence_sha(evidence_map.get(unit_id, {}).get("text", "")),
        }
        for unit_id in selected
    ]
    out_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


def _harvest_selected_evidence(
    selected: list[str],
    goal_state: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Harvest MO_GOAL_EVIDENCE_CMD output for the selected units.

    Returns ``{unit_id: {"text": <inline evidence>, "path": <evidence file>}}``.
    Best-effort: a missing command, blank target cwd, or a harvest crash yields
    the predicate reason as a graceful fallback so the wave never blocks on the
    evidence stage. The full text is also written to
    ``<run_dir>/evidence/<slug>.md`` for the child to read in full.
    """
    evidence_cmd = (os.environ.get("MO_GOAL_EVIDENCE_CMD") or "").strip()
    if not evidence_cmd or not selected:
        return {}
    target_cwd = os.environ.get("MO_GOAL_TARGET_CWD") or os.getcwd()
    try:
        raw = harvest_evidence(target_cwd, evidence_cmd, selected)
    except Exception as exc:  # noqa: BLE001 — evidence is advisory, never fatal
        raw = {uid: f"[evidence harvest failed: {exc}]" for uid in selected}

    run_dir = os.environ.get("MINI_ORK_RUN_DIR")
    ev_dir = Path(run_dir) / "evidence" if run_dir else None
    if ev_dir is not None:
        ev_dir.mkdir(parents=True, exist_ok=True)

    history_block = _render_wave_history_block(os.environ.get("MO_GOAL_WAVE_HISTORY", ""))

    out: dict[str, dict[str, str]] = {}
    for uid in selected:
        text = raw.get(uid, "") or goal_state.get(uid, {}).get("reason", "")
        if history_block:
            text = text + "\n" + history_block
        path_str = ""
        if ev_dir is not None:
            ev_path = ev_dir / f"{_slug(uid)}.md"
            try:
                ev_path.write_text(text + "\n", encoding="utf-8")
                path_str = str(ev_path)
            except OSError:
                path_str = ""
        out[uid] = {"text": text, "path": path_str}
    return out


def _render_wave_history_block(raw: str) -> str:
    """Render MO_GOAL_WAVE_HISTORY as a 'do NOT repeat these' section.

    Empty on absent/unparseable input, so an unset env var yields a
    byte-identical evidence file to today.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        waves = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(waves, list) or not waves:
        return ""
    lines = ["### Prior waves — do NOT repeat these", ""]
    if (os.environ.get("MO_GOAL_EVIDENCE_UNINFORMATIVE") or "").strip() == "1":
        lines += [
            f"The evidence above has been handed to the last {len(waves)} waves "
            "UNCHANGED and the predicate did not move. The cause is upstream of "
            "what those children patched — re-diagnose before editing the same "
            "surface again.",
            "",
        ]
    lines += ["| wave | attempted | child said | diff bytes | headroom | moved |",
              "|---|---|---|---|---|---|"]
    for w in waves:
        if not isinstance(w, dict):
            continue
        lines.append(
            f"| {w.get('wave')} | {','.join(map(str, w.get('attempted') or [])) or '-'} "
            f"| {','.join(map(str, w.get('child_verdict') or [])) or '-'} "
            f"| {','.join(map(str, w.get('review_diff_bytes') or [])) or '-'} "
            f"| {w.get('headroom_closed')} | {w.get('predicate_moved')} |"
        )
    lines += [
        "",
        "If a row above shows `child said=pass` with `moved=False`, that wave's "
        "patch was a self-approved non-fix. Do not reproduce it.",
        "",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# goal_apply_deploy — the loop-closing node (deploy fix -> re-dispatch -> await)
# ─────────────────────────────────────────────────────────────────────────

# Sweep outcomes whose fix child actually ran against the target worktree, so a
# candidate patch may be present to deploy. ``deferred`` (spawn-cap) and
# ``dry_run`` never touched the tree. A ``failed`` child still commonly leaves a
# real patch behind (a later node — reviewer/eval — reddened the child while its
# earlier edit survived), so it is included; whether there is anything to ship is
# decided by MO_GOAL_APPLY_CMD (a clean tree makes the deploy a harmless no-op).
_APPLIED_SWEEP_STATUSES = frozenset({"spawned", "failed", "fanned_out"})


def _int_env(name: str, default: int) -> int:
    """Read an int env var; a blank/absent/garbage value falls back to default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _run(cmd: list[str] | str, *, cwd: str | None, shell: bool) -> dict[str, Any]:
    """Run a command, capturing a truncated stdout/stderr receipt. Never raises."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, shell=shell, capture_output=True, text=True,
        )
        return {
            "cmd": cmd,
            "rc": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-2000:],
        }
    except OSError as exc:
        return {"cmd": cmd, "rc": 127, "stdout": "", "stderr": f"exec failed: {exc}"}


def _poll_until_zero(
    run_fn: Callable[[], int], *, timeout_s: int, poll_s: int,
) -> dict[str, Any]:
    """Call ``run_fn`` until it returns 0 or ``timeout_s`` elapses.

    ``poll_s == 0`` (test seam) collapses to at most two probes: the wait
    accounting jumps past the timeout so the loop cannot spin CPU-hot forever
    on a predicate that never settles.
    """
    step = poll_s if poll_s > 0 else (timeout_s + 1)
    waited = 0
    last: int | None = None
    while True:
        last = run_fn()
        if last == 0:
            return {"ok": True, "waited_s": waited, "last_rc": last}
        if waited >= timeout_s:
            return {"ok": False, "waited_s": waited, "last_rc": last}
        if poll_s > 0:
            time.sleep(poll_s)
        waited += step


def _poll_until_settled(
    *,
    success_fn: Callable[[], int],
    fail_fn: Callable[[], int] | None,
    timeout_s: int,
    poll_s: int,
) -> dict[str, Any]:
    """Poll a success probe, and an optional terminal-failure probe, until one
    settles or ``timeout_s`` elapses.

    ``success_fn() == 0`` ⇒ the goal was reached (``settled: True``).
    ``fail_fn() == 0`` ⇒ the unit reached a TERMINAL failure/stall — stop
    awaiting immediately (``settled: False, terminal: True``) instead of
    burning the whole window on a regen that will never pass. When ``fail_fn``
    is ``None`` this collapses to the historical wait-for-success-only loop.

    The success probe is checked FIRST every cycle: a unit that both passed and
    (racily) trips the failure probe is reported as passed. ``poll_s == 0``
    (test seam) collapses to at most two probes, mirroring ``_poll_until_zero``.
    """
    step = poll_s if poll_s > 0 else (timeout_s + 1)
    waited = 0
    last: int | None = None
    while True:
        last = success_fn()
        if last == 0:
            return {"settled": True, "terminal": True, "reason": "goal_met",
                    "waited_s": waited, "last_rc": last}
        if fail_fn is not None and fail_fn() == 0:
            return {"settled": False, "terminal": True, "reason": "terminal_failure",
                    "waited_s": waited, "last_rc": last}
        if waited >= timeout_s:
            return {"settled": False, "terminal": False, "reason": "timeout",
                    "waited_s": waited, "last_rc": last}
        if poll_s > 0:
            time.sleep(poll_s)
        waited += step


def _units_to_apply(run_dir: str) -> list[str]:
    """Read ``sweep-result.json`` and return the swept unit ids worth deploying."""
    sweep_path = Path(run_dir) / "sweep-result.json"
    if not sweep_path.is_file():
        return []
    try:
        data = json.loads(sweep_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    units = data.get("units", []) if isinstance(data, dict) else []
    out: list[str] = []
    for entry in units:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status", "")) in _APPLIED_SWEEP_STATUSES:
            uid = entry.get("unit_id")
            if uid is not None:
                out.append(str(uid))
    return out


# ── instrument guard: the fix child may not edit what scores it ──────────────
#
# The goal predicate is only as trustworthy as the code that writes its inputs.
# ``committed_complete`` and ``rubric_status`` are columns written by researcher
# code that lives INSIDE the fix child's own editable tree, and ``scope_gate``
# does NOT filter paths — it checks a task_class allowlist
# (mini_ork/gates/gate_registry.py::_evaluate_scope). So a child that cannot
# make a chapter pass can instead make the chapter's PASS MEANING cheaper, and
# this stage would faithfully deploy that edit to the running worker. The guard
# closes the loop from the other end: before anything is shipped, ask git
# whether the child touched a path the operator declared part of the
# instrument, and refuse to deploy if it did. Generation code stays fixable —
# only the scoring decision is frozen.


def _protected_globs() -> list[str]:
    """Globs from ``MO_GOAL_PROTECTED_PATHS``, newline/comma separated.

    ``#`` starts a comment. Unset/empty ⇒ the guard is inert (the historical
    behavior): arming it is an explicit operator act, and only then does a
    probe failure count as a violation.
    """
    raw = os.environ.get("MO_GOAL_PROTECTED_PATHS", "").replace(",", "\n")
    return [
        glob
        for glob in (chunk.strip() for chunk in raw.splitlines())
        if glob and not glob.startswith("#")
    ]


def _protected_mode() -> str:
    """``refuse`` (default, fail-closed) or ``warn`` (record the hit, ship anyway)."""
    return os.environ.get("MO_GOAL_PROTECTED_MODE", "refuse").strip().lower() or "refuse"


def _git_touched(cwd: str, glob: str) -> list[str] | None:
    """Paths under ``glob`` that differ from HEAD (staged, unstaged, untracked).

    Git does the matching, so a rename, a quoted path, or a nested directory
    cannot slip past a hand-rolled parser. ``None`` means the probe itself
    failed (not a repo, git missing) — the caller treats that as a violation,
    because a guard that cannot see the tree must not wave the deploy through.
    """
    res = _run(
        ["git", "-C", cwd, "status", "--porcelain", "-uall", "--", glob],
        cwd=None, shell=False,
    )
    if res["rc"] != 0:
        return None
    return [line for line in res["stdout"].splitlines() if line.strip()]


def _protected_violations(target_cwd: str) -> list[dict[str, Any]]:
    """``[{glob, paths}]`` for every instrument glob the fix child has touched."""
    found: list[dict[str, Any]] = []
    for glob in _protected_globs():
        touched = _git_touched(target_cwd, glob)
        if touched is None:
            found.append({"glob": glob, "paths": [], "error": "git-status-failed"})
        elif touched:
            found.append({"glob": glob, "paths": touched[:20]})
    return found


def _await_deploy() -> dict[str, Any]:
    """Block until the pushed fix is live on the target system.

    Two modes: poll ``MO_GOAL_AWAIT_DEPLOY_CMD`` until exit 0 (the caller's
    proof the new code is running), or — when that command is unset — sleep a
    fixed settle window that covers the target's auto-deploy latency.
    """
    cmd = os.environ.get("MO_GOAL_AWAIT_DEPLOY_CMD")
    if not cmd:
        settle = _int_env("MO_GOAL_DEPLOY_SETTLE_SECONDS", 600)
        if settle > 0:
            time.sleep(settle)
        return {"mode": "settle", "seconds": settle}
    timeout_s = _int_env("MO_GOAL_DEPLOY_TIMEOUT_SECONDS", 900)
    poll_s = _int_env("MO_GOAL_APPLY_POLL_SECONDS", 60)
    result = _poll_until_zero(
        lambda: _run(cmd, cwd=None, shell=True)["rc"],
        timeout_s=timeout_s, poll_s=poll_s,
    )
    return {"mode": "poll", "deployed": result["ok"], **result}


def _await_terminal(unit_id: str, cwd: str) -> dict[str, Any]:
    """Block until ``unit_id`` reaches a terminal state after re-dispatch.

    SUCCESS is the goal predicate passing: polls ``MO_GOAL_TERMINAL_CMD``
    (argv prefix + unit_id) when set, else ``MO_GOAL_PREDICATE_CMD``.

    FAILURE is optional and fast: when ``MO_GOAL_TERMINAL_FAIL_CMD`` is set and
    returns 0, the unit has terminally failed or stalled (e.g. a chapter left
    orphaned in ``generating`` with a fresh ``last_error``, or ``status=failed``
    / ``permanently_failed``). Without it the await is blind to failure and
    burns the entire ``MO_GOAL_APPLY_AWAIT_SECONDS`` window on a regen that will
    never pass — so a bad deploy costs 90 min before the loop can react. With
    it, the loop learns its deploy failed within one poll and the outer driver
    can quarantine / re-plan / stop. Unset ⇒ historical wait-for-pass behavior.

    The unit id is always the final argv slot — never shell-interpolated.
    """
    terminal_cmd = os.environ.get("MO_GOAL_TERMINAL_CMD") or os.environ.get(
        "MO_GOAL_PREDICATE_CMD",
    )
    if not terminal_cmd:
        return {"settled": False, "reason": "no MO_GOAL_TERMINAL_CMD/PREDICATE_CMD"}
    pass_argv = shlex.split(terminal_cmd) + [unit_id]
    fail_cmd = os.environ.get("MO_GOAL_TERMINAL_FAIL_CMD")
    fail_argv = shlex.split(fail_cmd) + [unit_id] if fail_cmd else None
    timeout_s = _int_env("MO_GOAL_APPLY_AWAIT_SECONDS", 5400)
    poll_s = _int_env("MO_GOAL_APPLY_POLL_SECONDS", 60)
    result = _poll_until_settled(
        success_fn=lambda: _run(pass_argv, cwd=cwd, shell=False)["rc"],
        fail_fn=(lambda: _run(fail_argv, cwd=cwd, shell=False)["rc"])
        if fail_argv is not None
        else None,
        timeout_s=timeout_s, poll_s=poll_s,
    )
    return {"unit_id": unit_id, **result}


def _run_apply(run_dir: str) -> dict[str, Any]:
    """Core of ``goal_apply_deploy`` — env-driven, ledger-free, unit-testable.

    Returns the ``apply-result.json`` payload. The transform wrapper only
    resolves the output path and writes the returned dict.
    """
    if os.environ.get("MO_GOAL_APPLY", "").strip() != "1":
        return {"status": "disabled", "units": []}

    target_cwd = os.environ.get("MO_GOAL_TARGET_CWD")
    apply_cmd = os.environ.get("MO_GOAL_APPLY_CMD")
    redispatch_cmd = os.environ.get("MO_GOAL_REDISPATCH_CMD")
    missing = [
        name
        for name, val in (
            ("MO_GOAL_TARGET_CWD", target_cwd),
            ("MO_GOAL_APPLY_CMD", apply_cmd),
            ("MO_GOAL_REDISPATCH_CMD", redispatch_cmd),
        )
        if not val
    ]
    if missing:
        raise ArtifactContractError(
            "goal_apply_deploy (MO_GOAL_APPLY=1) requires " + ", ".join(missing),
        )
    assert target_cwd and apply_cmd and redispatch_cmd  # narrowed for type-checkers

    units = _units_to_apply(run_dir)

    # Diagnostics-first: computed before the dry-run return so a rehearsal
    # reports the same verdict the live run would reach. Note this runs even
    # when ``units`` is empty — the deploy command fires unconditionally below,
    # so an empty sweep is not a reason to skip the check.
    violations = _protected_violations(target_cwd)

    if os.environ.get("MO_GOAL_APPLY_DRY", "").strip() == "1":
        return {
            "status": "dry_run",
            "target_cwd": target_cwd,
            "deploy_cmd": apply_cmd,
            "redispatch_cmd": redispatch_cmd,
            "protected_violations": violations,
            "units": [
                {"unit_id": u, "would": ["deploy", "await_deploy", "redispatch", "await_regen"]}
                for u in units
            ],
        }

    if violations and _protected_mode() != "warn":
        return {
            "status": "refused_instrument_edit",
            "target_cwd": target_cwd,
            "units": [],
            "violations": violations,
            "hint": (
                "the fix child edited a path the operator declared part of the "
                "instrument that scores it; NOTHING was deployed. Revert those "
                "paths, or set MO_GOAL_PROTECTED_MODE=warn to ship anyway."
            ),
        }

    # ── live: DEPLOY -> await-deploy -> REDISPATCH -> await-regen ──
    # Ordering is load-bearing: re-dispatching before the new code is live would
    # let the stale worker pick the unit up and re-fail with the same bug.
    deploy = _run(apply_cmd, cwd=target_cwd, shell=True)
    await_deploy = _await_deploy()
    unit_results: list[dict[str, Any]] = []
    for unit_id in units:
        redispatch = _run(
            shlex.split(redispatch_cmd) + [unit_id], cwd=target_cwd, shell=False,
        )
        settled = _await_terminal(unit_id, target_cwd)
        unit_results.append(
            {"unit_id": unit_id, "redispatch": redispatch, "await_regen": settled},
        )
    return {
        "status": "applied",
        "deploy": deploy,
        "await_deploy": await_deploy,
        "protected_violations": violations,
        "units": unit_results,
    }


@register_transform("goal_apply_deploy")
def goal_apply_deploy(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Deploy the sweep's fix, re-dispatch swept units, and await their terminal state.

    No-op passthrough (status ``disabled``) unless MO_GOAL_APPLY=1. See
    ``_run_apply`` for the full env contract; the wrapper only resolves the
    run-local ``apply-result.json`` output and persists the payload.
    """
    node = workflow.nodes[node_id]
    if "apply_result" not in node.outputs:
        raise ArtifactContractError("goal_apply_deploy requires an apply_result output")
    out_path = ledger.output_path(workflow, node_id, "apply_result")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", str(out_path.parent))
    payload = _run_apply(run_dir)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path