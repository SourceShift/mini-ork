"""Tell a failed run's owner what to fix, and gate a duplicate relaunch until
they say "fixed".

Companion to :mod:`mini_ork.recovery.retry_hint`. The hint answers *can we
retry?* and *what must change?*; this module answers *who needs to hear
that, and how do we keep paying for cycles that cannot succeed until they
fix it?*

Three responsibilities, in this order:

  1. ``owner(home, run_id)`` — return the run's owner record
     (``{"kind": "...", "id": "...", "label": "..."}``).
  2. ``fix_steps(hint)`` — turn the hint's ``needs_change`` into numbered,
     deterministic fix steps. No LLM, no network — strings only.
  3. ``notify(home, run_id)`` — best-effort side-channel that writes
     ``<run_dir>/owner.json``, ``<run_dir>/NEEDS-CHANGE.md``, and
     ``<run_dir>/retry-gate.json``, plus enqueues ONE
     ``mo_inbox_gates`` row whose ``gate_id='retry_precondition'`` and
     ``blocks_dispatch_for=<dispatch_key(kickoff)>`` (the kickoff's path AND
     its task line, see :func:`dispatch_key`). The dedupe query is
     run by :func:`notify` (oversight_inbox has no UNIQUE); repeat
     ``notify`` calls on the same run return the existing inbox_id.

The module never raises for a missing input — a missing run dir, a missing
hint, a missing DB all return ``None`` so the call site (the ``main.py``
post-verify hook) can fire-and-forget without breaking the run's exit
code. ``notify`` follows the same fail-soft discipline as
``retry_hint.compute``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["owner", "fix_steps", "lane_repair_message", "notify"]

GATE_ID = "retry_precondition"  # written into mo_inbox_gates.gate_id
PHASE = "retry"  # written into mo_inbox_gates.phase
OWNER_FILENAME = "owner.json"
NOTIFY_FILENAME = "NEEDS-CHANGE.md"
GATE_POINTER_FILENAME = "retry-gate.json"

# Env vars matching the import in 1 (the precedence rule: env wins when both
# the home has owner.json and MO_RUN_OWNER is set).
MO_RUN_OWNER = "MO_RUN_OWNER"
MO_IGNORE_PENDING_FIX = "MO_IGNORE_PENDING_FIX"

# Banner printed to stdout. Exact wording is part of the loop-parsing
# contract — the key=value lines below the banner are how loops pick up
# ``needs_change``, ``retry_hint``, and ``retry_inbox_id``.
NOTIFY_BANNER = "── needs a change before this run can continue ──"

# Verbatim owner kinds — the four branches owner() resolves.
_KIND_RUN = "run"
_KIND_LOOP = "loop"
_KIND_AUTOMATION = "automation"
_KIND_USER = "user"

# Env-var regex for fix_steps(environment): SCREAMING_CASE_NAMES within
# 40 chars of a context word ("not set", "unset", "export", "missing").
# 3-char minimum to avoid matching ``PID``, ``RC``, ``OS``.
_ENV_VAR_RE = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")
_ENV_CONTEXT_RE = re.compile(
    r"(not set|unset|export|missing|precondition)", re.IGNORECASE,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _run_dir(home: Path, run_id: str) -> Path:
    return Path(home) / "runs" / run_id


def _realpath(p: str | Path) -> str:
    try:
        return str(Path(p).resolve())
    except OSError:
        return str(p)


def _git_email() -> str:
    try:
        out = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _read_owner_json(run_dir: Path) -> dict[str, Any] | None:
    p = run_dir / OWNER_FILENAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("kind") or not data.get("id"):
        return None
    return data


def _read_run_profile(run_dir: Path) -> dict[str, Any]:
    p = run_dir / "run_profile.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _kickoff_path_under_home(run_dir: Path) -> tuple[Path | None, str, str]:
    """Return ``(base, segment, name)`` so the kickoff path branches can test
    ``<home>/rsi/<name>/`` and ``<home>/automations/<id>/``.

    The home is ``run_dir.parent.parent`` (``runs/<id>`` → home). Returns
    ``(None, "", "")`` when the home cannot be resolved. ``segment`` is
    the first path component under home (``"rsi"`` or ``"automations"``);
    it tells :func:`owner` which ``kind`` to attribute without re-reading
    the filesystem. Names without a recognised first segment yield
    ``(None, "", "")`` so the inference falls through to the registry walk.
    """
    profile = _read_run_profile(run_dir)
    kp = str(profile.get("kickoff_path") or "")
    if not kp:
        return None, "", ""
    p = Path(kp)
    home = run_dir.parent.parent
    try:
        rel = p.resolve().relative_to(home.resolve())
    except ValueError:
        return None, "", ""
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "rsi":
        return home, "rsi", parts[1]
    if len(parts) >= 2 and parts[0] == "automations":
        return home, "automations", parts[1]
    return None, "", ""


def _read_run_events_parent(home: Path, run_id: str) -> str | None:
    """``MINI_ORK_PARENT_RUN_ID``-style parent from ``run_events.parent_run_id``.

    The column lives on migration ``0016_recursive_orchestration``. Fail-soft:
    a missing schema, a missing row, a missing DB all return ``None``.
    """
    try:
        from mini_ork.web.db import db_for
        db = db_for(home)
    except Exception:  # noqa: BLE001
        return None
    try:
        if not db.has_table("run_events"):
            return None
        rows = db.rows(
            "SELECT parent_run_id FROM run_events WHERE run_id = ? "
            "AND parent_run_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        )
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    val = rows[0].get("parent_run_id")
    return str(val) if val else None


def _automation_record_for_run(home: Path, run_id: str) -> str | None:
    """Walk ``<home>/automations.json`` for a record naming ``run_id``.

    The registry is the JSON file (not a directory) — ``mini_ork.automations:3``.
    """
    path = Path(home) / "automations.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    rows = data.get("automations") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("last_run_id") or "") == run_id:
            return str(row.get("id") or row.get("name") or "")
    return None


def _lane_from_attempt_row(run_dir: Path) -> str:
    """Best-effort ``lane`` = the latest failed ``llm_calls.model_id`` for the
    run. Returns ``""`` when the schema is unavailable or no failed row exists.
    Lets :func:`_step_credentials` name the lane so the operator knows which
    provider key to rotate.

    ``node_attempts`` has no ``lane`` column (migration 0050 lists only
    ``node_type, started_at, ended_at, result, failure_class, …``), so the old
    ``SELECT lane FROM node_attempts`` always returned ``""``. ``llm_calls`` is
    the writer that records the resolved lane — ``model_id`` — so that is the
    real source.
    """
    if not run_dir.is_dir():
        return ""
    run_id = run_dir.name
    try:
        from mini_ork.web.db import db_for
    except Exception:  # noqa: BLE001
        return ""
    home = run_dir.parent.parent
    try:
        db = db_for(home)
    except Exception:  # noqa: BLE001
        return ""
    try:
        if not db.has_table("llm_calls"):
            return ""
        rows = db.rows(
            "SELECT model_id FROM llm_calls WHERE run_id = ? AND status = 'failed' "
            "ORDER BY id DESC LIMIT 1",
            (run_id,),
        )
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return ""
    return str(rows[0].get("model_id") or "")


# ── 1. owner ────────────────────────────────────────────────────────────────


def owner(home: Path, run_id: str) -> dict[str, Any] | None:
    """The run's owner record — ``{"kind", "id", "label"}`` — or ``None``.

    Resolution order (first non-empty wins) per kickoff section 1:
    ``owner.json`` is the persisted source of truth (written at run start
    only from ``MO_RUN_OWNER``, else by :func:`notify` from the inference),
    so it outranks the live env var. ``MO_RUN_OWNER`` is a runtime override
    that flows into the next ``owner.json`` write but does NOT supersede a
    previously-persisted record on read.

      1. ``<run_dir>/owner.json`` — written by ``main.py`` at run start.
      2. ``MO_RUN_OWNER`` env var in the form ``kind:id`` — set by the
         launcher when a higher layer already knows who owns the run.
      3. ``run_events.parent_run_id`` ⇒ ``{"kind": "run", "id": <parent>}``.
      4. Kickoff path under ``<home>/rsi/<name>/`` ⇒ ``loop:<name>``;
         under ``<home>/automations/<id>/`` ⇒ ``automation:<id>``.
      5. ``<home>/automations.json`` row whose ``last_run_id == run_id``
         ⇒ ``automation:<id>``.
      6. Fallback: ``{"kind": "user", "id": <email|$USER>, "label": "..."}``.

    The label is the same string the user-facing banner prints, prefixed
    by the kind for readability.
    """
    home = Path(home)
    run_dir = _run_dir(home, run_id)

    # 1. Persisted owner.json wins. ``main.py`` writes it at run start from
    # MO_RUN_OWNER (when set) or the inference. A later poll that races
    # the run-start write still falls back to step 2 below.
    persisted = _read_owner_json(run_dir)
    if persisted is not None:
        return persisted

    # 2. Runtime env override — fires when owner.json is missing or the
    # poll is racing the run-start write.
    env_owner = os.environ.get(MO_RUN_OWNER) or ""
    if env_owner and ":" in env_owner:
        kind, ident = env_owner.split(":", 1)
        if kind.strip() and ident.strip():
            return {
                "kind": kind.strip(),
                "id": ident.strip(),
                "label": f"{kind.strip()}:{ident.strip()}",
            }

    parent = _read_run_events_parent(home, run_id)
    if parent:
        return {"kind": _KIND_RUN, "id": parent, "label": f"run:{parent}"}

    base, segment, name = _kickoff_path_under_home(run_dir)
    if base is not None and name:
        if segment == "rsi":
            return {"kind": _KIND_LOOP, "id": name, "label": f"loop:{name}"}
        if segment == "automations":
            # under automations/<id>/ — handled below by the registry walk,
            # but the kickoff-path branch also lets us name a loop-less
            # automation kickoff without needing the registry to be written.
            return {"kind": _KIND_AUTOMATION, "id": name, "label": f"automation:{name}"}

    auto_id = _automation_record_for_run(home, run_id)
    if auto_id:
        return {"kind": _KIND_AUTOMATION, "id": auto_id, "label": f"automation:{auto_id}"}

    email = _git_email()
    return {"kind": _KIND_USER, "id": email, "label": f"user:{email}"}


# ── 2. fix_steps ────────────────────────────────────────────────────────────


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _env_var_names(text: str) -> list[str]:
    """Env var names within 40 chars of a context word ("not set", "export",
    …), in order. Whole-token matches over the full text, so a name next to
    the window edge is never cut short."""
    if not text:
        return []
    contexts = [(m.start(), m.end()) for m in _ENV_CONTEXT_RE.finditer(text)]
    if not contexts:
        return []
    out: list[str] = []
    for vmatch in _ENV_VAR_RE.finditer(text):
        start, end = vmatch.span()
        near = any(start <= c_end + 40 and end >= c_start - 40 for c_start, c_end in contexts)
        name = vmatch.group(1)
        if near and name not in out:
            out.append(name)
    return out


def _verifier_need_surfaces(run_dir: Path) -> list[str]:
    """Read every ``verifier_*.json`` for a top-level ``needs`` list.

    The verifier JSON is not pinned — newer payloads store surfaces
    under ``spec.needs`` (the kickoff calls this out explicitly), while
    older payloads put ``needs`` at the top level. We read BOTH keys so
    a run whose verifier JSON migrated between shapes still surfaces
    its needs list. Returns a flat list of strings so the caller can
    number them.
    """
    if not run_dir.is_dir():
        return []
    out: list[str] = []
    for path in sorted(run_dir.glob("verifier_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        # Spec-pinned surface list (``spec.needs``); fall back to the
        # top-level ``needs`` for older verifier JSON shapes.
        spec = data.get("spec")
        spec_needs = spec.get("needs") if isinstance(spec, dict) else None
        raw = spec_needs if isinstance(spec_needs, list) else data.get("needs")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    out.append(item.strip())
                elif isinstance(item, dict):
                    label = item.get("label") or item.get("path") or item.get("env")
                    if isinstance(label, str) and label.strip():
                        out.append(label.strip())
    # Dedup, preserve order.
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        deduped.append(s)
    return deduped


def _step_environment(detail: str, notes: list[str], run_dir: Path,
                      run_id: str = "") -> list[str]:
    text = "\n".join([detail or "", *(notes or [])])
    names = _env_var_names(text)
    surfaces = _verifier_need_surfaces(run_dir)
    steps: list[str] = []
    for name in names:
        steps.append(f"Set {name} in the backend's environment.")
    for surf in surfaces:
        if surf not in names:
            steps.append(f"Ensure {surf} is reachable from the backend.")
    if names:
        steps.append("Restart the backend so it picks the variable up.")
    if not steps:
        first = (detail or "").splitlines()[0] if detail else "(no detail)"
        steps.append(f"Read the evidence — it says: {_truncate(first, 200)}")
    confirm_run = run_id or "<run>"
    steps.append(f"Confirm: mini-ork board retry {confirm_run} --ack-change "
                 "(or approve the inbox item).")
    return steps


def _step_credentials(detail: str, run_dir: Path, run_id: str,
                      hint: dict[str, Any] | None = None) -> list[str]:
    line = detail or "the API credential set"
    # The kickoff says credentials steps must NAME the lane/provider from the
    # failed node's attempt row AND mention config/secrets.local.sh. The
    # ``node_attempts`` schema (0050_node_dag_checkpoints.sql) does not
    # currently carry ``lane`` / ``provider`` columns — the live lookup
    # therefore degrades to the hint's ``failed_node`` (which retry_hint
    # already populates from the same row) plus the secrets file. The
    # lane lookup is preserved as a forward-compatible seam so a future
    # migration adding those columns lights up automatically.
    lane = _lane_from_attempt_row(run_dir)
    where = "config/secrets.local.sh"
    if lane:
        where = f"the {lane} lane ({where})"
    failed_name = None
    if isinstance(hint, dict):
        cand = hint.get("failed_node")
        if isinstance(cand, str) and cand.strip():
            failed_name = cand.strip()
    if failed_name:
        where = f"{where} (the failed node was {failed_name})"
    confirm_run = run_id or "<run>"
    return [
        f"Update the credential in {where}: {_truncate(line, 200)}.",
        f"Confirm: mini-ork board retry {confirm_run} --ack-change "
        "(or approve the inbox item).",
    ]


def _step_budget(run_id: str = "") -> list[str]:
    confirm_run = run_id or "<run>"
    return [
        f"Raise the cost cap or approve the spend: mini-ork resume {confirm_run}.",
    ]


def _step_code(detail: str, notes: list[str]) -> list[str]:
    """One ``  - <reason>`` sub-bullet per reviewer reason.

    ``detail`` arrives from :func:`retry_hint._extract_review_detail` as up
    to three reasons joined one-per-line (and the code-kind hint sets
    ``notes`` to ``[]``), so the reasons live INSIDE a single multiline
    string. Packing that into one bullet left every reason after the first
    flush-left in NEEDS-CHANGE.md — the flattening this widget exists to
    avoid. Split on newlines so each reason is its own continuation line
    under the single "Start a revision run" step; fall back to ``notes``
    (also per-reason) only when ``detail`` carries no reasons.
    """
    def _reasons(raw: str) -> list[str]:
        return [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]

    bullets = _reasons(detail)
    if not bullets:
        for n in notes or []:
            bullets.extend(_reasons(str(n or "")))
    if not bullets:
        bullets = ["the reviewer asked for a revision"]
    return [
        "Start a revision run with these reasons:",
        *[f"  - {b}" for b in bullets],
    ]


def _last_log_lines(run_dir: Path, n: int = 5) -> list[str]:
    """The last ``n`` lines from the most recent ``impl-*.log`` or the run's
    ``run.stdout`` (whichever exists). Empty list when no log is found so
    the caller can degrade to the detail line.
    """
    if not run_dir.is_dir():
        return []
    candidates = sorted(
        run_dir.glob("impl-*.log"), key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        stdout = run_dir / "run.stdout"
        if stdout.is_file():
            candidates = [stdout]
    if not candidates:
        return []
    try:
        text = candidates[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


def _step_unknown(run_dir: Path) -> list[str]:
    last_lines = _last_log_lines(run_dir, n=5)
    if last_lines:
        return [
            "Read the evidence — last 5 log lines:",
            *[f"  | {ln}" for ln in last_lines],
        ]
    return [
        "Read the evidence — no impl log was captured. Re-run with "
        "MO_VERBOSE=1 to record it.",
    ]


def _run_dir_has_impl_artifact(run_dir: Path) -> bool:
    """True when the run dir carries ``framework-edit.diff`` or an implementer
    artifact (an ``impl-*.log`` / ``agent-implementer.*``) — i.e. the
    implementer ran and the tree was touched."""
    if not run_dir.is_dir():
        return False
    if (run_dir / "framework-edit.diff").is_file():
        return True
    if list(run_dir.glob("impl-*.log")):
        return True
    return bool(list(run_dir.glob("agent-implementer.*")))


def _step_lane(nc: dict[str, Any], hint: dict[str, Any], run_dir: Path) -> list[str]:
    """Kind ``lane`` — deterministic strings: the failed lane, the switch
    command (or the "no healthy lane" fallback), and the "nothing changed"
    line when the run stopped before the implementer."""
    lane = str(nc.get("lane") or "?")
    alias = str(nc.get("alias") or "?")
    provider = str(nc.get("provider") or "the provider")
    detail = str(nc.get("detail") or "")
    raw_nodes = nc.get("nodes")
    nodes = [str(n) for n in raw_nodes] if isinstance(raw_nodes, list) else []
    raw_sug = nc.get("suggestions")
    suggestions = [dict(s) for s in raw_sug] if isinstance(raw_sug, list) else []
    from_node = str(hint.get("from_node") or "")
    command = str(hint.get("command") or "")
    run_id = str(hint.get("run_id") or "<run>")
    code = bool(nc.get("code"))

    steps = [
        f"{provider} lane '{lane}' (used by {alias}: {', '.join(nodes)}) "
        f"failed: {detail}",
    ]
    if suggestions:
        s0 = suggestions[0]
        steps.append(
            f"Switch {alias} to '{s0.get('lane')}' ({s0.get('reason')}) and "
            f"resume from {from_node}: {command}",
        )
        for s in suggestions[1:]:
            steps.append(f"Or '{s.get('lane')}' ({s.get('reason')})")
    else:
        code_word = "code " if code else ""
        steps.append(
            f"No other {code_word}lane looks healthy — top up {provider} credits "
            f"or add a lane to providers.yaml, then: mini-ork recover {run_id}",
        )
    if not _run_dir_has_impl_artifact(run_dir):
        steps.append("Nothing was changed in your code; the run stopped before the implementer.")
    return steps


def fix_steps(hint: dict[str, Any] | None, *, home: Path | None = None) -> list[str]:
    """The hint's ``needs_change`` rendered as numbered fix steps.

    Returns a list of strings — each a single instruction the operator
    can run. No LLM, no I/O. ``[]`` when ``hint`` is missing or has no
    ``needs_change``. The optional ``home`` kwarg lets the caller (or the
    read-only proof from the kickoff) pass the mini-ork home so the
    verifier-needs surfaces branch can read ``<run_dir>/verifier_*.json``
    instead of guessing from ``MINI_ORK_HOME``.
    """
    if not isinstance(hint, dict):
        return []
    nc = hint.get("needs_change")
    if not isinstance(nc, dict):
        return []
    kind = str(nc.get("kind") or "unknown")
    detail = str(nc.get("detail") or "")
    summary = str(nc.get("summary") or "")
    evidence = str(nc.get("evidence") or "")
    raw_notes = hint.get("notes")
    notes: list[Any] = list(raw_notes) if isinstance(raw_notes, list) else []

    # ``run_dir`` is needed only for the verifier-needs surfaces branch.
    # Honour an explicit ``home`` kwarg first; fall back to the env so the
    # CLI read-only proof (which sets ``MINI_ORK_HOME``) Just Works.
    run_id = str(hint.get("run_id") or "")
    if home is not None:
        run_dir = _run_dir(Path(home), run_id) if run_id else Path(".")
    else:
        env_home = os.environ.get("MINI_ORK_HOME") or ""
        run_dir = _run_dir(Path(env_home), run_id) if (env_home and run_id) else Path(".")

    if kind == "environment":
        steps = _step_environment(detail, [str(n) for n in notes], run_dir, run_id)
    elif kind == "credentials":
        steps = _step_credentials(detail or summary, run_dir, run_id, hint)
    elif kind == "budget":
        steps = _step_budget(run_id)
    elif kind == "code":
        steps = _step_code(detail, [str(n) for n in notes])
    elif kind == "lane":
        steps = _step_lane(nc, hint, run_dir)
    else:  # unknown and any future kind
        steps = _step_unknown(run_dir)

    # Always append the evidence pointer when present so the operator knows
    # where to read.
    if evidence and not any(evidence in s for s in steps):
        steps.append(f"Evidence: {evidence}")
    return steps


# ── 3. notify ──────────────────────────────────────────────────────────────


def _step_line(step: str) -> tuple[str, str]:
    """Split ``step`` into ``(kind, text)`` — ``"item"`` for a numbered step,
    ``"detail"`` for an indented continuation of the step before it.

    Producers spell a continuation as a string that starts with whitespace:
    ``_step_code`` emits ``"  - <reason>"`` under "Start a revision run …",
    ``_step_unknown`` emits ``"  | <log line>"`` under "Read the evidence …".
    Numbering those would turn one instruction into N instructions, so the
    renderer must see them as detail, not as steps.
    """
    text = step.rstrip()
    if text[:1].isspace():
        return "detail", text.lstrip()
    return "item", text


def _write_needs_change_md(run_dir: Path, owner_rec: dict[str, Any],
                           hint: dict[str, Any], steps: list[str]) -> Path:
    raw_nc = hint.get("needs_change")
    nc: dict[str, Any] = raw_nc if isinstance(raw_nc, dict) else {}
    summary = str(nc.get("summary") or "")
    detail = str(nc.get("detail") or "")
    evidence = str(nc.get("evidence") or "")
    numbered: list[str] = []
    n = 0
    for step in steps:
        kind, text = _step_line(step)
        if kind == "detail" and numbered:
            # Three spaces: an indented block under the previous list item,
            # so the reason/log line reads as part of its step, not a new one.
            numbered.append(f"   {text}")
            continue
        n += 1
        numbered.append(f"{n}. {text}")
    out = [
        f"# {summary or 'A change is needed before this run can continue'}",
        "",
        f"owner: {owner_rec.get('label') or ''}",
        f"needs_change: {nc.get('kind') or ''}",
        "",
        "## What to do",
        "",
        *numbered,
        "",
    ]
    if detail:
        out.extend(["## Detail", "", detail, ""])
    if evidence:
        out.extend(["## Evidence", "", evidence, ""])
    path = run_dir / NOTIFY_FILENAME
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def record_owner_at_start(home: Path, run_id: str) -> dict[str, Any] | None:
    """Persist ``owner.json`` at run start, only when ``MO_RUN_OWNER`` names one.

    Without it the owner is inferred later, by :func:`notify`, once the run
    profile, kickoff path and run events exist; writing an inference at start
    would freeze the ``user:`` fallback before those signals are there.
    """
    env_owner = os.environ.get(MO_RUN_OWNER) or ""
    if ":" not in env_owner:
        return None
    rec = owner(home, run_id)
    if isinstance(rec, dict):
        run_dir = _run_dir(Path(home), run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_owner_json(run_dir, rec)
    return rec


def _write_owner_json(run_dir: Path, owner_rec: dict[str, Any]) -> None:
    (run_dir / OWNER_FILENAME).write_text(
        json.dumps(owner_rec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _enqueue_retry_gate(home: Path, run_id: str, hint: dict[str, Any],
                        steps: list[str], owner_rec: dict[str, Any],
                        blocks_for: str) -> tuple[int | None, list[dict]]:
    """Idempotent enqueue — returns ``(inbox_id, pending_rows)``.

    The dedupe query reads ``pending()`` first; the table has no UNIQUE
    on ``(feature, gate_id, status='pending')``, so the dedupe must live
    in :func:`notify`. Returns ``(None, [])`` when the enqueue module is
    missing or the DB is missing.
    """
    try:
        from mini_ork.gates import oversight_inbox
    except Exception:  # noqa: BLE001
        return None, []
    db_path = str(Path(home) / "state.db")
    if not Path(db_path).is_file():
        return None, []
    context = {
        "hint": hint,
        "steps": steps,
        "owner": owner_rec,
        "run_id": run_id,
    }
    try:
        existing = oversight_inbox.pending(db_path=db_path)
    except Exception:  # noqa: BLE001
        existing = []
    for row in existing:
        if (row.get("gate_id") == GATE_ID
                and str(row.get("feature") or "") == run_id):
            iid = row.get("inbox_id")
            return (int(iid) if iid else None), existing
    try:
        iid = oversight_inbox.enqueue(
            gate_id=GATE_ID,
            feature=run_id,
            phase=PHASE,
            context=context,
            blocks_dispatch_for=blocks_for,
            db_path=db_path,
        )
    except Exception:  # noqa: BLE001
        return None, existing
    return (int(iid) if iid else None), existing


def _write_gate_pointer(run_dir: Path, inbox_id: int, blocks_for: str) -> None:
    body = {
        "inbox_id": inbox_id,
        "blocks_dispatch_for": blocks_for,
    }
    (run_dir / GATE_POINTER_FILENAME).write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ── lane-repair delivery (offline thread append) ──────────────────────────
#
# A run whose lane is unavailable is not thrown away: the Zed thread that
# started it is told, and offered a resume on a working lane. The live path is
# ``MiniOrkAcpAgent._offer_lane_repair`` (an ACP message / permission card);
# the offline path lives in ``notify`` below — it appends ONE record to the
# thread's JSONL so the user reads it on the next ``session/load`` even when
# the thread was not open when the run failed. ``lane_repair_message`` is the
# one builder both paths share, so the text a user reads live is byte-identical
# to the text replayed from disk.

# The env kind a Zed thread writes into ``MO_RUN_OWNER`` (``thread:<id>``).
_THREAD_OWNER_KIND = "thread"


def lane_repair_message(hint: dict[str, Any]) -> str:
    """The "this run needs a lane repair" message the user sees.

    Reads the lane ``needs_change`` payload (``provider``, ``lane``, ``alias``,
    ``nodes``, ``detail``, ``suggestions``) plus the hint's top-level
    ``run_id`` / ``failed_node``. Pure string work — no I/O, never raises.
    """
    hint = hint if isinstance(hint, dict) else {}
    nc_raw = hint.get("needs_change")
    nc: dict[str, Any] = nc_raw if isinstance(nc_raw, dict) else {}
    run_id = str(hint.get("run_id") or "")
    failed_node = str(hint.get("failed_node") or "")
    provider = str(nc.get("provider") or "the")
    lane = str(nc.get("lane") or "")
    alias = str(nc.get("alias") or "")
    nodes = nc.get("nodes") if isinstance(nc.get("nodes"), list) else []
    nodes_txt = ", ".join(str(n) for n in nodes)
    detail = _truncate(str(nc.get("detail") or ""), 200)
    head = f"**{run_id} needs repair.** {provider} lane `{lane}`"
    if nodes_txt:
        head += f" (used by {alias}: {nodes_txt})"
    head += " failed:"
    lines = [head]
    if detail:
        lines.append(detail)
    lines.append(f"Resume from `{failed_node}` on a working lane:")
    suggestions = [s for s in (nc.get("suggestions") or []) if isinstance(s, dict)][:2]
    for s in suggestions:
        line = f"/recover {run_id} --lane {alias}={str(s.get('lane') or '')}"
        reason = str(s.get("reason") or "")
        if reason:
            line += f"   ({reason})"
        lines.append(line)
    if not suggestions:
        lines.append(f"/recover {run_id}")
    return "\n".join(lines)


def _append_lane_repair(
    home: Path, thread_id: str, run_id: str, hint: dict[str, Any]
) -> bool:
    """Append ONE lane-repair message to ``thread_id``'s JSONL (best-effort).

    The record matches the shape ``agent.py`` replays as an
    ``AgentMessageChunk`` (camelCase ``sessionUpdate``, ``by_alias`` JSON), and
    carries a ``lane-repair:<run_id>`` marker so a repeat call — or the
    identical message the live agent already emitted — never double-posts.
    Returns ``True`` when a record was appended. Never raises: a bad thread id
    (``ThreadStore`` raises ``ValueError`` on an unsafe id) or an unwritable
    home returns ``False``.
    """
    h = dict(hint) if isinstance(hint, dict) else {}
    h["run_id"] = h.get("run_id") or run_id
    text = lane_repair_message(h)
    marker = f"lane-repair:{run_id}"
    try:
        from mini_ork.acp.threads import ThreadStore

        store = ThreadStore(home)
        for rec in store.read(thread_id):
            if rec.get("marker") == marker:
                return False
            update = rec.get("update")
            if (rec.get("type") == "update" and isinstance(update, dict)
                    and update.get("sessionUpdate") == "agent_message_chunk"):
                content = update.get("content")
                if isinstance(content, dict) and content.get("text") == text:
                    return False  # the live agent already posted this
        store.append(thread_id, {
            "type": "update",
            "update": {
                "content": {"type": "text", "text": text},
                "sessionUpdate": "agent_message_chunk",
            },
            "marker": marker,
        })
        return True
    except Exception:  # noqa: BLE001 — delivery is best-effort, never raise
        return False


def notify(home: Path, run_id: str) -> dict[str, Any] | None:
    """Best-effort side-channel: write NEEDS-CHANGE.md, enqueue a gate,
    print a banner. Returns ``None`` when there is no ``needs_change``
    to notify about, or the operator has nothing actionable to read.

    The function never raises — every I/O path is fail-soft so a missing DB
    or run dir yields ``None`` and the call site (post-verify) keeps the
    run's exit code unchanged.
    """
    home = Path(home)
    run_dir = _run_dir(home, run_id)
    if not run_dir.is_dir():
        return None

    # Compute the owner + the hint. Hint is read via the cache-aware path
    # so a non-terminal (re-running) status returns None and we stay quiet.
    owner_rec = owner(home, run_id) or {
        "kind": _KIND_USER, "id": "unknown", "label": "user:unknown",
    }
    try:
        from mini_ork.recovery import retry_hint
        hint = retry_hint.load_or_compute(home, run_id, write=False)
    except Exception:  # noqa: BLE001
        hint = None
    if not isinstance(hint, dict):
        return None
    nc = hint.get("needs_change")
    if not isinstance(nc, dict):
        return None
    needs_kind = str(nc.get("kind") or "")
    retryable = bool(hint.get("retryable"))
    if not (needs_kind == "code" or retryable):
        return None

    # Offline delivery: when a Zed thread owns the run and it died on a lane,
    # append ONE message to that thread so the user sees it on the next
    # ``session/load`` even if the thread was not open when the run failed.
    if str(owner_rec.get("kind") or "") == _THREAD_OWNER_KIND and needs_kind == "lane":
        _append_lane_repair(home, str(owner_rec.get("id") or ""), run_id, hint)

    # Persist owner.json (so a later poll that reads owner.json before
    # MO_RUN_OWNER fires still gets the right answer).
    try:
        _write_owner_json(run_dir, owner_rec)
    except OSError:
        pass

    steps = fix_steps(hint, home=home)
    # Write NEEDS-CHANGE.md.
    try:
        _write_needs_change_md(run_dir, owner_rec, hint, steps)
    except OSError:
        pass

    # Enqueue the gate keyed on this run's task (kickoff path + task line).
    profile = _read_run_profile(run_dir)
    kickoff = str(profile.get("kickoff_path") or "")
    blocks_for = dispatch_key(kickoff) if kickoff else ""
    inbox_id, _ = _enqueue_retry_gate(home, run_id, hint, steps, owner_rec,
                                      blocks_for)
    if inbox_id is not None:
        try:
            _write_gate_pointer(run_dir, inbox_id, blocks_for)
        except OSError:
            pass

    # Banner + key=value lines (loops parse the latter).
    summary = str(nc.get("summary") or "")
    try:
        print(NOTIFY_BANNER)
        print(f"  owner: {owner_rec.get('label') or ''}")
        print(f"  needs_change: {needs_kind}")
        if summary:
            print(f"  summary: {summary}")
        step_n = 0
        for step in steps:
            kind, text = _step_line(step)
            if kind == "detail" and step_n:
                print(f"         {text}")
                continue
            step_n += 1
            print(f"  step {step_n}: {text}")
        print(f"needs_change={needs_kind}")
        print(f"retry_hint={run_dir / NOTIFY_FILENAME}")
        if inbox_id is not None:
            print(f"retry_inbox_id={inbox_id}")
    except OSError:
        pass

    return {
        "run_id": run_id,
        "owner": owner_rec,
        "hint": hint,
        "steps": steps,
        "inbox_id": inbox_id,
        "blocks_dispatch_for": blocks_for,
    }


# ── 5. start guard helpers (used by ``mini_ork.cli.main``) ────────────────


def dispatch_key(kickoff_path: str) -> str:
    """What a pending fix blocks: this kickoff path AND its task.

    Loops rewrite ONE kickoff file per cycle for different steps, so the path
    alone blocked unrelated steps (W5-112's pending fix stopped W5-98). The
    task is the kickoff's first non-empty line (its title, e.g.
    ``acq-wave5-rsi — cycle for step W5-91``): the same step re-run is blocked
    even when the rest of the kickoff (a ledger, notes) changed, a different
    step is not. A kickoff without a title line falls back to its content hash.
    """
    path = _realpath(kickoff_path)
    try:
        text = Path(kickoff_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    title = next((ln.strip().lstrip("#").strip() for ln in text.splitlines() if ln.strip()), "")
    if title:
        return f"{path}#{' '.join(title.split())}"
    import hashlib

    return f"{path}#sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def pending_fix_for_kickoff(home: Path, kickoff_path: str) -> dict[str, Any] | None:
    """A pending ``retry_precondition`` row for this kickoff's task
    (:func:`dispatch_key`), or ``None`` when the run may proceed.

    Rows written before the task was part of the key (a bare path, no ``#``)
    never block: they cannot tell one step of a loop from another.
    ``MO_IGNORE_PENDING_FIX=1`` short-circuits the check.
    """
    if os.environ.get(MO_IGNORE_PENDING_FIX) == "1":
        return None
    try:
        from mini_ork.gates import oversight_inbox
    except Exception:  # noqa: BLE001
        return None
    db_path = str(Path(home) / "state.db")
    if not Path(db_path).is_file():
        return None
    try:
        rows = oversight_inbox.pending(db_path=db_path)
    except Exception:  # noqa: BLE001
        return None
    want = dispatch_key(kickoff_path) if kickoff_path else ""
    if not want:
        return None
    for row in rows:
        if row.get("gate_id") != GATE_ID:
            continue
        if str(row.get("blocks_dispatch_for") or "") == want:
            return row
    return None


# ── 6. task_state helpers (used by ``mini_ork.acp.task_state``) ─────────────


def pending_fix_for_run(home: Path, run_dir: Path) -> dict[str, Any] | None:
    """The pending row pointed at by ``<run_dir>/retry-gate.json``.

    Returns ``None`` when the gate pointer is absent, the row is missing,
    or the row has been resolved. ``task_state()` calls this ONLY when
    ``retry-gate.json`` exists — the common case pays zero DB reads.
    """
    path = Path(run_dir) / GATE_POINTER_FILENAME
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    raw_iid = body.get("inbox_id")
    if raw_iid is None:
        return None
    if isinstance(raw_iid, int):
        iid = raw_iid
    else:
        try:
            iid = int(raw_iid)
        except (TypeError, ValueError):
            return None
    try:
        from mini_ork.gates import oversight_inbox
    except Exception:  # noqa: BLE001
        return None
    db_path = str(Path(home) / "state.db")
    if not Path(db_path).is_file():
        return None
    try:
        row = oversight_inbox.get(iid, db_path=db_path)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(row, dict):
        return None
    if str(row.get("status") or "") != "pending":
        return None
    return row


def supersede_gates_for_target(
    home: str | Path, target_dir: str | Path, note: str,
) -> list[str]:
    """Close pending retry gates whose run targeted ``target_dir``.

    Called when a task worktree is merged to main or removed: work on every
    failed run that targeted it is now settled (its fix landed or was
    dropped), so its gate must stop asking the operator forever. A gate is
    matched when the run's ``run_profile.json`` names a ``target_repo`` or
    ``roots.target`` equal (realpath) to ``target_dir``.

    For each still-pending gate: resolve the row ``rejected`` with ``note``,
    then write ``{"abandoned": true, "superseded": note}`` into the pointer
    file — the exact marker ``board gate reject`` writes. Idempotent: a
    second call finds no pending row (``resolve`` returns ``False``) and
    returns ``[]``. Never raises: a bad file is skipped, and the run ids that
    WERE closed are returned.
    """
    home = Path(home)
    want = _realpath(target_dir)
    closed: list[str] = []
    try:
        pointers = sorted(home.glob(f"runs/*/{GATE_POINTER_FILENAME}"))
    except OSError:
        return closed
    for pointer in pointers:
        try:
            run_dir = pointer.parent
            profile = _read_run_profile(run_dir)
            roots = profile.get("roots")
            target = roots.get("target") if isinstance(roots, dict) else None
            if not any(
                t and _realpath(t) == want
                for t in (profile.get("target_repo"), target)
            ):
                continue
            body = json.loads(pointer.read_text(encoding="utf-8"))
            if not isinstance(body, dict):
                continue
            raw_iid = body.get("inbox_id")
            if raw_iid is None:
                continue
            try:
                iid = int(raw_iid)
            except (TypeError, ValueError):
                continue
            from mini_ork.gates import oversight_inbox
            db_path = str(home / "state.db")
            if not Path(db_path).is_file():
                continue
            if oversight_inbox.resolve(
                iid, "rejected", review_note=note, db_path=db_path,
            ) is not True:
                continue
            body["abandoned"] = True
            body["superseded"] = note
            pointer.write_text(
                json.dumps(body, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            closed.append(run_dir.name)
        except Exception:  # noqa: BLE001
            continue
    return closed