"""Native mini-ork entrypoint / universal-loop dispatcher.

Subcommands run through native Python modules. The `run` recipe-runner walks
classify → profile → plan → execute → rubric → verify → reflect with deadline
soft-gates between stages. Recipe resolution and profile generation preserve
the golden outputs captured before Bash retirement.

    main(argv=None, *, root=None) -> int

Sibling command forks use subprocess with inherited stdio and return the
child's exit code, preserving the public stdout/stderr/exit-code contract.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

from mini_ork import trace_store
from mini_ork.context import (
    apply_env_overrides,
    context_env,
    context_env_snapshot,
    publish_env,
    run_context_scope,
)
from mini_ork.dispatch import agents_config, config_resolve, deadline_budget
from mini_ork.dispatch.run_env import RunEnvError, load_run_env
from mini_ork.dispatch.secrets import SecretStoreError
from mini_ork.dispatch.vault import VaultError
from mini_ork.features import gate_env
from mini_ork.vcs import repo_integrity_guard
from mini_ork.gates import rubric_prescreen
from mini_ork.orchestration import concord_run

_NATIVE_SUBS = {"apply", "classify", "plan", "verify", "verifier", "reflect", "garden", "validate"}

# Native module subcommands. Compatibility launchers under bin/ re-exec this
# dispatcher, so there is one public command implementation.
_NATIVE_MODULE_SUBS = {
    "improve": "mini_ork.cli.improve",
    "eval": "mini_ork.cli.eval",
    "promote": "mini_ork.cli.promote",
    "init": "mini_ork.cli.init",
    "update": "mini_ork.cli.update",
    "spawn": "mini_ork.cli.spawn",
    "scheduler": "mini_ork.scheduler",
    "epics": "mini_ork.cli.epics",
    "specs": "mini_ork.cli.specs",
    "bugs": "mini_ork.cli.bugs",
    "inject": "mini_ork.cli.inject",
    "review": "mini_ork.pre_push_review",
    "traceotter": "mini_ork.cli.traceotter",
    "metrics": "mini_ork.cli.metrics",
    "metric-anchor": "mini_ork.cli.metric_anchor",
    "features": "mini_ork.cli.features",
    "skills": "mini_ork.cli.skills",
    "rollback": "mini_ork.cli.rollback",
    "resume": "mini_ork.cli.resume",
    "recover": "mini_ork.recovery.planner",
    "repair": "mini_ork.cli.repair_cmd",
    "serve": "mini_ork.cli.serve",
    "bug-collector": "mini_ork.observability.bug_collector",
    "conductor": "mini_ork.orchestration.conductor",
    "coord": "mini_ork.orchestration.coord",
    "concord": "mini_ork.orchestration.concord",
    "lifetime": "mini_ork.orchestration.lifetime",
    "self-improve": "mini_ork.cli.self_improve",
    "topology": "mini_ork.cli.topology",
    "usage-report": "mini_ork.observability.usage_report",
    "watchdog": "mini_ork.orchestration.watchdog",
    "reap": "mini_ork.orchestration.run_reaper",
    "acp": "mini_ork.cli.acp_cmd",
    "mcp-context": "mini_ork.cli.mcp_context_cmd",
    "mcp": "mini_ork.cli.mcp_cmd",
    "vault": "mini_ork.cli.vault_cmd",
    "zed": "mini_ork.cli.zed_cmd",
    "calibrate": "mini_ork.cli.calibrate",
    "collapse-check": "mini_ork.cli.collapse",
    "collapse-precursor": "mini_ork.cli.collapse_precursor",
    "automations": "mini_ork.cli.automations_cmd",
    "board": "mini_ork.cli.board_cmd",
    "gate-fuzz": "mini_ork.cli.gate_fuzz",
    "memory-lifecycle": "mini_ork.cli.memory_lifecycle",
    "prefs": "mini_ork.cli.prefs_cmd",
    "lessons": "mini_ork.cli.lessons_cmd",
    "harness-contrast": "mini_ork.cli.harness_contrast",
    "harness-edit": "mini_ork.cli.harness_edit",
    "harness-audit": "mini_ork.cli.harness_audit",
    "hack-probe": "mini_ork.cli.hack_probe",
    "oversight": "mini_ork.cli.oversight",
    "active-eval": "mini_ork.cli.active_eval",
    "certify": "mini_ork.cli.certify",
    "node-agent": "mini_ork.cli.node_agent",
    "nodes": "mini_ork.cli.nodes",
    "triage": "mini_ork.cli.triage",
    "threads": "mini_ork.threads.cli",
    "intake": "mini_ork.cli.intake_cmd",
}

_HELP = """mini-ork — task operating system for agents (v0.1)

Universal loop subcommands:
  classify <kickoff.md>          Route kickoff to a task_class
  plan <kickoff.md>              Generate plan (decomposition + verifier contract)
  execute [<plan.json>]          Dispatch to workflow nodes (planner/researcher/
                                   implementer/reviewer/verifier/reflector/
                                   publisher/rollback)
  verify <artifact-path>         Run verifiers + gates for the artifact contract
  reflect [--since <timestamp>]  Extract gradients + patterns from recent runs
  improve                        Propose workflow candidates via group_evolver
  eval --candidate <id>          Run benchmark suite against a workflow candidate
  promote --candidate <id>       Promotion gate decision (promote|quarantine)
  apply --task-class <name>      Close the apply loop: pick→materialize→score→gate→
  --target <file>                write|quarantine (IMPL-3, opt-in via MO_APPLY_ENABLED=1)

Recipe runner:
  run <kickoff.md>               Classify kickoff, resolve recipe, then walk
                                   classify → plan → execute → verify
  run <recipe-name> <kickoff.md> Force a recipe, then walk the same lifecycle
  run … --set K=V                Repeatable env override; premium feature knobs
                                   are dropped unless MO_ACCEPT_PREMIUM=1

Self-improvement:
  self-improve                   Recursive self-improvement of this checkout in a
                                   worktree + branch per iteration (--dry-run,
                                   --soft-cap-hours, --hard-cap-hours, --auto-merge,
                                   --resume). Gated; never commits without verifiers.

Lifecycle:
  init                           Bootstrap project (creates .mini-ork/)
  install                        Install or repair the per-user mini-ork command
  update                         Apply migrations + report config drift
  doctor                         Check deps, environment, and provider preflight
  providers                      Configure or inspect credentials for workflow lanes
  mcp                            Manage the MCP server registry nodes grant by name
  vault                          Report which secret sources are wired, and where a key lives
  validate                       Pre-run static checks with Fix: hints
  garden                         Drift detection (collisions, orphans, stale runs)
  verifier list|annotate         Inspect verifier results and label false
                                   positives/negatives (abstain-gate calibration)
  recipe-eval                    Static evaluation of recipe definitions
  version

Coordination:
  concord <sub>                  Cross-agent coordination client (run/ps/stop/send/inbox/ack)
  mcp-context                    Read-only stdio MCP server exposing runs, learnings,
                                   cost, and lanes (for agents in Zed etc.)
  zed setup|status|uninstall     Wire (or unwire) mini-ork inside Zed's settings.json
  board [run|merge|discard|stop] The project's live state as JSON (IDE panels)
  automations <sub>              Manage per-project scheduled recipe runs
                                   (list|add|remove|pause|resume|run|tick|
                                   scheduler status|install|uninstall)

Provider credentials:
  providers status <lane>                    Safely show configured or missing credentials
  providers configure <lane>                 Prompt securely to configure one provider lane
  providers configure --workflow <path>      Configure every provider lane used by a workflow
  providers --help                           See automation options; keys never use CLI flags

MCP servers (granted per node by name):
  mcp list                                   Show the registry and what resolves
  mcp add <name> --url <url> | --command <c>  Define a server; --secret NAME stores its token
  mcp rm <name>                              Remove an operator-defined server
  mcp check --workflow <path>                Resolve every grant a recipe declares

Secret vault (where ${NAME} in configs and env.yaml is resolved):
  vault status                               Show the backend chain and its readiness
  vault which <NAME>                         Name the backend that holds NAME (never its value)
  # No vault.yaml needed: the default chain is the environment, then the
  # owner-only $MINI_ORK_HOME/config/secrets.local.sh. See config/vault.example.yaml
  # for sops (committable encrypted file), command (pass/op/bws/infisical) and
  # openbao (self-hosted, per-path policies) backends.

Environment:
  MINI_ORK_HOME   project home dir  (default: .mini-ork/)
  MINI_ORK_DB     sqlite3 state db  (default: $MINI_ORK_HOME/state.db)
  MINI_ORK_PROVIDERS  path to providers.yaml, overriding $MINI_ORK_HOME/config/
  MINI_ORK_AGENTS     personal lane overlay merged over $MINI_ORK_HOME/config/agents.yaml (default: config/agents.local.yaml)
  MINI_ORK_DRY_RUN  set to 1 for dry-run mode on all subcommands
