"""Compute a retry hint for a failed run.

The hint answers two questions for the operator:

  * Can the run be retried cheaply?
  * If yes — what must change first?

The classification is intentionally fail-soft. Every disk/DB read is guarded
so a half-written run dir, a missing recipe, a missing schema, a banner line
in front of a verifier JSON, or a missing ``node_attempts`` row all return
``None`` or fall through to the next case. The module never raises for a
missing input — only for an actual bug.

The 5-way classifier (first match wins) lives in :func:`compute`. The cache
wrapper :func:`load_or_compute` writes ``<run_dir>/retry-hint.json`` only
when ``write=True`` and the cache is older than every file the hint reads,
so a fresh verifier JSON automatically invalidates the cached hint.

The hint is a contract the parallel ``recover --strategy verify`` run reads.
Keep the keys exactly as documented — see ``kickoffs/auto/retry-hint.md``.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_ork.learning.failure_classifier import (
    PROVIDER_LIMIT,
    auto_recoverable,
    recovery_policy,
)
from mini_ork.recipes_catalog import find_recipe
from mini_ork.verify.levels import read_verifier_payload
from mini_ork.web.db import db_for

HINT_VERSION = 1
CACHE_FILENAME = "retry-hint.json"

# Reviewer/eval/judge verdicts that mean "the change was wrong".
_RETRY_VERDICTS = ("needs_revision", "reject", "fail")

# Tokens that mean the provider rejected the call on credentials grounds.
# The bare "401"/"403" digits live in a separate regex pass (see ``_looks_like_http_auth``)
# because implementation logs routinely contain ``tokens_in=14012`` or ``cost=$0.21`` —
# a substring ``in`` check would false-positive on every cost line. The word tokens
# below are precise enough to be left as ``in`` matches.
_AUTH_TOKENS = ("credential", "api key", "api_key", "unauthor")

# HTTP status code digits are only auth when they sit near an HTTP/auth context word.
_HTTP_AUTH_DIGITS = re.compile(r"\b(401|403)\b")
_HTTP_AUTH_CONTEXT = re.compile(
    r"(status|http|unauthori[sz]ed|forbidden)", re.IGNORECASE,
)
# How close a digit match must be to a context word to count as auth.
_HTTP_AUTH_PROXIMITY = 30

# Tokens in a verifier reason that mean "this isn't a code problem, the
# environment can't reach what it needs".
_UNREACH_TOKENS = ("unreachable", "not set", "missing", "precondition")

# Tokens that surface a useful hint in the implementer log.
_NOTE_TOKENS = ("precondition", "must be restarted", "export", "not set", "env")

_VERDICT_RE = re.compile(r'"verdict"\s*:\s*"([^"]+)"')


def _hint_path(home: Path, run_id: str) -> Path:
    return home / "runs" / run_id / CACHE_FILENAME


# Terminal-failed run statuses. A non-terminal status (executing, queued, …) means
# the run is still in flight or has been relaunched — a cached hint from the prior
# failed attempt would mislead the operator, so ``load_or_compute`` returns ``None``.
_TERMINAL_FAILED = ("failed", "rolled_back")


def _current_run_status(home: Path, run_id: str) -> str:
    """The run's current ``task_runs.status`` — empty string when the DB is missing
    or the row is absent. Fail-soft: a missing DB or schema drift is NOT an error.
    """
    try:
        db = db_for(home)
    except Exception:  # noqa: BLE001
        return ""
    try:
        if not db.has_table("task_runs"):
            return ""
        rows = db.rows("SELECT status FROM task_runs WHERE id = ? LIMIT 1", (run_id,))
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return ""
    return str(rows[0].get("status") or "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_card(home: Path, run_id: str) -> dict[str, Any] | None:
    """The run's card dict, or ``None`` when the run is not in the fleet."""
    try:
        from mini_ork.acp import fleet
    except Exception:  # noqa: BLE001 — fleet is optional; never block the hint
        return None
    try:
        card = fleet.run_card(home, run_id)
    except Exception:  # noqa: BLE001 — one bad row must not blank the hint
        return None
    return card if isinstance(card, dict) else None


