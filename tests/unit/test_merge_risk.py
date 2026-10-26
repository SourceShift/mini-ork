"""Merge-time risk tiers (rev6 R3).

Drives the real merge driver (``scripts/mini_ork_worktree.py``) against a
throwaway bare origin + clone, with the green gate stubbed via
``MINI_ORK_TEST_CMD=true``. ``ENGINE`` is therefore this checkout, and the
branch under merge is hermetic — no network, no shared DB, no credentials.

The tests cover the kickoff's acceptance list: classification, the driver's
refusal/approval flow, patch-id binding across an upstream move, the risk-class
lookup from ``task_runs``, the two guard overrides, and the terminal-only
``approvals approve wt:…`` path.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import mini_ork
from mini_ork.cli import approvals_cmd
from mini_ork.gates import merge_approval, merge_risk
from mini_ork.stores.migrate import init_db

REPO = Path(mini_ork.__file__).resolve().parents[1]
DRIVER = REPO / "scripts" / "mini_ork_worktree.py"

# The kickoff's fixture rules: only ``danger/**`` is high.
RULES_YAML = (
    "risk_tiers:\n"
    "  high:\n"
    "    - danger/**\n"
    "  include_frozen_kernel: false\n"
)

PROBE_LINE = 'x = os.environ.get("PYTEST_CURRENT_TEST")\n'


def _clean_git_env() -> dict:
    """The process env without the lane's global ``GIT_CONFIG_*`` push guard.

    The guard protects the real checkout; these tests push only to a throwaway
    bare repo under tmp, so it must not leak into the spawned git calls.
    """
    return {
        key: value for key, value in os.environ.items()
        if key != "GIT_CONFIG" and not key.startswith("GIT_CONFIG_")
    }


class Harness:
    """Bare origin + clone + isolated home/worktrees/state.db."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.origin = base / "origin.git"
        self.repo = base / "repo"
        self.wts = base / "worktrees"
        self.home = base / "home"
        self.db = self.home / "state.db"
        self.rules = base / "rules.yaml"
        self.nohooks = base / "nohooks"

    def setup(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self.nohooks.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.base)
        for key, value in (
            ("user.name", "mo-test"),
            ("user.email", "mo-test@example.invalid"),
            ("commit.gpgsign", "false"),
            ("core.hooksPath", str(self.nohooks)),
        ):
            self.git("config", key, value)
        (self.repo / "README.md").write_text("seed\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")
        self.git("clone", "-q", "--bare", str(self.repo), str(self.origin),
                 cwd=self.base)
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "-q", "-u", "origin", "main")
        self.git("fetch", "-q", "origin")
        self.home.mkdir(parents=True, exist_ok=True)
        self.wts.mkdir(parents=True, exist_ok=True)
        self.rules.write_text(RULES_YAML, encoding="utf-8")
        rc, out, err = init_db(db=str(self.db), root=str(REPO))
        assert rc == 0, f"init_db failed: {err or out}"

    # ── git / process primitives ───────────────────────────────────────────

    def git(self, *args: str, cwd=None, check: bool = True):
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd if cwd is not None else self.repo),
            env=_clean_git_env(), check=check, text=True, capture_output=True,
        )

    def env(self, **over) -> dict:
        env = {
            key: value for key, value in _clean_git_env().items()
            if not key.startswith(("MINI_ORK_", "MO_"))
            and key not in ("PYTHONPATH", "CN_BASE_URL")
        }
        env.update({
            "MINI_ORK_ROOT": str(self.repo),
            "MINI_ORK_WORKTREES_DIR": str(self.wts),
            "MINI_ORK_OWNERSHIP_FILE": str(self.wts / ".ownership"),
            "MINI_ORK_HOME": str(self.home),
            "MINI_ORK_DB": str(self.db),
            "MINI_ORK_WORKTREE_LOCK": str(self.base / "merge.lockdir"),
            "MO_CONCORD": "0",
            "MINI_ORK_TEST_CMD": "true",
            "MO_REVIEW_SKIP": "1",
            "MO_RISK_TIERS_FILE": str(self.rules),
        })
        for key, value in over.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def run_driver(self, *args: str, env=None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(DRIVER), *args],
            cwd=str(self.repo), env=env if env is not None else self.env(),
            text=True, capture_output=True, timeout=300,
        )

    # ── helpers ────────────────────────────────────────────────────────────

    def create(self, slug: str) -> Path:
        proc = self.run_driver("create", slug)
        assert proc.returncode == 0, f"create failed:\n{proc.stdout}\n{proc.stderr}"
        wt = self.wts / slug
        assert wt.is_dir(), f"no worktree at {wt}"
        return wt

    def write(self, root: Path, files: dict) -> None:
        for rel, content in files.items():
            path = root / rel
            if content is None:
                if path.exists():
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")

    def commit(self, wt: Path, files: dict, msg: str) -> None:
        self.write(wt, files)
        for rel in files:
            self.git("add", "-A", "--", rel, cwd=wt)
        self.git("commit", "-q", "-m", msg, cwd=wt)

    def push_main(self, files: dict, msg: str) -> None:
        self.git("fetch", "-q", "origin", "main")
        self.git("rebase", "-q", "origin/main")
        self.write(self.repo, files)
        for rel in files:
            self.git("add", "-A", "--", rel)
        self.git("commit", "-q", "-m", msg)
        self.git("push", "-q", "origin", "main")

    def main_sha(self) -> str:
        self.git("fetch", "-q", "origin", "main")
        return self.git("rev-parse", "origin/main").stdout.strip()

    def rows(self, sql: str, params=()) -> list:
        con = sqlite3.connect(str(self.db))
        con.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def pending(self, slug: str) -> list:
        return self.rows(
            "SELECT * FROM mo_inbox_gates WHERE gate_id = ? AND status = 'pending'",
            (f"merge:wt:{slug}",),
        )

    def add_task_run(self, run_id: str, recipe: str) -> None:
        con = sqlite3.connect(str(self.db))
        try:
            con.execute(
                "INSERT INTO task_runs (id, task_class, recipe, kickoff_path,"
                " created_at, updated_at) VALUES (?, ?, ?, 'kick.md', 0, 0)",
                (run_id, "demo", recipe),
            )
            con.commit()
        finally:
            con.close()


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    harness = Harness(tmp_path_factory.mktemp("merge-risk"))
    harness.setup()
    yield harness
    shutil.rmtree(harness.base, ignore_errors=True)


