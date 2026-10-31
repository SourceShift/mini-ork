"""The pre-lock review hands off a patch-id only for a cached, real review.

``_prereview`` runs the code-review gate before the merge lock, then passes the
reviewed patch-id to the in-lock push hook through
``MO_REVIEW_PRECHECKED_PATCH``. Two ways that handoff could lie:

* an ambient ``MO_REVIEW_PRECHECKED_PATCH`` in the operator's environment made
  the gate skip the very review ``_prereview`` exists to run, and
* a gate that exits 0 because the review was *unavailable* (simple tier, or a
  degraded panel) looked identical to one that reviewed, so the hook skipped a
  panel that had never run.

The gate writes ``<home>/state/pre-push-review-cache.json`` only for a
terminal, non-degraded review keyed by patch-id, so that cache is the evidence
the handoff is allowed to trust.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "mini_ork_worktree.py"

HANDOFF_MSG = (
    "[mo-worktree] pre-push review did not complete a cached review; "
    "the push hook will run it"
)

_CACHE_REL = Path("state") / "pre-push-review-cache.json"
_NO_CACHE = object()


class _GateRecorder:
    """Replace the driver's ``subprocess.run`` and record the gate invocation."""

    def __init__(self, rc: int) -> None:
        self.rc = rc
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        cmd = args[0] if args else kwargs.get("args")
        return subprocess.CompletedProcess(cmd, self.rc)


def _load_driver(monkeypatch, tmp_path) -> object:
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "primary"))
    monkeypatch.setenv("MINI_ORK_WORKTREES_DIR", str(tmp_path / "worktrees"))
    monkeypatch.setenv("MINI_ORK_OWNERSHIP_FILE", str(tmp_path / "ownership.tsv"))
    monkeypatch.setenv("MINI_ORK_WORKTREE_LOCK", str(tmp_path / "lockdir"))
    monkeypatch.delenv("MO_REVIEW_SKIP", raising=False)
    name = f"prereview_handoff_driver_{abs(hash(str(tmp_path)))}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _worktree(tmp_path: Path) -> str:
    wt = tmp_path / "wt"
    hook = wt / ".githooks" / "pre-push"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return str(wt)


def _write_cache(home: Path, payload) -> None:
    path = home / _CACHE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")


