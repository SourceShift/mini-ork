"""Unit tests for rev6-r2d: a degraded review is never cached, and an empty
LLM panel counts as "no LLM lens ran".

Hermetic: a tmp primary repo with a bare ``origin/main`` ref and a linked
worktree, a migrated state.db, and a fake ``review_fn`` that inserts a review
row with chosen ``review_lens_runs`` rows — no LLM, no network, no push.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_ork
from mini_ork.review import gate as gate_mod
from mini_ork.stores.migrate import init_db

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
T0 = 1_900_000_000  # pinned cache clock

FAILED_ROWS = (("llm.glm", "dispatch_failed"), ("llm.deepseek_flash", "dispatch_failed"))
OK_ROWS = (("llm.glm", "ok"),)


# ── fixtures ────────────────────────────────────────────────────────────────


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """A primary repo with a bare origin and a linked worktree."""
    base = tmp_path_factory.mktemp("gate-degraded")
    primary = base / "primary"
    origin = base / "origin.git"
    wt = base / "wt"
    subprocess.run(
        ["git", "init", "-b", "main", str(primary)],
        check=True, capture_output=True, text=True,
    )
    _git(primary, "config", "user.email", "test@example.invalid")
    _git(primary, "config", "user.name", "Degraded Tests")
    (primary / "file.txt").write_text("base\n", encoding="utf-8")
    _git(primary, "add", "file.txt")
    _git(primary, "-c", "commit.gpgsign=false", "commit", "-m", "base")
    subprocess.run(
        ["git", "init", "-b", "main", "--bare", str(origin)],
        check=True, capture_output=True, text=True,
    )
    _git(primary, "remote", "add", "origin", str(origin))
    # `git push` is disabled in some lanes (remapped to a stub), so the
    # remote-tracking ref is created directly — that is what the gate's
    # merge-base reads.
    _git(primary, "update-ref", "refs/remotes/origin/main",
         _git(primary, "rev-parse", "HEAD"))
    _git(primary, "worktree", "add", str(wt), "-b", "feature")
    (wt / "file.txt").write_text("base\nchange\n", encoding="utf-8")
    _git(wt, "add", "file.txt")
    _git(wt, "-c", "commit.gpgsign=false", "commit", "-m", "change")
    base_sha = _git(wt, "rev-parse", "origin/main")
    head_sha = _git(wt, "rev-parse", "HEAD")
    pid = gate_mod.patch_id(base_sha, head_sha, wt)
    assert pid, "fixture: the feature diff must yield a non-empty patch-id"
    return SimpleNamespace(
        primary=primary, origin=origin, wt=wt,
        base_sha=base_sha, head_sha=head_sha, pid=pid,
    )


@pytest.fixture(scope="module")
def state(repo, tmp_path_factory):
    db = tmp_path_factory.mktemp("gate-degraded-db") / "state.db"
    rc, out, err = init_db(db=str(db), root=str(REPO_ROOT))
    assert rc == 0, f"init_db rc={rc}: {(err or out)[-500:]}"
    return SimpleNamespace(db=db)


@pytest.fixture()
def fresh_db(tmp_path):
    """A per-test DB, for the test that drops ``review_lens_runs``."""
    db = tmp_path / "state.db"
    rc, out, err = init_db(db=str(db), root=str(REPO_ROOT))
    assert rc == 0, f"init_db rc={rc}: {(err or out)[-500:]}"
    return db


# ── helpers ─────────────────────────────────────────────────────────────────


def _connect(db):
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _insert_review(db, *, verdict="approve", mode="hybrid", lens_rows=()):
    """A review row plus chosen lens rows; returns the review id."""
    con = _connect(db)
    try:
        cur = con.execute(
            "INSERT INTO pre_push_reviews"
            " (reviewed_at, source_sha, target_branch, reviewer_mode, verdict)"
            " VALUES (?, ?, ?, ?, ?)",
            (T0, "degraded-sha", "main", mode, verdict),
        )
        review_id = int(cur.lastrowid)
        for lens, status in lens_rows:
            con.execute(
                "INSERT INTO review_lens_runs"
                " (review_id, lens, status, issues, elapsed_ms, created_at)"
                " VALUES (?, ?, ?, 0, 0, ?)",
                (review_id, lens, status, T0),
            )
        con.commit()
        return review_id
    finally:
        con.close()


def _review_fn(*, verdict="approve", lens_rows=()):
    """A fake ``review_run``: inserts the chosen review, counts its calls."""
    calls = []

    def fn(head_sha, target, mode="heuristic", base="", cwd=".", db=None):
        calls.append({"head_sha": head_sha, "target": target, "mode": mode})
        return _insert_review(db, verdict=verdict, mode=mode, lens_rows=lens_rows)

    return fn, calls


def _gate_env(home, db, *, hybrid=True, secure=False):
    home.mkdir(parents=True, exist_ok=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MINI_ORK_") and not key.startswith("MO_REVIEW_")
    }
    env.update({"MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(db)})
    if hybrid:
        env["MO_REVIEW_LLM_LENSES"] = "1"
    if secure:
        config = home / "config"
        config.mkdir(parents=True, exist_ok=True)
        (config / "review.yaml").write_text("review:\n  tier: secure\n", encoding="utf-8")
    return env


def _cache_path(home):
    return Path(home) / gate_mod.CACHE_REL


def _read_cache(home):
    path = _cache_path(home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache_entry(home, pid, review_id, *, mode="hybrid", target="main"):
    path = _cache_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = _read_cache(home)
    cache[pid] = {
        "review_id": int(review_id), "mode": mode, "target": target, "at": T0,
    }
    path.write_text(json.dumps(cache), encoding="utf-8")


def _run(repo, env, review_fn):
    return gate_mod.run_gate(
        target="main",
        cwd=str(repo.wt),
        env=env,
        head="HEAD",
        base="",
        review_fn=review_fn,
        now=T0,
    )


# ── 1. a degraded review is not cached ─────────────────────────────────────


def test_degraded_review_is_not_cached_and_reruns(repo, state, tmp_path):
    home = tmp_path / "home"
    env = _gate_env(home, state.db)
    review_fn, calls = _review_fn(verdict="approve", lens_rows=FAILED_ROWS)

    first = _run(repo, env, review_fn)
    assert first.cached is False
    assert repo.pid not in _read_cache(home)

    second = _run(repo, env, review_fn)
    assert len(calls) == 2, "a degraded review must not be reused from the cache"
    assert second.cached is False
    assert second.review_id != first.review_id
    assert repo.pid not in _read_cache(home)


# ── 2. a healthy review is still cached ────────────────────────────────────


def test_healthy_review_is_cached_as_before(repo, state, tmp_path, capsys):
    home = tmp_path / "home"
    env = _gate_env(home, state.db)
    review_fn, calls = _review_fn(verdict="approve", lens_rows=OK_ROWS)

    first = _run(repo, env, review_fn)
    assert first.cached is False
    assert "no LLM lens ran" not in capsys.readouterr().out
    cache = _read_cache(home)
    assert cache[repo.pid]["review_id"] == first.review_id

    second = _run(repo, env, review_fn)
    assert len(calls) == 1, "a healthy review must still be a cache hit"
    assert second.cached is True
    assert second.review_id == first.review_id


# ── 3. a degraded cached entry is a cache miss ─────────────────────────────


def test_degraded_cached_entry_reruns(repo, state, tmp_path):
    home = tmp_path / "home"
    env = _gate_env(home, state.db)
    degraded_rid = _insert_review(state.db, verdict="approve", lens_rows=FAILED_ROWS)
    _write_cache_entry(home, repo.pid, degraded_rid)

    review_fn, calls = _review_fn(verdict="approve", lens_rows=OK_ROWS)
    result = _run(repo, env, review_fn)
    assert len(calls) == 1, "a cached degraded entry must be treated as a miss"
    assert result.cached is False
    assert result.review_id != degraded_rid
    # The healthy retry is the normal fresh path, so it repopulates the cache:
    # the outage no longer pins the patch for the TTL.
    assert _read_cache(home)[repo.pid]["review_id"] == result.review_id


def test_degraded_cached_entry_falls_back_when_the_rerun_raises(
    repo, state, tmp_path, capsys
):
    """A re-run that cannot happen leaves the earlier degraded review standing."""
    home = tmp_path / "home"
    env = _gate_env(home, state.db, secure=True)
    degraded_rid = _insert_review(state.db, verdict="approve", lens_rows=FAILED_ROWS)
    _write_cache_entry(home, repo.pid, degraded_rid)

    def boom(*_args, **_kwargs):
        raise AssertionError("lens backend exploded")

    result = _run(repo, env, boom)
    out = capsys.readouterr().out
    assert "no LLM lens ran (all 2 failed)" in out, out
    assert "lens backend exploded" not in out, out
    assert result.rc == 1, (result, out)
    assert result.verdict == "unavailable", (result, out)
    assert result.review_id == degraded_rid, result


# ── 4/5. an empty panel is degraded ────────────────────────────────────────


def test_empty_panel_fresh_simple_tier(repo, state, tmp_path, capsys):
    home = tmp_path / "home"
    env = _gate_env(home, state.db)
    review_fn, _calls = _review_fn(verdict="approve", lens_rows=())

    result = _run(repo, env, review_fn)
    out = capsys.readouterr().out
    assert "no LLM lens ran (the panel is empty)" in out, out
    assert result.rc == 0, "simple tier: rc still follows the verdict"
    assert repo.pid not in _read_cache(home)


def test_empty_panel_secure_tier_blocks(repo, state, tmp_path):
    home = tmp_path / "home"
    env = _gate_env(home, state.db, secure=True)
    review_fn, _calls = _review_fn(verdict="approve", lens_rows=())

    result = _run(repo, env, review_fn)
    assert result.rc == 1
    assert result.verdict == "unavailable"
    assert result.reason == "secure tier: no LLM lens ran"
    assert repo.pid not in _read_cache(home)


# ── 6. a missing table keeps the old behaviour ─────────────────────────────


def test_missing_table_no_warning_and_cache_written(repo, fresh_db, tmp_path, capsys):
    con = _connect(fresh_db)
    con.execute("DROP TABLE review_lens_runs")
    con.commit()
    con.close()

    home = tmp_path / "home"
    env = _gate_env(home, fresh_db)
    review_fn, calls = _review_fn(verdict="approve", lens_rows=())

    _run(repo, env, review_fn)
    assert "no LLM lens ran" not in capsys.readouterr().out
    assert repo.pid in _read_cache(home)

    second = _run(repo, env, review_fn)
    assert len(calls) == 1
    assert second.cached is True


# ── 7. heuristic mode is untouched ─────────────────────────────────────────


def test_heuristic_mode_never_warns_and_caches(repo, state, tmp_path, capsys):
    home = tmp_path / "home"
    env = _gate_env(home, state.db, hybrid=False)
    review_fn, calls = _review_fn(verdict="approve", lens_rows=())

    _run(repo, env, review_fn)
    assert "no LLM lens ran" not in capsys.readouterr().out
    assert repo.pid in _read_cache(home)

    second = _run(repo, env, review_fn)
    assert len(calls) == 1
    assert second.cached is True


# ── _llm_lenses_all_failed(fresh=...) contract ─────────────────────────────


def test_all_failed_fresh_true_with_no_rows_but_table_present(state):
    assert gate_mod._llm_lenses_all_failed(999_002, state.db, fresh=True) == (True, 0)


def test_all_failed_fresh_false_with_no_rows_is_false(state):
    assert gate_mod._llm_lenses_all_failed(999_002, state.db) == (False, 0)
    assert gate_mod._llm_lenses_all_failed(999_002, state.db, fresh=False) == (False, 0)


def test_all_failed_missing_table_is_false_even_fresh(fresh_db):
    con = _connect(fresh_db)
    con.execute("DROP TABLE review_lens_runs")
    con.commit()
    con.close()
    assert gate_mod._llm_lenses_all_failed(1, fresh_db, fresh=True) == (False, 0)
    assert gate_mod._llm_lenses_all_failed(1, fresh_db) == (False, 0)


def test_all_failed_counts_rows_with_and_without_fresh(state):
    failed = _insert_review(state.db, lens_rows=FAILED_ROWS)
    assert gate_mod._llm_lenses_all_failed(failed, state.db, fresh=True) == (True, 2)
    assert gate_mod._llm_lenses_all_failed(failed, state.db) == (True, 2)

    mixed = _insert_review(
        state.db, lens_rows=(("llm.glm", "ok"), ("llm.deepseek_flash", "error"))
    )
    assert gate_mod._llm_lenses_all_failed(mixed, state.db, fresh=True) == (False, 2)


def test_warning_text_all_n_failed(repo, state, tmp_path, capsys):
    home = tmp_path / "home"
    env = _gate_env(home, state.db)
    review_fn, _calls = _review_fn(verdict="approve", lens_rows=FAILED_ROWS)
    _run(repo, env, review_fn)
    assert "no LLM lens ran (all 2 failed)" in capsys.readouterr().out


# ── 8. an unreadable lens record is degraded, not clean ────────────────────


def _raise_locked(*_args, **_kwargs):
    """Stand-in for ``_lens_runs`` when the state DB cannot be read."""
    raise sqlite3.OperationalError("database is locked")


def test_all_failed_read_error_is_degraded(monkeypatch, state):
    monkeypatch.setattr(gate_mod, "_lens_runs", _raise_locked)
    assert gate_mod._llm_lenses_all_failed(1, state.db) == (True, 0)
    assert gate_mod._llm_lenses_all_failed(1, state.db, fresh=True) == (True, 0)


def test_read_error_review_is_not_cached(monkeypatch, repo, state, tmp_path):
    home = tmp_path / "home"
    env = _gate_env(home, state.db)
    # Every lens row is `ok`; only the *read* fails.
    review_fn, calls = _review_fn(verdict="approve", lens_rows=OK_ROWS)
    monkeypatch.setattr(gate_mod, "_lens_runs", _raise_locked)

    first = _run(repo, env, review_fn)
    assert first.cached is False
    assert repo.pid not in _read_cache(home)

    second = _run(repo, env, review_fn)
    assert len(calls) == 2, "an unreadable review must not be reused from the cache"
    assert second.cached is False
    assert repo.pid not in _read_cache(home)


def test_read_error_secure_tier_blocks(monkeypatch, repo, state, tmp_path):
    home = tmp_path / "home"
    env = _gate_env(home, state.db, secure=True)
    review_fn, _calls = _review_fn(verdict="approve", lens_rows=OK_ROWS)
    monkeypatch.setattr(gate_mod, "_lens_runs", _raise_locked)

    result = _run(repo, env, review_fn)
    assert result.rc == 1
    assert result.verdict == "unavailable"
    assert result.reason == "secure tier: no LLM lens ran"
    assert repo.pid not in _read_cache(home)
