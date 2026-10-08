"""Auto-repair — a failed run fixes itself in a bounded feedback loop.

A failed run used to wait for a human click (``mini-ork recover``) or be
abandoned. This module is the *policy* + *apply* half of restoring the user
rule "instead of accepting that these failed … always revive and fix":

    diagnose → pick a repair → apply it → resume the SAME run from the failed
    node → repeat, bounded → hand over to the human only when stuck.

``decide`` is pure policy (no side effects): it reads the retry hint, the run's
lifecycle events, the level report and the ``repair.json`` history and returns a
decision dict. ``apply`` performs it: it writes the revise/prove feedback file,
appends the attempt to ``repair.json``, emits a ``run_events`` row, and — when
there is something to resume — spawns a detached ``mini-ork recover``.

The intended loop is a chain of detached processes: the ``run`` / ``recover``
flow that owns a terminal failure calls :func:`maybe_repair`; when it spawns a
``recover``, that child inherits ``MO_AUTO_REPAIR_ATTEMPT`` and, when *it* fails
again, decides the next attempt through the same hooks. Attempts are bounded by
``MO_AUTO_REPAIR_MAX`` (default 2) and a spend ceiling
``MO_AUTO_REPAIR_BUDGET_USD`` (default 10).

Default-on, explicit: the ``run`` flow publishes ``MO_AUTO_REPAIR=1`` before
execute when the operator left it unset. ``MO_AUTO_REPAIR=0`` is never
overridden and turns the whole loop off (``decide`` returns ``action="none"``).
The manual ``mini-ork repair`` CLI treats unset as *on*.

Failure-triage sequencing: ``mini_ork.triage`` escalates a failure to a separate
framework-edit epic. Auto-repair fixes the SAME run first, so it owns the trigger
and calls triage exactly once on *give-up* (and only when ``MO_FAILURE_TRIAGE=1``
opts in), recording ``"triaged"`` on every give-up so absence is explicit.

Hand-off: the ``run`` / ``recover`` flows spawn their repair from inside the
lifecycle that still OWNS the run record (``main._close_run_record`` runs in the
caller's ``finally``, after reflect). A child that dispatched immediately would
have its ``executing`` status flipped to ``failed`` by that teardown, so those
callers mark the spawn with ``MO_AUTO_REPAIR_WAIT_PID`` and the child blocks in
:func:`wait_for_spawner` until the spawner has exited. A spawn from a process
that does not own the run (``repair --sweep``, the board) is not marked. A
spawned recover that bails out before dispatching is handed to the human by
:func:`note_stuck` rather than left with ``state='repairing'`` and no owner.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_ork.context import context_env

# ── tuning (env-overridable) ────────────────────────────────────────────────
_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_BUDGET_USD = 10.0
_DEFAULT_ESCALATE_LANE = "opus"
# Safety valve for ``wait_for_spawner``: the spawner is a run lifecycle that may
# still be reflecting (``MO_REFLECT_TIMEOUT_SECONDS`` = 360 s by default), so the
# bound only exists to keep a pathological spawner from stalling a repair forever.
_DEFAULT_WAIT_S = 1800.0

_ATTEMPT_FILE = "repair.json"
_TERMINAL_FAILED = ("failed", "rolled_back")

# Child env markers set by ``_spawn_recover``: the attempt number this child is
# executing, the run it is repairing (``note_stuck`` needs both), and the pid of
# the process that spawned it (lifecycle-owned spawns only — see
# ``wait_for_spawner``).
_ATTEMPT_ENV = "MO_AUTO_REPAIR_ATTEMPT"
_RUN_ID_ENV = "MO_AUTO_REPAIR_RUN_ID"
_WAIT_PID_ENV = "MO_AUTO_REPAIR_WAIT_PID"

# Explicit failing signals that mean "the environment/transport died, not the
# change". The kickoff's full vocabulary (killed, sigterm, rc_137, …) is kept so
# a future executor that emits it is classified; ``timeout`` / ``cost_limit`` are
# the values this codebase actually emits today (``finish_reason_for_failure``).
_INFRA_FINISH = frozenset({
    "timeout", "cost_limit", "killed", "sigterm", "rc_137", "rc_143",
    "network", "provider_error", "overloaded", "rate_limited",
})
# ``node_attempts.failure_class`` values that mean provider/infra trouble.
_INFRA_FAILURE_CLASSES = frozenset({"infra_interrupt", "provider_limit"})

# Hint ``needs_change.kind`` values the *owner* must fix — auto-repair scans
# these for a human rather than spawning anything.
_OWNER_KINDS = frozenset({"environment", "credentials", "budget"})

# Reviewer verdicts that mean "the change needs another round".
_FAIL_VERDICTS = frozenset({
    "needs_revision", "reject", "fail", "request_changes", "escalate",
    "failed", "crash",
})

# Actions that resume the run through ``mini-ork recover``.
_SPAWNABLE = frozenset({"revise", "prove", "lane", "infra", "reverify"})

# Re-entrancy guard: run ids this *process* already spawned a repair for. The
# loop is a chain of detached processes (a spawned recover that fails again
# spawns the next one), so a process that has already handed a run to a child
# must not spawn a second one for the same failure.
_SPAWNED_THIS_PROCESS: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_int(key: str, default: int) -> int:
    try:
        return int(context_env(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(context_env(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _max_attempts() -> int:
    return _env_int("MO_AUTO_REPAIR_MAX", _DEFAULT_MAX_ATTEMPTS)


def _budget_usd() -> float:
    return _env_float("MO_AUTO_REPAIR_BUDGET_USD", _DEFAULT_BUDGET_USD)


def _escalate_lane() -> str:
    return context_env("MO_AUTO_REPAIR_ESCALATE_LANE", _DEFAULT_ESCALATE_LANE) or _DEFAULT_ESCALATE_LANE


# ── small readers ────────────────────────────────────────────────────────────


def _run_dir(home: Path, run_id: str) -> Path:
    return Path(home) / "runs" / run_id


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _load_repair(run_dir: Path) -> dict[str, Any]:
    """``repair.json`` as a dict — a fresh skeleton when absent/unreadable."""
    data = _read_json(run_dir / _ATTEMPT_FILE)
    if not isinstance(data, dict):
        data = {}
    attempts = data.get("attempts")
    if not isinstance(attempts, list):
        data["attempts"] = []
    return data


def _write_repair(run_dir: Path, data: dict[str, Any]) -> None:
    """Best-effort atomic write of ``repair.json`` (temp + ``os.replace``)."""
    target = run_dir / _ATTEMPT_FILE
    tmp = run_dir / f".{_ATTEMPT_FILE}.tmp.{os.getpid()}"
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _recipe_of(home: Path, run_id: str) -> str:
    """The run's recipe from ``run_profile.json``, else the ``task_runs`` row."""
    profile = _read_json(_run_dir(home, run_id) / "run_profile.json")
    if isinstance(profile, dict) and profile.get("recipe"):
        return str(profile["recipe"])
    try:
        from mini_ork.web.db import db_for
        db = db_for(Path(home))
        if db.has_table("task_runs"):
            row = db.row("SELECT recipe FROM task_runs WHERE id = ? LIMIT 1", (run_id,))
            if row and row.get("recipe"):
                return str(row["recipe"])
    except Exception:  # noqa: BLE001 — recipe is advisory; never raise
        pass
    return ""


