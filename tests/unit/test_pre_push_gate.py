"""Unit tests for ``mini_ork.review.gate`` and the Layer 3 wiring (rev6 R2a).

Hermetic: a tmp git repo with a bare ``origin/main`` ref and a linked worktree,
a migrated state.db at ``<primary>/.mini-ork/state.db``, and a fake
``review_fn`` — no LLM, no network, no push.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import mini_ork
import mini_ork.pre_push_review
from mini_ork.review import gate
from mini_ork.review.lenses import _default_llm_panel

REPO = Path(mini_ork.__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "mini_ork_worktree.py"
T0 = 1_700_000_000


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class FakeReview:
    """Insert a ``pre_push_reviews`` row with the chosen verdict; count calls."""

    def __init__(self, verdict: str = "approve"):
        self.verdict = verdict
        self.calls: list[dict] = []

    def __call__(self, head_sha, target, *, mode, base, cwd, db):
        self.calls.append(
            {"head_sha": head_sha, "target": target, "mode": mode,
             "base": base, "cwd": cwd, "db": db}
        )
        con = sqlite3.connect(db)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            cur = con.execute(
                "INSERT INTO pre_push_reviews "
                "(reviewed_at, source_sha, target_branch, reviewer_mode, verdict) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), head_sha, target, mode, self.verdict),
            )
            con.commit()
            assert cur.lastrowid is not None
            self.calls[-1]["rid"] = int(cur.lastrowid)
            return int(cur.lastrowid)
        finally:
            con.close()


class Case:
    """tmp repo + linked worktree; ``init_db()`` creates the primary DB."""

    def __init__(self, root: Path):
        self.root = root
        self.primary = root / "primary"
        self.wt = root / "wt"
        _git(root, "init", "-b", "main", str(self.primary))
        _write(self.primary / "README.md", "base\n")
        _git(self.primary, "add", "README.md")
        _git(self.primary, "commit", "-m", "base")
        _git(self.primary, "update-ref", "refs/remotes/origin/main",
             _git(self.primary, "rev-parse", "HEAD").stdout.strip())
        _git(self.primary, "worktree", "add", str(self.wt), "-b", "feature-x")
        _write(self.wt / "change.txt", "a change\n")
        _git(self.wt, "add", "change.txt")
        _git(self.wt, "commit", "-m", "feature")
        self.env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("MINI_ORK_", "MO_REVIEW_"))
        }
        self.home = self.primary / ".mini-ork"
        self.db = self.home / "state.db"

    def init_db(self):
        from mini_ork.stores.migrate import init_db

        rc, *_ = init_db(db=str(self.db), root=str(REPO))
        assert rc == 0
        return self.db

    def advance_origin_main(self):
        _write(self.primary / "upstream.txt", f"upstream {time.time()}\n")
        _git(self.primary, "add", "upstream.txt")
        _git(self.primary, "commit", "-m", "upstream")
        _git(self.primary, "update-ref", "refs/remotes/origin/main",
             _git(self.primary, "rev-parse", "HEAD").stdout.strip())

    def current_pid(self) -> str:
        head = _git(self.wt, "rev-parse", "HEAD").stdout.strip()
        base = _git(self.wt, "merge-base", "HEAD", "origin/main").stdout.strip()
        return gate.patch_id(base, head, str(self.wt))


@pytest.fixture
def case(tmp_path):
    return Case(tmp_path)


# ── constants / result shape ───────────────────────────────────────────────


def test_cache_constants():
    assert gate.CACHE_REL == "state/pre-push-review-cache.json"
    assert gate.CACHE_TTL_S == 7 * 86400


def test_gate_result_shape():
    result = gate.GateResult(rc=0, verdict="approve", review_id=None,
                             cached=False, reason="x")
    assert (result.rc, result.verdict, result.review_id, result.cached) == (
        0, "approve", None, False)
    with pytest.raises(Exception):
        result.rc = 1  # frozen


# ── root / home / db resolution ────────────────────────────────────────────


def test_primary_root_resolves_linked_worktree(case):
    assert Path(gate.primary_root(str(case.wt))).resolve() == case.primary.resolve()
    assert Path(gate.primary_root(str(case.primary))).resolve() == case.primary.resolve()


def test_resolve_home_and_db_from_a_worktree(case):
    assert Path(gate.resolve_home(str(case.wt), case.env)).resolve() == case.home.resolve()
    assert Path(gate.resolve_db(str(case.wt), case.env)).resolve() == case.db.resolve()


def test_resolve_home_and_db_honour_env(case, tmp_path):
    home, db = tmp_path / "h", tmp_path / "x.db"
    assert gate.resolve_home(str(case.wt), {**case.env, "MINI_ORK_HOME": str(home)}) == home
    assert gate.resolve_db(str(case.wt), {**case.env, "MINI_ORK_DB": str(db)}) == db
    assert gate.resolve_home(str(case.wt), {**case.env, "MINI_ORK_HOME": ""}) == case.home


# ── local.sh env merge ─────────────────────────────────────────────────────


def test_load_review_env_sources_local_sh(case):
    _write(case.home / "config" / "local.sh",
           "# comment\n\nexport MO_REVIEW_LLM_LENSES='1'\n"
           'MO_REVIEW_PANEL="glm minimax deepseek_flash"\nOTHER=nope\n')
    out = gate.load_review_env(case.home, dict(case.env))
    assert out["MO_REVIEW_LLM_LENSES"] == "1"
    assert out["MO_REVIEW_PANEL"] == "glm minimax deepseek_flash"
    assert "OTHER" not in out


def test_load_review_env_does_not_override_or_mutate(case):
    _write(case.home / "config" / "local.sh", "MO_REVIEW_PANEL='from file'\n")
    env = dict(case.env)
    env["MO_REVIEW_PANEL"] = "from env"
    out = gate.load_review_env(case.home, env)
    assert out["MO_REVIEW_PANEL"] == "from env"
    assert env["MO_REVIEW_PANEL"] == "from env"


def test_run_gate_reads_llm_flag_from_local_sh(case):
    _write(case.home / "config" / "local.sh", "export MO_REVIEW_LLM_LENSES='1'\n")
    case.init_db()
    fake = FakeReview("approve")
    gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                  review_fn=fake, now=T0)
    assert fake.calls and fake.calls[0]["mode"] == "hybrid"


# ── patch id ───────────────────────────────────────────────────────────────


def test_patch_id_is_stable_and_empty_for_no_diff(case):
    head = _git(case.wt, "rev-parse", "HEAD").stdout.strip()
    base = _git(case.wt, "merge-base", "HEAD", "origin/main").stdout.strip()
    pid = gate.patch_id(base, head, str(case.wt))
    assert pid and " " not in pid
    assert gate.patch_id(head, head, str(case.wt)) == ""


# ── verdict → rc ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("verdict", "rc"), [("approve", 0), ("warn", 0),
                                             ("block", 1)])
def test_verdict_maps_to_rc(case, verdict, rc):
    case.init_db()
    fake = FakeReview(verdict)
    result = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                           review_fn=fake, now=T0)
    assert (result.verdict, result.rc, result.cached) == (verdict, rc, False)
    assert result.review_id == fake.calls[-1]["rid"]


def test_review_fn_receives_the_resolved_base(case):
    case.init_db()
    fake = FakeReview("approve")
    gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                  review_fn=fake, now=T0)
    (call,) = fake.calls
    expected_base = _git(case.wt, "merge-base", "HEAD", "origin/main").stdout.strip()
    assert call["base"] == expected_base
    assert Path(call["cwd"]).resolve() == case.wt.resolve()
    assert Path(call["db"]).resolve() == case.db.resolve()


def test_block_prints_prefixed_lines_and_summary(case, capsys):
    case.init_db()
    gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                  review_fn=FakeReview("block"), now=T0)
    text = capsys.readouterr()
    lines = [ln for ln in (text.out + text.err).splitlines() if ln.strip()]
    assert lines and all(ln.startswith("pre-push review:") for ln in lines)
    assert re.search(
        r"^pre-push review: verdict=block review_id=\d+ cached=no target=main$",
        text.out + text.err, re.MULTILINE,
    )


# ── cache ──────────────────────────────────────────────────────────────────


def test_same_patch_after_rebase_is_cached(case):
    case.init_db()
    fake = FakeReview("approve")
    first = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                          review_fn=fake, now=T0)
    case.advance_origin_main()
    _git(case.wt, "rebase", "origin/main")
    second = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                           review_fn=fake, now=T0)
    assert second.cached is True
    assert second.review_id == first.review_id
    assert len(fake.calls) == 1


def test_changed_patch_is_reviewed_again(case):
    case.init_db()
    fake = FakeReview("approve")
    gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                  review_fn=fake, now=T0)
    with (case.wt / "change.txt").open("a") as handle:
        handle.write("more\n")
    _git(case.wt, "add", "change.txt")
    _git(case.wt, "commit", "-m", "grow")
    second = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                           review_fn=fake, now=T0)
    assert len(fake.calls) == 2 and second.cached is False


def test_cache_entry_is_pid_keyed_and_ttl_checked(case):
    case.init_db()
    fake = FakeReview("warn")
    result = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                           review_fn=fake, now=T0)
    entry = json.loads((case.home / gate.CACHE_REL).read_text())[case.current_pid()]
    assert entry == {"review_id": result.review_id, "mode": "heuristic",
                     "target": "main", "at": T0}
    expired = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                            review_fn=fake, now=T0 + gate.CACHE_TTL_S + 1)
    assert expired.cached is False and len(fake.calls) == 2


def test_non_terminal_verdict_is_not_cached(case):
    case.init_db()
    result = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                           review_fn=FakeReview("aborted"), now=T0)
    assert result.verdict == "unavailable" and result.rc == 0
    assert not (case.home / gate.CACHE_REL).exists()


# ── skip / empty ───────────────────────────────────────────────────────────


def test_skip_short_circuits(case):
    case.init_db()
    fake = FakeReview("approve")
    result = gate.run_gate(target="main", cwd=str(case.wt),
                           env={**case.env, "MO_REVIEW_SKIP": "1"},
                           review_fn=fake, now=T0)
    assert result.verdict == "skipped" and result.rc == 0
    assert result.review_id is None and fake.calls == []


def test_no_diff_is_empty(case, tmp_path):
    case.init_db()
    clean = tmp_path / "clean"
    _git(case.primary, "worktree", "add", str(clean), "-b", "even-with-main")
    fake = FakeReview("approve")
    result = gate.run_gate(target="main", cwd=str(clean), env=dict(case.env),
                           review_fn=fake, now=T0)
    assert (result.verdict, result.rc, result.reason) == (
        "empty", 0, "no diff to review")
    assert fake.calls == []


# ── unavailable + tiers ────────────────────────────────────────────────────


def test_missing_db_simple_tier_warns_and_proceeds(tmp_path, capsys):
    case = Case(tmp_path)
    result = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                           review_fn=FakeReview("approve"), now=T0)
    assert result.verdict == "unavailable" and result.rc == 0
    out = capsys.readouterr()
    warnings = [ln for ln in (out.out + out.err).splitlines()
                if ln.startswith("pre-push review:") and "⚠" in ln]
    assert warnings


def test_missing_db_secure_tier_fails_closed(tmp_path):
    case = Case(tmp_path)
    _write(case.home / "config" / "review.yaml", "review:\n  tier: secure\n")
    result = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                           review_fn=FakeReview("approve"), now=T0)
    assert result.verdict == "unavailable" and result.rc == 1
    assert result.reason == (
        "secure tier: the code review could not run, so the push fails closed")


def test_raising_review_follows_the_tier_rule(tmp_path):
    case = Case(tmp_path)
    case.init_db()

    def boom(*_args, **_kwargs):
        raise RuntimeError("lens backend exploded")

    simple = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                           review_fn=boom, now=T0)
    assert simple.verdict == "unavailable" and simple.rc == 0
    _write(case.home / "config" / "review.yaml", "review:\n  tier: secure\n")
    secure = gate.run_gate(target="main", cwd=str(case.wt), env=dict(case.env),
                           review_fn=boom, now=T0)
    assert secure.verdict == "unavailable" and secure.rc == 1


# ── lens panel ─────────────────────────────────────────────────────────────


def test_llm_panel_runs_concurrently_in_panel_order(monkeypatch):
    monkeypatch.setenv("MO_REVIEW_PANEL", "lens_a lens_b lens_c")
    calls: list[str] = []

    def dispatch(model, _prompt, out_file, _timeout, _turns):
        calls.append(model)
        time.sleep(1.0)
        Path(out_file).write_text(json.dumps(
            {"issues": [{"title": f"issue-from-{model}", "severity": "low"}]}))
        return 0

    started = time.monotonic()
    issues = _default_llm_panel("diff --git a/x b/x\n", dispatch_fn=dispatch)
    assert time.monotonic() - started < 2.5
    assert sorted(calls) == ["lens_a", "lens_b", "lens_c"]
    assert [issue["title"] for issue in issues] == [
        "issue-from-lens_a", "issue-from-lens_b", "issue-from-lens_c"]


def test_default_panel_is_the_live_lanes(monkeypatch):
    monkeypatch.delenv("MO_REVIEW_PANEL", raising=False)
    calls: list[str] = []

    def dispatch(model, _prompt, out_file, _timeout, _turns):
        calls.append(model)
        Path(out_file).write_text('{"issues":[]}')
        return 0

    assert _default_llm_panel("diff", dispatch_fn=dispatch) == []
    assert sorted(calls) == ["deepseek_flash", "glm"]


# ── CLI / hook / driver wiring ─────────────────────────────────────────────


def test_gate_help_exits_zero_and_lists_flags():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MINI_ORK_", "MO_REVIEW_"))}
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.run(
        [sys.executable, "-m", "mini_ork.pre_push_review", "gate", "--help"],
        cwd=str(REPO), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    for flag in ("--target", "--head", "--base", "--cwd"):
        assert flag in proc.stdout
    assert "main" in proc.stdout and "feature" in proc.stdout


def test_pre_push_review_dispatches_the_gate(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_gate_main(argv=None):
        seen.append(list(argv or []))
        return 7

    monkeypatch.setattr(gate, "main", fake_gate_main)
    rc = mini_ork.pre_push_review.main(
        ["gate", "--target", "feature", "--cwd", str(tmp_path)])
    assert rc == 7
    assert seen == [["--target", "feature", "--cwd", str(tmp_path)]]


def test_hook_delegates_to_the_python_gate():
    text = (REPO / ".githooks" / "pre-push").read_text()
    assert "pre_push_review.sh" not in text
    assert "bin/mini-ork-review" not in text
    assert "mini_ork.pre_push_review" in text
    assert '"gate"' in text
    assert "PYTHONPATH" in text


def test_merge_driver_calls_prereview_between_gate_and_lock():
    text = SCRIPT.read_text()
    start = text.index("def merge_worktree")
    end = text.index("\ndef ", start + 10)
    body = text[start:end]
    assert "_prereview(" in body
    assert body.index("_prereview(") > body.index("green gate failed")
    assert body.index("_prereview(") < body.index("with _merge_lock")


# ── scripts/mini_ork_worktree.py `_prereview` (kickoff §5) ──────────────────

def _load_worktree_driver(tmp_path, monkeypatch):
    """Load the driver with ``MINI_ORK_ROOT`` pinned, so ``ROOT`` is ``tmp_path``."""
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        f"mo_wt_under_test_{abs(hash(str(tmp_path)))}", str(SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RunRecorder:
    def __init__(self, rc: int) -> None:
        self.rc = rc
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": list(cmd), **kwargs})
        return subprocess.CompletedProcess(cmd, self.rc)


def _worktree_with_hook(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    hook = wt / ".githooks" / "pre-push"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    return wt


def test_prereview_runs_the_gate_with_the_worktree_on_pythonpath(tmp_path, monkeypatch):
    module = _load_worktree_driver(tmp_path, monkeypatch)
    wt = _worktree_with_hook(tmp_path)
    recorder = _RunRecorder(0)
    monkeypatch.setattr(module.subprocess, "run", recorder)

    assert module._prereview(str(wt)) == ""  # R2e: returns the reviewed patch-id or ""
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["cmd"][1:] == ["-m", "mini_ork.pre_push_review", "gate",
                               "--target", "main", "--cwd", str(wt)]
    assert str(call["cwd"]) == str(wt)
    assert call["check"] is False
    pythonpath = call["env"]["PYTHONPATH"]
    assert pythonpath == str(wt) or pythonpath.startswith(str(wt) + os.pathsep)
    assert call["cmd"][0] == sys.executable


def test_prereview_prefers_the_root_venv_python(tmp_path, monkeypatch):
    module = _load_worktree_driver(tmp_path, monkeypatch)
    wt = _worktree_with_hook(tmp_path)
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    venv_python.chmod(0o755)
    recorder = _RunRecorder(0)
    monkeypatch.setattr(module.subprocess, "run", recorder)

    module._prereview(str(wt))
    assert Path(recorder.calls[0]["cmd"][0]) == venv_python


def test_prereview_skips_on_review_skip_or_without_a_hook(tmp_path, monkeypatch):
    module = _load_worktree_driver(tmp_path, monkeypatch)
    wt = _worktree_with_hook(tmp_path)
    recorder = _RunRecorder(0)
    monkeypatch.setattr(module.subprocess, "run", recorder)

    monkeypatch.setenv("MO_REVIEW_SKIP", "1")
    assert module._prereview(str(wt)) == ""  # R2e: returns the reviewed patch-id or ""
    assert recorder.calls == []

    monkeypatch.delenv("MO_REVIEW_SKIP")
    bare_wt = tmp_path / "bare-wt"
    bare_wt.mkdir()
    assert module._prereview(str(bare_wt)) == ""
    assert recorder.calls == []


def test_prereview_blocks_the_merge_on_rc1(tmp_path, monkeypatch, capsys):
    module = _load_worktree_driver(tmp_path, monkeypatch)
    wt = _worktree_with_hook(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", _RunRecorder(1))

    with pytest.raises(SystemExit) as exc:
        module._prereview(str(wt))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith(
        "[mo-worktree] pre-push code review blocked the merge (findings above); "
        "fix them, or re-run with MO_REVIEW_SKIP=1 if a human accepts them")


def test_prereview_continues_on_a_reviewer_error(tmp_path, monkeypatch, capsys):
    module = _load_worktree_driver(tmp_path, monkeypatch)
    wt = _worktree_with_hook(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", _RunRecorder(3))

    assert module._prereview(str(wt)) == ""  # R2e: returns the reviewed patch-id or ""
    out = capsys.readouterr()
    assert ("[mo-worktree] pre-push review errored (rc=3); "
            "the push hook will retry it") in (out.out + out.err)