def _recipe_workflow(home: Path, recipe: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(nodes, edges)`` from ``<recipe>/workflow.yaml``. ``([], [])`` on miss."""
    if not recipe:
        return [], []
    info = find_recipe(recipe, home)
    if info is None:
        return [], []
    path = info.path / "workflow.yaml"
    try:
        import yaml
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ImportError, TypeError, ValueError):
        return [], []
    nodes = [n for n in (wf or {}).get("nodes") or [] if isinstance(n, dict) and n.get("name")]
    edges = [e for e in (wf or {}).get("edges") or [] if isinstance(e, dict)]
    return nodes, edges


def _topo_order(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[str]:
    """Node names in workflow topo order — longest ``depends_on`` chain first.

    Control-flow edges (``escalates_to`` and ``retries``) do not contribute to
    the depth — they are sibling/loop signals, not real ordering constraints.
    Skipping ``retries`` is what protects the walker from a cycle in recipes
    that carry real ``edge_type: retries`` edges (e.g. ``prompt-graph-loop``).
    A visited set on the depth walker is the belt-and-braces guard: any cycle
    that survives the edge filter (e.g. a depends_on loop) cannot recurse.
    """
    names = [str(n.get("name")) for n in nodes if n.get("name")]
    known = set(names)
    preds: dict[str, set[str]] = {n: set() for n in names}
    _CONTROL_FLOW_EDGE_TYPES = ("escalates_to", "retries")
    for e in edges:
        src, dst = str(e.get("from") or ""), str(e.get("to") or "")
        if src in known and dst in known and src != dst:
            if str(e.get("edge_type") or "") in _CONTROL_FLOW_EDGE_TYPES:
                # Control-flow edges are not ordering constraints — they are
                # either a sibling escalation or a retry loop. Either way,
                # they do not delay the target.
                continue
            preds.setdefault(dst, set()).add(src)
    for n in nodes:
        deps = n.get("depends_on")
        if isinstance(deps, list):
            preds.setdefault(str(n.get("name")), set()).update(
                str(d) for d in deps if d in known
            )
    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def d(name: str) -> int:
        if name in depth:
            return depth[name]
        # Cycle guard — a leftover depends_on cycle (or any other loop) must
        # never recurse, even after the edge filter dropped control-flow.
        if name in visiting:
            return 0
        visiting.add(name)
        try:
            ps = preds.get(name) or set()
            depth[name] = 0 if not ps else 1 + max(d(p) for p in ps)
            return depth[name]
        finally:
            visiting.discard(name)

    for n in names:
        d(n)
    return sorted(names, key=lambda n: depth[n])


def _node_artifacts(run_dir: Path, node_name: str,
                    wf_node: dict[str, Any]) -> dict[str, Any]:
    """Read the run's verdict artefacts for one node.

    Returns ``{"verifier": <payload>}`` and/or ``{"review": {verdict, text}}``
    when present. The keys are absent (not ``None``) when the artefact is
    missing — callers gate on ``"verifier" in artifacts``.
    """
    out: dict[str, Any] = {}
    verifier_ref = wf_node.get("verifier_ref") if isinstance(wf_node, dict) else None
    if verifier_ref:
        stem = Path(str(verifier_ref)).stem
    else:
        stem = node_name.replace("_", "-")
    vp = run_dir / f"verifier_{stem}.json"
    if vp.is_file():
        payload = read_verifier_payload(str(vp), stem)
        if isinstance(payload, dict):
            out["verifier"] = payload
    rp = run_dir / f"review-{node_name}.json"
    if rp.is_file():
        try:
            text = rp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = _VERDICT_RE.search(text)
        if m:
            out["review"] = {"verdict": m.group(1), "text": text}
    return out


def _verifier_failed(payload: dict[str, Any]) -> bool:
    if payload.get("pass") is False:
        return True
    return str(payload.get("status") or "").upper() in ("UNVERIFIED", "REFUTED", "FAIL")


def _verifier_passed(payload: dict[str, Any]) -> bool:
    if payload.get("pass") is True:
        return True
    return str(payload.get("status") or "").upper() in ("PROVEN", "NA")


def _review_failed(review: dict[str, Any]) -> bool:
    return str(review.get("verdict") or "").lower() in _RETRY_VERDICTS


def _first_failed_node(run_dir: Path, recipe: str, home: Path
                       ) -> tuple[str | None, dict[str, Any]]:
    """First node in workflow topo order whose run artefact says it failed."""
    nodes, edges = _recipe_workflow(home, recipe)
    order = _topo_order(nodes, edges)
    name_to_node = {str(n.get("name")): n for n in nodes if n.get("name")}
    for name in order:
        wf_node = name_to_node.get(name, {})
        arts = _node_artifacts(run_dir, name, wf_node)
        verifier = arts.get("verifier") if isinstance(arts.get("verifier"), dict) else None
        review = arts.get("review") if isinstance(arts.get("review"), dict) else None
        if isinstance(verifier, dict) and _verifier_failed(verifier):
            return name, arts
        if isinstance(review, dict) and _review_failed(review):
            return name, arts
    return None, {}


def _earlier_verifiers_passed(run_dir: Path, nodes: list[dict[str, Any]],
                              edges: list[dict[str, Any]], target: str) -> bool:
    """True when every verifier node earlier in topo order has a passing artefact."""
    order = _topo_order(nodes, edges)
    name_to_node = {str(n.get("name")): n for n in nodes if n.get("name")}
    for name in order:
        if name == target:
            return True
        wf_node = name_to_node.get(name, {})
        if not (wf_node.get("verifier_ref") or wf_node.get("type") == "verifier"):
            continue
        arts = _node_artifacts(run_dir, name, wf_node)
        verifier = arts.get("verifier") if isinstance(arts.get("verifier"), dict) else None
        if not (isinstance(verifier, dict) and _verifier_passed(verifier)):
            return False
    return True


def _any_other_verifier_hard_failed(run_dir: Path, nodes: list[dict[str, Any]],
                                    edges: list[dict[str, Any]],
                                    target: str) -> bool:
    """True when any verifier node other than ``target`` has a REFUTED/FAIL
    verdict. Case 2 only fires when the UNVERIFIED target is the *only* kind
    of verifier failure in the run — a parallel REFUTED means a code problem
    is what actually failed the run, not the environment.
    """
    return _find_other_verifier_hard_failed(run_dir, nodes, edges, target) is not None


def _find_other_verifier_hard_failed(run_dir: Path, nodes: list[dict[str, Any]],
                                     edges: list[dict[str, Any]],
                                     target: str) -> dict[str, Any] | None:
    """The first sibling verifier's payload when it has a REFUTED/FAIL verdict.

    Returns ``None`` when every other verifier either passed, is UNVERIFIED,
    or has no artefact. Used by :func:`_case_code` to surface the sibling's
    reason as the case-3 detail when the target is UNVERIFIED (case 2 filtered
    out) but a parallel REFUTED is the real reason the run failed.
    """
    order = _topo_order(nodes, edges)
    name_to_node = {str(n.get("name")): n for n in nodes if n.get("name")}
    for name in order:
        if name == target:
            continue
        wf_node = name_to_node.get(name, {})
        if not (wf_node.get("verifier_ref") or wf_node.get("type") == "verifier"):
            continue
        arts = _node_artifacts(run_dir, name, wf_node)
        verifier = arts.get("verifier") if isinstance(arts.get("verifier"), dict) else None
        if not isinstance(verifier, dict):
            continue
        status = str(verifier.get("status") or "").upper()
        if status in ("REFUTED", "FAIL") or verifier.get("pass") is False and status != "UNVERIFIED":
            return verifier
    return None


def _extract_implementer_notes(run_dir: Path) -> list[str]:
    """Up to 3 lines from ``impl-*.log`` that mention a relevant hint token."""
    notes: list[str] = []
    for path in sorted(run_dir.glob("impl-*.log")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            low = line.lower()
            if any(tok in low for tok in _NOTE_TOKENS):
                stripped = line.strip()[:280]
                if stripped:
                    notes.append(stripped)
                if len(notes) >= 3:
                    return notes
    return notes


def _verifier_reason(payload: dict[str, Any]) -> str:
    """The verifier's reason field — first 800 chars across the candidate keys."""
    for key in ("reason", "error_summary", "reasons", "detail"):
        v = payload.get(key)
        if v:
            text = "; ".join(str(x) for x in v) if isinstance(v, list) else str(v)
            return text[:800]
    return ""


def _extract_review_detail(text: str) -> str:
    """The first three reviewer reasons (or notes) as plain text, one per line.

    Returns ``""`` when the file is not JSON or carries no ``reasons``/``notes``
    block. The kickoff mandates plain text (not raw JSON) so the operator sees
    the reviewer's exact language instead of a comma-and-quote array dump.
    """
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        # The review file may have a banner line or trailing log; the JSON
        # object is the only thing we want to read from.
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("reasons", "notes"):
        items = data.get(key)
        if isinstance(items, list) and items:
            lines = [str(x) for x in items[:3] if x]
            joined = "\n".join(lines)
            return joined[:800]
    return ""


def _tail_log(run_dir: Path, node_name: str, *, n: int = 20) -> str:
    """Last ``n`` lines of the most informative log for ``node_name``."""
    candidates = [run_dir / f"impl-{name}.log" for name in (node_name,)]
    candidates += [run_dir / f"agent-{node_name}.live.jsonl",
                   run_dir / "execute.log"]
    for path in candidates:
        if not path.is_file() or not path.name:
            continue
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 16_384))
                data = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        lines = data.splitlines()[-n:]
        if lines:
            return "\n".join(lines)[:2000]
    return ""


