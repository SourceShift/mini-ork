"""Unit tests: mini_ork.memory.store (bash parity halves removed; formerly vs lib/memory.sh).

Each call goes through the python port against a fresh tmp MINI_ORK_HOME
seeded by db/init.sh with a pinned REPO_ROOT. The DB is then projected as
JSON and asserted on semantic fields (wall-clock fields like
created_at/updated_at/annotated_at are not asserted). ``list_*`` cases
assert the stdout TSV content + ordering contract.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.memory import store as m

class _py_env:
    """Context manager: pin MINI_ORK_DB/HOME/REPO_ROOT to the python-side
    tmp paths so direct calls of the form ``m.fn(...)`` write to py_db."""
    def __init__(self, db: Path, repo_root: Path, run_dir: Path | None = None,
                 run_id: str = ""):
        self.db = db
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.run_id = run_id
        self._saved: dict = {}

    def __enter__(self):
        for k in ("MINI_ORK_DB", "MINI_ORK_HOME", "REPO_ROOT", "MO_CYCLE_ID",
                  "MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID",
                  "MINI_ORK_KICKOFF_PATH"):
            self._saved[k] = os.environ.get(k)
        os.environ["MINI_ORK_DB"] = str(self.db)
        os.environ["MINI_ORK_HOME"] = str(self.db.parent.parent)
        os.environ["REPO_ROOT"] = str(self.repo_root)
        os.environ["MO_CYCLE_ID"] = "cycle-pytest"
        if self.run_dir is not None:
            os.environ["MINI_ORK_RUN_DIR"] = str(self.run_dir)
        if self.run_id:
            os.environ["MINI_ORK_RUN_ID"] = self.run_id
        return self

    def __exit__(self, *args):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _g(cwd, *args, env=None):
    e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if env:
        e.update(env)
    r = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True, env=e)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def _init_db(home: Path, db: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(db)},
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"db/init.sh failed: {r.stderr}\n{r.stdout}"


def _dump(db: Path, sql: str) -> list[dict]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql).fetchall()]
    con.close()
    return rows


# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path):
    """A temp git repo with one cited file at a fixed path containing
    deterministic text so reflection capture is reproducible."""
    r = tmp_path / "repo"
    r.mkdir()
    _g(r, "init", "-q", "-b", "main")
    (r / "mem.py").write_text("alpha\nbeta\ngamma\n")
    _g(r, "add", "-A")
    _g(r, "commit", "-qm", "fixture")
    return r


@pytest.fixture
def py_db(tmp_path):
    """An initialised tmp MINI_ORK_HOME DB for the python side."""
    home = tmp_path / "py_home"
    db = home / "state.db"
    _init_db(home, db)
    return db


# ─── arch_specs ────────────────────────────────────────────────────────


def test_arch_spec_round_trip(tmp_path, repo, py_db):
    with _py_env(py_db, repo):
        m.put_arch_spec(
            "ARCH-1", "feat-foo", "title one", "P1", "Q1", "echo ok",
            frame_json="[\"a.py:1\"]", evidence_json="[\"mem.py:2\"]",
            info_gain="0.42",
        )

    proj = _dump(py_db, "SELECT * FROM arch_specs WHERE arch_id='ARCH-1'")
    assert len(proj) == 1
    row = proj[0]
    assert row["arch_id"] == "ARCH-1"
    assert row["feature"] == "feat-foo"
    assert row["title"] == "title one"
    assert abs(float(row["info_gain"]) - 0.42) < 1e-6


def test_arch_spec_list_filtered(tmp_path, repo, py_db):
    with _py_env(py_db, repo):
        for arch in ("ARCH-A", "ARCH-B"):
            m.put_arch_spec(arch, "feat", f"title {arch}", "P", "Q", "echo ok")

        list_p = m.list_arch_specs("feat", "proposed")
        lines = [ln for ln in list_p.splitlines() if ln]
        assert len(lines) == 2
        assert any(ln.startswith("ARCH-A\t") and "title ARCH-A" in ln for ln in lines)
        assert any(ln.startswith("ARCH-B\t") and "title ARCH-B" in ln for ln in lines)
        # filter: a different feature lists nothing
        assert m.list_arch_specs("other-feat", "proposed") == ""


# ─── node_annotations ──────────────────────────────────────────────────


def test_node_annotation_hit_and_miss(tmp_path, repo, py_db):
    with _py_env(py_db, repo):
        miss_p = m.get_node_annotation("node:x", "hash:zz")
    assert miss_p is None

    with _py_env(py_db, repo):
        m.put_node_annotation("node:x", "mem.py", "sym", "hash:abc",
                              "task-1", "{pre}", "{post}", "1",
                              '[{"side":1}]')
        hit_p = m.get_node_annotation("node:x", "hash:abc")
    assert hit_p is not None
    proj = _dump(py_db, "SELECT * FROM node_annotations WHERE node_id='node:x'")
    assert len(proj) == 1
    assert proj[0]["file_path"] == "mem.py"
    assert proj[0]["content_hash"] == "hash:abc"


# ─── inspector_runs ───────────────────────────────────────────────────


def test_inspector_run_record(tmp_path, repo, py_db):
    with _py_env(py_db, repo):
        m.record_inspector_run("stage1", "ph:1", '{"v":"opus"}', '{"v":"codex"}',
                               "0", "0", "1", "null", '{"final":"ok"}',
                               "120", "150", "")

    proj = _dump(py_db, "SELECT * FROM inspector_runs WHERE site='stage1'")
    assert len(proj) == 1
    row = proj[0]
    assert row["prompt_hash"] == "ph:1"
    assert row["final_verdict_json"] == '{"final":"ok"}'


# ─── module_plans ──────────────────────────────────────────────────────


def test_module_plan_put_and_list(tmp_path, repo, py_db):
    plan = [
        ("M1", "M1-A", "ARCH-1", "max cohesion", "0.9", "0.1", "1"),
        ("M1", "M1-B", "ARCH-1", "min churn", "0.4", "0.5", "0"),
        ("M1", "M1-C", "ARCH-1", "balanced", "0.7", "0.3", "0"),
    ]
    with _py_env(py_db, repo):
        for module_id, cand, arch, label, coh, coupl, rec in plan:
            m.put_module_plan(module_id, cand, arch, label, "3", "[\"a.py\"]",
                              coh, coupl, "0.6", "0.2", "[]", rec)

        proj = _dump(py_db, "SELECT * FROM module_plans WHERE arch_id='ARCH-1'")
        assert len(proj) == 3

        list_p = m.list_module_plans("ARCH-1")
        lines = [ln for ln in list_p.splitlines() if ln]
        assert len(lines) == 3
        # ORDER BY is_recommended DESC, cohesion_score DESC
        assert lines[0].startswith("M1\tM1-A\t")
        assert lines[1].startswith("M1\tM1-C\t")
        assert lines[2].startswith("M1\tM1-B\t")


# ─── atom_prs ──────────────────────────────────────────────────────────


def test_atom_pr_put_and_list(tmp_path, repo, py_db):
    with _py_env(py_db, repo):
        m.put_atom_pr("ATOM-PR-1", "M1", "M1-A", "rename x", "rename")
        m.put_atom_pr("ATOM-PR-2", "M1", "", "extract y", "extract",
                      depends_on="[\"ATOM-PR-1\"]")

        proj = _dump(py_db, "SELECT * FROM atom_prs WHERE module_id='M1'")
        assert len(proj) == 2

        list_p = m.list_atom_prs("M1")
        lines = [ln for ln in list_p.splitlines() if ln]
        # ORDER BY pr_id
        assert [ln.split("\t")[0] for ln in lines] == ["ATOM-PR-1", "ATOM-PR-2"]
        assert "rename x" in lines[0] and "extract y" in lines[1]


# ─── adrs ──────────────────────────────────────────────────────────────


def test_adr_put_and_list(tmp_path, repo, py_db):
    with _py_env(py_db, repo):
        m.put_adr("ADR-1", "ARCH-1", "decide X", "P", "Q", "echo ok", "# body",
                  supersedes="ADR-0")

        proj = _dump(py_db, "SELECT * FROM adrs WHERE adr_id='ADR-1'")
        assert len(proj) == 1
        assert proj[0]["title"] == "decide X"
        assert proj[0]["supersedes"] == "ADR-0"

        list_p = m.list_adrs()
        assert "ADR-1" in list_p and "decide X" in list_p


# ─── smoke ─────────────────────────────────────────────────────────────


def test_smoke_ok(tmp_path, repo, py_db):
    with _py_env(py_db, repo):
        msg_p, rc_p = m.smoke(str(py_db))
    assert rc_p == 0
    assert msg_p == "OK — 14 memory tables present"


# ─── memory_write_task idempotence + sentinel ────────────────────────


def test_write_task_idempotent(tmp_path, repo, py_db):
    py_run_dir = tmp_path / "run_py"
    py_run_dir.mkdir()

    # Write twice — second call short-circuits via the sentinel.
    with _py_env(py_db, repo, run_dir=py_run_dir, run_id="run-py-1"):
        m.write_task("code_fix", "success", "1200", "0.42",
                     "[\"f1.json\",\"f2.json\"]")
        m.write_task("code_fix", "success", "1200", "0.42",
                     "[\"f1.json\",\"f2.json\"]")

    cnt = _dump(py_db, "SELECT COUNT(*) AS c FROM task_memory")[0]["c"]
    assert cnt == 1, f"idempotence: {cnt} rows (expected 1)"

    proj = _dump(py_db, "SELECT task_class, outcome, kickoff_hash, "
                        "duration_ms, cost_usd, artifacts_produced "
                        "FROM task_memory")
    row = proj[0]
    assert row["task_class"] == "code_fix"
    assert row["outcome"] == "success"
    assert int(row["duration_ms"]) == 1200
    assert abs(float(row["cost_usd"]) - 0.42) < 1e-6


# ─── memory_write_failure category whitelist ──────────────────────────


def test_write_failure_category_whitelist(tmp_path, repo, py_db):
    py_run_dir = tmp_path / "fail_run_p"
    py_run_dir.mkdir()

    # valid category
    with _py_env(py_db, repo, run_dir=py_run_dir, run_id="run-py-f1"):
        m.write_failure("verify", "verifier_fail", "boom")

    # bogus category — must normalize to dispatch_error and still insert
    with _py_env(py_db, repo, run_dir=py_run_dir, run_id="run-py-f2"):
        m.write_failure("verify", "totally-bogus", "x")

    proj = _dump(py_db, "SELECT workflow_stage, failure_category, error_message "
                        "FROM failure_memory ORDER BY rowid")
    assert len(proj) == 2
    for row in proj:
        assert row["workflow_stage"] == "verify"
    assert proj[0]["failure_category"] == "verifier_fail"
    assert proj[1]["failure_category"] == "dispatch_error"
