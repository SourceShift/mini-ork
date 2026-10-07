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
_AUTH_TOKENS = ("401", "403", "credential", "api key", "api_key", "unauthor")

# Tokens in a verifier reason that mean "this isn't a code problem, the
# environment can't reach what it needs".
_UNREACH_TOKENS = ("unreachable", "not set", "missing", "precondition")

# Tokens that surface a useful hint in the implementer log.
_NOTE_TOKENS = ("precondition", "must be restarted", "export", "not set", "env")

_VERDICT_RE = re.compile(r'"verdict"\s*:\s*"([^"]+)"')


def _hint_path(home: Path, run_id: str) -> Path:
    return home / "runs" / run_id / CACHE_FILENAME


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

    Verifier/reviewer/publisher/rollback nodes that follow an ``escalates_to``
    edge are placed last so a failure at a verifier surfaces before its
    rollback node (which is structurally a sibling).
    """
    names = [str(n.get("name")) for n in nodes if n.get("name")]
    known = set(names)
    preds: dict[str, set[str]] = {n: set() for n in names}
    for e in edges:
        src, dst = str(e.get("from") or ""), str(e.get("to") or "")
        if src in known and dst in known and src != dst:
            if str(e.get("edge_type") or "") == "escalates_to":
                # escalation does NOT delay the target; if the target has any
                # non-escalation deps they win.
                continue
            preds.setdefault(dst, set()).add(src)
    for n in nodes:
        deps = n.get("depends_on")
        if isinstance(deps, list):
            preds.setdefault(str(n.get("name")), set()).update(
                str(d) for d in deps if d in known
            )
    depth: dict[str, int] = {}

    def d(name: str, stack: tuple[str, ...]) -> int:
        if name in depth:
            return depth[name]
        ps = preds.get(name) or set()
        depth[name] = 0 if not ps else 1 + max(d(p, stack + (name,)) for p in ps)
        return depth[name]

    for n in names:
        d(n, ())
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
            return True
    return False


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
    """Pull the first ``"reasons": […]`` or ``"notes": […]`` block from a review file."""
    for key in ("reasons", "notes"):
        m = re.search(rf'"{key}"\s*:\s*\[([^\]]*)\]', text)
        if m:
            return m.group(1)[:800]
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
    ]).lower()
    return any(tok in blob for tok in _AUTH_TOKENS)


def _build_command(strategy: str, run_id: str, *, needs_ack: bool) -> str:
    if strategy == "resume-cost":
        return f"mini-ork resume {run_id}"
    if strategy == "none":
        return ""
    parts = ["mini-ork recover", run_id, "--strategy", strategy]
    if needs_ack:
        parts.append("--ack-change")
    return " ".join(parts)


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
        "command": _build_command("verify", run_id, needs_ack=True),
        "computed_at": _now_iso(),
    }


def _case_code(failed_name: str | None,
               verifier: dict[str, Any] | None,
               review: dict[str, Any] | None,
               run_id: str) -> dict[str, Any] | None:
    """Case 3: verifier REFUTED/FAIL (not case 2) or reviewer reject/fail/needs_revision.

    ``UNVERIFIED`` is case 2's signature — when case 2 did not match (because
    earlier verifiers failed), the failure is treated as code, not environment.
    The command is the bare ``mini-ork recover <run>`` so an operator with
    ``--force`` can still kick the recovery (which is what the board verb does).
    """
    summary = "The change was judged wrong — it needs a revision"
    detail = ""
    if isinstance(review, dict) and _review_failed(review):
        detail = _extract_review_detail(str(review.get("text") or ""))
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
            "command": _build_command("resume", run_id, needs_ack=True),
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
        "command": _build_command("resume", run_id, needs_ack=False),
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
        "command": _build_command("resume-cost", run_id, needs_ack=False),
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

    # Case 3 — code revision needed
    code = _case_code(failed_name, verifier, review, run_id)
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


def _cache_dependencies(run_dir: Path) -> dict[str, int]:
    """Mtimes of every file the hint reads — cache is valid only when fresher than all of them."""
    deps: dict[str, int] = {}
    if not run_dir.is_dir():
        return deps
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
    return deps


def load_or_compute(home: Path, run_id: str, *, write: bool = True) -> dict[str, Any] | None:
    """Cache-aware compute.

    Reads ``<run_dir>/retry-hint.json`` when it is newer than every file the
    hint depends on; otherwise calls :func:`compute` and, when ``write=True``,
    saves the result. When ``write=False``, the hint is never written into
    a real run dir — the IDE page uses that mode.
    """
    home = Path(home)
    run_dir = home / "runs" / run_id
    cached_path = _hint_path(home, run_id)
    deps = _cache_dependencies(run_dir)
    if cached_path.is_file() and deps:
        try:
            cache_mtime = cached_path.stat().st_mtime_ns
            if all(cache_mtime > m for m in deps.values() if m):
                data = json.loads(cached_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("run_id") == run_id:
                    return data
        except (OSError, ValueError, TypeError):
            pass
    hint = compute(home, run_id)
    if hint is not None and write:
        try:
            cached_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(hint, indent=2), encoding="utf-8")
            tmp.replace(cached_path)
        except OSError:
            pass
    return hint