def _failure_class_for_node(home: Path, run_id: str,
                            node_name: str) -> tuple[str | None, str]:
    """``(failure_class, log_tail)`` for the latest ``node_attempts`` row.

    The ``log_tail`` carries the original error text so :func:`_is_auth` can
    spot ``401`` / ``credential`` tokens without an extra DB hit. Returns
    ``(None, "")`` when the table is missing or no row matches — case 4 then
    falls through to case 5.
    """
    if not node_name:
        return None, ""
    try:
        db = db_for(home)
    except Exception:  # noqa: BLE001 — missing state.db: never raise
        return None, ""
    if not db.has_table("node_attempts"):
        return None, ""
    try:
        rows = db.rows(
            "SELECT failure_class FROM node_attempts "
            "WHERE run_id = ? AND node_id = ? ORDER BY attempt_no DESC LIMIT 1",
            (run_id, node_name),
        )
    except Exception:  # noqa: BLE001 — schema drift: never raise
        return None, ""
    if not rows:
        return None, ""
    return (str(rows[0].get("failure_class") or "") or None), ""


def _failure_class_for_any(home: Path, run_id: str
                           ) -> tuple[str | None, str | None]:
    """``(failure_class, node_id)`` for the most recent failed attempt in the run.

    Used when no verifier/reviewer artefact picked a failed node — we still
    need to surface the LLM-node that crashed with a provider-trouble class
    so case 4 / case 5 name the right node. Returns ``(None, None)`` when
    the table is missing or no failed row exists.
    """
    try:
        db = db_for(home)
    except Exception:  # noqa: BLE001 — missing state.db: never raise
        return None, None
    if not db.has_table("node_attempts"):
        return None, None
    try:
        rows = db.rows(
            "SELECT node_id, failure_class FROM node_attempts "
            "WHERE run_id = ? AND result = 'failure' "
            "ORDER BY attempt_no DESC LIMIT 1",
            (run_id,),
        )
    except Exception:  # noqa: BLE001 — schema drift: never raise
        return None, None
    if not rows:
        return None, None
    row = rows[0]
    fc = str(row.get("failure_class") or "") or None
    node = str(row.get("node_id") or "") or None
    return fc, node