def _out(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


# ── 1. classification ──────────────────────────────────────────────────────


def test_classify_low_for_ordinary_paths(h) -> None:
    wt = h.create("risk-low")
    h.commit(wt, {"ok.txt": "ordinary\n"}, "ordinary change")
    verdict = merge_risk.classify(
        str(wt), engine_root=str(REPO), db=str(h.db), env=h.env(),
    )
    assert verdict.tier == "low"
    assert verdict.reasons == ()
    assert set(verdict.files) == {"ok.txt"}
    assert verdict.patch_id


def test_classify_high_naming_the_path_rule(h) -> None:
    wt = h.create("risk-high")
    h.commit(wt, {"danger/x.txt": "risky\n"}, "risky change")
    verdict = merge_risk.classify(
        str(wt), engine_root=str(REPO), db=str(h.db), env=h.env(),
    )
    assert verdict.tier == "high"
    assert "danger/x.txt matches danger/**" in verdict.reasons


def test_classify_dir_glob_matches_nested_paths(h) -> None:
    """``dir/**`` covers everything under ``dir/``, not the path ``dir`` itself."""
    wt = h.create("risk-nested")
    h.commit(wt, {"danger/sub/deep.txt": "nested\n"}, "nested change")
    verdict = merge_risk.classify(
        str(wt), engine_root=str(REPO), db=str(h.db), env=h.env(),
    )
    assert verdict.tier == "high"
    assert "danger/sub/deep.txt matches danger/**" in verdict.reasons

    # A flat path named exactly like the directory prefix is not "under" it.
    flat = h.create("risk-flat")
    h.commit(flat, {"danger": "flat file\n"}, "flat file")
    flat_verdict = merge_risk.classify(
        str(flat), engine_root=str(REPO), db=str(h.db), env=h.env(),
    )
    assert not any("matches danger/**" in reason
                   for reason in flat_verdict.reasons)


def test_load_rules_prefers_origin_main_over_branch_copy(h) -> None:
    h.push_main(
        {"config/risk-tiers.yaml":
         "risk_tiers:\n  high:\n    - originonly/**\n"
         "  include_frozen_kernel: false\n"},
        "publish rules to origin/main",
    )
    try:
        wt = h.create("risk-precedence")
        h.commit(wt, {
            "config/risk-tiers.yaml":
                "risk_tiers:\n  high:\n    - selfloose/**\n"
                "  include_frozen_kernel: false\n",
            "originonly/x.txt": "hit\n",
            "selfloose/a.txt": "must stay low\n",
        }, "weaken the rules on the branch")
        assert merge_risk.load_rules(str(wt), {})["high"] == ["originonly/**"]
        verdict = merge_risk.classify(
            str(wt), engine_root=str(REPO), db=str(h.db), env={},
        )
        assert verdict.tier == "high"
        assert any("originonly/x.txt matches originonly/**" in r
                   for r in verdict.reasons)
        assert not any("selfloose" in r for r in verdict.reasons)
    finally:
        h.push_main({"config/risk-tiers.yaml": None},
                    "drop test rules from origin/main")


def test_load_rules_unparsable_file_raises_naming_source(h) -> None:
    bad = h.base / "bad-rules.yaml"
    bad.write_text("{{{: not yaml\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        merge_risk.load_rules(str(h.repo), {"MO_RISK_TIERS_FILE": str(bad)})
    assert str(bad) in str(excinfo.value)

    not_list = h.base / "nothigh.yaml"
    not_list.write_text("risk_tiers:\n  high: 42\n", encoding="utf-8")
    with pytest.raises(ValueError):
        merge_risk.load_rules(str(h.repo), {"MO_RISK_TIERS_FILE": str(not_list)})


# ── 2-6. the driver's merge decision ───────────────────────────────────────


def test_high_tier_refused_without_approval(h) -> None:
    slug = "risk-refused"
    wt = h.create(slug)
    h.commit(wt, {"danger/x.txt": "risky\n"}, "risky change")
    before = h.main_sha()

    proc = h.run_driver("merge", slug)
    assert proc.returncode != 0
    assert "mini-ork approvals approve wt:risk-refused --head" in _out(proc)
    assert "without a person's approval" in _out(proc)
    assert "danger/x.txt matches danger/**" in _out(proc)

    rows = h.pending(slug)
    assert len(rows) == 1
    assert json.loads(rows[0]["context_json"])["head_sha"] == \
        merge_risk.branch_patch_id(str(wt))

    h.git("fetch", "-q", "origin", "main")
    assert h.git("rev-parse", "origin/main").stdout.strip() == before


def test_high_tier_merges_after_patch_approval(h) -> None:
    slug = "risk-approved"
    wt = h.create(slug)
    h.commit(wt, {"danger/x.txt": "approved risky\n"}, "risky")
    assert h.run_driver("merge", slug).returncode != 0

    pid = merge_risk.branch_patch_id(str(wt))
    merge_approval.approve(
        f"wt:{slug}", pid, approver="human:test", db_path=str(h.db), env={})

    proc = h.run_driver("merge", slug)
    assert proc.returncode == 0, _out(proc)
    assert h.git("show", "origin/main:danger/x.txt").stdout == "approved risky\n"


def test_approval_for_a_different_patch_is_refused(h) -> None:
    slug = "risk-wrongpatch"
    db2 = h.base / "wrong-patch.sqlite"
    rc, out, err = init_db(db=str(db2), root=str(REPO))
    assert rc == 0, err or out
    wt = h.create(slug)
    h.commit(wt, {"danger/x.txt": "risky\n"}, "different patch")

    # A permission slip for a different diff, on its own fresh ledger.
    merge_approval.approve(
        f"wt:{slug}", "f" * 40, approver="human:test", db_path=str(db2), env={})
    before = h.main_sha()
    proc = h.run_driver("merge", slug, env=h.env(MINI_ORK_DB=str(db2)))
    assert proc.returncode != 0
    assert "needs a person's approval" in _out(proc)
    h.git("fetch", "-q", "origin", "main")
    assert h.git("rev-parse", "origin/main").stdout.strip() == before


def test_upstream_move_after_approval_still_merges(h) -> None:
    slug = "risk-upstream"
    wt = h.create(slug)
    h.commit(wt, {"danger/y.txt": "risky\n"}, "risky")
    assert h.run_driver("merge", slug).returncode != 0

    pid = merge_risk.branch_patch_id(str(wt))
    merge_approval.approve(
        f"wt:{slug}", pid, approver="human:test", db_path=str(h.db), env={})
    # An unrelated peer lands on main: the patch-id is unchanged, so the
    # approval still covers this branch.
    h.push_main({"unrelated/z.txt": "peer landed\n"}, "peer change")

    proc = h.run_driver("merge", slug)
    assert proc.returncode == 0, _out(proc)
    assert h.git("show", "origin/main:unrelated/z.txt").stdout == "peer landed\n"
    assert h.git("show", "origin/main:danger/y.txt").stdout == "risky\n"


def test_low_tier_merges_with_no_approval_row(h) -> None:
    slug = "risk-lowmerge"
    wt = h.create(slug)
    h.commit(wt, {"ok.txt": "ordinary\n"}, "low tier change")
    before = h.main_sha()

    proc = h.run_driver("merge", slug)
    assert proc.returncode == 0, _out(proc)
    assert h.main_sha() != before
    assert h.git("show", "origin/main:ok.txt").stdout == "ordinary\n"
    assert h.pending(slug) == []


# ── 7. risk class from task_runs ───────────────────────────────────────────


def test_high_risk_recipe_run_makes_a_branch_high(h) -> None:
    h.push_main({"recipes/demo/task_class.yaml": "risk_class: high\n"},
                "publish a high-risk recipe")
    try:
        h.add_task_run("r-hi", "demo")
        wt = h.create("risk-runclass")
        h.commit(wt, {"ok.txt": "low path\n"}, "[run r-hi] run a high recipe")

        verdict = merge_risk.classify(
            str(wt), engine_root=str(REPO), db=str(h.db), env=h.env(),
        )
        assert verdict.tier == "high"
        assert "run r-hi is recipe demo (risk_class high)" in verdict.reasons
    finally:
        h.push_main({"recipes/demo/task_class.yaml": None},
                    "drop the test recipe")


# ── 8. test-gaming guard ───────────────────────────────────────────────────


def test_gaming_guard_refuses_a_test_probe(h) -> None:
    slug = "risk-probe"
    wt = h.create(slug)
    h.commit(wt, {"mod.py": PROBE_LINE, "ok.txt": "x\n"}, "probe")
    before = h.main_sha()

    proc = h.run_driver("merge", slug)
    assert proc.returncode != 0
    assert "test-gaming patterns in production code" in _out(proc)
    h.git("fetch", "-q", "origin", "main")
    assert h.git("rev-parse", "origin/main").stdout.strip() == before


def test_gaming_override_lets_a_person_through(h) -> None:
    slug = "risk-probe-ok"
    wt = h.create(slug)
    h.commit(wt, {"mod.py": PROBE_LINE, "ok.txt": "x\n"}, "probe with override")

    proc = h.run_driver(
        "merge", slug, env=h.env(MO_MERGE_ALLOW_TEST_PROBE="1"))
    assert proc.returncode == 0, _out(proc)
    assert "MO_MERGE_ALLOW_TEST_PROBE" in _out(proc)
    assert h.git("show", "origin/main:ok.txt").stdout == "x\n"


# ── 9. broken rules file ───────────────────────────────────────────────────


def test_unparsable_rules_file_refuses_the_merge(h) -> None:
    slug = "risk-badrules"
    wt = h.create(slug)
    h.commit(wt, {"ok.txt": "ordinary\n"}, "ordinary")
    bad = h.base / "unparsable-rules.yaml"
    bad.write_text("}: not yaml [\n", encoding="utf-8")
    before = h.main_sha()

    proc = h.run_driver("merge", slug, env=h.env(MO_RISK_TIERS_FILE=str(bad)))
    assert proc.returncode != 0
    assert "risk check" in _out(proc)
    h.git("fetch", "-q", "origin", "main")
    assert h.git("rev-parse", "origin/main").stdout.strip() == before


# ── 11. approvals approve wt:<slug> ────────────────────────────────────────


def test_approve_wt_requires_a_terminal(h, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MINI_ORK_HOME", str(h.home))
    monkeypatch.setenv("MINI_ORK_DB", str(h.db))
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    saved = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        rc = approvals_cmd.main(["approve", "wt:nope", "--db", str(h.db)])
    finally:
        sys.stdin = saved

    assert rc == 1
    captured = capsys.readouterr()
    assert "must be typed by a person at a terminal" in \
        (captured.out + captured.err)


class _TTYStringIO(io.StringIO):
    """``input()`` treats this stream as a terminal."""

    def isatty(self) -> bool:
        return True


def test_approve_wt_refuses_an_empty_patch_id(h, monkeypatch, capsys) -> None:
    """An empty head (no commits on the branch) → rc 1 with a message."""
    slug = "risk-empty"
    wt = h.create(slug)  # freshly branched off origin/main: no own commits
    assert merge_risk.branch_patch_id(str(wt)) == ""

    monkeypatch.setenv("MINI_ORK_HOME", str(h.home))
    monkeypatch.setenv("MINI_ORK_WORKTREES_DIR", str(h.wts))
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    saved = sys.stdin
    sys.stdin = _TTYStringIO("anything\n")
    try:
        rc = approvals_cmd.main(["approve", f"wt:{slug}", "--db", str(h.db)])
    finally:
        sys.stdin = saved

    assert rc == 1
    captured = capsys.readouterr()
    assert "empty patch id" in (captured.out + captured.err)
    assert not merge_approval.is_approved(
        f"wt:{slug}", "", db_path=str(h.db), env={})


def test_approve_wt_refuses_an_explicit_empty_head(h, monkeypatch, capsys) -> None:
    """An explicit ``--head ""`` is an empty head, not "flag absent"."""
    slug = "risk-explicit-empty"
    wt = h.create(slug)
    h.commit(wt, {"danger/x.txt": "risky\n"}, "risky")
    pid = merge_risk.branch_patch_id(str(wt))
    assert pid  # a real patch exists, so only the empty flag can refuse

    monkeypatch.setenv("MINI_ORK_HOME", str(h.home))
    monkeypatch.setenv("MINI_ORK_WORKTREES_DIR", str(h.wts))
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    saved = sys.stdin
    sys.stdin = _TTYStringIO("anything\n")
    try:
        rc = approvals_cmd.main(
            ["approve", f"wt:{slug}", "--head", "", "--db", str(h.db)])
    finally:
        sys.stdin = saved

    assert rc == 1
    captured = capsys.readouterr()
    assert "empty patch id" in (captured.out + captured.err)
    # Neither the empty string nor the real patch id was approved.
    assert not merge_approval.is_approved(
        f"wt:{slug}", "", db_path=str(h.db), env={})
    assert not merge_approval.is_approved(
        f"wt:{slug}", pid, db_path=str(h.db), env={})


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        # ``dir/**`` covers anything under ``dir/`` …
        ("danger/**", "danger/x.txt", True),
        ("danger/**", "danger/sub/deep.txt", True),
        # … but not the flat path that merely shares the prefix.
        ("danger/**", "danger", False),
        ("danger/**", "dangerish/x.txt", False),
        (".github/workflows/**", ".github/workflows/ci.yml", True),
        ("db/migrations/**", "db/migrations/0085_x.sql", True),
        # A non-glob rule matches the exact path, and is not a prefix match.
        ("install.sh", "install.sh", True),
        ("install.sh", "nested/install.sh", False),
    ],
)
def test_matches_dir_glob_semantics(pattern, path, expected) -> None:
    assert merge_risk._matches(path, pattern) is expected
