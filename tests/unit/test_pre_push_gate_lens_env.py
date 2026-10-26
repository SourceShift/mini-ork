"""Unit tests for rev6-r2c: the review env reaches the LLM lenses, and a
hybrid review with no successful lens says so.

Hermetic: a tmp primary repo with a bare origin and a linked worktree, a
migrated state.db at ``<primary>/.mini-ork/state.db``, and fake ``review_fn`` /
dispatch functions — no LLM, no network, no credentials.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_ork
from mini_ork.review import gate as gate_mod
from mini_ork.review import lenses as lenses_mod
from mini_ork.stores.migrate import init_db

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]


# ── fixtures ────────────────────────────────────────────────────────────────


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, "git %s failed: %s" % (
        list(args), proc.stderr.strip(),
    )
    return proc.stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """A primary repo with a bare origin and a linked worktree."""
    base = tmp_path_factory.mktemp("gate-lens-env")
    primary = base / "primary"
    primary.mkdir()
    _git(primary, "init", "-b", "main")
    _git(primary, "config", "user.email", "test@example.invalid")
    _git(primary, "config", "user.name", "Lens Env Tests")
    (primary / "README.md").write_text("initial\n", encoding="utf-8")
    _git(primary, "add", "README.md")
    _git(primary, "commit", "-m", "initial")
    origin = base / "origin.git"
    _git(primary, "clone", "--bare", str(primary), str(origin))
    _git(primary, "remote", "add", "origin", str(origin))
    wt = base / "wt"
    _git(primary, "worktree", "add", str(wt), "-b", "wt/lens-env")
    (wt / "README.md").write_text("initial\n\nchange\n", encoding="utf-8")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-m", "change")
    return SimpleNamespace(
        primary=primary,
        origin=origin,
        wt=wt,
        base_sha=_git(primary, "rev-parse", "HEAD"),
        head_sha=_git(wt, "rev-parse", "HEAD"),
    )


@pytest.fixture(scope="module")
def state(repo):
    """The primary checkout's ``.mini-ork`` home with an initialized state.db."""
    home = repo.primary / ".mini-ork"
    db = home / "state.db"
    rc, out, err = init_db(db=str(db), root=str(REPO_ROOT))
    assert rc == 0, "init_db rc=%s: %s" % (rc, (err or out)[-500:])
    return SimpleNamespace(home=home, db=db)


@pytest.fixture(autouse=True)
def _isolated_review_state(state, monkeypatch):
    """Strip the caller's MINI_ORK_*/MO_REVIEW_* env and any earlier tier/cache."""
    for key in list(os.environ):
        if key.startswith("MINI_ORK_") or key.startswith("MO_REVIEW_"):
            monkeypatch.delenv(key, raising=False)
    (state.home / "config" / "review.yaml").unlink(missing_ok=True)
    (state.home / "state" / "pre-push-review-cache.json").unlink(missing_ok=True)
    yield


# ── helpers ─────────────────────────────────────────────────────────────────