def _looks_like_http_auth(blob: str) -> bool:
    """True when ``blob`` contains a bare 401/403 within 30 chars of an HTTP
    context word. Substring ``in`` matching on digits is unreliable — it
    matches ``tokens_in=14012`` or ``cost=$0.21``. The proximity check is the
    smallest rule that keeps both negatives.
    """
    lower = blob.lower()
    if not any(tok in lower for tok in _AUTH_TOKENS):
        # No word-token auth hit — only count digit matches near an HTTP word.
        for m in _HTTP_AUTH_DIGITS.finditer(blob):
            start = max(0, m.start() - _HTTP_AUTH_PROXIMITY)
            end = min(len(blob), m.end() + _HTTP_AUTH_PROXIMITY)
            if _HTTP_AUTH_CONTEXT.search(blob[start:end]):
                return True
        return False
    return True


def _is_auth(run_dir: Path, node_name: str,
             verifier: dict[str, Any] | None) -> bool:
    """True when the verifier reason OR the node's log mentions an auth token.

    When ``node_name`` is empty the walk glob over ``impl-*.log`` happens
    anyway — case 4 needs the broadest possible auth-tokens sweep when no
    ``failed_name`` and no failed ``node_attempts`` row are available.
    """
    log_tail = ""
    if run_dir.is_dir():
        if node_name:
            log_tail = _tail_log(run_dir, node_name, n=40)
        else:
            blobs = []
            for path in sorted(run_dir.glob("impl-*.log")):
                try:
                    blobs.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
            log_tail = "\n".join(blobs)[:8192]
    blob = " ".join([
        (verifier or {}).get("reason") or "",
        (verifier or {}).get("error_summary") or "",
        log_tail,
    ])
    return _looks_like_http_auth(blob)


