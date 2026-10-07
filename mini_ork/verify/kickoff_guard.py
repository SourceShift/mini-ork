"""Kickoff guard — a run must not rewrite its own contract.

Observed 2026-10-07 (sdd-i3-kickoff-contract r1): the implementer added a file
to its own kickoff's "Files in scope" to legitimise a scope deviation. The
reviewer happened to catch it, but nothing stopped it: kickoffs often live in
the target worktree, which every node can write.

The contract a run received is snapshotted before any node runs, as
``kickoff_sha256`` + ``kickoff_snapshot`` in ``context-pack.v2.json`` (written
by the planner via ``context_v2.pack_for_run``). At publish time this module
compares the kickoff file against that snapshot:

- ``intact``      — unchanged; pass.
- ``modified``    — the run changed its own kickoff; would block.
- ``deleted``     — the run removed its kickoff; would block.
- ``no_snapshot`` — no pack/snapshot (context v2 off, or a run that predates
  it); pass, recorded as unverified rather than proven intact.

Modes (``MO_KICKOFF_GUARD``): ``off`` | ``shadow`` (default — evaluate and
record ``kickoff-guard.json`` + a task_runs note, never block) | ``on`` (block
the publish and restore the kickoff from the snapshot, so the edited contract
is never committed). Shadow-before-flip, as with I1 probe validity.
"""
from __future__ import annotations

import difflib
import hashlib
import os

from mini_ork import context_v2
from mini_ork.context import context_env

FLAG = "MO_KICKOFF_GUARD"
MODES = ("off", "shadow", "on")
DEFAULT_MODE = "shadow"
REPORT = "kickoff-guard.json"
BLOCKING = ("modified", "deleted")


def mode() -> str:
    value = context_env(FLAG, DEFAULT_MODE).strip().lower()
    return value if value in MODES else DEFAULT_MODE


def evaluate(run_dir: str) -> dict:
    """Compare the run's kickoff file against the snapshot taken before any node ran."""
    pack = context_v2.load_pack(run_dir) if run_dir else {}
    path = pack.get("kickoff_path") or ""
    before = pack.get("kickoff_sha256") or ""
    if not path or not before:
        return {"status": "no_snapshot", "kickoff_path": path}
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return {"status": "deleted", "kickoff_path": path, "sha_before": before}
    except OSError as exc:
        return {"status": "no_snapshot", "kickoff_path": path, "error": str(exc)}
    after = hashlib.sha256(data).hexdigest()
    if after == before:
        return {"status": "intact", "kickoff_path": path, "sha_before": before}
    diff = list(difflib.unified_diff(
        (pack.get("kickoff_snapshot") or "").splitlines(),
        data.decode("utf-8", errors="replace").splitlines(),
        "kickoff (as received)", "kickoff (at publish)", lineterm="", n=1))
    return {"status": "modified", "kickoff_path": path, "sha_before": before,
            "sha_after": after, "diff": diff[:60]}


def _restore(report: dict, run_dir: str) -> bool:
    snapshot = context_v2.load_pack(run_dir).get("kickoff_snapshot")
    path = report.get("kickoff_path")
    if snapshot is None or not path:
        return False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(snapshot)
        return True
    except OSError:
        return False


def publish_gate(run_dir: str, *, gate_mode: str) -> tuple[bool, str, dict]:
    """``(ok, reason, report)``. ``gate_mode`` is ``shadow`` or ``on``; only
    ``on`` restores the kickoff. The report is written to ``kickoff-guard.json``."""
    report = evaluate(run_dir)
    report["mode"] = gate_mode
    ok = report["status"] not in BLOCKING
    reason = "" if ok else f"kickoff_{report['status']}_by_run"
    report["would_block"] = not ok
    if not ok and gate_mode == "on":
        report["restored"] = _restore(report, run_dir)
    if run_dir:
        context_v2.write_json(os.path.join(run_dir, REPORT), report)
    return ok, reason, report
