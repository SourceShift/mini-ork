"""I/O driver for failure triage: read a failed run → attribute → queue a fix.

Reads a run's ``run_events`` (``node_end`` rows carry ``finish_reason``) plus the
node logs under ``${MINI_ORK_HOME}/runs/<run_id>/``, attributes the failure with
:func:`mini_ork.triage.blame.attribute`, and — when the blame is ``mini_ork`` and
not a dry run — emits a bug report and (optionally) promotes it to a
``framework-edit`` epic that the scheduler will dispatch in an isolated worktree.

Nothing here is on the hot path of a run: the caller (``execute`` fail branch, or
the ``mini-ork triage`` CLI) invokes it explicitly and it never raises into a run.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from mini_ork.observability import bug_report
from mini_ork.triage import blame as _blame
from mini_ork.triage.blame import Evidence, NodeFailure

# fail-marked finish_reasons — everything the run-level verdict counts as a failure.
_FAILED_REASONS = frozenset({"error", "timeout", "cost_limit", "interrupted", "max_steps", "verdict_fail", "verdict_revise"})

_DEFAULT_FIX_RECIPE = "framework-edit"
_LOG_TAIL_LINES = 120
_LOG_TAIL_BYTES = 16384

_ERROR_LINE_RE = re.compile(r"^.*(?:Error|Exception|error:|Traceback)\b.*$", re.MULTILINE)


@dataclass
class TriageResult:
    run_id: str
    recipe: str = ""
    blame: str = "unknown"
    evidence: list[Evidence] = field(default_factory=list)
    failed_nodes: list[NodeFailure] = field(default_factory=list)
    signature: str = ""
    bug_id: int | None = None
    epic_id: str | None = None
    kickoff_path: str | None = None
    reason: str = ""
    dry_run: bool = False

    @property
    def fix_recipe(self) -> str:
        return os.environ.get("MO_TRIAGE_FIX_RECIPE", _DEFAULT_FIX_RECIPE)

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "recipe": self.recipe,
            "blame": self.blame,
            "signature": self.signature,
            "bug_id": self.bug_id,
            "epic_id": self.epic_id,
            "kickoff_path": self.kickoff_path,
            "reason": self.reason,
            "dry_run": self.dry_run,
            "evidence": [{"rule": e.rule, "detail": e.detail} for e in self.evidence],
            "failed_nodes": [
                {"node_id": n.node_id, "node_type": n.node_type, "finish_reason": n.finish_reason}
                for n in self.failed_nodes
            ],
        }


def resolve_home(home: str | os.PathLike[str] | None = None) -> str:
    if home is not None:
        return str(home)
    return os.environ.get("MINI_ORK_HOME") or ".mini-ork"


def resolve_db(db: str | os.PathLike[str] | None = None, *, home: str | None = None) -> str:
    if db is not None:
        return str(db)
    env = os.environ.get("MINI_ORK_DB")
    if env:
        return env
    return os.path.join(resolve_home(home), "state.db")


def resolve_root(root: str | os.PathLike[str] | None = None) -> str:
    if root is not None:
        return str(root)
    env = os.environ.get("MINI_ORK_ROOT")
    if env:
        return env
    # mini_ork/triage/failures.py → mini_ork/triage → mini_ork → REPO
    return str(Path(__file__).resolve().parent.parent.parent)


def _tail(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _LOG_TAIL_BYTES))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-_LOG_TAIL_LINES:])


def _node_log_stems(node_id: str) -> list[str]:
    """Filename stems a node's logs are written under (best-effort).

    The node id and the log stem are not the same string in practice: the
    workflow node ``test_verifier`` writes ``verifier-test.log`` and
    ``evidence/test-<ts>.log``; ``static_check_verifier`` writes
    ``verifier-static-check.log``. Derive the stems so a real traceback is not
    missed purely on a naming mismatch.
    """
    stems = [node_id]
    if node_id.endswith("_verifier"):
        base = node_id[: -len("_verifier")]
        stems += [base, base.replace("_", "-")]
    stems += [s.replace("_", "-") for s in list(stems)]
    seen, out = set(), []
    for s in stems:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _find_node_log(run_dir: Path, node_id: str) -> str:
    """Best-effort: gather the logs belonging to *node_id* and tail each.

    Collects *all* matching logs (exact names, then globs under the run dir and
    ``evidence/``) rather than stopping at the first hit — the checks log and
    the raw verifier log differ, and a traceback can live in either.
    """
    if not run_dir.is_dir():
        return ""
    stems = _node_log_stems(node_id)
    found: list[Path] = []
    for stem in stems:
        for cand in (
            run_dir / f"verifier-{stem}.log",
            run_dir / f"{stem}.log",
            run_dir / f"impl-{stem}.log",
            run_dir / "logs" / f"{stem}.log",
            run_dir / "evidence" / f"{stem}.log",
        ):
            if cand.is_file() and cand not in found:
                found.append(cand)
    for stem in stems:
        for root_dir in (run_dir, run_dir / "evidence"):
            for match in sorted(root_dir.glob(f"*{stem}*.log")):
                if match.is_file() and match not in found:
                    found.append(match)
    if not found:
        return _tail(run_dir / "execute.log")
    return "\n".join(_tail(p) for p in found[:6])


def load_failures(db_path: str, run_id: str, *, home: str | None = None) -> list[NodeFailure]:
    """Read ``node_end`` events for *run_id* whose finish_reason is a failure."""
    if not os.path.isfile(db_path):
        return []
    run_dir = Path(resolve_home(home)) / "runs" / run_id
    con = sqlite3.connect(db_path, timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        rows = con.execute(
            "SELECT payload_json, finish_reason FROM run_events "
            "WHERE run_id=? AND event_type='node_end' ORDER BY created_at",
            (run_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()

    out: list[NodeFailure] = []
    for payload_json, finish_reason in rows:
        if (finish_reason or "").lower() not in _FAILED_REASONS:
            continue
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        node_id = str(payload.get("node_id") or "")
        if not node_id:
            continue
        out.append(
            NodeFailure(
                node_id=node_id,
                node_type=str(payload.get("node_type") or ""),
                finish_reason=finish_reason,
                log_excerpt=_find_node_log(run_dir, node_id),
                artifact_path=str(payload.get("artifact_path") or ""),
            )
        )
    return out


def _run_recipe(db_path: str, run_id: str) -> str:
    if not os.path.isfile(db_path):
        return ""
    con = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = con.execute("SELECT recipe FROM task_runs WHERE id=?", (run_id,)).fetchone()
        return (row[0] or "") if row else ""
    except sqlite3.Error:
        return ""
    finally:
        con.close()


def latest_failed_run(db_path: str) -> str | None:
    if not os.path.isfile(db_path):
        return None
    con = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = con.execute(
            "SELECT id FROM task_runs WHERE status='failed' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _first_error_line(*texts: str) -> str:
    for text in texts:
        m = _ERROR_LINE_RE.search(text or "")
        if m:
            return m.group(0).strip()[:200]
    return ""


def _signature(recipe: str, failed: NodeFailure) -> str:
    seed = f"{recipe}|{failed.node_id}|{failed.finish_reason or ''}|{_first_error_line(failed.log_excerpt)}"
    return hashlib.sha256(seed.encode()).hexdigest()


def _bug_title(recipe: str, failed: NodeFailure) -> str:
    # Stable across recurrences so the fingerprint-based sweep dedupes them.
    return f"failed run node: {failed.node_id} [{failed.finish_reason or 'error'}] in {recipe or 'unknown-recipe'}"


def triage_run(
    run_id: str,
    *,
    home: str | os.PathLike[str] | None = None,
    db: str | os.PathLike[str] | None = None,
    root: str | os.PathLike[str] | None = None,
    promote: bool = False,
    dry_run: bool = False,
) -> TriageResult:
    """Attribute *run_id*'s failure and — unless *dry_run* — record/queue a fix."""
    home_s = resolve_home(home)
    db_s = resolve_db(db, home=home_s)
    root_s = resolve_root(root)

    res = TriageResult(run_id=run_id, dry_run=dry_run, recipe=_run_recipe(db_s, run_id))

    failures = load_failures(db_s, run_id, home=home_s)
    res.failed_nodes = failures
    if not failures:
        res.blame = "unknown"
        res.reason = "no failing node_end events for this run"
        return res

    # Attribute every failure; the run is blamed on its most framework-specific one.
    verdicts: list[tuple[str, NodeFailure, list[Evidence]]] = []
    for f in failures:
        v, ev = _blame.attribute(f, root=root_s)
        verdicts.append((v, f, ev))

    for pref in ("mini_ork", "consumer", "unknown"):
        chosen = next((t for t in verdicts if t[0] == pref), None)
        if chosen:
            res.blame, primary, res.evidence = chosen
            break
    else:  # pragma: no cover — verdicts is non-empty here
        res.blame, primary, res.evidence = "unknown", failures[0], []

    res.signature = _signature(res.recipe, primary)
    res.reason = f"primary failed node {primary.node_id} ({primary.finish_reason}) → {res.blame}"

    if dry_run or res.blame != "mini_ork":
        return res

    # Record the bug. Only on first sighting: ``bug_report_sweep`` re-counts
    # every line still present in the sink, so re-emitting the same signature
    # would inflate ``frequency`` — repeats reuse the stored row instead.
    title = _bug_title(res.recipe, primary)
    fp = bug_report._fingerprint(title)
    res.bug_id = _bug_id_for_fingerprint(db_s, fp)
    if res.bug_id is None:
        run_dir = os.path.join(home_s, "runs", run_id)
        detail = "; ".join(e.detail for e in res.evidence)
        try:
            bug_report.bug_report_emit(
                "verifier",
                "high",
                title,
                description=f"{res.reason}\n\n{detail}\n\n{primary.log_excerpt[-1500:]}",
                suggested_fix=primary.log_excerpt[-1500:],
                observed_in=run_dir,
                confidence=0.6,
                run_dir=run_dir,
            )
            bug_report.bug_report_sweep(since=0, home=home_s)
        except Exception as exc:  # noqa: BLE001 — triage must never raise into a run
            res.reason += f"; emit failed: {exc}"
            return res
        res.bug_id = _bug_id_for_fingerprint(db_s, fp)

    if promote and res.bug_id is not None:
        try:
            bug_report.bug_report_promote(
                top=1,
                repo_root=root_s,
                recipe=res.fix_recipe,
                bug_ids=[res.bug_id],
            )
            res.epic_id, res.kickoff_path = _epic_for_bug(db_s, res.bug_id, root_s)
        except Exception as exc:  # noqa: BLE001
            res.reason += f"; promote failed: {exc}"
    return res


def _bug_id_for_fingerprint(db_path: str, fingerprint: str) -> int | None:
    con = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = con.execute("SELECT id FROM bug_reports WHERE fingerprint=?", (fingerprint,)).fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _epic_for_bug(db_path: str, bug_id: int, root: str) -> tuple[str | None, str | None]:
    con = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = con.execute(
            "SELECT promoted_to_epic_id FROM bug_reports WHERE id=?", (bug_id,)
        ).fetchone()
        epic_id = row[0] if row and row[0] else None
        if not epic_id:
            return None, None
        krow = con.execute("SELECT kickoff_path FROM epics WHERE id=?", (epic_id,)).fetchone()
        kickoff = krow[0] if krow and krow[0] else None
        return epic_id, (os.path.join(root, kickoff) if kickoff else None)
    except sqlite3.Error:
        return None, None
    finally:
        con.close()