def _build_command(strategy: str, run_id: str) -> str:
    """The hint's command string — never embeds ``--ack-change`` or ``--force``.

    The board verb appends those flags on its own when the operator typed them.
    Baking them into the hint would couple the hint to a particular operator
    action and make the hint's `command` field unreliable for read-only
    consumers (the run page, the IDE shell, ``mini-ork recover --strategy
    verify``).
    """
    if strategy == "resume-cost":
        return f"mini-ork resume {run_id}"
    if strategy == "none":
        return ""
    return " ".join(["mini-ork recover", run_id, "--strategy", strategy])


def _case_environment(run_dir: Path, recipe: str, home: Path,
                      run_id: str, failed_name: str,
                      verifier: dict[str, Any]) -> dict[str, Any] | None:
    """Case 2: verifier UNVERIFIED / unreachable-precondition while earlier verifiers passed."""
    status = str(verifier.get("status") or "").upper()
    reason_text = str(
        verifier.get("reason") or verifier.get("error_summary") or ""
    ).lower()
    reach = status == "UNVERIFIED" or (
        verifier.get("pass") is False
        and any(tok in reason_text for tok in _UNREACH_TOKENS)
    )
    if not reach:
        return None
    nodes, edges = _recipe_workflow(home, recipe)
    if not _earlier_verifiers_passed(run_dir, nodes, edges, failed_name):
        return None
    if _any_other_verifier_hard_failed(run_dir, nodes, edges, failed_name):
        return None
    notes = _extract_implementer_notes(run_dir)
    return {
        "version": HINT_VERSION,
        "run_id": run_id,
        "failed_node": failed_name,
        "retryable": True,
        "strategy": "verify",
        "from_node": failed_name,
        "needs_change": {
            "kind": "environment",
            "summary": f"The {failed_name} check could not reach something it needs",
            "detail": _verifier_reason(verifier),
            "evidence": str(verifier.get("evidence_path") or ""),
        },
        "notes": notes,
        "command": _build_command("verify", run_id),
        "computed_at": _now_iso(),
    }