def _prepare(monkeypatch, tmp_path, *, gate_rc: int = 0, pid: str = "PID42",
             cache=_NO_CACHE):
    """Load the driver with a tmp ``MINI_ORK_HOME`` and stub the gate + git."""
    driver = _load_driver(monkeypatch, tmp_path)
    wt = _worktree(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    if cache is not _NO_CACHE:
        _write_cache(home, cache)
    recorder = _GateRecorder(gate_rc)
    monkeypatch.setattr(driver.subprocess, "run", recorder)
    monkeypatch.setattr(driver, "_patch_id", lambda *a, **k: pid)
    return driver, wt, recorder


# ── the ambient precheck never reaches the gate ────────────────────────────


def test_ambient_prechecked_patch_is_scrubbed_from_the_gate_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MO_REVIEW_PRECHECKED_PATCH", "abc")
    driver, wt, recorder = _prepare(
        monkeypatch, tmp_path,
        cache={"PID42": {"review_id": 1, "mode": "hybrid", "target": "main", "at": int(time.time())}},
    )

    assert driver._prereview(wt) == "PID42"
    assert recorder.calls, "the gate subprocess must have been launched"
    env = recorder.calls[0][1]["env"]
    assert "MO_REVIEW_PRECHECKED_PATCH" not in env


# ── exit 0 hands off only a cached review of this exact patch ──────────────


def test_exit0_with_fresh_cache_returns_the_patch_id(monkeypatch, tmp_path):
    driver, wt, _recorder = _prepare(
        monkeypatch, tmp_path,
        cache={"PID42": {"review_id": 1, "mode": "hybrid", "target": "main", "at": int(time.time())}},
    )
    assert driver._prereview(wt) == "PID42"


def test_exit0_without_a_cache_returns_empty_and_warns(monkeypatch, tmp_path, capsys):
    driver, wt, _recorder = _prepare(monkeypatch, tmp_path, cache=_NO_CACHE)

    assert driver._prereview(wt) == ""
    captured = capsys.readouterr()
    assert HANDOFF_MSG in (captured.out + captured.err)


def test_exit0_with_a_stale_cache_returns_empty(monkeypatch, tmp_path):
    stale_at = int(time.time()) - 8 * 86400
    driver, wt, _recorder = _prepare(
        monkeypatch, tmp_path,
        cache={"PID42": {"review_id": 1, "mode": "hybrid", "target": "main", "at": stale_at}},
    )
    assert driver._prereview(wt) == ""


def test_exit0_with_a_malformed_cache_returns_empty(monkeypatch, tmp_path):
    driver, wt, _recorder = _prepare(
        monkeypatch, tmp_path, cache="{not valid json")
    assert driver._prereview(wt) == ""


def test_exit0_with_empty_patch_id_returns_empty(monkeypatch, tmp_path):
    driver, wt, _recorder = _prepare(
        monkeypatch, tmp_path, pid="",
        cache={"PID42": {"review_id": 1, "mode": "hybrid", "target": "main", "at": int(time.time())}},
    )
    assert driver._prereview(wt) == ""


def test_exit0_with_a_cache_for_another_patch_returns_empty(monkeypatch, tmp_path):
    driver, wt, _recorder = _prepare(
        monkeypatch, tmp_path,
        cache={"SOME-OTHER-PATCH": {"review_id": 1, "mode": "hybrid", "target": "main", "at": int(time.time())}},
    )
    assert driver._prereview(wt) == ""


# ── non-zero gate exits are unchanged ──────────────────────────────────────


def test_reviewer_error_does_not_hand_off_a_patch_id(monkeypatch, tmp_path):
    driver, wt, _recorder = _prepare(
        monkeypatch, tmp_path, gate_rc=3,
        cache={"PID42": {"review_id": 1, "mode": "hybrid", "target": "main", "at": int(time.time())}},
    )
    assert driver._prereview(wt) == ""


# ── review 147 ──────────────────────────────────────────────────────────────


def test_a_cached_review_for_another_target_is_not_handed_off(monkeypatch, tmp_path):
    driver, wt, _ = _prepare(
        monkeypatch, tmp_path,
        cache={"PID42": {"review_id": 1, "mode": "hybrid", "target": "develop",
                         "at": int(time.time())}},
    )
    assert driver._prereview(wt) == ""


def test_a_cache_entry_without_a_review_id_is_not_handed_off(monkeypatch, tmp_path):
    driver, wt, _ = _prepare(
        monkeypatch, tmp_path,
        cache={"PID42": {"mode": "hybrid", "target": "main", "at": int(time.time())}},
    )
    assert driver._prereview(wt) == ""


def test_push_env_drops_an_ambient_precheck(monkeypatch, tmp_path):
    driver = _load_driver(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_REVIEW_PRECHECKED_PATCH", "AMBIENT")
    assert "MO_REVIEW_PRECHECKED_PATCH" not in driver._push_env("")
    assert driver._push_env("PID42")["MO_REVIEW_PRECHECKED_PATCH"] == "PID42"



# ── review 166: only a review written by THIS gate run is handed off ────────


def test_a_stale_entry_from_an_earlier_run_is_not_handed_off(monkeypatch, tmp_path, capsys):
    """Fresh within the TTL but written before this gate ran: it may be another
    mode's review (heuristic-only), which the driver cannot judge."""
    driver, wt, _ = _prepare(
        monkeypatch, tmp_path,
        cache={"PID42": {"review_id": 1, "mode": "heuristic", "target": "main",
                         "at": int(time.time()) - 60}},
    )
    assert driver._prereview(wt) == ""
    assert HANDOFF_MSG in capsys.readouterr().out


def test_an_entry_the_gate_writes_during_this_run_is_handed_off(monkeypatch, tmp_path):
    driver, wt, recorder = _prepare(monkeypatch, tmp_path)
    home = tmp_path / "home"

    def _gate_writes_its_review(*args, **kwargs):
        _write_cache(home, {"PID42": {"review_id": 7, "mode": "hybrid",
                                      "target": "main", "at": int(time.time())}})
        return recorder(*args, **kwargs)

    monkeypatch.setattr(driver.subprocess, "run", _gate_writes_its_review)
    assert driver._prereview(wt) == "PID42"
