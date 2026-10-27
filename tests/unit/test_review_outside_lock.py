"""The pre-push review runs before the merge lock, never inside it (R2e).

Covers the three open findings on ``mini_ork/review/gate.py`` (a crashed panel
is degraded, a zero-lens-row cached review is re-run, the secure tier prints
the blocking evidence it used to swallow) and the driver handoff that lets the
in-lock push hook skip the review it just ran.

Hermetic: throwaway git repos under ``tmp_path`` (a bare origin, a seed and a
clone — never this checkout), a hand-built ``state.db``, and a fake
``review_fn``. No LLM, no network.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import mini_ork

T0 = 1_000_000


# ── process + git helpers ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _local_git_only(monkeypatch):
    """Drop this lane's process-level push block for the test's duration.

    The block rewrites every remote named ``origin`` through ``GIT_CONFIG_*``,
    including the throwaway bare repos built inside ``tmp_path``. Those local
    pushes never leave the machine; monkeypatch restores the env afterwards.
    """
    for key in list(os.environ):
        if key.startswith("GIT_CONFIG_"):
            monkeypatch.delenv(key, raising=False)


def _sh(*argv, cwd=None, check=True, input=None):
    proc = subprocess.run(
        [str(a) for a in argv],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
        input=input,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"{argv} failed rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


def _identity(repo):
    _sh("git", "-C", repo, "config", "user.email", "test@example.invalid")
    _sh("git", "-C", repo, "config", "user.name", "test")


def _commit(repo, name, content):
    Path(repo, name).write_text(content, encoding="utf-8")
    _sh("git", "-C", repo, "add", name)
    _sh("git", "-C", repo, "commit", "-m", f"test: {name}")


def _topology(tmp_path, *, slug="sluggy"):
    """Bare origin + seed + clone under ``<worktrees>/<slug>``."""
    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", origin)
    seed = tmp_path / "seed"
    _sh("git", "init", "-b", "main", seed)
    _identity(seed)
    _commit(seed, "base.txt", "base\n")
    _sh("git", "-C", seed, "remote", "add", "origin", str(origin))
    _sh("git", "-C", seed, "push", "-q", "origin", "main")
    worktrees = tmp_path / "worktrees"
    wt = worktrees / slug
    _sh("git", "clone", "-q", origin, wt)
    _identity(wt)
    return origin, worktrees, wt


def _branch_commit(wt, name, content, branch="wt/sluggy"):
    _sh("git", "-C", wt, "checkout", "-q", "-b", branch)
    _commit(wt, name, content)


def _patch_id_threedot(wt):
    diff = _sh("git", "-C", wt, "diff", "--no-color", "--no-renames",
               "origin/main...HEAD")
    if not diff.stdout.strip():
        return ""
    out = _sh("git", "-C", wt, "patch-id", "--stable", input=diff.stdout)
    tokens = out.stdout.split()
    return tokens[0] if tokens else ""


def _patch_id_merge_base(wt):
    base = _sh("git", "-C", wt, "merge-base", "HEAD", "origin/main").stdout.strip()
    head = _sh("git", "-C", wt, "rev-parse", "HEAD").stdout.strip()
    diff = _sh("git", "-C", wt, "diff", "--no-color", "--no-renames", base, head)
    if not diff.stdout.strip():
        return ""
    out = _sh("git", "-C", wt, "patch-id", "--stable", input=diff.stdout)
    tokens = out.stdout.split()
    return tokens[0] if tokens else ""


# ── gate fixtures (state.db shaped for the review gate) ─────────────────────


DB_SCHEMA = """
CREATE TABLE pre_push_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewed_at INTEGER NOT NULL DEFAULT 0,
    source_sha TEXT,
    target_branch TEXT,
    reviewer_mode TEXT,
    files_changed INTEGER,
    lines_added INTEGER,
    lines_removed INTEGER,
    issues_open INTEGER,
    issues_critical INTEGER,
    rationale TEXT,
    verdict TEXT
);
CREATE TABLE review_lens_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    lens TEXT,
    status TEXT,
    issues INTEGER,
    elapsed_ms INTEGER,
    created_at INTEGER
);
CREATE TABLE pre_push_review_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    lens TEXT,
    severity TEXT,
    file_path TEXT,
    line_no INTEGER,
    title TEXT,
    description TEXT,
    suggested_fix TEXT,
    status TEXT
);
"""


def _make_db(path, *, verdict="approve", lens_rows=()):
    con = sqlite3.connect(str(path))
    try:
        con.executescript(DB_SCHEMA)
        rid = int(con.execute(
            "INSERT INTO pre_push_reviews (reviewed_at, source_sha, target_branch,"
            " reviewer_mode, files_changed, lines_added, lines_removed, issues_open,"
            " issues_critical, rationale, verdict)"
            " VALUES (0, 'deadbeef', 'main', 'hybrid', 1, 1, 0, 0, 0, 'fixture', ?)",
            (verdict,),
        ).lastrowid)
        for lens, status in lens_rows:
            con.execute(
                "INSERT INTO review_lens_runs (review_id, lens, status, issues,"
                " elapsed_ms, created_at) VALUES (?, ?, ?, 0, NULL, 0)",
                (rid, lens, status),
            )
        con.commit()
    finally:
        con.close()
    return rid


def _write_cache(home, pid, review_id):
    cache = Path(home) / "state" / "pre-push-review-cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({
        pid: {"review_id": review_id, "mode": "hybrid", "target": "main", "at": T0}
    }), encoding="utf-8")


def _env(home, db, **extra):
    env = {"MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(db)}
    env.update(extra)
    return env


class _RecordingReview:
    def __init__(self, review_id):
        self.review_id = review_id
        self.calls = []

    def __call__(self, head, target, *, mode, base, cwd, db):
        self.calls.append({"head": head, "target": target, "mode": mode,
                           "base": base, "cwd": cwd, "db": db})
        return self.review_id


@pytest.fixture
def gate_repo(tmp_path):
    """A real repo one commit past ``origin/main`` plus isolated gate inputs."""
    origin, worktrees, wt = _topology(tmp_path)
    _branch_commit(wt, "change.txt", "spec change\n")
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "state.db"
    pid = _patch_id_merge_base(wt)
    assert pid, "fixture sanity: the branch must have a non-empty patch-id"
    return wt, home, db, pid


# ── the driver script ──────────────────────────────────────────────────────


def _driver_fixture(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "primary"))
    monkeypatch.setenv("MINI_ORK_WORKTREES_DIR", str(tmp_path / "worktrees"))
    monkeypatch.setenv("MINI_ORK_OWNERSHIP_FILE", str(tmp_path / "ownership.tsv"))
    monkeypatch.setenv("MINI_ORK_WORKTREE_LOCK", str(tmp_path / "lockdir"))
    monkeypatch.delenv("MO_REVIEW_SKIP", raising=False)
    script = Path(mini_ork.__file__).resolve().parents[1] / "scripts" / "mini_ork_worktree.py"
    spec = importlib.util.spec_from_file_location("review_outside_lock_driver", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def driver(monkeypatch, tmp_path):
    return _driver_fixture(monkeypatch, tmp_path)


# ── declared test 1/2: the precheck skip ───────────────────────────────────


def test_matching_precheck_skips_the_review(gate_repo, capsys):
    from mini_ork.review.gate import run_gate

    wt, home, db, pid = gate_repo
    review = _RecordingReview(1)
    result = run_gate(
        target="main", cwd=wt, now=T0,
        env=_env(home, db, MO_REVIEW_PRECHECKED_PATCH=pid),
        review_fn=review,
    )
    assert review.calls == [], "a matching precheck must not dispatch a review"
    assert result.rc == 0
    assert result.verdict == "skipped"
    assert result.review_id is None
    assert result.cached is False
    assert result.reason == f"reviewed before the merge lock (patch {pid[:12]})"
    out = capsys.readouterr().out
    assert "verdict=skipped" in out and "target=main" in out


def test_mismatched_precheck_runs_the_review(gate_repo):
    from mini_ork.review.gate import run_gate

    wt, home, db, pid = gate_repo
    rid = _make_db(db, verdict="approve")
    review = _RecordingReview(rid)
    result = run_gate(
        target="main", cwd=wt, now=T0,
        env=_env(home, db, MO_REVIEW_PRECHECKED_PATCH="0" * 40),
        review_fn=review,
    )
    assert len(review.calls) == 1
    assert result.rc == 0
    assert result.verdict == "approve"


# ── declared test 3: a crashed panel is degraded ───────────────────────────


def test_crashed_panel_counts_as_all_failed(tmp_path):
    from mini_ork.review.gate import _llm_lenses_all_failed

    db = tmp_path / "state.db"
    rid = _make_db(db, lens_rows=[("llm.glm", "ok"), ("llm.panel", "error")])
    assert _llm_lenses_all_failed(rid, db) == (True, 2)


def test_ok_rows_without_a_panel_error_are_not_degraded(tmp_path):
    from mini_ork.review.gate import _llm_lenses_all_failed

    db = tmp_path / "state.db"
    rid = _make_db(db, lens_rows=[("llm.glm", "ok"), ("llm.claude", "ok")])
    assert _llm_lenses_all_failed(rid, db) == (False, 2)


# ── declared test 4: a zero-row cached review re-runs ──────────────────────


def test_zero_row_cache_hit_re_runs(gate_repo):
    from mini_ork.review.gate import run_gate

    wt, home, db, pid = gate_repo
    rid = _make_db(db, verdict="approve", lens_rows=())
    _write_cache(home, pid, rid)
    review = _RecordingReview(rid)
    result = run_gate(
        target="main", cwd=wt, now=T0,
        env=_env(home, db, MO_REVIEW_LLM_LENSES="1"),
        review_fn=review,
    )
    assert len(review.calls) == 1, "zero lens rows is degraded and must re-run"
    assert result.cached is False


# ── declared test 5: the secure tier prints the blocking evidence ──────────


def test_secure_tier_degraded_block(gate_repo, capsys):
    from mini_ork.review.gate import run_gate

    wt, home, db, pid = gate_repo
    rid = _make_db(db, verdict="block", lens_rows=[("llm.glm", "error")])
    review = _RecordingReview(rid)
    result = run_gate(
        target="main", cwd=wt, now=T0,
        env=_env(home, db, MO_REVIEW_LLM_LENSES="1", MO_REVIEW_TIER="secure"),
        review_fn=review,
    )
    assert result.rc == 1
    assert result.verdict == "unavailable"
    assert result.reason.startswith("secure tier: no LLM lens ran")
    assert result.reason.endswith("(review verdict: block)")
    out = capsys.readouterr().out
    assert "dismiss one with: mini-ork review resolve" in out
    assert "pre-push review: block | " in out


def test_secure_tier_degraded_warn_prints_without_the_block_hint(gate_repo, capsys):
    from mini_ork.review.gate import run_gate

    wt, home, db, pid = gate_repo
    rid = _make_db(db, verdict="warn", lens_rows=[("llm.glm", "error")])
    review = _RecordingReview(rid)
    result = run_gate(
        target="main", cwd=wt, now=T0,
        env=_env(home, db, MO_REVIEW_LLM_LENSES="1", MO_REVIEW_TIER="secure"),
        review_fn=review,
    )
    assert result.rc == 1
    assert result.reason == "secure tier: no LLM lens ran"
    out = capsys.readouterr().out
    assert "pre-push review: warn | " in out
    assert "dismiss one with: mini-ork review resolve" not in out


# ── declared test 6: the driver's patch-id ─────────────────────────────────


def test_patch_id_is_empty_with_no_commits_beyond_origin(driver, tmp_path):
    origin, worktrees, wt = _topology(tmp_path)
    assert driver._patch_id(str(wt)) == ""


def test_patch_id_is_nonempty_and_stable_across_a_clean_rebase(driver, tmp_path):
    origin, worktrees, wt = _topology(tmp_path)
    _branch_commit(wt, "change.txt", "spec change\n")
    expected = _patch_id_threedot(wt)
    assert expected != ""
    before = driver._patch_id(str(wt))
    assert before == expected

    # Advance origin/main from a second clone, then rebase cleanly.
    other = tmp_path / "advance"
    _sh("git", "clone", "-q", origin, other)
    _identity(other)
    _commit(other, "upstream.txt", "upstream\n")
    _sh("git", "-C", other, "push", "-q", "origin", "main")
    _sh("git", "-C", wt, "fetch", "-q", "origin", "main")
    _sh("git", "-C", wt, "rebase", "origin/main")

    after = driver._patch_id(str(wt))
    assert after != ""
    assert after == before, "a clean rebase leaves the patch, hence the patch-id"


def test_patch_id_is_empty_on_git_failure(driver, tmp_path):
    orphan = tmp_path / "orphan"
    _sh("git", "init", "-b", "main", orphan)
    assert driver._patch_id(str(orphan)) == ""


def test_driver_has_no_top_level_mini_ork_import():
    import ast

    script = Path(mini_ork.__file__).resolve().parents[1] / "scripts" / "mini_ork_worktree.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not (alias.name == "mini_ork" or alias.name.startswith("mini_ork."))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not (module == "mini_ork" or module.startswith("mini_ork."))


# ── declared test 7: the in-lock push carries the prechecked patch ─────────


def _merge_and_capture_push(module, monkeypatch, tmp_path, reviewed_patch):
    wt = tmp_path / "worktrees" / "sluggy"
    wt.mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_TEST_CMD", "true")

    def fake_prereview(wt_arg):
        return reviewed_patch

    monkeypatch.setattr(module, "_prereview", fake_prereview)
    monkeypatch.setattr(module, "_oss_guard", lambda wt_arg, slug_arg: None)
    monkeypatch.setattr(module, "_gaming_guard", lambda wt_arg: None)
    monkeypatch.setattr(module, "_risk_guard", lambda wt_arg, slug_arg: False)
    monkeypatch.setattr(module, "_supersede_gates", lambda target_dir, note: [])

    pushes = []

    def fake_git(*args, cwd=None, check=True, capture=False, env=None):
        if "push" in args:
            pushes.append((args, env))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(module, "git", fake_git)
    module.merge_worktree(["sluggy"])
    assert pushes, "merge_worktree completed without ever pushing"
    return pushes[0]


def test_push_env_carries_prechecked_patch(driver, monkeypatch, tmp_path):
    pid = "f00dcafe" * 5
    args, env = _merge_and_capture_push(driver, monkeypatch, tmp_path, pid)
    assert "push" in args
    assert env is not None
    assert env.get("MO_REVIEW_PRECHECKED_PATCH") == pid


def test_push_env_has_no_prechecked_key_when_prereview_empty(driver, monkeypatch, tmp_path):
    args, env = _merge_and_capture_push(driver, monkeypatch, tmp_path, "")
    assert "push" in args
    assert env is None or "MO_REVIEW_PRECHECKED_PATCH" not in env


# ── _prereview records the reviewed patch only on a clean exit ─────────────


def _prereview_fixture(driver, monkeypatch, tmp_path, rc, pid):
    wt = tmp_path / "wt"
    hook = wt / ".githooks" / "pre-push"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.delenv("MO_REVIEW_SKIP", raising=False)
    monkeypatch.setattr(
        driver.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, rc, stdout=b"", stderr=b""),
    )
    monkeypatch.setattr(driver, "_patch_id", lambda path: pid)
    return str(wt)


def test_prereview_returns_the_reviewed_patch(driver, monkeypatch, tmp_path):
    # F2: a clean gate exit alone is not evidence of a review (an unavailable
    # review also exits 0); the gate's cache must hold this exact patch.
    import time

    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "state" / "pre-push-review-cache.json").write_text(json.dumps(
        {"abc123": {"review_id": 1, "mode": "hybrid", "target": "main",
                    "at": int(time.time())}}))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    wt = _prereview_fixture(driver, monkeypatch, tmp_path, 0, "abc123")
    assert driver._prereview(wt) == "abc123"


def test_prereview_returns_empty_when_the_reviewer_errors(driver, monkeypatch, tmp_path):
    wt = _prereview_fixture(driver, monkeypatch, tmp_path, 3, "abc123")
    assert driver._prereview(wt) == ""


# ── F1: a degraded fallback is never approved silently ─────────────────────


def test_degraded_fallback_with_zero_lens_rows_warns(gate_repo, capsys):
    """A cached zero-row review whose re-run raises leaves that review
    standing, but the hybrid check must still say no LLM lens ran."""
    from mini_ork.review.gate import run_gate

    wt, home, db, pid = gate_repo
    rid = _make_db(db, verdict="approve", lens_rows=())
    _write_cache(home, pid, rid)

    def raising_review(*args, **kwargs):
        raise RuntimeError("review lane down")

    result = run_gate(
        target="main", cwd=wt, now=T0,
        env=_env(home, db, MO_REVIEW_LLM_LENSES="1"),  # hybrid, simple tier
        review_fn=raising_review,
    )
    out = capsys.readouterr().out

    assert "no LLM lens ran" in out, out
    assert result.verdict == "approve"
    assert result.review_id == rid
    assert result.cached is True
    assert result.rc == 0, "simple tier: the warning does not block the push"
