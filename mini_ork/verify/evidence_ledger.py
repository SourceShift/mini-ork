"""Evidence ledger — verdicts bound to the exact working-tree hash (I5).

A run's approval must rest on evidence tied to the code it judged. The
run-level ``<run_dir>/evidence-ledger.jsonl`` records, one JSON line per row:

``{ts, row_id, ac_id, probe, verdict, tree, log}``

``tree`` is the working-tree hash computed with :func:`tree_hash` — a temporary
index (``GIT_INDEX_FILE=<tmp> git add -A`` + ``write-tree``), because
``git write-tree`` alone hashes the index and misses in-place edits. A row whose
``tree`` differs from the tree being published is void: :func:`valid_rows`
excludes it.

:func:`publish_gate` backs an approval only with valid rows (AC2) and refuses a
run whose ``implementer-summary.json`` claims implementation while the tree has
not moved off ``pre-implementer-ref`` (AC3). It runs only behind
``MO_EVIDENCE_LEDGER`` — ``0`` (default, legacy behaviour byte-identical),
``shadow`` (write the ledger + evaluate + record would-blocks, never block) or
``1`` (enforce). Flag/mode parsing mirrors
:mod:`mini_ork.verify.probe_validity` exactly.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FLAG = "MO_EVIDENCE_LEDGER"
LEDGER_NAME = "evidence-ledger.jsonl"
GATE_NAME = "evidence-ledger-gate.json"
DEFAULT_GIT_TIMEOUT_S = 30.0

# Named reasons the gate reports (task_runs notes, evidence-ledger-gate.json,
# [BLOCK] lines).
NO_LEDGER = "no_ledger"
NO_VALID_ROWS = "no_valid_rows"
FAILING_ROWS = "failing_rows"
TREE_HASH_UNAVAILABLE = "tree_hash_unavailable"
IMPLEMENTER_CLAIM_UNBACKED = "implementer_claim_unbacked"


def mode(environ: dict | None = None) -> str:
    """``off`` (default), ``enforce`` (``MO_EVIDENCE_LEDGER=1``) or ``shadow``.

    Shadow writes the ledger and evaluates what it WOULD block
    (``evidence-ledger-gate.json`` + a task_runs note) but never blocks — the
    data clock for the flip decision. Mirrors :func:`probe_validity.mode`.
    """
    if environ is not None:
        raw = environ.get(FLAG, "0")
    else:
        from mini_ork.context import context_env

        raw = context_env(FLAG, "0")
    return {"1": "enforce", "shadow": "shadow"}.get(raw, "off")


def enabled(environ: dict | None = None) -> bool:
    """Enforcing only — shadow never changes a verdict."""
    return mode(environ) == "enforce"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tree_hash(target_repo: str, *, timeout: float = DEFAULT_GIT_TIMEOUT_S) -> str | None:
    """The working-tree hash, via a temporary index (AC1).

    ``git write-tree`` alone hashes the index; a temp index populated with
    ``git add -A`` captures in-place edits and untracked files. The real index
    and working tree are byte-identical afterwards (the temp index is removed in
    a ``finally``). Returns ``None`` when the repo is missing or any git call
    times out / fails — a timeout is "cannot evaluate", not a hang.
    """
    if not target_repo or not os.path.isdir(target_repo):
        return None
    tmpdir = tempfile.mkdtemp(prefix="evidence-ledger-index-")
    try:
        # The temp index must be a path that does not exist yet: git refuses a
        # 0-byte file ("index file smaller than expected") and creates it fresh.
        tmp_index = os.path.join(tmpdir, "index")
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = tmp_index
        try:
            # Seed it with a COPY of the real index: its stat cache lets `add -A`
            # re-hash only changed files. From an empty index every file is
            # re-hashed, which took >30 s on the researcher repo and timed out.
            # It also keeps force-added tracked files a fresh index would drop.
            real = subprocess.run(["git", "-C", target_repo, "rev-parse", "--git-path", "index"],
                                  timeout=timeout, capture_output=True, text=True)
            real_index = real.stdout.strip() if real.returncode == 0 else ""
            if real_index and not os.path.isabs(real_index):
                real_index = os.path.join(target_repo, real_index)
            if real_index and os.path.isfile(real_index):
                shutil.copyfile(real_index, tmp_index)
            subprocess.run(["git", "-C", target_repo, "add", "-A"], env=env,
                           timeout=timeout, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tree = subprocess.run(["git", "-C", target_repo, "write-tree"], env=env,
                                  timeout=timeout, check=True,
                                  capture_output=True, text=True).stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
        return tree or None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def append_row(run_dir: str, *, ac_id: str, probe: str, verdict: str, log: str,
               tree: str | None, row_id: str | None = None, ts: str | None = None) -> dict | None:
    """Append one row to ``<run_dir>/evidence-ledger.jsonl`` (append, never rewrite).

    Returns the row, or ``None`` when there is no run_dir. Callers treat a write
    failure as enrichment: they warn and continue, never fail the node.
    """
    if not run_dir:
        return None
    row = {
        "ts": ts or _utc_now(),
        "row_id": row_id or uuid.uuid4().hex,
        "ac_id": ac_id,
        "probe": probe,
        "verdict": verdict,
        "tree": tree,
        "log": log,
    }
    ledger = Path(run_dir) / LEDGER_NAME
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row


def load_rows(run_dir: str) -> list[dict]:
    """Every row in the ledger, malformed/blank lines skipped. ``[]`` when absent."""
    if not run_dir:
        return []
    ledger = Path(run_dir) / LEDGER_NAME
    if not ledger.is_file():
        return []
    rows: list[dict] = []
    for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def valid_rows(rows: list[dict], tree: str) -> list[dict]:
    """Rows bound to the exact ``tree`` — any other row is void (AC1)."""
    return [r for r in rows if r.get("tree") == tree]


def verifier_ac_id(evidence_path: str) -> str | None:
    """The ``ac_id`` a verifier declared in its one-line JSON verdict, if any.

    Verifier evidence may be prefixed by log lines; scan the last JSON-looking
    line like :func:`probe_validity._evidence_pass` does.
    """
    try:
        text = Path(evidence_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("ac_id"):
            return str(obj["ac_id"])
    return None


def _ref_tree_hash(target_repo: str, ref: str, *, timeout: float) -> str | None:
    """The tree object of ``ref``, or ``None`` when it cannot be resolved."""
    try:
        proc = subprocess.run(["git", "-C", target_repo, "rev-parse", f"{ref}^{{tree}}"],
                              capture_output=True, text=True, timeout=timeout, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout.strip() or None


def _ledger_verdict(rows: list[dict], valid: list[dict]) -> tuple[bool, str]:
    """AC2: at least one valid pass row, and no valid fail for the same ac_id."""
    if not rows:
        return False, NO_LEDGER
    if not valid:
        return False, NO_VALID_ROWS
    passes = {r.get("ac_id") for r in valid if r.get("verdict") == "pass"}
    fails = {r.get("ac_id") for r in valid if r.get("verdict") == "fail"}
    if not passes:
        # Valid rows exist but none pass: the only valid evidence is failure.
        return False, f"{FAILING_ROWS}: {','.join(sorted(fails))}" if fails else NO_VALID_ROWS
    conflicting = sorted(passes & fails)
    if conflicting:
        return False, f"{FAILING_ROWS}: {','.join(conflicting)}"
    return True, ""


def _implementer_claim_check(run_dir: str, target_repo: str, tree: str,
                             timeout: float) -> tuple[bool, str]:
    """AC3: a claimed implementation that left the tree at ``pre-implementer-ref``
    cannot publish (the claim is unbacked by any code change)."""
    summary = Path(run_dir) / "implementer-summary.json" if run_dir else None
    if not summary or not summary.is_file():
        return True, ""
    try:
        data = json.loads(summary.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, ""
    if not isinstance(data, dict):
        return True, ""
    claimed = str(data.get("status", "")) == "implemented" or (
        isinstance(data.get("files_changed"), list) and len(data["files_changed"]) > 0)
    if not claimed:
        return True, ""
    ref_file = Path(run_dir) / "pre-implementer-ref"
    if not ref_file.is_file():
        return True, ""
    try:
        base_ref = ref_file.read_text(encoding="utf-8").strip()
    except OSError:
        return True, ""
    if not base_ref or not target_repo:
        return True, ""
    base_tree = _ref_tree_hash(target_repo, base_ref, timeout=timeout)
    if base_tree is None:
        return True, ""  # cannot evaluate — don't block on an unprovable claim
    if tree == base_tree:
        return False, IMPLEMENTER_CLAIM_UNBACKED
    return True, ""


def publish_gate(*, run_dir: str, target_repo: str,
                 timeout: float = DEFAULT_GIT_TIMEOUT_S,
                 gate_mode: str = "enforce") -> tuple[bool, str, dict]:
    """``(ok, reason, report)``; writes ``<run_dir>/evidence-ledger-gate.json``.

    The report carries ``mode`` and ``would_block`` / ``reasons`` so shadow
    evaluations can be counted later. ``gate_mode`` is ``shadow`` or ``enforce``.
    """
    report: dict[str, Any] = {"flag": FLAG, "mode": gate_mode,
                              "rows_total": 0, "rows_valid": 0, "tree": None}
    ok, reason = True, ""
    tree = tree_hash(target_repo, timeout=timeout)
    report["tree"] = tree
    rows = load_rows(run_dir) if run_dir else []
    report["rows_total"] = len(rows)
    if tree is None:
        ok, reason = False, TREE_HASH_UNAVAILABLE
    else:
        valid = valid_rows(rows, tree)
        report["rows_valid"] = len(valid)
        ok, reason = _ledger_verdict(rows, valid)
        if ok:
            ok, reason = _implementer_claim_check(run_dir, target_repo, tree, timeout)
    report["ok"], report["reason"] = ok, reason
    report["would_block"] = not ok
    report["reasons"] = [reason] if reason else []
    if run_dir:
        with contextlib.suppress(OSError):
            (Path(run_dir) / GATE_NAME).write_text(
                json.dumps(report, indent=1, default=str), encoding="utf-8")
    return ok, reason, report