def _run_status(home: Path, run_id: str) -> str:
    from mini_ork.recovery import retry_hint
    return retry_hint._current_run_status(Path(home), run_id)


def _is_withheld(run_dir: Path) -> bool:
    """True when the run's publish was *withheld* — the level report abstains.

    ``publisher.py`` forces ``status='failed'`` when it withholds (``abstain``),
    so a withheld run is already a failed run and rule 1 admits it. But a run
    whose level report still abstains while ``task_runs.status`` reads
    ``published`` — a later status rewrite, e.g. a manual republish that
    bypassed the level gate — is that *same* withheld run. Rule 5 (reverify)
    owns it, so rule 1 must not short-circuit it as a non-failed run. Reuses
    ``task_state.withheld_publish`` so it can never disagree with the retry
    hint, the run tile or the outcome page about "merely withheld".
    """
    try:
        from mini_ork.acp.task_state import withheld_publish
        return withheld_publish(Path(run_dir), None) is not None
    except Exception:  # noqa: BLE001 — a missing report is simply "not withheld"
        return False


def _events(home: Path, run_id: str) -> list[dict[str, Any]] | None:
    from mini_ork.recovery import retry_hint
    return retry_hint._run_node_events(Path(home), run_id)


def _workflow(home: Path, recipe: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from mini_ork.recovery import retry_hint
    return retry_hint._recipe_workflow(Path(home), recipe)


def _node_type(home: Path, recipe: str, node_name: str | None) -> str:
    if not node_name:
        return ""
    nodes, _edges = _workflow(home, recipe)
    for n in nodes:
        if str(n.get("name")) == node_name:
            return str(n.get("type") or "")
    return ""


def _first_node_of_type(home: Path, recipe: str, node_type: str) -> str:
    """The first workflow node of ``node_type`` in topo order (``""`` on miss)."""
    from mini_ork.recovery import retry_hint
    nodes, edges = _workflow(home, recipe)
    by_name = {str(n.get("name")): n for n in nodes if n.get("name")}
    for name in retry_hint._topo_order(nodes, edges):
        if str((by_name.get(name) or {}).get("type") or "") == node_type:
            return name
    return ""


def _first_verifier_node(home: Path, recipe: str) -> str:
    from mini_ork.recovery import retry_hint
    nodes, edges = _workflow(home, recipe)
    return retry_hint._first_verifier_node(nodes, edges)


def _role_lane(home: Path, run_id: str, node_name: str) -> str:
    """The lane a workflow node ran on — from ``ide_pages.run._load`` (best effort)."""
    if not node_name:
        return ""
    try:
        from mini_ork.ide_pages import run as run_page
        run = run_page._load(Path(home), run_id)
        if run is None:
            return ""
        node = run.nodes.get(node_name)
        return str(getattr(node, "role_lane", "") or "") if node is not None else ""
    except Exception:  # noqa: BLE001 — lane is advisory
        return ""


def _failure_class(home: Path, run_id: str, node_name: str | None) -> str:
    from mini_ork.recovery import retry_hint
    try:
        if node_name:
            fc, _tail = retry_hint._failure_class_for_node(Path(home), run_id, node_name)
            if fc:
                return str(fc)
        fc, _node = retry_hint._failure_class_for_any(Path(home), run_id)
        return str(fc or "")
    except Exception:  # noqa: BLE001
        return ""


def _levels_reasons(run_dir: Path) -> dict[str, Any]:
    try:
        from mini_ork.acp.task_state import run_level_report
        report = run_level_report(run_dir)
        if isinstance(report, dict) and isinstance(report.get("levels_reasons"), dict):
            return report["levels_reasons"]
    except Exception:  # noqa: BLE001
        pass
    return {}


def _first_finding(run_dir: Path) -> str:
    """The first reviewer finding issue, else the first failing verifier check id."""
    for path in sorted(run_dir.glob("review-*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        findings = data.get("findings")
        if isinstance(findings, list):
            for f in findings:
                if isinstance(f, dict) and f.get("issue"):
                    return str(f["issue"])[:200]
    for path in sorted(run_dir.glob("verifier_*.json")):
        data = _read_json(path)
        if isinstance(data, dict):
            for key in ("failed_checks", "checks"):
                checks = data.get(key)
                if isinstance(checks, list):
                    for c in checks:
                        if isinstance(c, dict) and (
                            c.get("status") in ("FAIL", "fail")
                            or c.get("pass") is False
                        ):
                            return str(c.get("id") or c.get("name") or "")[:200]
    return ""


def _signature(failed_node: str | None, reason: str, finding: str) -> str:
    raw = f"{failed_node or ''}|{reason or ''}|{finding or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _decision(action: str, *, from_node: str | None = None,
              lanes: dict[str, str] | None = None, ack_change: bool = False,
              feedback: str = "", reason: str = "", signature: str = "",
              stop: bool = False) -> dict[str, Any]:
    """A decision. ``stop`` marks a *stop rule* (attempts exhausted, budget,
    no progress) — a give-up the loop reached itself, as opposed to a first
    failure it hands over immediately. See ``apply``'s ``repair.json`` state.
    """
    return {
        "action": action,
        "from_node": from_node,
        "lanes": dict(lanes or {}),
        "ack_change": bool(ack_change),
        "feedback": feedback,
        "reason": reason,
        "signature": signature,
        "stop": bool(stop),
    }


# ── policy ───────────────────────────────────────────────────────────────────


def decide(home: Path, run_id: str) -> dict[str, Any]:
    """Pure policy — the repair decision for ``run_id`` (no side effects).

    Returns ``{"action", "from_node", "lanes", "ack_change", "feedback",
    "reason", "signature"}``. ``action`` is one of ``none``, ``human``, ``lane``,
    ``infra``, ``reverify``, ``prove`` or ``revise``. First match wins; the rule
    order is the kickoff's policy table.
    """
    home = Path(home)
    if context_env("MO_AUTO_REPAIR", "1") == "0":
        return _decision("none", reason="auto-repair disabled (MO_AUTO_REPAIR=0)")

    run_dir = _run_dir(home, run_id)
    status = _run_status(home, run_id)
    # Rule 1 is "the run is not failed/rolled_back". A withheld publish counts
    # as failed even when the status flag disagrees (see ``_is_withheld``): the
    # loop must not go blind to a withheld run that a status rewrite republished.
    if status not in _TERMINAL_FAILED and not _is_withheld(run_dir):
        return _decision("none", reason=f"run is not failed/rolled_back (status={status or 'unknown'})")

    recipe = _recipe_of(home, run_id)
    hint = _load_hint(home, run_id)
    events = _events(home, run_id)
    failing = None
    if events is not None:
        from mini_ork.acp.task_state import _failing_node
        failing = _failing_node(events)
    failed_node = failing[0] if failing else None
    fail_reason = failing[1] if failing else ""

    withheld: list[str] | None = None
    if events is not None:
        from mini_ork.acp.task_state import withheld_publish
        withheld = withheld_publish(run_dir, events)

    repair = _load_repair(run_dir)
    attempts = list(repair.get("attempts") or [])

    finding = _first_finding(run_dir)
    sig_reason = fail_reason or (f"withheld:{','.join(withheld)}" if withheld else "")
    signature = _signature(failed_node, sig_reason, finding)

    nc = hint.get("needs_change") if isinstance(hint, dict) else None
    nc = nc if isinstance(nc, dict) else None
    hint_kind = str(nc.get("kind") or "") if nc else ""

    # 2. Stop: human needed.
    if len(attempts) >= _max_attempts():
        return _decision("human", from_node=failed_node, signature=signature, stop=True,
                         reason=f"repair attempts exhausted ({len(attempts)}/{_max_attempts()})")
    spent = _cost_since_first_repair(home, run_id, attempts)
    if spent is not None and spent > _budget_usd():
        return _decision("human", from_node=failed_node, signature=signature, stop=True,
                         reason=f"repair budget exceeded (${spent:.2f} > ${_budget_usd():.2f})")
    # "No progress" is the same failure signature twice. It does NOT apply to a
    # withheld run: the second identical attempt deliberately escalates
    # reverify → prove (a *different* action), which is progress, not a stall.
    if attempts and str(attempts[-1].get("signature") or "") == signature \
            and not (withheld and not failed_node):
        return _decision("human", from_node=failed_node, signature=signature, stop=True,
                         reason="no progress — the same failure signature repeats")

    # 3. Dead lane → switch it.
    if hint_kind == "lane" and isinstance(nc.get("suggestions"), list) and nc["suggestions"]:
        first = nc["suggestions"][0]
        lane = str((first or {}).get("lane") or "")
        alias = str(nc.get("alias") or "")
        if lane and alias:
            return _decision(
                "lane",
                from_node=str((hint or {}).get("from_node") or failed_node or "") or None,
                lanes={alias: lane},
                reason=f"lane {alias} is unavailable — switch to {lane}",
                signature=signature,
            )

    # 4. Infra / provider trouble → same node, same lane.
    if _is_infra(home, run_id, hint, nc, failed_node, fail_reason):
        return _decision(
            "infra",
            from_node=failed_node or (str((hint or {}).get("from_node") or "") or None),
            reason=f"infrastructure failure ({fail_reason or hint_kind or 'provider'})",
            signature=signature,
        )

    # 5. Withheld publish with no failing node → re-verify; a repeat → prove.
    if withheld and not failed_node:
        prior_reverify = any(str(a.get("action")) == "reverify" for a in attempts)
        if prior_reverify:
            impl = _first_node_of_type(home, recipe, "implementer")
            level = withheld[0]
            lr = str(_levels_reasons(run_dir).get(level, ""))
            feedback = (
                f"Every check passed but level `{level}` is UNVERIFIED (`{lr}`). "
                "Add the smallest test that exercises the change so the level can "
                "be proven; change nothing else."
            )
            return _decision("prove", from_node=impl or None, feedback=feedback,
                             reason=f"withheld level {level} — prove it with a test",
                             signature=signature)
        return _decision(
            "reverify",
            from_node=_first_verifier_node(home, recipe) or None,
            reason=f"withheld publish ({', '.join(withheld)} unverified) — re-verify",
            signature=signature,
        )

    # 6. Code: revision exhausted → send the implementer back with the errors.
    node_type = _node_type(home, recipe, failed_node)
    if hint_kind == "code" or node_type in ("reviewer", "verifier", "eval") \
            or str(fail_reason).lower() in _FAIL_VERDICTS:
        impl = _first_node_of_type(home, recipe, "implementer")
        prior_revise = sum(1 for a in attempts if str(a.get("action")) == "revise")
        lanes: dict[str, str] = {}
        if prior_revise >= 1:
            role_lane = _role_lane(home, run_id, impl)
            if role_lane:
                lanes = {role_lane: _escalate_lane()}
        return _decision("revise", from_node=impl or None, lanes=lanes,
                         ack_change=True, reason="the change needs a revision",
                         signature=signature)

    # 7. Owner-only fixes the loop cannot make.
    if hint_kind in _OWNER_KINDS:
        return _decision("human", from_node=failed_node, signature=signature,
                         reason=f"owner-only fix required ({hint_kind})")

    # 8. Anything else — hand over with the hint summary + last log lines.
    summary = str((hint or {}).get("needs_change", {}).get("summary", "")) \
        if isinstance(hint, dict) and isinstance(hint.get("needs_change"), dict) else ""
    reason = summary or str((hint or {}).get("notes") or "unclassified failure")
    return _decision("human", from_node=failed_node or (str((hint or {}).get("failed_node") or "") or None),
                     signature=signature, reason=reason or "unclassified failure")


def _load_hint(home: Path, run_id: str) -> dict[str, Any] | None:
    try:
        from mini_ork.recovery import retry_hint
        hint = retry_hint.load_or_compute(Path(home), run_id, write=False)
        return hint if isinstance(hint, dict) else None
    except Exception:  # noqa: BLE001 — a missing hint degrades the policy, never raises
        return None


def _is_infra(home: Path, run_id: str, hint: dict[str, Any] | None,
              nc: dict[str, Any] | None, failed_node: str | None,
              fail_reason: str) -> bool:
    if fail_reason and str(fail_reason).lower() in _INFRA_FINISH:
        return True
    if nc is not None and str(nc.get("kind") or "") == "infra":
        return True
    fc = _failure_class(home, run_id, failed_node)
    if fc in _INFRA_FAILURE_CLASSES:
        return True
    # Provider trouble surfaces as ``strategy: resume`` with no ``needs_change``;
    # a withheld publish is ``strategy: verify`` (handled later), never this.
    if isinstance(hint, dict) and nc is None and str(hint.get("strategy") or "") == "resume":
        return True
    return False


def _cost_since_first_repair(home: Path, run_id: str,
                             attempts: list[dict[str, Any]]) -> float | None:
    """The run's ``cost_usd`` when at least one repair has been attempted.

    The kickoff says "cost since the first repair"; ``task_runs.cost_usd`` is the
    run's cumulative spend, the best available proxy (and the one the retry hint
    already reads). ``None`` when there are no attempts or the DB is unreadable.
    """
    if not attempts:
        return None
    try:
        from mini_ork.web.db import db_for
        db = db_for(Path(home))
        if not db.has_table("task_runs"):
            return None
        row = db.row("SELECT cost_usd FROM task_runs WHERE id = ? LIMIT 1", (run_id,))
        if not row:
            return None
        return float(row.get("cost_usd") or 0.0)
    except Exception:  # noqa: BLE001
        return None


# ── feedback file (revise / prove) ───────────────────────────────────────────


def _next_round(run_dir: Path) -> int:
    highest = 0
    revise_dir = run_dir / "revise"
    if revise_dir.is_dir():
        for path in revise_dir.glob("round-*.md"):
            stem = path.stem[len("round-"):]
            if stem.isdigit():
                highest = max(highest, int(stem))
    return highest + 1


def _real_error_lines(path: str, max_lines: int = 40, max_bytes: int = 6000) -> list[str]:
    """Error-bearing lines from a verifier log, each with its next 2 lines of context.

    Uses ``execute._ERROR_LINE_RE`` (the exact regex the in-run revise rounds use)
    so the auto-repair round file reads the same diagnostics a human would.
    """
    from mini_ork.cli import execute as execute_mod
    regex = execute_mod._ERROR_LINE_RE
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            all_lines = fh.read().splitlines()
    except OSError:
        return []
    out: list[str] = []
    used = 0
    i = 0
    n = len(all_lines)
    while i < n and len(out) < max_lines:
        if regex.search(all_lines[i]):
            for j in range(i, min(i + 3, n)):
                line = all_lines[j]
                used += len(line) + 1
                if used > max_bytes:
                    return out
                out.append(line)
                if len(out) >= max_lines:
                    return out
            i += 3
        else:
            i += 1
    return out


def _failing_verifier_sections(run_dir: Path) -> list[str]:
    """One section per failing verifier: checks + the real captured error text."""
    from mini_ork.cli import execute as execute_mod
    sections: list[str] = []
    for path in sorted(run_dir.glob("verifier_*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        status = str(data.get("status") or "").upper()
        if not (data.get("pass") is False or status in ("REFUTED", "FAIL")):
            continue
        stem = path.stem[len("verifier_"):]
        lines = [f"## Verifier ({stem})"]
        if status:
            lines.append(f"status: {status}")
        summary = data.get("error_summary") or data.get("reason") or data.get("reasons")
        if summary:
            lines.append(f"error summary: {summary}")
        checks = data.get("failed_checks") or data.get("checks")
        if isinstance(checks, list):
            for c in checks:
                if not isinstance(c, dict):
                    continue
                if not (c.get("pass") is False or str(c.get("status") or "").upper() in ("FAIL", "REFUTED")):
                    continue
                cid = c.get("id") or c.get("name") or "check"
                lines.append(f"failing check: {cid}")
                log = c.get("log") or c.get("output")
                if isinstance(log, str) and log.strip():
                    lines.extend(log.splitlines()[:8])
        real = []
        for candidate in execute_mod._verifier_log_candidates(str(run_dir), stem):
            real = _real_error_lines(candidate)
            if real:
                lines.append(f"verifier errors ({os.path.basename(candidate)}):")
                lines.extend(real)
                break
        if not real:
            lines.append(
                "the build/test output was not captured; re-run the verification "
                "command first and read its errors"
            )
        sections.append("\n".join(lines))
    return sections


def _compose_feedback(run_dir: Path) -> tuple[int, str]:
    """``(round_no, text)`` for a revise/prove round file."""
    round_no = _next_round(run_dir)
    header = (
        f"Repair round {round_no} (auto-repair): the run failed after its revise "
        "rounds; these problems are still open. Fix ONLY these, on top of the "
        "working tree; do not start over."
    )
    sections: list[str] = []

    for path in sorted(run_dir.glob("review-*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        verdict = str(data.get("verdict") or "").lower()
        if verdict not in _FAIL_VERDICTS:
            continue
        lines = [f"## Reviewer ({path.stem})", f"verdict: {data.get('verdict', 'unknown')}"]
        findings = data.get("findings")
        if isinstance(findings, list) and findings:
            lines.append("findings:")
            for f in findings:
                if not isinstance(f, dict):
                    continue
                issue = str(f.get("issue") or "").strip()
                where = str(f.get("file") or "").strip()
                line_no = f.get("line")
                snippet = str(f.get("snippet") or "").strip()
                bullet = f"- {issue}"
                if where:
                    bullet += f" · {where}:{line_no}"
                if snippet:
                    bullet += f" · {snippet}"
                lines.append(bullet)
        reasons = data.get("reasons")
        if reasons:
            lines.append("reasons: " + (reasons if isinstance(reasons, str)
                                        else json.dumps(reasons)[:4000]))
        sections.append("\n".join(lines))

    sections.extend(_failing_verifier_sections(run_dir))

    body = "\n\n".join(sections) if sections else \
        "(no reviewer findings or failing verifier checks were recorded)"
    return round_no, header + "\n\n" + body + "\n"


def _write_current(run_dir: Path, round_no: int, feedback_path: Path) -> None:
    """Write ``revise/current.json`` — the channel ``_read_revise_feedback`` reads."""
    revise_dir = run_dir / "revise"
    revise_dir.mkdir(parents=True, exist_ok=True)
    payload = {"round": round_no, "max_rounds": round_no, "feedback": str(feedback_path)}
    (revise_dir / "current.json").write_text(json.dumps(payload), encoding="utf-8")


# ── apply ────────────────────────────────────────────────────────────────────


def apply(home: Path, run_id: str, decision: dict[str, Any], *,
          spawn: bool = True, wait_for_exit: bool = False) -> dict[str, Any]:
    """Perform ``decision``: write feedback, record the attempt, resume the run.

    Returns a result dict — for a spawn, ``{"pid", "log", "command", "attempt"}``;
    for ``human``, ``{"action": "human", "gate": <bool>}``; for a dry/no-op,
    ``{"action": <action>}``. Writes the attempt to ``repair.json`` and emits a
    ``run_events`` ``auto_repair`` row in every case. ``wait_for_exit`` is for a
    spawn made by the process that still owns the run record (see
    ``wait_for_spawner``).
    """
    home = Path(home)
    run_dir = _run_dir(home, run_id)
    action = str(decision.get("action") or "none")
    repair = _load_repair(run_dir)
    attempts = list(repair.get("attempts") or [])
    n = len(attempts) + 1

    result: dict[str, Any] = {"action": action, "attempt": n}

    if action in ("revise", "prove"):
        round_no, text = _compose_feedback(run_dir)
        if action == "prove" and decision.get("feedback"):
            # prove carries an explicit instruction; keep it as the header line.
            text = str(decision["feedback"]) + "\n\n" + text
        revise_dir = run_dir / "revise"
        revise_dir.mkdir(parents=True, exist_ok=True)
        feedback_path = revise_dir / f"round-{round_no}.md"
        feedback_path.write_text(text, encoding="utf-8")
        _write_current(run_dir, round_no, feedback_path)
        result["feedback"] = str(feedback_path)

    gate_pending = False
    if action == "human":
        gate_pending = _ensure_human_gate(home, run_id, run_dir)
        result["gate"] = gate_pending

    # Record the attempt.
    attempt = {
        "n": n,
        "ts": _now_iso(),
        "action": action,
        "from_node": decision.get("from_node"),
        "lanes": decision.get("lanes") or {},
        "reason": decision.get("reason") or "",
        "signature": decision.get("signature") or "",
    }
    prior_attempts = len(attempts)
    attempts.append(attempt)
    repair["attempts"] = attempts
    # ``gave_up`` is the loop's own verdict: a stop rule fired, or at least one
    # repair was actually attempted. A first-failure hand-off (environment /
    # owner-only / unclassified) gives the run to the human without that claim,
    # so it must not be labelled ``gave_up``.
    if action == "human":
        if prior_attempts >= 1 or decision.get("stop"):
            repair["state"] = "gave_up"
        else:
            repair.pop("state", None)
    elif action in _SPAWNABLE:
        repair["state"] = "repairing"
    repair["triaged"] = bool(repair.get("triaged", False))
    if action == "human":
        repair["triaged"] = _triage_on_give_up(home, run_id, repair)
    _write_repair(run_dir, repair)
    _emit_run_event(home, run_id, attempt)

    if action in _SPAWNABLE and spawn:
        spawned = _spawn_recover(home, run_id, run_dir, decision, n,
                                 wait_for_exit=wait_for_exit)
        result.update(spawned)
        result["command"] = spawned.get("command", "")
    elif action in _SPAWNABLE and not spawn:
        result["spawned"] = False
    return result


def _ensure_human_gate(home: Path, run_id: str, run_dir: Path) -> bool:
    """Make sure the existing needs-you path is armed — idempotently.

    ``retry_notify.notify`` dedupes a pending gate internally; we still skip the
    call entirely when a gate is already pending so we never churn the row.
    Returns whether a gate is pending afterwards.
    """
    try:
        from mini_ork.recovery import retry_notify
    except Exception:  # noqa: BLE001
        return False
    try:
        if retry_notify.pending_fix_for_run(home, run_dir) is not None:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        retry_notify.notify(home, run_id)
    except Exception:  # noqa: BLE001 — needs-you path is best-effort
        return False
    try:
        return retry_notify.pending_fix_for_run(home, run_dir) is not None
    except Exception:  # noqa: BLE001
        return False


def _triage_on_give_up(home: Path, run_id: str, repair: dict[str, Any]) -> bool:
    """Call ``triage_run`` exactly once per run, on give-up, when opted in.

    Returns the value recorded in ``repair.json["triaged"]``. Fail-soft: triage
    must never raise into the run that just gave up.
    """
    if repair.get("triaged") is True:
        return True
    if context_env("MO_FAILURE_TRIAGE", "") != "1":
        return False
    try:
        from mini_ork.triage.failures import triage_run
        triage_run(
            run_id,
            home=str(home),
            db=str(Path(home) / "state.db"),
            root=context_env("MINI_ORK_ROOT") or None,
            promote=context_env("MO_FAILURE_TRIAGE_PROMOTE", "") == "1",
        )
        return True
    except Exception:  # noqa: BLE001 — triage never fails a run
        return False


def _spawn_recover(home: Path, run_id: str, run_dir: Path,
                   decision: dict[str, Any], n: int, *,
                   wait_for_exit: bool = False) -> dict[str, Any]:
    """Spawn a detached ``mini-ork recover`` for the SAME run, from the failed node."""
    argv = [sys.executable, "-m", "mini_ork.recovery.planner", str(run_id)]
    from_node = decision.get("from_node")
    if from_node:
        argv += ["--from-node", str(from_node)]
    for alias, lane in (decision.get("lanes") or {}).items():
        argv += ["--lane", f"{alias}={lane}"]
    if decision.get("ack_change"):
        argv += ["--ack-change"]
    # ``--force`` is the loop's own override: the retry hint is written for a
    # *human* deciding whether to retry, and it marks a code-shaped failure
    # ``retryable: false`` (strategy='none'). Without this the spawned recover
    # refuses every revise/prove attempt outright ("retry-hint refuses this run
    # … pass --force") and the main repair case could never resume.
    argv += ["--force"]

    env = dict(os.environ)
    env[_ATTEMPT_ENV] = str(n)
    env[_RUN_ID_ENV] = str(run_id)
    # The child must resolve the SAME home/db as this process: the CLI's own
    # cwd is ``MINI_ORK_ROOT``, which is not the mini-ork home, so an inherited
    # MINI_ORK_HOME (or its absence) would strand the child on the wrong
    # state.db and run dir.
    env["MINI_ORK_HOME"] = str(home)
    # Auto-repair owns this failure: the child's execute must not also triage
    # it (``execute._maybe_triage_failed_run`` stands down only on an explicit
    # "1"), or one failure would have two owners.
    env["MO_AUTO_REPAIR"] = "1"
    if wait_for_exit:
        env[_WAIT_PID_ENV] = str(os.getpid())
    log_path = run_dir / f"repair-{n}.log"
    cwd = context_env("MINI_ORK_ROOT") or os.getcwd()
    proc = _spawn(argv, cwd=cwd, env=env, stdout_path=log_path)
    return {
        "pid": getattr(proc, "pid", None),
        "log": str(log_path),
        "command": " ".join(argv),
    }


def _spawn(argv: list[str], *, cwd: str, env: dict[str, str],
           stdout_path: Path) -> subprocess.Popen:
    """Spawn ``argv`` detached, logging to ``stdout_path`` (mirrors acp.commands._spawn)."""
    log_fh = open(stdout_path, "ab")
    try:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_fh.close()


def _emit_run_event(home: Path, run_id: str, payload: dict[str, Any]) -> None:
    """Append an ``auto_repair`` row to ``run_events`` (best-effort).

    Written through a plain read-write ``sqlite3`` connection, the way
    ``cli/plan.py`` records ``asks_blocked``: ``web.db.StateDB`` opens its
    connection ``PRAGMA query_only = ON`` (the observability path is
    read-only), so an INSERT through it fails with "attempt to write a
    readonly database" — silently, since this helper swallows ``sqlite3.Error``.
    ``event_id`` embeds ``run_id`` because ``run_events.event_id`` is UNIQUE:
    a sweep repairing two runs in the same second would otherwise collide.
    """
    db_path = Path(home) / "state.db"
    if not (run_id and db_path.is_file()):
        return
    import sqlite3
    now = int(time.time())
    event_id = f"evt-auto_repair-{run_id}-{now}-{payload.get('n')}"
    try:
        con = sqlite3.connect(db_path, timeout=5.0)
        try:
            con.execute("PRAGMA busy_timeout = 5000")
            con.execute(
                "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (event_id, run_id, "auto_repair", json.dumps(payload), now),
            )
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:  # noqa: BLE001 — the event log is a side channel
        pass


# ── entry point ──────────────────────────────────────────────────────────────


def maybe_repair(home: Path, run_id: str, *, spawn: bool = True,
                 wait_for_exit: bool = False) -> dict[str, Any] | None:
    """``decide`` → ``apply``. Never raises; a no-op when auto-repair is off.

    Called unconditionally by the ``run`` and ``recover`` flow hooks — the run's
    DB status decides whether anything happens, so a green run pays only a few
    cheap reads. A withheld publish sets ``status='failed'`` while the flow's rc
    is 0, which is exactly why the hooks must not gate on rc.

    ``wait_for_exit`` belongs to those two hooks: they spawn while their own
    lifecycle still owns the run record, so the child is told to wait for them
    to exit (``wait_for_spawner``).
    """
    try:
        if context_env("MO_AUTO_REPAIR", "1") == "0":
            return None

        # Re-entrancy guard (see ``_SPAWNED_THIS_PROCESS``): the loop is a chain
        # of detached processes, so a process that already handed this run to a
        # child must not spawn a second one for the same failure.
        if run_id in _SPAWNED_THIS_PROCESS:
            return None

        decision = decide(home, run_id)
        action = str(decision.get("action") or "none")
        if action == "none":
            _mark_repaired_if_done(home, run_id)
            return decision
        result = apply(home, run_id, decision, spawn=spawn,
                       wait_for_exit=wait_for_exit)
        if spawn and action in _SPAWNABLE:
            _SPAWNED_THIS_PROCESS.add(run_id)
        return result
    except Exception as exc:  # noqa: BLE001 — auto-repair must never fail a run
        try:
            sys.stderr.write(f"auto_repair: {exc}\n")
        except Exception:  # noqa: BLE001
            pass
        return None


def wait_for_spawner() -> None:
    """Block until the process that spawned this repair has exited.

    Called at the top of ``recovery.planner.cli_main`` when — and only when —
    the spawning process marked the child with ``MO_AUTO_REPAIR_WAIT_PID``:
    the ``run`` / ``recover`` lifecycle, which still owns the run record while
    it reflects and only closes it in ``main._close_run_record`` (the caller's
    ``finally``). Dispatching before that teardown would let it observe the
    child's ``executing`` status and flip the *live* repair to ``failed``.

    Liveness is read from ``os.getppid()``: the kernel reparents us the instant
    the spawner exits, so a spawner that is briefly a zombie (not yet reaped by
    its own parent) can never stall the wait. Best-effort and bounded — a
    spawner that outlives ``MO_AUTO_REPAIR_WAIT_S`` is logged and ignored.
    """
    raw = context_env(_WAIT_PID_ENV, "")
    if not raw.isdigit():
        return
    pid = int(raw)
    if pid <= 1 or pid == os.getpid() or os.getppid() != pid:
        return
    wait_s = _env_float("MO_AUTO_REPAIR_WAIT_S", _DEFAULT_WAIT_S)
    deadline = time.monotonic() + wait_s
    while os.getppid() == pid and time.monotonic() < deadline:
        time.sleep(0.2)
    if os.getppid() == pid:
        try:
            sys.stderr.write(
                f"auto_repair: spawner {pid} still alive after "
                f"{wait_s:.0f}s; resuming without the hand-off\n")
        except Exception:  # noqa: BLE001
            pass


def note_stuck(home: Path | None = None, run_id: str = "", *,
               reason: str = "") -> bool:
    """Hand a stranded repair to the human — a spawned recover that bailed out.

    A recover this loop spawned can return *before* it dispatches: the retry
    hint refuses it, ``plan_recovery`` raises ``RecoveryRefused``, another
    process holds the lease, a carry-patch is refused, or every node is
    reusable. The attempt is already recorded, so leaving ``repair.json`` at
    ``state='repairing'`` would strand the run with no owner and no hand-off.
    Records the give-up (stop rule) and arms the needs-you gate instead.

    ``home`` / ``run_id`` default to the markers ``_spawn_recover`` exports to
    the child (``MINI_ORK_HOME`` / ``MO_AUTO_REPAIR_RUN_ID``), so the child's
    ``cli_main`` need only pass ``reason``.

    Only the attempt that is still the live one may hand over: the child
    carries its attempt number in ``MO_AUTO_REPAIR_ATTEMPT``, so an attempt a
    newer (concurrent) repair superseded is never stepped on. Returns whether
    the hand-off was recorded; never raises.
    """
    raw = context_env(_ATTEMPT_ENV, "")
    if not raw.isdigit():
        return False  # not a spawned repair attempt — nothing to hand over
    run_id = run_id or context_env(_RUN_ID_ENV, "")
    home_str = str(home or context_env("MINI_ORK_HOME", ""))
    if not (run_id and home_str):
        return False
    try:
        run_dir = _run_dir(Path(home_str), run_id)
        repair = _load_repair(run_dir)
        attempts = list(repair.get("attempts") or [])
        if repair.get("state") != "repairing":
            return False
        if not attempts or str(attempts[-1].get("n")) != raw:
            return False
        decision = _decision(
            "human",
            reason=f"the repair attempt could not resume the run: {reason}"
                   if reason else "the repair attempt could not resume the run",
            stop=True)
        apply(Path(home_str), run_id, decision, spawn=False)
        return True
    except Exception as exc:  # noqa: BLE001 — the hand-off is best-effort
        try:
            sys.stderr.write(f"auto_repair: note_stuck failed: {exc}\n")
        except Exception:  # noqa: BLE001
            pass
        return False


def _mark_repaired_if_done(home: Path, run_id: str) -> None:
    """Flip ``repair.json`` to ``state="repaired"`` when the run has published.

    Only touches a run that already carries a repair history — a green run with
    no ``repair.json`` stays untouched.
    """
    run_dir = _run_dir(home, run_id)
    path = run_dir / _ATTEMPT_FILE
    if not path.is_file():
        return
    try:
        status = _run_status(home, run_id)
    except Exception:  # noqa: BLE001
        return
    if status not in ("published", "done"):
        return
    repair = _load_repair(run_dir)
    if repair.get("state") == "repaired":
        return
    repair["state"] = "repaired"
    _write_repair(run_dir, repair)