"""


def _module_env(root):
    """Env for a ``python -m mini_ork.cli.*`` child that must import THIS engine.

    ``PYTHONPATH`` alone does not decide that. For ``-m`` and ``-c`` the
    interpreter puts the working directory at ``sys.path[0]``, AHEAD of
    ``PYTHONPATH``, so a child spawned with the cwd inside another checkout that
    happens to contain a ``mini_ork/`` tree imports that tree instead of the
    engine we named. The goal-loop does exactly this: its children run with the
    cwd set to the repo under repair. ``PYTHONSAFEPATH`` drops the implicit cwd
    entry so the explicit ``PYTHONPATH`` is the one that resolves.
    """
    env = context_env_snapshot()
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONSAFEPATH"] = "1"
    return env


_REFLECT_TIMEOUT_FALLBACK = 360
_REFLECT_TIMEOUT_HEADROOM = 3
_REFLECT_TIMEOUT_CEILING = 21600
_REFLECT_TIMEOUT_SAMPLE = 20
# Slack added on top of the run's remaining --deadline when capping reflect:
# enough for the child to notice the stop and flush its log, not enough to
# meaningfully overshoot a budget the operator asked us to honour.
_REFLECT_DEADLINE_GRACE = 120


def _reflect_durations(db: str | None = None,
                       *, sample: int = _REFLECT_TIMEOUT_SAMPLE) -> list[int]:
    """Durations, in seconds, of the most recent *successful* reflect passes.

    Read through the same ladder every other reader uses (explicit arg →
    ``MINI_ORK_DB`` → ``$MINI_ORK_HOME/state.db``). Returns ``[]`` when the DB
    or the table is missing: a home that has never completed a reflect has no
    recorded work to size a bound from, and an empty list is the honest answer
    rather than a guess.
    """
    path = db or os.environ.get("MINI_ORK_DB") or os.path.join(
        os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork"),
        "state.db")
    if not path or not os.path.exists(path):
        return []
    import sqlite3  # noqa: PLC0415 — only this diagnostic needs it
    try:
        con = sqlite3.connect(path, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT duration_ms FROM execution_traces "
            "WHERE task_class='__reflect__' AND status='success' "
            "AND duration_ms IS NOT NULL AND duration_ms > 0 "
            "ORDER BY created_at DESC LIMIT ?",
            (sample,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    return [int(r[0]) // 1000 for r in rows if r and r[0]]


def _reflect_timeout(db: str | None = None) -> tuple[int, str]:
    """``(seconds, why)`` — the wall-clock bound on the post-verdict reflect child.

    Reflect runs after the verdict is already final, so it can never change the
    outcome — only hold the process open. A lane that never returns used to
    wedge `mini-ork run` forever; the campaign launcher worked around it with a
    six-minute shell watchdog. That bound belongs in the code that spawns the
    child, not in the script that calls it.

    A *flat* bound is wrong in both directions. The successful passes on the
    live homes average 363 s but reach 5538 s, so 360 s kills real work — and
    because ``code_findings`` is the last of eight sequential side channels,
    every kill took the "Your code" tab's only data source with it. The bound is
    therefore derived from the work this home has actually recorded: the
    slowest of the last ``_REFLECT_TIMEOUT_SAMPLE`` successful passes, times
    ``_REFLECT_TIMEOUT_HEADROOM``, floored at the legacy default and capped.

    ``MO_REFLECT_TIMEOUT_SECONDS`` still pins an explicit bound (an operator who
    set it outranks the derivation) and ``MO_REFLECT_TIMEOUT_MAX_SECONDS`` moves
    the ceiling. Fail-closed: an unreadable DB, no recorded pass, or a malformed
    value all fall back to the floor, and the second element always names the
    rule that produced the number so the log line can be read back.
    """
    pinned = os.environ.get("MO_REFLECT_TIMEOUT_SECONDS")
    if pinned:
        try:
            return max(1, int(pinned)), "pinned by MO_REFLECT_TIMEOUT_SECONDS"
        except ValueError:
            return _REFLECT_TIMEOUT_FALLBACK, (
                f"MO_REFLECT_TIMEOUT_SECONDS={pinned!r} is not an integer; "
                f"floor {_REFLECT_TIMEOUT_FALLBACK}s")
    try:
        ceiling = max(1, int(os.environ.get(
            "MO_REFLECT_TIMEOUT_MAX_SECONDS", str(_REFLECT_TIMEOUT_CEILING))))
    except ValueError:
        ceiling = _REFLECT_TIMEOUT_CEILING
    durations = _reflect_durations(db)
    if not durations:
        return _REFLECT_TIMEOUT_FALLBACK, (
            f"no successful reflect recorded; floor {_REFLECT_TIMEOUT_FALLBACK}s")
    slowest = max(durations)
    derived = min(ceiling, slowest * _REFLECT_TIMEOUT_HEADROOM)
    if derived <= _REFLECT_TIMEOUT_FALLBACK:
        return _REFLECT_TIMEOUT_FALLBACK, (
            f"slowest of {len(durations)} recent pass(es) {slowest}s; "
            f"floor {_REFLECT_TIMEOUT_FALLBACK}s")
    return derived, (
        f"{_REFLECT_TIMEOUT_HEADROOM}x the slowest of {len(durations)} recent "
        f"pass(es) ({slowest}s), ceiling {ceiling}s")


def _reflect_timeout_seconds() -> int:
    """The bound alone, for a caller with no log to write the reason into.

    ``mini_ork.cli.execute_handlers`` imports this name; the signature and the
    return type it binds to stay as they are.
    """
    return _reflect_timeout()[0]


def _cap_reflect_to_deadline(bound: int, why: str, *,
                            deadline_seconds: int,
                            remaining_seconds: int,
                            hit: bool) -> tuple[int, str]:
    """``(seconds, why)`` — ``bound`` tightened to the run's remaining deadline.

    ``--deadline N`` is the operator's statement of how long the whole run may
    take, and the derived reflect bound (see ``_reflect_timeout``) knows nothing
    about it: a home that once recorded a 5538 s pass can hand reflect a bound
    far larger than the deadline the caller just asked us to honour. Reflect
    still runs post-verdict, so shortening it cannot change the outcome — it
    only stops the tail of a run from overshooting a budget that was set
    deliberately.

    Pure by construction: no I/O, no env reads, no ``deadline_budget`` call.
    The call site owns the (guarded, best-effort) status lookup and hands the
    three numbers in, which is what makes this testable without a live run.
    ``deadline_seconds <= 0`` means no deadline was armed, so the bound is
    returned untouched along with its original reason. Otherwise the cap is the
    remaining budget (never negative — a spent or already-tripped deadline caps
    at the grace alone) plus ``_REFLECT_DEADLINE_GRACE``; a cap that would not
    actually shorten the bound leaves it — and its reason — alone. When it does
    bite, the reason names the rule and keeps the original derivation after the
    semicolon so the log line is still readable back.
    """
    if deadline_seconds <= 0:
        return bound, why
    capped = max(0, remaining_seconds) + _REFLECT_DEADLINE_GRACE
    if capped >= bound:
        return bound, why
    note = "capped to the --deadline remainder"
    if hit:
        note += " (deadline already hit)"
    return capped, f"{note} (+{_REFLECT_DEADLINE_GRACE}s grace); {why}"


def engine_version(root: str | None = None) -> str:
    """The version of the engine checkout that is running, or "" if unknown.

    More than one engine can serve one project — the IDE's ``serve`` and the
    copy vendored in the project's own ``.mini-ork`` — and they drift (the
    researcher's is 34 lines behind on ``reflect.py`` alone). A version alone
    cannot tell two checkouts at the same version apart, so the *root* is
    printed beside it wherever this is used; "" is the honest answer for a
    checkout with no readable pyproject, and callers render it as "?".
    """
    base = root or os.environ.get("MINI_ORK_ROOT") or str(
        Path(__file__).resolve().parents[2])
    try:
        import tomllib  # noqa: PLC0415 — py311+, and only this diagnostic needs it
        with open(os.path.join(base, "pyproject.toml"), "rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, ValueError):
        return ""
    return str((doc.get("project") or {}).get("version") or "")


def _engine_commit(root: str | None = None) -> str:
    """The running checkout's short commit, or "" — ``doctor``'s drift tie-break.

    Two checkouts of the same release differ only by commit, so the version
    comparison alone cannot see the drift this is here to expose. Best-effort
    and quiet: a checkout with no git, or none at all, is not a doctor failure.
    """
    base = root or os.environ.get("MINI_ORK_ROOT") or str(
        Path(__file__).resolve().parents[2])
    try:
        proc = subprocess.run(
            ["git", "-C", base, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _db_engine_version(home: str) -> str:
    """The engine version recorded in a home's ``schema_migrations``, or "".

    ``migrate`` stamps the version of the engine that applied each migration
    onto its row, so the newest row names the last engine that touched this DB
    — the other half of the drift check. "" for a missing DB, a pre-version DB
    (the column arrived with a migration), or a DB with no rows yet.
    """
    path = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    if not os.path.exists(path):
        return ""
    import sqlite3  # noqa: PLC0415 — only this diagnostic needs it
    try:
        con = sqlite3.connect(path, timeout=2.0)
    except sqlite3.Error:
        return ""
    try:
        row = con.execute(
            "SELECT mini_ork_version FROM schema_migrations "
            "ORDER BY applied_at DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return ""
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    return str(row[0]) if row and row[0] else ""


def _reflect_log_name() -> str:
    """``reflect.log``; a rehearsal gets its own file, as ``execute.log`` does."""
    return ("reflect.dryrun.log"
            if os.environ.get("MINI_ORK_DRY_RUN", "0") == "1" else "reflect.log")


def _child_text(value) -> str:
    """A captured child stream as text, whichever of the two it arrived as.

    ``subprocess.TimeoutExpired`` hands back bytes even when the parent asked
    for text, so the killed pass would otherwise reach the log as a ``b'…'``
    repr — the one case this log exists for.
    """
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _write_reflect_log(run_dir: str, head: str, status: str,
                       out: str, err: str) -> str:
    """Write the reflect child's whole output under ``<run_dir>/reflect.log``.

    Returns the path, or "" when it could not be written — the outcome is
    already on stdout, and a diagnostic that fails must never fail the run it
    is diagnosing.
    """
    text = f"{head}exit:   {status}\n"
    if out:
        text += f"\n── reflect stdout ──\n{out}"
    if err:
        text += f"\n── reflect stderr ──\n{err}"
    path = os.path.join(run_dir, _reflect_log_name())
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        sys.stderr.write(f"  [reflect] log not written to {path}: {exc}\n")
        return ""
    return path


def resolve_recipe(root: str, task_class: str) -> str:
    """Verbatim transcription of the user-first recipe-resolution python."""
    import yaml
    recipes = os.path.join(root, "recipes")
    if os.path.isdir(recipes):
        for name in sorted(os.listdir(recipes)):
            tc = os.path.join(recipes, name, "task_class.yaml")
            if not os.path.isfile(tc):
                continue
            try:
                with open(tc) as f:
                    data = yaml.safe_load(f) or {}
            except Exception:
                continue
            if (data.get("name") or "").strip() == task_class:
                return name
    fallback = task_class.replace("_", "-")
    if os.path.isdir(os.path.join(recipes, fallback)):
        return fallback
    return ""


def _resolve_recipe_base(root: str, recipe: str):
    """Locate a recipe across the MINI_ORK_HOME overlay and the root checkout.

    An embedding consumer (the researcher) overlays private recipes by
    symlinking them into ``$MINI_ORK_HOME/recipes/`` while ``MINI_ORK_ROOT``
    still points at the primary mini-ork checkout — so ``verified-artifact``
    lives under home but not under root. Resolution historically consulted only
    ``root/recipes``, so the overlay was invisible ("no recipe: …") and every
    chapter that reached the verified-artifact gate failed with exit_code=2.

    ``MINI_ORK_HOME`` alone is not trustworthy across a spawn boundary: the
    launcher's ``_configure_paths`` prefers an inherited ``MINI_ORK_PROJECT_HOME``
    and *overwrites* ``MINI_ORK_HOME`` with it, so a consumer that spawns with
    ``cwd=<overlay-home>`` and a stale engine ``MINI_ORK_PROJECT_HOME`` in its
    environment gets ``MINI_ORK_HOME`` clobbered back to the engine checkout
    before this runs. The invocation cwd is the un-clobberable signal in that
    case (parents spawn with ``cwd=<overlay-home>``; nothing here chdirs), so it
    is consulted too. Search order: home, cwd, root — the given name and its
    ``_``→``-`` spelling — deduped by realpath. Returns ``(base, name)`` or
    ``("", recipe)`` when found nowhere. Dev checkouts (home unset / == cwd ==
    root) collapse to a single ``root`` probe and reproduce the historical
    resolution byte-for-byte.
    """
    bases: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        if not candidate:
            return
        try:
            real = os.path.realpath(candidate)
        except OSError:
            return
        if real in seen:
            return
        seen.add(real)
        bases.append(candidate)

    _add(os.environ.get("MINI_ORK_HOME", ""))
    try:
        _add(os.getcwd())
    except OSError:
        pass
    _add(root)
    for base in bases:
        for name in (recipe, recipe.replace("_", "-")):
            if os.path.isdir(os.path.join(base, "recipes", name)):
                return base, name
    return "", recipe


def gen_profile(kickoff_path, root, recipe, task_class, profile_path, agents_path,
                recipe_base=None) -> dict:
    """Verbatim transcription of the run-profile embedded python. Writes the
    profile.json and returns the same dict (caller prints the key=value lines)."""
    root = Path(root)
    rbase = Path(recipe_base) if recipe_base else root
    kickoff = Path(kickoff_path)
    profile = Path(profile_path)
    text = kickoff.read_text(encoding="utf-8", errors="replace")

    def section_lines(names):
        wanted = {n.lower() for n in names}
        current = None
        lines = []
        in_fence = False
        for raw in text.splitlines():
            if raw.lstrip().startswith("```"):
                in_fence = not in_fence
            # A shell comment inside a fenced block (``## …``) is not a heading.
            m = None if in_fence else re.match(r"^\s*#{2,6}\s+(.+?)\s*$", raw)
            if m:
                title = m.group(1).strip().lower()
                current = title if any(w in title for w in wanted) else None
                continue
            if current:
                lines.append(raw.rstrip())
        # Blank lines are KEPT: bullets() needs them to tell a hard-wrapped
        # continuation (flush-left line directly after the item it continues)
        # from the first line of a new paragraph (separated by a blank).
        return lines

    def bullets(lines):
        # A wrapped kickoff bullet — a markdown list item whose text spills
        # onto an indented continuation line — must stay ONE item, so the
        # success criteria and scope lists don't split mid-sentence. A line
        # starts a new item when it is a list item (`- `/`* `/`1.`/`1)`) or is
        # not indented; an indented non-list line continues the previous item.
        # Nested bullets (indented `- …`) stay separate items, as before.
        # A flush-left NON-list line ADJACENT to an item that does not END a
        # sentence (no terminal `.`/`:`/`;`/`!`/`?`) also continues that item:
        # the kickoff writer hard-wrapped prose at ~78 columns and the wrap
        # landed at column 0 (run-1791736854 split "…every heading" / "below,
        # verbatim, …" into two success criteria). A blank line, or terminal
        # punctuation on the previous item, closes the thought — the next
        # flush-left line is then genuinely a new item.
        items = []
        after_blank = True
        for line in lines:
            stripped = re.sub(r"^\s*[-*]\s*", "", line).strip()
            if not stripped:
                after_blank = True
                continue
            is_list_item = bool(re.match(r"^\s*(?:[-*]\s+|\d+[.)]\s+)", line))
            if is_list_item:
                items.append(stripped)
            elif line[:1].isspace() and items:
                # Join the raw continuation text: the marker strip above would
                # eat a leading '-'/'*' that is content ('--flag', '*emph*').
                items[-1] = items[-1] + " " + line.strip()
            elif (
                after_blank
                or not items
                or items[-1].rstrip()[-1:] in (".", ":", ";", "!", "?")
            ):
                items.append(stripped)
            else:
                # Reaching here: flush-left, adjacent, previous item does not
                # end a sentence — a hard-wrapped continuation. Join it.
                items[-1] = items[-1] + " " + line.strip()
            after_blank = False
        return items

    def first_heading():
        for line in text.splitlines():
            m = re.match(r"^\s*#\s+(.+?)\s*$", line)
            if m:
                return m.group(1).strip()
        return kickoff.stem.replace("-", " ").replace("_", " ")

    def load_yaml(path):
        try:
            import yaml
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def command_hints():
        patterns = [
            r"\bpnpm\s+(?:test|run\s+test|type-check)\b[^\n`]*",
            r"\bnpm\s+(?:test|run\s+test)\b[^\n`]*",
            r"\bpytest\b[^\n`]*",
            r"\bcargo\s+test\b[^\n`]*",
            r"\bgo\s+test\b[^\n`]*",
            r"\bbash\s+tests/[^\n`]+",
            r"\bmake\s+(?:test|check|verify|smoke|probe|coverage|lint|ci)[A-Za-z0-9_.:-]*(?:\s+&&\s+make\s+[A-Za-z0-9_.:-]+)*[^\n`]*",
            r"\bbash\s+recipes/[^\s`]+\.sh\b[^\n`]*",
            r"\bbash\s+verifiers/[^\s`]+\.sh\b[^\n`]*",
            r"\b\./verifiers/[^\s`]+\.sh\b[^\n`]*",
            r"\bbin/mini-ork(?:-[a-z]+)?\s+(?:verify|run|classify|plan)\b[^\n`]*",
        ]
        found = []
        for pattern in patterns:
            found.extend(m.group(0).strip().rstrip(".") for m in re.finditer(pattern, text, re.I))
        return list(dict.fromkeys(found))

    success = bullets(section_lines(["success", "definition of done", "done when", "acceptance"]))
    scope_allow = bullets(section_lines(["scope allow", "in scope", "scope"]))
    scope_deny = bullets(section_lines(["scope deny", "out of scope", "forbidden"]))
    def _bullet_only(lines):
        out = []
        for line in lines:
            m = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
            if m:
                out.append(m.group(1).strip())
        return out
    # The explicit "## Verification commands" section is the authoritative
    # acceptance bar — when it exists, store exactly its bullets, in order,
    # backtick-stripped and deduped, with no cap. command_hints() regex-scans
    # the whole kickoff prose for runner invocations and was sweeping prose
    # mentions ("…names the test runner…") into the verifier's slot 1 and
    # pushing real commands past the cap (run-1791853722-board-k3 ran a bare
    # runner over 8647 tests and timed out, never reaching the kickoff's
    # `ruff check`). Keep it as the no-section fallback with its cap of three.
    def _section_commands(lines):
        # Bullets outside fences, and every command inside a fenced block, in
        # document order. A kickoff writes a long command as one fenced shell
        # line split with ``\`` continuations; reading only bullets left the
        # section empty, fell through to the whole-kickoff regex scan, and the
        # verifier ran a bare ``pytest`` over the full suite until it timed
        # out (cloud-providers A1/A2, 2026-10-13). Continuations are joined;
        # blank lines, ``#`` comments and a leading ``$ `` prompt are dropped.
        out, buf, in_fence = [], "", False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                if buf:
                    out.append(buf)
                    buf = ""
                in_fence = not in_fence
                continue
            if not in_fence:
                out.extend(_bullet_only([line]))
                continue
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("$ "):
                stripped = stripped[2:].strip()
            buf = f"{buf} {stripped}" if buf else stripped
            if buf.endswith("\\"):
                buf = buf[:-1].rstrip()
                continue
            out.append(buf)
            buf = ""
        if buf:
            out.append(buf)
        return out
    _explicit_verify = _section_commands(section_lines([
        "verification command", "verification commands", "proof of success"]))
    _explicit = []
    for _c in _explicit_verify:
        _c = _c.strip()
        if _c.startswith("`") and _c.endswith("`") and len(_c) >= 2:
            _c = _c[1:-1].strip()
        if _c and _c not in _explicit:
            _explicit.append(_c)
    if _explicit:
        commands = _explicit
    else:
        commands = command_hints()

    task_yaml = load_yaml(rbase / "recipes" / recipe / "task_class.yaml")
    artifact_yaml = load_yaml(rbase / "recipes" / recipe / "artifact_contract.yaml")
    agents_yaml = load_yaml(agents_path)

    outputs = artifact_yaml.get("outputs") or []
    if isinstance(outputs, str):
        outputs = [outputs]

    lanes = {}
    if isinstance(agents_yaml.get("lanes"), dict):
        lanes = agents_yaml["lanes"]

    questions = []
    if not success:
        questions.append("What exact success criteria should the verifier use?")
    if not scope_allow:
        questions.append("Which files or directories are explicitly in scope?")
    if not commands:
        questions.append("What command should prove this run succeeded?")
    if task_class == "db_migration":
        questions = [
            "Which database engine and version should this migration target?",
            "Is downtime allowed, and what is the maximum acceptable window?",
            "What exact rollback or backup restore path should the planner assume?",
        ]
    elif task_class == "ui_audit" and len(questions) < 3:
        questions.append("Which target user profile or viewport should the audit prioritize?")

    questions = questions[:3]
    confidence = 0.35
    confidence += 0.15 if success else 0
    confidence += 0.15 if scope_allow else 0
    confidence += 0.15 if commands else 0
    confidence += 0.10 if outputs else 0
    confidence += 0.10 if lanes else 0
    confidence = min(confidence, 0.95)

    high_risk = task_class in {"db_migration", "bdd_first_delivery"}
    status = "ready"
    if questions:
        status = "blocked_profile" if high_risk else "needs_answers"

    # The implementer reads this file to learn WHICH repo it is fixing, so it must
    # agree with the edit surface `_resolve_target_cwd` pins. `Path.cwd()` did not:
    # a spawned child runs with cwd = an empty scratch workspace while
    # MO_TARGET_CWD — the documented lever for where dispatched agents write —
    # carries the real target repo. Live receipt: every goal-loop child read
    # `target_repo=<empty runs/.../children/<id>/worktree>`, reported "no source
    # visibility", and no-op'd, so the outer loop saw a 0-byte diff it could never
    # move. Prefer the explicit lever; leave the cwd fallback for host runs.
    _explicit_target = context_env("MO_TARGET_CWD", "") or ""
    data = {
        "schema_version": "1.0",
        "kickoff_path": str(kickoff.resolve()),
        "target_repo": _explicit_target if os.path.isdir(_explicit_target)
        else str(Path.cwd().resolve()),
        "recipe": recipe,
        "task_class": task_class,
        "user_goal": first_heading(),
        "success_criteria": success,
        "scope_allow": scope_allow,
        "scope_deny": scope_deny,
        "risk_tolerance": "conservative" if high_risk else "standard",
        "budget_cap_usd": task_yaml.get("budget_cap_usd"),
        "provider_policy": {
            "source": str(Path(agents_path).resolve()),
            "lanes": lanes,
            "env": {"MINI_ORK_PROVIDER_POLICY": str(Path(agents_path).resolve()) if lanes else ""},
        },
        "artifact_destination": outputs,
        "verification_command": commands if _explicit else commands[:3],
        "human_questions": questions,
        "confidence": round(confidence, 2),
        "profile_status": status,
    }
    # remote-nodes-14 §1: the run's resolved placement + environment, so the
    # API/UI can badge a remote run. Absent on a default run (unchanged profile).
    placement = context_env("MO_PLACEMENT", "").strip()
    node_env = context_env("MO_NODE_ENV", "").strip()
    if placement:
        data["placement"] = placement
    if node_env:
        data["environment"] = node_env
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return data


def _gen_profile_or_reuse(kickoff, root, recipe, task_class, profile_path, agents_path,
                          rbase) -> dict:
    """Reuse an already-answered profile instead of re-deriving its questions.

    ``mini-ork resume --answer`` applies answers to ``run_profile.json``
    (``profile_status`` → ``ready``, ``human_questions`` → ``[]``) before
    re-entering the lifecycle. Without this guard ``gen_profile`` would re-derive
    the very questions the run was blocked on from the unchanged kickoff and
    re-block it, so the profile gate would never pass. A fresh run has no
    ``run_profile.json`` yet and always generates.
    """
    try:
        existing = json.load(open(profile_path, encoding="utf-8"))
    except Exception:
        existing = {}
    if existing.get("profile_status") == "ready" and existing.get("answers"):
        return existing
    return gen_profile(kickoff, root, recipe, task_class, profile_path, agents_path,
                       recipe_base=rbase)


def _prior_run_roots(profile_path) -> dict | None:
    """The ``roots`` an existing ``run_profile.json`` already pinned, if any."""
    try:
        roots = json.load(open(profile_path, encoding="utf-8")).get("roots")
    except Exception:
        return None
    return roots if isinstance(roots, dict) and roots else None


def _pin_run_roots_early(profile_path, prior_roots: dict | None) -> None:
    """Pin the run's roots right after the profile is written, before planning.

    Roots used to be pinned only at the pre-implementer baseline, so the
    planner dispatched with none. Under ``--placement remote`` the planner lane
    is a remote spawn, and a remote spawn without the run's pinned roots refuses
    to start (it cannot translate a single host path), so every remote run died
    in its planner (Daytona e2e, run-1791910667-50438). ``gen_profile`` rewrites
    the whole profile, so roots an earlier cycle pinned are carried over first:
    a resumed run keeps its original roots, as ``persist_run_roots`` promises.
    """
    from mini_ork.runtime.run_roots import persist_run_roots

    run_dir = os.path.dirname(str(profile_path))
    if prior_roots:
        try:
            prof = json.load(open(profile_path, encoding="utf-8"))
            if isinstance(prof, dict) and "roots" not in prof:
                prof["roots"] = prior_roots
                with open(profile_path, "w", encoding="utf-8") as fh:
                    json.dump(prof, fh, indent=2, sort_keys=True)
        except (OSError, ValueError):
            pass
    persist_run_roots(run_dir)


def _grep_kv(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return ""


def _deadline(root, *args) -> int:
    """Call the native deadline port while retaining the old helper contract."""
    del root
    fn = args[0]
    if fn == "mo_deadline_init":
        return deadline_budget.init(args[1], int(args[2]), args[3])
    if fn == "mo_deadline_check":
        return deadline_budget.check(args[1])
    raise ValueError(f"unsupported deadline function: {fn}")


def _execute_log_name() -> str:
    """The live run is the only author of its own ``execute.log``.

    A rehearsal (``MINI_ORK_DRY_RUN=1``, e.g. a nested lifecycle sharing the run
    id) used to win the old ``not os.path.isfile`` race, leaving the run dir with
    a record of a different workflow. Rehearsals get their own filename.
    """
    return ("execute.dryrun.log"
            if os.environ.get("MINI_ORK_DRY_RUN", "0") == "1"
            else "execute.log")


def _run_lifecycle(argv, root) -> int:
    """Strip the machine-readable ``--json`` flag (accepted anywhere in argv),
    run the lifecycle, and — when requested — emit one stable
    ``mini_ork_result={...}`` line so in-process callers parse a single JSON
    object instead of scraping scattered key=value lines."""
    emit_json = False
    inner: list[str] = []
    for a in argv:
        if a == "--json":
            emit_json = True
        else:
            inner.append(a)
    sink: dict = {}
    crashed = False
    rc: int | None = None
    # Run-level boundary: everything the lifecycle publishes (artifact path,
    # run dir, per-node MO_* vars) is wiped from the contextvar layer when
    # the run exits. The lifecycle owns the run context; in-process callers
    # (SDK, tests) must not inherit a finished run's bindings. The os.environ
    # write keeps its historical leak-forever semantics.
    with run_context_scope({}):
        try:
            rc = _run_lifecycle_impl(inner, root, sink)
        except BaseException:
            crashed = True
            raise
        finally:
            try:
                _release_remote_session()
            finally:
                # End the run principal on every return path and on exceptions,
                # and pop the handle so --json never emits the object.
                concord_run.stop(sink.pop("_concord", None))
            # A run that leaves here still non-terminal would read as in flight
            # forever. Teardown is best-effort: it MUST NOT change the rc.
            try:
                _close_run_record(sink, crashed=crashed, rc=rc)
            except Exception as exc:  # noqa: BLE001 — teardown is best-effort
                sys.stderr.write(f"[warn] run record close failed: {exc}\n")
            # Raise task_runs.cost_usd to the ledger total so every reporter
            # (web UI, MCP list_runs / run_status / wait_for_run, ACP history,
            # the ``mini_ork_result`` line) shows the same number. Best-effort:
            # reconcile errors MUST NOT change the run's return code.
            try:
                _reconcile_run_cost(sink)
            except Exception as exc:  # noqa: BLE001 — teardown is best-effort
                sys.stderr.write(f"[warn] cost reconcile failed: {exc}\n")
    if emit_json:
        sink["returncode"] = rc
        sys.stdout.write(
            "mini_ork_result=" + json.dumps(sink, separators=(",", ":")) + "\n"
        )
    return rc


def _release_remote_session() -> None:
    """remote-nodes-15: a ``--placement remote`` run ends with no session left
    on the node. execute closes its own, but every later step that ran a
    check (rubric, verify) is a separate process whose session nobody closed."""
    if context_env("MO_PLACEMENT", "").strip().lower() != "remote":
        return
    run_id = context_env("MINI_ORK_RUN_ID", "")
    if not run_id:
        return
    try:
        from mini_ork.runtime.workspace_session import release_remote_session

        release_remote_session(run_id, context_env("MINI_ORK_RUN_DIR", ""),
                               env=context_env_snapshot())
    except Exception as exc:  # noqa: BLE001 — teardown is best-effort; the node TTLs it
        sys.stderr.write(f"[warn] remote session release failed: {exc}\n")


def _write_execute_log(run_dir: str, out: str, err: str) -> None:
    """``execute.log`` = execute's stdout, then its stderr under a marker.

    The publisher's ``[fail]`` / ``[warn]`` / ``[BLOCK]`` reasons go to stderr;
    a rejection whose reason never reaches the run dir is unattributable
    (K0: nine approved SDD runs rolled back with no recorded cause).
    """
    log = out
    if err.strip():
        log += "\n── execute stderr ──\n" + err
    with open(os.path.join(run_dir, _execute_log_name()), "w", encoding="utf-8") as fh:
        fh.write(log)


def _close_run_record(sink: dict, *, crashed: bool, rc: int | None = None) -> None:
    """End the run's ``task_runs`` row, then drop its ``.pid``.

    Status first, ``.pid`` second: a kill between the two leaves a dead
    ``.pid`` on a terminal row, which the reaper ignores; the reverse order
    would leave a non-terminal row with no owner record — unprovable.
    """
    run_dir = sink.pop("_pid_run_dir", None)
    if not run_dir:
        return  # dry run, or the run never started (budget preflight)
    from mini_ork.learning.advantage_store import resolve_db_path
    from mini_ork.orchestration import run_reaper

    try:
        db = resolve_db_path(context_env("MINI_ORK_DB") or None)
        if os.path.isfile(db):
            run_reaper.close_run_record(db, sink.get("run_id") or "", Path(run_dir), crashed=crashed, rc=rc)
    finally:
        run_reaper.release_pid_file(Path(run_dir))


def _reconcile_run_cost(sink: dict) -> None:
    """Mirror ``cost_ledger.reconcile_run_cost`` into the lifecycle's ``sink``.

    Skips on dry-run (no task_runs row to update) and when the lifecycle has
    not stamped a ``run_id`` into the sink. Resolves the db the way the
    lifecycle already does (``MINI_ORK_DB`` → ``<MINI_ORK_HOME>/state.db``).
    Never raises — the caller wraps it in ``try/except`` as belt-and-braces.
    """
    if os.environ.get("MINI_ORK_DRY_RUN", "0") == "1":
        return
    run_id = sink.get("run_id") or context_env("MINI_ORK_RUN_ID", "")
    if not run_id:
        return
    from mini_ork import cost_ledger
    from mini_ork.learning.advantage_store import resolve_db_path

    db = resolve_db_path(context_env("MINI_ORK_DB") or None)
    cost = cost_ledger.reconcile_run_cost(db, run_id)
    if cost is not None:
        sink["cost_usd"] = round(cost, 6)


RC_BLOCKED = 75  # EX_TEMPFAIL: the run never started; retry once the cause clears


def _spent_last_24h(db: str) -> float | None:
    """Display-only mirror of the spend cost_circuit_open compares.

    Reads the same ``cost_ledger.spent_last_24h`` ledger the circuit uses (so
    the printed total matches the gate decision), but returns ``None`` on
    exception rather than ``0.0`` so the budget-preflight can distinguish
    "no spend recorded" from "could not read the ledger". The DECISION
    always comes from cost_circuit_open itself."""
    try:
        from mini_ork import cost_ledger
        return cost_ledger.spent_last_24h(db)
    except Exception:
        return None


def _budget_preflight(sink) -> int | None:
    """Refuse to start when the rolling-24h budget is already spent.

    The cost circuit used to trip per LLM call, inside the run: every node
    then failed in seconds with finish_reason cost_limit, and the run ended
    as an ordinary verdict=fail with files_changed 0 — indistinguishable from
    a model that tried and failed. Say so once, before anything is dispatched.
    """
    if os.environ.get("MINI_ORK_DRY_RUN", "0") == "1":
        return None
    from mini_ork.dispatch.llm_dispatch import cost_circuit_open
    from mini_ork.learning.advantage_store import resolve_db_path
    raw = os.environ.get("MO_DAILY_BUDGET_USD", "50")
    try:
        budget = float(raw)
    except ValueError:
        return None  # the dispatcher reports a malformed budget itself
    db = resolve_db_path(context_env("MINI_ORK_DB") or None)
    if not cost_circuit_open(db, budget):
        return None
    spent = _spent_last_24h(db)
    shown = f"${spent:.2f}" if spent is not None else "the budget"
    sys.stderr.write(
        f"mini-ork: daily budget exhausted — {shown} spent in the last 24h against "
        f"MO_DAILY_BUDGET_USD=${budget:.2f}. Nothing was dispatched.\n"
        "  Raise MO_DAILY_BUDGET_USD for this run, or wait for the window to roll.\n")
    sink.update({"verdict": "blocked", "blocked_reason": "cost_limit",
                 "budget_usd": budget})
    if spent is not None:
        sink["spent_24h_usd"] = round(spent, 2)
    return RC_BLOCKED


def _gate_set_overrides(
    pairs: Sequence[str], base_env: Mapping[str, str]
) -> tuple[dict[str, str], list[str], str]:
    """Parse repeatable ``--set K=V`` values and strip premium feature knobs.

    Pure — no I/O — so the ``run`` flag is unit-testable without driving the
    lifecycle. Returns ``(filtered_env, blocked_feature_ids, error)``:

    * a pair without ``=`` yields ``({}, [], "--set requires K=V")`` and the
      caller writes ``error`` to stderr and returns 2;
    * otherwise the merged overrides go through
      :func:`mini_ork.features.gate_env`, with ``accept_premium`` read from the
      pairs overlaid on ``base_env`` — so ``--set MO_ACCEPT_PREMIUM=1`` unlocks
      premium in the same invocation — and ``blocked_feature_ids`` names any
      premium feature whose knobs were dropped.
    """
    overrides: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            return {}, [], "--set requires K=V"
        overrides[key] = value
    accept = overrides.get(
        "MO_ACCEPT_PREMIUM", base_env.get("MO_ACCEPT_PREMIUM", "")
    ) not in ("", "0")
    filtered, blocked = gate_env(overrides, accept_premium=bool(accept))
    return filtered, blocked, ""


def _run_lifecycle_impl(argv, root, sink) -> int:
    t0 = int(time.time())
    # ── flag pre-parse: pull --deadline / --placement / --env / --set out ──
    deadline = ""
    placement = ""
    node_env = ""
    set_pairs: list[str] = []
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--deadline":
            if i + 1 >= len(argv):
                sys.stderr.write("--deadline requires <seconds>\n"); return 2
            v = argv[i + 1]
            if not v.isdigit():
                sys.stderr.write(f"--deadline: seconds must be a positive integer (got '{v}')\n"); return 2
            deadline = v; i += 2
        elif a.startswith("--deadline="):
            v = a.split("=", 1)[1]
            if not v.isdigit():
                sys.stderr.write(f"--deadline: seconds must be a positive integer (got '{v}')\n"); return 2
            deadline = v; i += 1
        elif a == "--placement":
            if i + 1 >= len(argv):
                sys.stderr.write("--placement requires local|remote\n"); return 2
            v = argv[i + 1]
            if v not in ("local", "remote"):
                sys.stderr.write(f"--placement: expected local|remote (got '{v}')\n"); return 2
            placement = v; i += 2
        elif a.startswith("--placement="):
            v = a.split("=", 1)[1]
            if v not in ("local", "remote"):
                sys.stderr.write(f"--placement: expected local|remote (got '{v}')\n"); return 2
            placement = v; i += 1
        elif a == "--env":
            if i + 1 >= len(argv):
                sys.stderr.write("--env requires <name>\n"); return 2
            node_env = argv[i + 1]; i += 2
        elif a.startswith("--env="):
            node_env = a.split("=", 1)[1]; i += 1
        elif a == "--set":
            if i + 1 >= len(argv):
                sys.stderr.write("--set requires K=V\n"); return 2
            set_pairs.append(argv[i + 1]); i += 2
        elif a.startswith("--set="):
            set_pairs.append(a.split("=", 1)[1]); i += 1
        else:
            rest.append(a); i += 1

    # ── --set K=V: parse, gate premium knobs, publish before recipe resolution ──
    # The filtered overrides are published here so dispatch, the session factory
    # and gen_profile all read them; a blocked premium feature is announced once
    # and the run continues with the survivors (never an abort).
    if set_pairs:
        set_env, blocked, set_err = _gate_set_overrides(set_pairs, context_env_snapshot())
        if set_err:
            sys.stderr.write(set_err + "\n"); return 2
        if blocked:
            sys.stderr.write(
                "mini-ork: premium feature(s) not enabled: " + ", ".join(blocked)
                + " — set MO_ACCEPT_PREMIUM=1 to enable premium features\n")
        if set_env:
            publish_env(set_env)

    # remote-nodes-14 §1: the flag wins over MO_PLACEMENT / MO_NODE_ENV; the
    # resolved values are published so dispatch, the session factory and
    # gen_profile all read one answer. Unset → nothing published → today's run.
    placement = placement or context_env("MO_PLACEMENT", "").strip()
    node_env = node_env or context_env("MO_NODE_ENV", "").strip()
    if placement not in ("", "local", "remote"):
        sys.stderr.write(f"MO_PLACEMENT: expected local|remote (got '{placement}')\n"); return 2
    if placement == "remote":
        from mini_ork.remote.environments import list_profiles, load_profile
        env_now = context_env_snapshot()
        problem = ""
        if not node_env:
            problem = "--placement remote requires --env <name> (or MO_NODE_ENV)"
        else:
            try:
                load_profile(node_env, env=env_now)
            except FileNotFoundError:
                problem = f"--env: environment profile '{node_env}' not found"
            except ValueError as exc:   # schema violation or a secret-looking env key
                problem = f"--env: environment profile '{node_env}' is invalid: {exc}"
        if problem:
            names = list_profiles(env=env_now)
            avail = ", ".join(names) if names else (
                "none (add config/environments/<name>.yaml; see default.yaml.example)")
            sys.stderr.write(f"{problem}. Available profiles: {avail}\n")
            return 2
    if placement or node_env:
        publish_env({k: v for k, v in (("MO_PLACEMENT", placement), ("MO_NODE_ENV", node_env)) if v})
    if placement == "remote":
        # One node session for the whole run: execute keeps it for the later
        # steps; _release_remote_session ends it when the lifecycle returns.
        publish_env({"MO_REMOTE_SESSION_SCOPE": "lifecycle"})

    if not rest:
        sys.stderr.write("recipe name or kickoff.md path required\n"); return 2
    first = rest.pop(0)

    if os.path.isfile(first):
        kickoff = first
        probe = subprocess.run(
            [sys.executable, "-m", "mini_ork.cli.classify", kickoff],
            capture_output=True,
            text=True,
            env={**_module_env(root), "MINI_ORK_DRY_RUN": "1"},
        )
        if probe.returncode != 0:
            sys.stderr.write(probe.stderr); return probe.returncode
        probed_class = _grep_kv(probe.stdout, "task_class")
        recipe = resolve_recipe(root, probed_class)
        if not recipe:
            sys.stderr.write(f"could not resolve recipe for task_class={probed_class}\n"); return 2
    else:
        recipe = first
        if not rest:
            sys.stderr.write("kickoff.md path required\n"); return 2
        kickoff = rest.pop(0)

    rbase, recipe = _resolve_recipe_base(root, recipe)
    if not rbase:
        probed, seen = [], set()
        for cand in (os.environ.get("MINI_ORK_HOME", ""), os.getcwd(), root):
            if not cand:
                continue
            real = os.path.realpath(cand)
            if real not in seen:
                seen.add(real)
                probed.append(f"{cand}/recipes/")
        sys.stderr.write(f"no recipe: {recipe} (ls {' or '.join(probed)})\n"); return 2
    os.environ["MINI_ORK_RECIPE"] = recipe
    os.environ["MINI_ORK_RECIPE_ROOT"] = rbase
    os.environ["MINI_ORK_WORKFLOW"] = os.path.join(rbase, "recipes", recipe, "workflow.yaml")
    if not os.path.isfile(kickoff):
        sys.stderr.write(f"kickoff not found: {kickoff}\n"); return 2
    run_id = os.environ.setdefault("MINI_ORK_RUN_ID", f"run-{int(time.time())}-{os.getpid()}")
    sink["run_id"] = run_id
    # Name the protection gap once, and only when the vault chain changes:
    # values resolving in the clear is the legitimate zero-setup tier, but the
    # operator of a shared or audited run should hear it without having to
    # remember to ask. A bad config is left to the boundary that already reports
    # it, so this never changes which error a run dies on.
    try:
        from mini_ork.dispatch.vault import (
            VaultError,
            report_protection_gap,
            vault_environment,
        )

        report_protection_gap(vault_environment())
    except VaultError:
        pass
    # Heal reflect rows a hard-killed parent (SIGKILL, uncatchable) left stuck
    # 'running' — the live path finalizes its own row in the reflect `finally`,
    # but a parent killed mid-reflect leaves one for the next run to mop up.
    # Age-guarded, so a reflect still in flight is never touched.
    with contextlib.suppress(Exception):
        trace_store.finalize_reflect_traces()
    if (blocked := _budget_preflight(sink)) is not None:
        return blocked

    # Start guard: refuse dispatch while a previous run with the same kickoff
    # has a pending ``retry_precondition`` gate. ``MO_IGNORE_PENDING_FIX=1``
    # bypasses for opt-in situations (a user rerunning the same kickoff after
    # they believe the fix is in). Reuses ``RC_BLOCKED = 75`` so the caller
    # maps exit-code failure cleanly. The home resolution below is the same
    # one the rest of the lifecycle uses (canonicalised by the canonical
    # setter at L846+); keeping a single source of truth means the guard
    # sees the same env contract the run will dispatch under.
    try:
        from mini_ork.recovery import retry_notify
        _pid_home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
        _pending = retry_notify.pending_fix_for_kickoff(
            Path(_pid_home), os.path.realpath(kickoff),
        )
        if _pending is not None:
            _pid_run_id = str(_pending.get("feature") or "?")
            _ctxt = _pending.get("context") if isinstance(_pending.get("context"), dict) else {}
            _nc = _ctxt.get("hint", {}).get("needs_change") if isinstance(_ctxt.get("hint"), dict) else None
            _summary = (_nc or {}).get("summary") or "an upstream change is required"
            sys.stderr.write(
                f"needs a change before this run can continue: "
                f"pending run {_pid_run_id} ({_summary}). "
                f"Confirm with: mini-ork board retry {_pid_run_id} --ack-change "
                f"(or set MO_IGNORE_PENDING_FIX=1).\n"
            )
            return RC_BLOCKED
    except Exception:  # noqa: BLE001 — guard is best-effort, never break the run
        pass

    # Every run is a Concord principal: register it (fail-open) so the node
    # workers this run spawns inherit CONCORD_PRINCIPAL. Concord must never
    # fail or delay a run, so any error here only logs and drops the handle.
    try:
        sink["_concord"] = concord_run.start(run_id, recipe, kickoff)
    except Exception as exc:
        sys.stderr.write(f"[concord] run principal not started: {exc}\n")
        sink["_concord"] = None

    # Resolve the home ONCE for the rest of the lifecycle. The start guard
    # above and the pid claim below both used to repeat the
    # ``env.get(MINI_ORK_HOME) or cwd/.mini-ork`` dance; canonicalising it
    # here removes two divergent sources of truth and keeps the home the
    # notify / task_state readers see the same one the writer used.
    pid_home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")

    # The run's owner, when the launcher named one (``MO_RUN_OWNER``); else it
    # is inferred when the run fails (retry_notify.notify).
    if os.environ.get("MINI_ORK_DRY_RUN", "0") != "1":
        try:
            from mini_ork.recovery import retry_notify
            retry_notify.record_owner_at_start(Path(pid_home), run_id)
        except Exception:  # noqa: BLE001 — never break the run
            pass

    # Owner record: <run_dir>/.pid names this process from before classify
    # until teardown, so a row whose .pid process is gone is provably orphaned
    # (orchestration/run_reaper.py). Same home the run dir resolves to below.
    if os.environ.get("MINI_ORK_DRY_RUN", "0") != "1":
        from mini_ork.orchestration import run_reaper

        pid_run_dir = Path(pid_home) / "runs" / run_id
        run_reaper.claim_pid_file(pid_run_dir)
        sink["_pid_run_dir"] = str(pid_run_dir)

    # derived task_class from recipe's task_class.yaml::name
    derived = ""
    tc_yaml = os.path.join(rbase, "recipes", recipe, "task_class.yaml")
    if os.path.isfile(tc_yaml):
        try:
            import yaml
            derived = ((yaml.safe_load(open(tc_yaml)) or {}).get("name") or "").strip()
        except Exception:
            derived = ""
    if not derived:
        derived = recipe.replace("-", "_")

    # repo-integrity guard (best-effort native side-channel)
    try:
        repo_integrity_guard.check_and_heal()
    except Exception:
        pass

    # ── classify ──
    cl = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.classify",
         "--task-class", derived, kickoff],
        capture_output=True,
        text=True,
        env=_module_env(root),
    )
    if cl.returncode != 0:
        sys.stderr.write(cl.stderr); return cl.returncode
    sys.stdout.write(cl.stdout)
    task_class = _grep_kv(cl.stdout, "task_class")
    os.environ["MINI_ORK_TASK_CLASS"] = task_class
    sink["task_class"] = task_class

    home = os.environ.setdefault("MINI_ORK_HOME", os.path.join(os.getcwd(), ".mini-ork"))
    run_dir = os.path.join(home, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    # Pin the canonical run dir so a stale ambient MINI_ORK_RUN_DIR (e.g. leaked
    # from a long-lived parent worker) can't split the run — classify/plan wrote
    # here, and execute's node artifacts must land here too, or the caller reads
    # runs/<id>/verified-artifact.json from a dir the artifacts never reached.
    publish_env({"MINI_ORK_RUN_DIR": run_dir})
    try:
        config_resolve.snapshot_run_config(run_dir)
    except Exception:
        pass
    # Run-scoped environment (config/env.yaml). Published here, ahead of
    # classify/plan, so the planner and every child process see it — the node
    # boundary later re-publishes it plus that node's own overrides. A malformed
    # file stops the run now, before any spend, rather than at the first node
    # whose overrides would have silently gone missing.
    try:
        _run_env = load_run_env(run_dir)
    except (RunEnvError, SecretStoreError, VaultError) as exc:
        sys.stderr.write(f"run environment is invalid: {exc}\n")
        return 2
    if _run_env.run_level:
        publish_env(_run_env.run_level)
    profile_path = os.path.join(run_dir, "run_profile.json")
    os.environ["MINI_ORK_PROFILE_PATH"] = profile_path

    if deadline and int(deadline) > 0:
        if _deadline(root, "mo_deadline_init", run_id, deadline, run_dir) != 0:
            sys.stderr.write("deadline init failed\n"); return 2

    def _gate(where, artifact="") -> bool:
        if deadline and _deadline(root, "mo_deadline_check", run_id) != 0:
            tail = f"; best-so-far artifact: {artifact or '<none>'}" if artifact else \
                   "; no best-so-far artifact yet"
            sys.stderr.write(f"deadline_hit after {where}{tail}; exiting cleanly\n")
            return False
        return True

    if not _gate("classify"):
        return 0

    # ── profile ──
    # MINI_ORK_AGENTS now feeds the overlay resolver
    # (dispatch/agents_config.effective_path): the env var still points at
    # the user's overlay file; the resolver merges it over the team template
    # and materialises the effective policy. With no overlay present the
    # resolved path is byte-equivalent to the prior "home/config/agents.yaml".
    agents_path = agents_config.overlay_or(
        os.path.join(home, "config", "agents.yaml"), home=home)
    prior_roots = _prior_run_roots(profile_path)
    data = _gen_profile_or_reuse(kickoff, root, recipe, task_class, profile_path,
                                 agents_path, rbase)
    _pin_run_roots_early(profile_path, prior_roots)
    sys.stdout.write(f"profile_path={profile_path}\n")
    sys.stdout.write(f"profile_status={data['profile_status']}\n")
    sys.stdout.write(f"profile_confidence={data['confidence']:.2f}\n")
    sink["profile_status"] = data["profile_status"]
    sink["profile_confidence"] = round(float(data["confidence"]), 4)
    if data["human_questions"]:
        sys.stdout.write("profile_questions=" + json.dumps(data["human_questions"], separators=(",", ":")) + "\n")
    if os.environ.get("MINI_ORK_PROFILE_STRICT", "0") == "1" and data["profile_status"] == "blocked_profile":
        sys.stderr.write("profile blocked: answer profile_questions before planning\n"); return 2

    if not _gate("profile"):
        return 0

    # ── plan ──
    plan_env = _module_env(root)
    pl = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.plan", kickoff],
        capture_output=True, text=True, env=plan_env,
    )
    if pl.returncode != 0:
        sys.stderr.write(pl.stderr); return pl.returncode
    sys.stdout.write(pl.stdout)
    plan_path = _grep_kv(pl.stdout, "plan_path")
    os.environ["MINI_ORK_PLAN_PATH"] = plan_path
    sink["plan_path"] = plan_path
    if not _gate("plan", plan_path):
        return 0

    # ── auto-repair: default ON ── (user rule: always revive and fix).
    # Published BEFORE execute so the whole run flow (and the triage gate in
    # execute.py, which stands down when MO_AUTO_REPAIR=1) sees one owner for a
    # terminal failure. An explicit operator "0" is never overridden.
    if context_env("MO_AUTO_REPAIR", "") == "":
        apply_env_overrides({"MO_AUTO_REPAIR": "1"})

    # ── execute ── (failures do not exit; verify+reflect still fire)
    from mini_ork.cli import execute as mini_ork_execute
    execute_stdout = io.StringIO()
    execute_stderr = io.StringIO()
    with contextlib.redirect_stdout(execute_stdout), contextlib.redirect_stderr(execute_stderr):
        run_rc = mini_ork_execute.main([], root=root)
    if sink.get("_pid_run_dir"):
        # execute drops .pid in its own finally; this process still owns the run.
        from mini_ork.orchestration import run_reaper

        run_reaper.claim_pid_file(Path(sink["_pid_run_dir"]))
    execute_out = execute_stdout.getvalue()
    execute_err = execute_stderr.getvalue()
    sys.stdout.write(execute_out)
    sys.stderr.write(execute_err)
    artifact = _grep_kv(execute_out, "artifact_path")
    if artifact:
        publish_env({"MINI_ORK_ARTIFACT_PATH": artifact})
        sink["artifact_path"] = artifact
    if deadline and _deadline(root, "mo_deadline_check", run_id) != 0:
        sys.stderr.write(f"deadline_hit after execute; best-so-far artifact: {artifact or '<none>'}\n")
        return run_rc

    _run_dir = context_env("MINI_ORK_RUN_DIR", "")
    if not _run_dir and plan_path:
        _run_dir = os.path.dirname(plan_path)
    if _run_dir and _run_dir != "." and os.path.isdir(_run_dir):
        try:
            _write_execute_log(_run_dir, execute_out, execute_err)
        except OSError:
            pass

    # ── auto-repair: hand the failure off as soon as execute has decided it ──.
    # UNCONDITIONAL — a withheld publish sets status='failed' during execute
    # while run_rc == 0, so the status is already terminal when the hook runs and
    # gating on rc would silently miss every abstain case. ``maybe_repair`` reads
    # the run's DB status itself and never raises, so a green run stays quiet.
    # Handing off here — before the rubric/verify/reflect tail — is safe: the
    # spawned recover already waits for this lifecycle to exit
    # (``MO_AUTO_REPAIR_WAIT_PID``), so if this process later dies in verify or
    # reflect, the repair still happens.
    try:
        from mini_ork.recovery import auto_repair
        auto_repair.maybe_repair(Path(home), run_id, wait_for_exit=True)
    except Exception:  # noqa: BLE001
        pass

    # ── rubric pre-screen (advisory, native side-channel) ──
    if _should_run_rubric(_run_dir):
        sys.stdout.write("── rubric (advisory pre-screen) ──\n")
        try:
            # The parity port contains real print() calls; keep the launcher's
            # stdout reserved for lifecycle key/value and verdict output.
            with contextlib.redirect_stdout(io.StringIO()):
                rubric_prescreen.mo_rubric_run_score(
                    kickoff,
                    _run_dir,
                    task_class or "generic",
                    mini_ork_root=root,
                    mini_ork_home=home,
                    mini_ork_db=context_env("MINI_ORK_DB") or None,
                )
        except Exception:
            pass
        try:
            trace_store.grade_run_reward(
                _run_dir,
                run_id,
                db=context_env("MINI_ORK_DB") or None,
            )
        except Exception:
            pass

    # ── verify ──
    if _run_dir and _run_dir != "." and os.path.isdir(_run_dir):
        publish_env({"MINI_ORK_RUN_DIR": _run_dir})
    vargs = [sys.executable, "-m", "mini_ork.cli.verify"] + ([artifact] if artifact else [])
    vr = subprocess.run(vargs, capture_output=True, text=True, env=_module_env(root))
    sys.stdout.write(vr.stdout); sys.stderr.write(vr.stderr)
    _verdicts = re.findall(r'"verdict"\s*:\s*"([^"]+)"', vr.stdout)
    if _verdicts:
        sink["verdict"] = _verdicts[-1]
    if run_rc == 0:
        run_rc = vr.returncode

    # ── retry-notify: best-effort side-channel that records the owner,
    # writes NEEDS-CHANGE.md, and enqueues a ``retry_precondition`` gate
    # when the run failed and the hint classifies the cause. Never changes
    # the run's exit code — failures inside ``retry_notify.notify`` are
    # caught inside the helper. Only fires when ``run_rc != 0`` so a
    # green run stays quiet.
    if run_rc != 0 and _run_dir and _run_dir != "." and os.path.isdir(_run_dir):
        try:
            from mini_ork.recovery import retry_notify
            retry_notify.notify(Path(home), run_id)
        except Exception:  # noqa: BLE001
            pass

    if not _gate("verify", artifact):
        return run_rc

    # ── reflect (best-effort, Python-sole entrypoint) ──
    if os.environ.get("MO_AUTO_REFLECT", "1") == "1":
        sys.stdout.write("── reflect (auto, since run start) ──\n")
        reflect_db = context_env("MINI_ORK_DB") or os.path.join(home, "state.db")
        reflect_bound, reflect_bound_why = _reflect_timeout(reflect_db)
        # Tighten that derived bound to what is left of the run's --deadline:
        # reflect runs post-verdict, so it must not be the stage that blows a
        # budget the caller set. Best-effort — an unarmed deadline or an
        # unreadable status leaves the derived bound alone rather than wedging
        # the child on a failed cap.
        if deadline and int(deadline) > 0:
            try:
                st = deadline_budget.status(run_id)
                reflect_bound, reflect_bound_why = _cap_reflect_to_deadline(
                    reflect_bound, reflect_bound_why,
                    deadline_seconds=int(deadline),
                    remaining_seconds=int(st.get("remaining_seconds", 0) or 0),
                    hit=bool(st.get("hit", False)),
                )
            except Exception:  # noqa: BLE001
                pass
        # The child's output used to go into a pipe that was discarded on
        # return, so a reflect that died mid-pipeline left no evidence of where
        # it died — the researcher home's 1930 leaked 'running' rows are all
        # there is to show for months of passes. The log is written for every
        # outcome, the kill included, and it names the engine and the DB so a
        # drifted vendored copy or a home/DB mismatch is visible in the artifact
        # rather than only in a process list.
        reflect_head = (
            "=== mini-ork reflect (auto) ===\n"
            f"since:  {t0}\n"
            f"bound:  {reflect_bound}s ({reflect_bound_why})\n"
            f"engine: {root} (v{engine_version(root) or '?'})\n"
            f"home:   {home}\n"
            f"db:     {reflect_db}\n"
        )
        reflect_out = ""
        reflect_err = ""
        reflect_status = ""
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "mini_ork.cli.reflect", "--since", str(t0)],
                capture_output=True,
                text=True,
                timeout=reflect_bound,
                env={
                    **_module_env(root),
                    "MO_REFLECTION_BATCH": os.environ.get("MO_REFLECTION_BATCH", "25"),
                },
            )
            reflect_out = _child_text(completed.stdout)
            reflect_err = _child_text(completed.stderr)
            reflect_status = f"exit {completed.returncode}"
        except subprocess.TimeoutExpired as exc:
            reflect_out = _child_text(exc.stdout)
            reflect_err = _child_text(exc.stderr)
            reflect_status = f"timed out after {reflect_bound}s; skipped"
            sys.stdout.write("── reflect (timed out; skipped) ──\n")
        finally:
            # A timed-out reflect is SIGKILLed by subprocess.run, so its own
            # terminal trace write never runs and the row it opened as
            # 'running' leaks forever — inflating the running count AND hiding
            # the run from `resolve_finished_runs` (which reads "run over = no
            # trace running"). The parent owns the kill, so the parent owns the
            # terminal write. Keyed on run_id, so it only touches this run's
            # reflect row.
            with contextlib.suppress(Exception):
                trace_store.finalize_reflect_traces(run_id)
        reflect_log = ""
        if _run_dir and _run_dir != "." and os.path.isdir(_run_dir):
            reflect_log = _write_reflect_log(
                _run_dir, reflect_head, reflect_status, reflect_out, reflect_err)
        sys.stdout.write(
            f"── reflect ({reflect_status}"
            f"{f'; log: {reflect_log}' if reflect_log else ''}) ──\n"
        )

    # ── trajectory retention (roadmap Step 2 / A2): best-effort TTL prune of
    # turn_jsonl artifacts. MO_TRAJECTORY_TTL_DAYS=0 disables; never gates.
    try:
        from mini_ork.dispatch import retention as _retention  # noqa: PLC0415
        _pruned = _retention.prune_from_env()
        if _pruned:
            print(f"  [retention] pruned {_pruned} turn_jsonl artifact(s) past TTL")
    except Exception:
        pass
    return run_rc


def _should_run_rubric(run_dir: str) -> bool:
    """Return whether the provider-backed rubric side channel may run.

    A dry run may create plans and local artifacts, but it must not invoke a
    model-provider CLI.  The rubric prescreen shells out to one, so keep it
    out of that lifecycle while preserving the existing MO_RUBRIC opt-out for
    real runs.
    """
    return (
        os.environ.get("MO_RUBRIC", "1") == "1"
        and os.environ.get("MINI_ORK_DRY_RUN", "0") != "1"
        and bool(run_dir)
        and os.path.isdir(run_dir)
    )


def _load_secret_store_env() -> None:
    """Bash-dispatcher parity: the retired dispatcher `source`d
    secrets.local.sh at startup, so MO_*/MINI_ORK_* overrides in the store
    reached every child process — nodes, transports, AND verifiers. Native
    dispatch only fed the store to provider transports, which silently
    stranded non-credential overrides (e.g. MINI_ORK_TYPECHECK_CMD) outside
    the verifier env. Load the store here with setdefault so the whole
    process tree inherits it while real environment variables still win.
    An unreadable store degrades to a warning: `mini-ork help` must not fail
    on a mis-permissioned credential file (mirrors the doctor handler)."""
    from mini_ork.dispatch.secrets import SecretStoreError, read_secret_exports
    try:
        stored = read_secret_exports()
    except SecretStoreError as exc:
        sys.stderr.write(f"warning: secret store not loaded: {exc}\n")
        return
    for name, value in stored.items():
        os.environ.setdefault(name, value)


def _load_feature_defaults() -> None:
    """Apply the per-project feature/setting defaults from features.yaml.

    Loaded BEFORE the secret store. Both writers use ``setdefault``, so the
    first caller wins; an IDE toggle (features.yaml) must therefore beat a stale
    hand-edit in secrets.local.sh, while a real environment variable still beats
    both. Precedence: env > features.yaml > secrets.local.sh > engine default.
    A malformed file degrades to a warning — ``mini-ork help`` must not fail."""
    from mini_ork.features.store import effective_env
    try:
        overrides = effective_env()
    except Exception as exc:  # noqa: BLE001 — a bad file must never break the CLI
        sys.stderr.write(f"warning: feature defaults not loaded: {exc}\n")
        return
    for name, value in overrides.items():
        os.environ.setdefault(name, value)


def main(argv=None, *, root=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.path.dirname(
        os.path.dirname(os.path.realpath(__file__)))
    os.environ["MINI_ORK_ROOT"] = root
    _load_feature_defaults()
    _load_secret_store_env()
    # NOTE: main() deliberately does NOT publish a derived MINI_ORK_DB. A
    # process-wide default here is indistinguishable from an operator-set one,
    # so it would (a) shadow ``--home`` for subcommands that re-point the home
    # for DB resolution (lessons forget/restore, serve) and (b) leak into
    # web/control.py:launch_run children — those copy os.environ and re-pin
    # MINI_ORK_HOME but not MINI_ORK_DB, so a run for project X would read and
    # write another project's state.db. The default lives instead in the
    # resolution ladder (``context_assembler._db_path``) and at the run boundary
    # (``execute.main``), neither of which re-points the home it derived from.
    sub = argv[0] if argv else "help"
    rest = argv[1:]

    # Subcommand registry (OCP): adding a subcommand is register_subcommand()
    # — no edit to this dispatcher. Invocation mechanism (python -m subprocess,
    # in-process import, bash entrypoint) is a property of the handler.
    handler = SUBCOMMAND_REGISTRY.get(sub)
    if handler is None:
        sys.stderr.write(f"Unknown subcommand: {sub}. Try: mini-ork help\n")
        return 2
    return handler(rest, root)


# ── Subcommand registry (SOLID M7, OCP) ─────────────────────────────────────
# Handler signature: (rest: list[str], root: str) -> int (exit code).


def _native_module_handler(module: str):
    def _handler(rest, root):
        return subprocess.run(
            [sys.executable, "-m", module, *rest],
            env=_module_env(root),
        ).returncode
    return _handler


def _execute_inprocess(rest, root):
    from mini_ork.cli import execute as mini_ork_execute
    return mini_ork_execute.main(rest, root=root)


def _run_handler(rest, root):
    # ``--resume <run_id>`` / ``--checkpoint-id <id>`` (P1.3). The preflight
    # prints the one-line plan and HARD-ERRORS on an unknown run_id (C1 / F4 —
    # no silent fresh-start fallback). A resume must NOT enter the lifecycle:
    # that would classify and plan again and dispatch every node, so the
    # printed skip list would be a lie. It re-dispatches only the plan's
    # redispatch set against the run's existing plan.
    from mini_ork.cli import run_resume

    rc, sink = run_resume.run_with_resume(rest)
    if rc != run_resume.RC_OK:
        return rc
    if sink.get("resume_id"):
        return run_resume.dispatch_resume(sink, root=root)
    return _run_lifecycle(rest, root)


def _doctor_handler(rest, root):
    del rest
    import shutil
    # The native path contract defaults project home to cwd/.mini-ork.
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    print("=== mini-ork doctor ===")
    for d in ("sqlite3", "git", "curl", "claude", "codex", "python3"):
        print(f"  [OK]      {d}" if shutil.which(d) else f"  [MISSING] {d}")
    if os.environ.get("MINI_ORK_HOME"):
        print(f"  [OK]      MINI_ORK_HOME={os.environ['MINI_ORK_HOME']}")
    elif home:
        print(f"  [OK]      MINI_ORK_HOME={home}")
    if os.environ.get("MINI_ORK_DB"):
        print(f"  [OK]      MINI_ORK_DB={os.environ['MINI_ORK_DB']}")
    else:
        print("  [WARN]    MINI_ORK_DB unset (default: $MINI_ORK_HOME/state.db)")
    # Which engine is running, and which one last wrote this home. Two engines
    # can serve one project (the IDE's serve, the copy vendored in the project's
    # .mini-ork) and they drift; the commit is what separates two checkouts of
    # the same release, the recorded DB version is what separates a home from a
    # newer engine that never migrated it.
    print("")
    print("Engine:")
    engine_root = os.environ.get("MINI_ORK_ROOT") or root
    engine_ver = engine_version(engine_root)
    engine_commit = _engine_commit(engine_root)
    print(f"  [OK]      root: {engine_root} "
          f"(v{engine_ver or '?'}{f' @{engine_commit}' if engine_commit else ''})")
    recorded = _db_engine_version(home)
    if not recorded:
        print("  [WARN]    home DB records no engine version "
              "(never migrated by a versioned engine, or no schema_migrations row)")
    elif not engine_ver:
        print(f"  [WARN]    home DB last migrated by v{recorded}; this engine's "
              "version could not be read")
    elif recorded == engine_ver:
        print(f"  [OK]      home DB last migrated by v{recorded} (this engine)")
    else:
        print(f"  [WARN]    home DB last migrated by v{recorded}, this engine is "
              f"v{engine_ver} — a newer engine has written this home; run "
              "`mini-ork migrate` from the engine you mean to use")
    print("")
    print("Provider preflight:")
    for tool, name in (("claude", "anthropic"), ("codex", "codex")):
        if shutil.which(tool):
            print(f"  [OK]      {name} ({tool} CLI present)")
        else:
            print(f"  [WARN]    {name} ({tool} CLI missing)")
    from mini_ork.dispatch.providers import provider_environment
    from mini_ork.dispatch.secrets import SecretStoreError

    try:
        provider_env = provider_environment()
    except SecretStoreError as exc:
        print(f"  [WARN]    local credential store unavailable: {exc}")
        provider_env = dict(os.environ)
    for env_var, family in (("GLM_API_KEY", "glm"), ("KIMI_API_KEY", "kimi"),
                            ("MINIMAX_API_KEY", "minimax"), ("DEEPSEEK_API_KEY", "deepseek")):
        if provider_env.get(env_var):
            print(f"  [OK]      {family} (${env_var} set)")
        else:
            print(f"  [WARN]    {family} (${env_var} unset; run: mini-ork providers configure {family})")

    print("")
    print("Secret vault:")
    from mini_ork.dispatch.vault import vault_environment

    try:
        environment = vault_environment()
    except VaultError as exc:
        print(f"  [WARN]    vault config: {exc}")
    else:
        for backend_status in environment.status():
            tag = "[OK]      " if backend_status.ready else "[WARN]    "
            print(f"  {tag}{backend_status.backend}: {backend_status.detail}")
        gap = environment.protection_gap()
        if gap:
            print(f"  [WARN]    {gap}")
    return 0


def _version_handler(rest, root):
    del rest, root
    print("mini-ork 0.9.0 (universal task loop runtime)")
    return 0


def _help_handler(rest, root):
    del rest, root
    sys.stdout.write(_HELP)
    return 0


def _providers_handler(rest, root):
    from mini_ork.cli import providers
    return providers.main(rest, root=root)


def _mcp_handler(rest, root):
    from mini_ork.cli import mcp_cmd
    return mcp_cmd.main(rest, root=root)


def _vault_handler(rest, root):
    from mini_ork.cli import vault_cmd
    return vault_cmd.main(rest, root=root)


def _install_handler(rest, root):
    from mini_ork.cli import install_command
    return install_command.main(rest, root=root)


def _board_handler(rest, root):
    from mini_ork.cli import board_cmd
    return board_cmd.main(rest, root=root)


def _build_default_registry() -> dict:
    registry = {sub: _native_module_handler(f"mini_ork.cli.{sub}") for sub in _NATIVE_SUBS}
    # "recipe-eval" is native too, but the dash is not importable in a module
    # name — register it explicitly against mini_ork.cli.recipe_eval.
    registry["recipe-eval"] = _native_module_handler("mini_ork.cli.recipe_eval")
    # "sandbox-gc" — same dash-not-importable case; sweeps leaked sandboxes.
    registry["sandbox-gc"] = _native_module_handler("mini_ork.cli.sandbox_gc")
    registry["execute"] = _execute_inprocess
    for sub, module in _NATIVE_MODULE_SUBS.items():
        registry[sub] = _native_module_handler(module)
    # `board` is the IDE's hot path: the shell polls it every 1.5 s and every
    # panel, tab and action re-reads it, so a second interpreter per call is the
    # dominant cost of the whole IDE (measured: 0.04 s of work, ~0.25 s through
    # `python -m`). Registered in-process on top of the native table entry,
    # which stays so `_NATIVE_MODULE_SUBS` keeps mirroring runtime-select.sh
    # (tests/unit/test_native_dispatch_py.py asserts the mapping). `board retry`
    # is the one verb that spawns a child (`mini-ork recover`); it sets
    # MINI_ORK_ROOT and cwd=root itself, so it does not need the child-env
    # protection `_module_env` adds.
    registry["board"] = _board_handler
    registry["run"] = _run_handler
    registry["doctor"] = _doctor_handler
    registry["install"] = _install_handler
    registry["providers"] = _providers_handler
    registry["mcp"] = _mcp_handler
    registry["vault"] = _vault_handler
    registry["version"] = _version_handler
    for alias in ("help", "--help", "-h"):
        registry[alias] = _help_handler
    return registry


SUBCOMMAND_REGISTRY: dict = _build_default_registry()


def register_subcommand(name: str, handler) -> None:
    """Register (or replace) a subcommand: handler(rest, root) -> exit code."""
    SUBCOMMAND_REGISTRY[name] = handler


if __name__ == "__main__":
    raise SystemExit(main())