def _case_code(failed_name: str | None,
               verifier: dict[str, Any] | None,
               review: dict[str, Any] | None,
               run_id: str,
               sibling_hard_fail: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Case 3: verifier REFUTED/FAIL (not case 2) or reviewer reject/fail/needs_revision.

    ``UNVERIFIED`` is case 2's signature — when case 2 did not match (because
    earlier verifiers failed), the failure is treated as code, not environment.
    The command is the bare ``mini-ork recover <run>`` so an operator with
    ``--force`` can still kick the recovery (which is what the board verb does).

    Order of preference for the detail (the operator-facing string):

      1. Reviewer reasons (when ``review`` is needs_revision/reject/fail).
         The reviewer is authoritative on rework — its language wins.
      2. A REFUTED/FAIL sibling's reason. The target may be UNVERIFIED (case 2
         filtered out), but a parallel REFUTED means the code itself is wrong
         and the operator must see THAT verifier's reason — not an empty
         string from the UNVERIFIED target.
      3. The target verifier's own reason (only when it is REFUTED/FAIL —
         UNVERIFIED is empty here on purpose).
    """
    summary = "The change was judged wrong — it needs a revision"
    detail = ""
    if isinstance(review, dict) and _review_failed(review):
        detail = _extract_review_detail(str(review.get("text") or ""))
    if not detail and isinstance(sibling_hard_fail, dict):
        detail = _verifier_reason(sibling_hard_fail)
    if not detail and isinstance(verifier, dict):
        status = str(verifier.get("status") or "").upper()
        if status != "UNVERIFIED":
            detail = _verifier_reason(verifier)
    if not detail:
        return None  # case 2 already filtered; nothing left to report
    return {
        "version": HINT_VERSION,
        "run_id": run_id,
        "failed_node": failed_name,
        "retryable": False,
        "strategy": "none",
        "from_node": failed_name,
        "needs_change": {
            "kind": "code",
            "summary": summary,
            "detail": detail[:800],
            "evidence": "",
        },
        "notes": [],
        "command": "",
        "computed_at": _now_iso(),
    }


def _case_provider_trouble(home: Path, run_id: str, run_dir: Path,
                           failed_name: str | None,
                           verifier: dict[str, Any] | None) -> dict[str, Any] | None:
    """Case 4: LLM node with provider-trouble failure_class, retryable.

    Looks up the failure class from ``node_attempts``. When ``failed_name``
    is None (no verifier/reviewer fired) we still probe the latest failed
    attempt across the whole run so an infra_interrupt on the implementer
    surfaces as the right hint instead of falling through to case 5.

    Auth tokens in any impl log also qualify — a missing failure_class row
    (older runs, schema drift) must not hide a 401.
    """
    fc = None
    any_node_name: str | None = None
    node_for_log = failed_name or ""
    if failed_name:
        fc, _ = _failure_class_for_node(home, run_id, failed_name)
    if not fc:
        fc, any_node_name = _failure_class_for_any(home, run_id)
        if fc and any_node_name:
            node_for_log = any_node_name

    # Auth check uses the most recent failed LLM node's tail — or any impl
    # log when no failed row is recorded. The walk-glob inside ``_is_auth``
    # already handles an empty node name (it scans all impl-*.log tails
    # when ``node_name`` is empty), so a bare credential hit is enough.
    auth_node = node_for_log
    if not auth_node:
        auth_node = _latest_impl_log_node(run_dir)
    auth = _is_auth(run_dir, auth_node, verifier) if auth_node else False
    if auth:
        label = failed_name or any_node_name or auth_node or "implementer"
        return {
            "version": HINT_VERSION,
            "run_id": run_id,
            "failed_node": label,
            "retryable": True,
            "strategy": "resume",
            "from_node": label,
            "needs_change": {
                "kind": "credentials",
                "summary": "The provider rejected the call (auth/credential)",
                "detail": _verifier_reason(verifier or {}),
                "evidence": "",
            },
            "notes": [],
            "command": _build_command("resume", run_id),
            "computed_at": _now_iso(),
        }

    if not fc:
        return None
    policy = recovery_policy(fc)
    if policy.get("marks_run_failed"):
        return None
    is_trouble = auto_recoverable(fc) or fc == PROVIDER_LIMIT
    if not is_trouble:
        return None
    label = failed_name or any_node_name or node_for_log
    return {
        "version": HINT_VERSION,
        "run_id": run_id,
        "failed_node": label,
        "retryable": True,
        "strategy": "resume",
        "from_node": label,
        "needs_change": None,
        "notes": [],
        "command": _build_command("resume", run_id),
        "computed_at": _now_iso(),
    }


def _latest_impl_log_node(run_dir: Path) -> str | None:
    """The node name of the lexicographically-last ``impl-*.log`` in the run
    dir. Used by case 4 when no ``failed_name`` and no failed ``node_attempts``
    row exist but the run dir still carries impl output to walk for auth hits.
    """
    paths = sorted(run_dir.glob("impl-*.log")) if run_dir.is_dir() else []
    if not paths:
        return None
    last = paths[-1]
    name = last.name[len("impl-"):-len(".log")]
    return name or None


def _case_unknown(run_dir: Path, failed_name: str | None, run_id: str,
                  fallback_name: str | None = None) -> dict[str, Any]:
    """Case 5: not classified — surface the last log lines so the operator can decide.

    ``fallback_name`` is the most recent failed LLM-node (when no
    verifier/reviewer fired) so the summary names something concrete.
    """
    label = failed_name or fallback_name
    return {
        "version": HINT_VERSION,
        "run_id": run_id,
        "failed_node": label,
        "retryable": False,
        "strategy": "none",
        "from_node": label,
        "needs_change": {
            "kind": "unknown",
            "summary": f"Failed at {label or '?'}; the cause was not classified",
            "detail": _tail_log(run_dir, label or ""),
            "evidence": "",
        },
        "notes": [],
        "command": "",
        "computed_at": _now_iso(),
    }


def _case_cost_pause(run_id: str) -> dict[str, Any]:
    return {
        "version": HINT_VERSION,
        "run_id": run_id,
        "failed_node": None,
        "retryable": True,
        "strategy": "resume-cost",
        "from_node": None,
        "needs_change": {
            "kind": "budget",
            "summary": "Paused at the cost cap",
            "detail": "",
            "evidence": "",
        },
        "notes": [],
        "command": _build_command("resume-cost", run_id),
        "computed_at": _now_iso(),
    }


def compute(home: Path, run_id: str) -> dict[str, Any] | None:
    """Pure read — return the retry hint for ``run_id`` or ``None``.

    ``None`` when the run is still in flight, has succeeded, or its run dir
    is gone. The 5-way classifier (first match wins) lives here; see the
    module docstring for the full rule set.
    """
    home = Path(home)
    card = _run_card(home, run_id)
    if card is None:
        return None
    status = str(card.get("status") or "")
    if status == "published":
        return None
    if status not in ("failed", "rolled_back"):
        return None
    recipe = str(card.get("recipe") or "")
    run_dir = home / "runs" / run_id
    if not run_dir.is_dir():
        return None

    # Case 1 — cost-pause sentinel
    if (run_dir / ".cost-pause").is_file():
        return _case_cost_pause(run_id)

    failed_name, artifacts = _first_failed_node(run_dir, recipe, home)
    verifier = artifacts.get("verifier") if isinstance(artifacts.get("verifier"), dict) else None
    review = artifacts.get("review") if isinstance(artifacts.get("review"), dict) else None

    # Case 2 — environment / unreachable precondition
    if isinstance(verifier, dict) and failed_name:
        env = _case_environment(run_dir, recipe, home, run_id, failed_name, verifier)
        if env is not None:
            return env

    # Case 3 — code revision needed. Look up the sibling hard-fail payload
    # up front so an UNVERIFIED target (case 2 filtered out) still surfaces
    # the REFUTED sibling's reason as the operator-facing detail.
    nodes, edges = _recipe_workflow(home, recipe)
    sibling_hard_fail = _find_other_verifier_hard_failed(
        run_dir, nodes, edges, failed_name or "",
    ) if failed_name else None
    code = _case_code(failed_name, verifier, review, run_id,
                      sibling_hard_fail=sibling_hard_fail)
    if code is not None:
        return code

    # Case 4 — provider trouble on an LLM node
    trouble = _case_provider_trouble(home, run_id, run_dir, failed_name, verifier)
    if trouble is not None:
        return trouble

    # Case 5 — unclassified. Surface the most recent failed LLM-node as the
    # best-effort label so the operator doesn't read a bare "?".
    _, fallback_node = _failure_class_for_any(home, run_id)
    if not fallback_node:
        fallback_node = _latest_impl_log_node(run_dir)
    return _case_unknown(run_dir, failed_name, run_id, fallback_name=fallback_node)


def _cache_dependencies(home: Path, run_dir: Path) -> dict[str, int]:
    """Mtimes of every file/state the hint reads — cache is valid only when
    fresher than all of them.

    The on-disk mtimes alone are not enough: a fresh row in ``node_attempts``
    or a new ``run_profile.json`` write can flip a case-4 hint or change a
    case-2 ``from_node`` without touching any artefact file in the run dir.
    Both feed the cache invalidation so the IDE shell never serves a hint
    computed against stale state.
    """
    deps: dict[str, int] = {}
    if run_dir.is_dir():
        for pattern in ("verifier_*.json", "review-*.json", "impl-*.log"):
            for path in run_dir.glob(pattern):
                try:
                    deps[f"{pattern}:{path.name}"] = path.stat().st_mtime_ns
                except OSError:
                    pass
        sentinel = run_dir / ".cost-pause"
        if sentinel.is_file():
            try:
                deps["sentinel:cost-pause"] = sentinel.stat().st_mtime_ns
            except OSError:
                pass
    # ``run_profile.json`` lives alongside the run dir (home/runs/<id>/..).
    profile = run_dir / "run_profile.json" if run_dir.is_dir() else None
    if profile is not None and profile.is_file():
        try:
            deps["run_profile.json"] = profile.stat().st_mtime_ns
        except OSError:
            pass
    # node_attempts can move without a corresponding file write — pull the
    # latest ended_at for the run and use that as a synthetic mtime.
    run_id = run_dir.name if run_dir.is_dir() else ""
    if run_id and home is not None:
        try:
            db = db_for(home)
            if db.has_table("node_attempts"):
                rows = db.rows(
                    "SELECT MAX(ended_at) AS last_ended FROM node_attempts "
                    "WHERE run_id = ?",
                    (run_id,),
                )
                if rows and rows[0].get("last_ended"):
                    # mtime_ns is a virtual second-precision synthetic — the
                    # cache file's own mtime_ns is real, and the comparison
                    # only needs a monotonic ordering.
                    deps["node_attempts:last_ended"] = int(
                        rows[0]["last_ended"]
                    ) * 1_000_000_000
        except Exception:  # noqa: BLE001 — missing state.db: never raise
            pass
    return deps


def load_or_compute(home: Path, run_id: str, *, write: bool = True) -> dict[str, Any] | None:
    """Cache-aware compute.

    Reads ``<run_dir>/retry-hint.json`` when it is newer than every file the
    hint depends on; otherwise calls :func:`compute` and, when ``write=True``,
    saves the result. When ``write=False``, the hint is never written into
    a real run dir — the IDE page uses that mode.

    A non-terminal run status (executing, queued, …) short-circuits BEFORE the
    cache read so a relaunched run never returns the prior failed attempt's
    hint. The cache file is left in place — a status flip back to failed
    should still find the cached hint on the next call.
    """
    home = Path(home)
    run_dir = home / "runs" / run_id
    cached_path = _hint_path(home, run_id)
    current_status = _current_run_status(home, run_id)
    # Non-terminal ⇒ no hint, period. The run is in flight or was relaunched;
    # the cached hint from the prior failed attempt is no longer authoritative.
    # We intentionally do NOT unlink the cache: a status flip back to failed
    # should still find it on the next call.
    if current_status and current_status not in _TERMINAL_FAILED:
        return None
    deps = _cache_dependencies(home, run_dir)
    if cached_path.is_file() and deps:
        try:
            cache_mtime = cached_path.stat().st_mtime_ns
            if all(cache_mtime > m for m in deps.values() if m):
                data = json.loads(cached_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("run_id") == run_id:
                    # A status change since the hint was cached also invalidates
                    # the entry — the run may now be re-running on a different
                    # attempt's artifacts.
                    if current_status and data.get("status") != current_status:
                        pass  # fall through to recompute
                    else:
                        return data
        except (OSError, ValueError, TypeError):
            pass
    hint = compute(home, run_id)
    if hint is not None and write:
        # Stamp the current status onto the cache record so the next call can
        # spot a status change even when the on-disk mtimes have not moved.
        if current_status:
            hint["status"] = current_status
        try:
            cached_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(hint, indent=2), encoding="utf-8")
            tmp.replace(cached_path)
        except OSError:
            pass
    return hint