def _gate_env(**extra):
    """A caller env with every MINI_ORK_*/MO_REVIEW_* key stripped, plus extras."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MINI_ORK_") and not key.startswith("MO_REVIEW_")
    }
    env.update(extra)
    return env


def _insert_review(db, *, head_sha, target, mode, verdict, lens_statuses=()):
    con = sqlite3.connect(str(db))
    try:
        cur = con.execute(
            "INSERT INTO pre_push_reviews"
            " (reviewed_at, source_sha, target_branch, reviewer_mode, verdict)"
            " VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), head_sha, target, mode, verdict),
        )
        review_id = int(cur.lastrowid)
        for index, status in enumerate(lens_statuses):
            con.execute(
                "INSERT INTO review_lens_runs"
                " (review_id, lens, status, issues, elapsed_ms, created_at)"
                " VALUES (?, ?, ?, 0, 1, ?)",
                (review_id, "llm.fake%d" % (index + 1), status, int(time.time())),
            )
        con.commit()
        return review_id
    finally:
        con.close()


def _review_fn(db, *, verdict="approve", lens_statuses=(), seen=None):
    """A fake review_run: records a review plus lens-run rows."""
    def fn(head_sha, target, **kwargs):
        review_id = _insert_review(
            kwargs.get("db", str(db)),
            head_sha=head_sha,
            target=target,
            mode=kwargs.get("mode", "heuristic"),
            verdict=verdict,
            lens_statuses=lens_statuses,
        )
        if seen is not None:
            seen.append({"review_id": review_id, "env": dict(os.environ)})
        return review_id
    return fn


def _write_tier(home, tier):
    config = home / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "review.yaml").write_text(
        "review:\n  tier: %s\n" % tier, encoding="utf-8",
    )


# ── 1. MINI_ORK_HOME reaches the review ─────────────────────────────────────


def test_resolved_home_reaches_review_fn(repo, state):
    seen = []
    fn = _review_fn(state.db, lens_statuses=("ok",), seen=seen)
    result = gate_mod.run_gate(
        target="main",
        cwd=str(repo.wt),
        base=repo.base_sha,
        env=_gate_env(MO_REVIEW_LLM_LENSES="1"),
        review_fn=fn,
        now=1_000_000,
    )
    assert result.rc == 0, result
    assert len(seen) == 1, "review_fn did not run"
    captured = seen[0]["env"]
    assert Path(captured.get("MINI_ORK_HOME", "")).resolve() == (
        repo.primary / ".mini-ork"
    ).resolve()
    assert captured.get("MO_REVIEW_LLM_LENSES") == "1"
    assert "MINI_ORK_HOME" not in os.environ, "process env must be restored"


def test_caller_set_home_is_unchanged(repo, state, monkeypatch):
    explicit = str(state.home) + "/"  # trailing slash: unchanged means verbatim
    monkeypatch.setenv("MINI_ORK_HOME", explicit)
    seen = []
    fn = _review_fn(state.db, lens_statuses=("ok",), seen=seen)
    result = gate_mod.run_gate(
        target="main",
        cwd=str(repo.wt),
        base=repo.base_sha,
        env=_gate_env(MINI_ORK_HOME=explicit, MO_REVIEW_LLM_LENSES="1"),
        review_fn=fn,
        now=1_100_000,
    )
    assert result.rc == 0, result
    assert len(seen) == 1, "review_fn did not run"
    assert seen[0]["env"].get("MINI_ORK_HOME") == explicit
    assert os.environ.get("MINI_ORK_HOME") == explicit


def test_empty_home_is_treated_as_unset(repo, state, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", "")
    seen = []
    fn = _review_fn(state.db, lens_statuses=("ok",), seen=seen)
    result = gate_mod.run_gate(
        target="main",
        cwd=str(repo.wt),
        base=repo.base_sha,
        env=_gate_env(MINI_ORK_HOME="", MO_REVIEW_LLM_LENSES="1"),
        review_fn=fn,
        now=1_200_000,
    )
    assert result.rc == 0, result
    assert len(seen) == 1, "review_fn did not run"
    captured = seen[0]["env"]
    assert Path(captured.get("MINI_ORK_HOME", "")).resolve() == (
        repo.primary / ".mini-ork"
    ).resolve()
    assert os.environ.get("MINI_ORK_HOME") == ""


# ── 2. all LLM lenses failed ────────────────────────────────────────────────


def test_all_lenses_failed_simple_tier_warns_and_passes(repo, state, capsys):
    fn = _review_fn(state.db, lens_statuses=("dispatch_failed", "dispatch_failed"))
    result = gate_mod.run_gate(
        target="main",
        cwd=str(repo.wt),
        base=repo.base_sha,
        env=_gate_env(MO_REVIEW_LLM_LENSES="1"),
        review_fn=fn,
        now=2_000_000,
    )
    out = capsys.readouterr().out
    assert result.rc == 0, (result, out)
    assert result.verdict == "approve", (result, out)
    assert (
        "pre-push review: ⚠ no LLM lens ran (all 2 failed); "
        "this verdict rests on heuristics only"
    ) in out, out


def test_all_lenses_failed_secure_tier_blocks(repo, state, capsys):
    _write_tier(state.home, "secure")
    seen = []
    fn = _review_fn(
        state.db,
        verdict="warn",  # warn would normally pass with rc 0
        lens_statuses=("dispatch_failed", "dispatch_failed"),
        seen=seen,
    )
    result = gate_mod.run_gate(
        target="main",
        cwd=str(repo.wt),
        base=repo.base_sha,
        env=_gate_env(MO_REVIEW_LLM_LENSES="1"),
        review_fn=fn,
        now=2_200_000,
    )
    out = capsys.readouterr().out
    assert len(seen) == 1, "review_fn did not run"
    assert result.rc == 1, (result, out)
    assert result.verdict == "unavailable", (result, out)
    assert result.reason == "secure tier: no LLM lens ran", result
    assert result.review_id == seen[0]["review_id"], result
    assert result.cached is False, result
    assert "no LLM lens ran (all 2 failed)" in out
    assert (
        "verdict=unavailable review_id=%d cached=no target=main"
        % seen[0]["review_id"]
    ) in out, out


def test_one_ok_lens_no_warning(repo, state, capsys):
    fn = _review_fn(state.db, lens_statuses=("ok", "dispatch_failed"))
    result = gate_mod.run_gate(
        target="main",
        cwd=str(repo.wt),
        base=repo.base_sha,
        env=_gate_env(MO_REVIEW_LLM_LENSES="1"),
        review_fn=fn,
        now=2_400_000,
    )
    out = capsys.readouterr().out
    assert result.rc == 0, (result, out)
    assert result.verdict == "approve", (result, out)
    assert "no LLM lens ran" not in out, out


def test_heuristic_mode_failed_rows_no_warning(repo, state, capsys):
    fn = _review_fn(state.db, lens_statuses=("dispatch_failed", "dispatch_failed"))
    result = gate_mod.run_gate(
        target="main",
        cwd=str(repo.wt),
        base=repo.base_sha,
        env=_gate_env(),  # MO_REVIEW_LLM_LENSES unset → heuristic mode
        review_fn=fn,
        now=2_500_000,
    )
    out = capsys.readouterr().out
    assert result.rc == 0, (result, out)
    assert result.verdict == "approve", (result, out)
    assert "no LLM lens ran" not in out, out


def test_policy_error_fails_closed(repo, state, capsys):
    fn = _review_fn(state.db, lens_statuses=("dispatch_failed", "dispatch_failed"))
    result = gate_mod.run_gate(
        target="main",
        cwd=str(repo.wt),
        base=repo.base_sha,
        env=_gate_env(
            MO_REVIEW_LLM_LENSES="1",
            MO_REVIEW_TIER="definitely-not-a-tier",
        ),
        review_fn=fn,
        now=2_600_000,
    )
    out = capsys.readouterr().out
    assert "no LLM lens ran (all 2 failed)" in out, out
    assert result.rc == 1, (result, out)
    assert result.verdict == "unavailable", (result, out)


# ── 3. cache hit ────────────────────────────────────────────────────────────


def test_cache_hit_all_failed_secure_tier_blocks(repo, state, capsys):
    _write_tier(state.home, "secure")
    review_id = _insert_review(
        state.db,
        head_sha=repo.head_sha,
        target="main",
        mode="hybrid",
        verdict="approve",
        lens_statuses=("dispatch_failed", "dispatch_failed"),
    )
    pid = gate_mod.patch_id(repo.base_sha, repo.head_sha, repo.wt)
    assert pid, "fixture failed to compute a patch-id"
    cache_path = state.home / "state" / "pre-push-review-cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({
            pid: {
                "review_id": review_id,
                "mode": "hybrid",
                "target": "main",
                "at": 3_000_000,
            },
        }),
        encoding="utf-8",
    )

    def boom(*args, **kwargs):
        raise AssertionError("cache miss: review_fn must not run")

    result = gate_mod.run_gate(
        target="main",
        cwd=str(repo.wt),
        base=repo.base_sha,
        env=_gate_env(MO_REVIEW_LLM_LENSES="1"),
        review_fn=boom,
        now=3_000_000,
    )
    out = capsys.readouterr().out
    assert "no LLM lens ran (all 2 failed)" in out, out
    assert "cache miss" not in out, out
    assert result.rc == 1, (result, out)
    assert result.verdict == "unavailable", (result, out)


# ── 4. lens timeout default ─────────────────────────────────────────────────


def test_default_llm_panel_timeout_is_600(monkeypatch):
    monkeypatch.delenv("MO_REVIEW_LENS_TIMEOUT_S", raising=False)
    monkeypatch.delenv("MO_REVIEW_PANEL", raising=False)
    calls = []

    def fake_dispatch(model, prompt, out_file, timeout, retries):
        calls.append((model, timeout))
        Path(out_file).write_text('{"issues": []}', encoding="utf-8")
        return 0

    lenses_mod._default_llm_panel(
        "+++ b/x.py\n+pass\n",
        dispatch_fn=fake_dispatch,
        prior=[],
        lens_sink=[],
    )
    assert calls, "no lens was dispatched"
    assert {timeout for _, timeout in calls} == {"600"}, calls


# ── 5. _llm_lenses_all_failed contract ──────────────────────────────────────

_LENS_RUNS_DDL = (
    "CREATE TABLE review_lens_runs ("
    " id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL,"
    " lens TEXT NOT NULL, status TEXT NOT NULL,"
    " issues INTEGER NOT NULL DEFAULT 0, elapsed_ms INTEGER,"
    " created_at INTEGER NOT NULL)"
)


def test_llm_lenses_all_failed_contract(tmp_path):
    all_failed = getattr(gate_mod, "_llm_lenses_all_failed", None)
    assert all_failed is not None

    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    assert all_failed(1, empty) == (False, 0)

    db = tmp_path / "rows.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute(_LENS_RUNS_DDL)
        con.executemany(
            "INSERT INTO review_lens_runs"
            " (review_id, lens, status, issues, elapsed_ms, created_at)"
            " VALUES (?, ?, ?, 0, 1, 1)",
            [
                (1, "llm.a", "dispatch_failed"),
                (1, "llm.b", "unparseable"),
                (2, "llm.a", "ok"),
                (2, "llm.b", "dispatch_failed"),
            ],
        )
        con.commit()
    finally:
        con.close()
    assert all_failed(1, db) == (True, 2)
    assert all_failed(2, db) == (False, 2)
    assert all_failed(3, db) == (False, 0)
