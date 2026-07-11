"""Parity gate: mini_ork.ported.memory vs lib/memory.sh.

Sends each call through both sides:
- bash: ``. lib/memory.sh && fn args`` invoked in a fresh tmp MINI_ORK_HOME
  seeded by db/init.sh with a pinned REPO_ROOT
- python: mini_ork.ported.memory.fn() against a separate tmp MINI_ORK_HOME
  also seeded by db/init.sh with the same REPO_ROOT

The two DBs are then projected as JSON, non-deterministic fields
(reflection_at/created_at/updated_at/annotated_at/written_at/ran_at/
reflection_last_check, reflection_sha, reflected_substrate, git
fingerprint in reflected_substrate) are masked to a sentinel, and the
two projections must compare equal field-by-field. Float columns use
math.isclose with rel_tol=1e-6. ``list_*`` cases additionally compare
the stdout TSV byte-for-byte (modulo float repr — sqlite prints same).

NO mocking, NO hardcoded results — the bash function runs via
subprocess against the live source every time.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import memory as m  # noqa: E402

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


SH = REPO / "lib" / "memory.sh"
TABLES_FOR_SMOKE = (
    "arch_specs","module_plans","atom_prs","adrs","node_annotations",
    "communities","validations","fixes","cascade_invalidations",
    "reflection_log","decision_basins","decision_basin_membership",
    "emergent_patterns","inspector_runs",
)


# Fields whose values will differ between two separate bash/py runs (or even
# two back-to-back bash runs) because they're driven by wall-clock, git state
# the infra has touched between invocations, or by precision we don't care
# about. Test compares after these are masked.
NONDET_FIELDS = {
    "reflection_at", "reflection_last_check", "created_at", "updated_at",
    "annotated_at", "written_at", "ran_at",
    "reflection_sha", "reflected_substrate", "reflection_drift_log",
}
NON_FLOAT_FIELDS = {
    "info_gain", "cohesion_score", "coupling_score", "files_touched_score",
    "volatility_score", "files_touched", "duration_ms", "duration_ms_opus",
    "duration_ms_codex", "cost_usd",
}

SENTINEL = "MASKED"


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


def _bash_call(fn: str, args: list[str], db: Path, repo_root: Path,
               run_dir: Path | None = None, extra_env: dict | None = None,
               run_id: str = "") -> subprocess.CompletedProcess:
    del extra_env  # reserved for future per-call tuning
    env = {
        "MINI_ORK_DB": str(db),
        "REPO_ROOT": str(repo_root),
        "MO_CYCLE_ID": "cycle-pytest",
    }
    if run_dir is not None:
        env["MINI_ORK_RUN_DIR"] = str(run_dir)
    if run_id:
        env["MINI_ORK_RUN_ID"] = run_id
    return subprocess.run(
        ["bash", "-c", f'. "{SH}" && {fn} "$@"', "_", *args],
        capture_output=True, text=True, env={**os.environ, **env},
    )


def _dump(db: Path, sql: str) -> list[dict]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql).fetchall()]
    con.close()
    return rows


def _mask(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        rd = {}
        for k, v in r.items():
            if k in NONDET_FIELDS:
                rd[k] = SENTINEL
            else:
                rd[k] = v
        out.append(rd)
    return out


def _compare(mask_b: list, mask_p: list, float_tol: float = 1e-6) -> None:
    assert len(mask_b) == len(mask_p), (
        f"row count: bash={len(mask_b)} python={len(mask_p)}"
    )
    # Sort both by a stable key (every column name appears) for order-immune
    # compare; fall back to dict repr when project has no natural key.
    def _keyfn(rows):
        keys = list(rows[0].keys()) if rows else []
        return keys
    k_b = _keyfn(mask_b)
    k_p = _keyfn(mask_p)
    assert k_b == k_p, f"schema diff: {k_b} vs {k_p}"
    s_b = sorted(mask_b, key=lambda r: json.dumps({k: r[k] for k in k_b},
                                                  sort_keys=True, default=str))
    s_p = sorted(mask_p, key=lambda r: json.dumps({k: r[k] for k in k_p},
                                                  sort_keys=True, default=str))
    for rb, rp in zip(s_b, s_p):
        for k in k_b:
            vb, vp = rb[k], rp[k]
            if isinstance(vb, float) or isinstance(vp, float):
                assert math.isclose(float(vb or 0), float(vp or 0),
                                    rel_tol=float_tol, abs_tol=float_tol), (
                    f"float drift on {k}: bash={vb!r} py={vp!r}"
                )
                continue
            if isinstance(vb, str) and isinstance(vp, str):
                if vb != vp:
                    # fall through to generic compare
                    pass
            assert vb == vp, f"diff on {k}: bash={vb!r} py={vp!r}"


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


def _two_homes(tmp_path: Path, repo_root: Path):
    """Return (bash_home, bash_db, py_home, py_db) with both DBs initialised."""
    del repo_root  # DBs only need a clean MINI_ORK_HOME; repo_root lives on bash side
    bash_home = tmp_path / "bash_home"
    py_home = tmp_path / "py_home"
    bash_db = bash_home / "state.db"
    py_db = py_home / "state.db"
    _init_db(bash_home, bash_db)
    _init_db(py_home, py_db)
    return bash_home, bash_db, py_home, py_db


# ─── arch_specs ────────────────────────────────────────────────────────


def test_arch_spec_round_trip(tmp_path, repo):
    bash_home, bash_db, py_home, py_db = _two_homes(tmp_path, repo)

    rb = _bash_call(
        "mo_mem_put_arch_spec",
        ["ARCH-1", "feat-foo", "title one", "P1", "Q1", "echo ok",
         "[\"a.py:1\"]", "[\"mem.py:2\"]", "0.42"],
        bash_db, repo,
    )
    assert rb.returncode == 0, rb.stderr
    with _py_env(py_db, repo):
        m.put_arch_spec(
            "ARCH-1", "feat-foo", "title one", "P1", "Q1", "echo ok",
            frame_json="[\"a.py:1\"]", evidence_json="[\"mem.py:2\"]",
            info_gain="0.42",
        )

    proj_b = _dump(bash_db, "SELECT * FROM arch_specs WHERE arch_id='ARCH-1'")
    proj_p = _dump(py_db, "SELECT * FROM arch_specs WHERE arch_id='ARCH-1'")
    assert len(proj_b) == len(proj_p) == 1
    _compare(_mask(proj_b), _mask(proj_p))


def test_arch_spec_list_filtered(tmp_path, repo):
    _bash_home, bash_db, _py_home, py_db = _two_homes(tmp_path, repo)

    for arch in ("ARCH-A", "ARCH-B"):
        _bash_call("mo_mem_put_arch_spec", [arch, "feat", f"title {arch}",
                                            "P", "Q", "echo ok"],
                   bash_db, repo)
    with _py_env(py_db, repo):
        for arch in ("ARCH-A", "ARCH-B"):
            m.put_arch_spec(arch, "feat", f"title {arch}", "P", "Q", "echo ok")

        rb = _bash_call("mo_mem_list_arch_specs", ["feat", "proposed"],
                        bash_db, repo)
        list_b = rb.stdout
        list_p = m.list_arch_specs("feat", "proposed")
        assert list_b == list_p, f"stdout diff:\nbash={list_b!r}\npython={list_p!r}"


# ─── node_annotations ──────────────────────────────────────────────────


def test_node_annotation_hit_and_miss(tmp_path, repo):
    _bash_home, bash_db, _py_home, py_db = _two_homes(tmp_path, repo)

    miss_b = _bash_call("mo_mem_get_node_annotation",
                        ["node:x", "hash:zz"], bash_db, repo)
    with _py_env(py_db, repo):
        miss_p = m.get_node_annotation("node:x", "hash:zz")
    assert miss_b.stdout.strip() == "null"
    assert miss_p is None

    _bash_call("mo_mem_put_node_annotation",
               ["node:x", "mem.py", "sym", "hash:abc", "task-1",
                "{pre}", "{post}", "1", "[{\"side\":1}]"],
               bash_db, repo)
    with _py_env(py_db, repo):
        m.put_node_annotation("node:x", "mem.py", "sym", "hash:abc",
                              "task-1", "{pre}", "{post}", "1",
                              '[{"side":1}]')

        hit_p = m.get_node_annotation("node:x", "hash:abc")
    hit_b = _bash_call("mo_mem_get_node_annotation",
                       ["node:x", "hash:abc"], bash_db, repo)
    assert hit_b.stdout.strip().startswith("{")
    assert hit_p is not None
    proj_b = _dump(bash_db, "SELECT * FROM node_annotations WHERE node_id='node:x'")
    proj_p = _dump(py_db, "SELECT * FROM node_annotations WHERE node_id='node:x'")
    _compare(_mask(proj_b), _mask(proj_p))


# ─── inspector_runs ───────────────────────────────────────────────────


def test_inspector_run_record(tmp_path, repo):
    _bash_home, bash_db, _py_home, py_db = _two_homes(tmp_path, repo)

    rb = _bash_call("mo_mem_record_inspector_run",
                    ["stage1", "ph:1", '{"v":"opus"}', '{"v":"codex"}',
                     "0", "0", "1", "null", '{"final":"ok"}',
                     "120", "150", ""],
                    bash_db, repo)
    assert rb.returncode == 0, rb.stderr
    with _py_env(py_db, repo):
        m.record_inspector_run("stage1", "ph:1", '{"v":"opus"}', '{"v":"codex"}',
                               "0", "0", "1", "null", '{"final":"ok"}',
                               "120", "150", "")

    proj_b = _dump(bash_db, "SELECT * FROM inspector_runs WHERE site='stage1'")
    proj_p = _dump(py_db, "SELECT * FROM inspector_runs WHERE site='stage1'")
    _compare(_mask(proj_b), _mask(proj_p))


# ─── module_plans ──────────────────────────────────────────────────────


def test_module_plan_put_and_list(tmp_path, repo):
    _bash_home, bash_db, _py_home, py_db = _two_homes(tmp_path, repo)

    plan = [
        ("M1", "M1-A", "ARCH-1", "max cohesion", "0.9", "0.1", "1"),
        ("M1", "M1-B", "ARCH-1", "min churn", "0.4", "0.5", "0"),
        ("M1", "M1-C", "ARCH-1", "balanced", "0.7", "0.3", "0"),
    ]
    for module_id, cand, arch, label, coh, coupl, rec in plan:
        _bash_call("mo_mem_put_module_plan",
                   [module_id, cand, arch, label, "3", "[\"a.py\"]",
                    coh, coupl, "0.6", "0.2", "[]", rec],
                   bash_db, repo)
    with _py_env(py_db, repo):
        for module_id, cand, arch, label, coh, coupl, rec in plan:
            m.put_module_plan(module_id, cand, arch, label, "3", "[\"a.py\"]",
                              coh, coupl, "0.6", "0.2", "[]", rec)

        proj_b = _dump(bash_db, "SELECT * FROM module_plans WHERE arch_id='ARCH-1'")
        proj_p = _dump(py_db, "SELECT * FROM module_plans WHERE arch_id='ARCH-1'")
        _compare(_mask(proj_b), _mask(proj_p))

        rb = _bash_call("mo_mem_list_module_plans", ["ARCH-1"], bash_db, repo)
        list_b = rb.stdout
        list_p = m.list_module_plans("ARCH-1")
        assert list_b == list_p, f"sort drift:\nbash=\n{list_b}\npython=\n{list_p}"


# ─── atom_prs ──────────────────────────────────────────────────────────


def test_atom_pr_put_and_list(tmp_path, repo):
    _bash_home, bash_db, _py_home, py_db = _two_homes(tmp_path, repo)

    _bash_call("mo_mem_put_atom_pr",
               ["ATOM-PR-1", "M1", "M1-A", "rename x", "rename",
                "[]", "[]", "true", "true"],
               bash_db, repo)
    _bash_call("mo_mem_put_atom_pr",
               ["ATOM-PR-2", "M1", "", "extract y", "extract",
                "[]", "[\"ATOM-PR-1\"]", "true", "true"],
               bash_db, repo)
    with _py_env(py_db, repo):
        m.put_atom_pr("ATOM-PR-1", "M1", "M1-A", "rename x", "rename")
        m.put_atom_pr("ATOM-PR-2", "M1", "", "extract y", "extract",
                      depends_on="[\"ATOM-PR-1\"]")

        proj_b = _dump(bash_db, "SELECT * FROM atom_prs WHERE module_id='M1'")
        proj_p = _dump(py_db, "SELECT * FROM atom_prs WHERE module_id='M1'")
        _compare(_mask(proj_b), _mask(proj_p))

        rb = _bash_call("mo_mem_list_atom_prs", ["M1"], bash_db, repo)
        list_b = rb.stdout
        list_p = m.list_atom_prs("M1")
        assert list_b == list_p


# ─── adrs ──────────────────────────────────────────────────────────────


def test_adr_put_and_list(tmp_path, repo):
    _bash_home, bash_db, _py_home, py_db = _two_homes(tmp_path, repo)

    _bash_call("mo_mem_put_adr",
               ["ADR-1", "ARCH-1", "decide X", "P", "Q", "echo ok", "# body",
                "ADR-0"],
               bash_db, repo)
    with _py_env(py_db, repo):
        m.put_adr("ADR-1", "ARCH-1", "decide X", "P", "Q", "echo ok", "# body",
                  supersedes="ADR-0")

        proj_b = _dump(bash_db, "SELECT * FROM adrs WHERE adr_id='ADR-1'")
        proj_p = _dump(py_db, "SELECT * FROM adrs WHERE adr_id='ADR-1'")
        _compare(_mask(proj_b), _mask(proj_p))

        rb = _bash_call("mo_mem_list_adrs", [], bash_db, repo)
        list_b = rb.stdout
        list_p = m.list_adrs()
        assert list_b == list_p


# ─── smoke ─────────────────────────────────────────────────────────────


def test_smoke_ok(tmp_path, repo):
    _bash_home, bash_db, _py_home, py_db = _two_homes(tmp_path, repo)

    rb = _bash_call("mo_mem_smoke", [], bash_db, repo)
    with _py_env(py_db, repo):
        msg_p, rc_p = m.smoke(str(py_db))
    msg_b, rc_b = rb.stdout.strip(), rb.returncode
    assert (msg_b, rc_b) == (msg_p, rc_p), (
        f"smoke mismatch:\n  bash=({msg_b!r}, {rc_b})\n  py=({msg_p!r}, {rc_p})"
    )


# ─── memory_write_task idempotence + sentinel ────────────────────────


def test_write_task_idempotent(tmp_path, repo):
    _bash_home, bash_db, _py_home, py_db = _two_homes(tmp_path, repo)

    # Seed a run row so FK passes for task_memory; use the bash flow.
    bash_run_dir = tmp_path / "run_bash"
    bash_run_dir.mkdir()
    py_run_dir = tmp_path / "run_py"
    py_run_dir.mkdir()

    # Bash: write once, then again — second call short-circuits via sentinel.
    _bash_call("memory_write_task", ["code_fix", "success", "1200", "0.42",
                                      "[\"f1.json\",\"f2.json\"]"],
               bash_db, repo, run_dir=bash_run_dir, run_id="run-bash-1")
    _bash_call("memory_write_task", ["code_fix", "success", "1200", "0.42",
                                      "[\"f1.json\",\"f2.json\"]"],
               bash_db, repo, run_dir=bash_run_dir, run_id="run-bash-1")

    # Python: same path.
    with _py_env(py_db, repo, run_dir=py_run_dir, run_id="run-bash-1"):
        m.write_task("code_fix", "success", "1200", "0.42",
                     "[\"f1.json\",\"f2.json\"]")
        m.write_task("code_fix", "success", "1200", "0.42",
                     "[\"f1.json\",\"f2.json\"]")

    cnt_b = _dump(bash_db, "SELECT COUNT(*) AS c FROM task_memory")[0]["c"]
    cnt_p = _dump(py_db, "SELECT COUNT(*) AS c FROM task_memory")[0]["c"]
    assert cnt_b == cnt_p == 1, (
        f"idempotence: bash={cnt_b} py={cnt_p} rows (expected 1 each)"
    )

    proj_b = _dump(bash_db, "SELECT task_class, outcome, kickoff_hash, "
                             "duration_ms, cost_usd, artifacts_produced "
                             "FROM task_memory")
    proj_p = _dump(py_db, "SELECT task_class, outcome, kickoff_hash, "
                             "duration_ms, cost_usd, artifacts_produced "
                             "FROM task_memory")
    _compare(_mask(proj_b), _mask(proj_p))


# ─── memory_write_failure category whitelist ──────────────────────────


def test_write_failure_category_whitelist(tmp_path, repo):
    _bash_home, bash_db, _py_home, py_db = _two_homes(tmp_path, repo)

    bash_run_dir = tmp_path / "fail_run_b"
    bash_run_dir.mkdir()
    py_run_dir = tmp_path / "fail_run_p"
    py_run_dir.mkdir()

    # valid category
    rb = _bash_call("memory_write_failure", ["verify", "verifier_fail", "boom"],
                    bash_db, repo, run_dir=bash_run_dir, run_id="run-bash-f1")
    assert rb.returncode == 0, rb.stderr
    with _py_env(py_db, repo, run_dir=py_run_dir, run_id="run-bash-f1"):
        m.write_failure("verify", "verifier_fail", "boom")

    # bogus category — both must normalize to dispatch_error and still insert
    rb2 = _bash_call("memory_write_failure", ["verify", "totally-bogus", "x"],
                     bash_db, repo, run_dir=bash_run_dir, run_id="run-bash-f2")
    assert rb2.returncode == 0, rb2.stderr
    with _py_env(py_db, repo, run_dir=py_run_dir, run_id="run-bash-f2"):
        m.write_failure("verify", "totally-bogus", "x")

    proj_b = _dump(bash_db, "SELECT workflow_stage, failure_category, error_message "
                             "FROM failure_memory ORDER BY rowid")
    proj_p = _dump(py_db, "SELECT workflow_stage, failure_category, error_message "
                             "FROM failure_memory ORDER BY rowid")
    assert len(proj_b) == len(proj_p) == 2
    for rb_, rp_ in zip(proj_b, proj_p):
        assert rb_["workflow_stage"] == rp_["workflow_stage"] == "verify"
        assert rb_["failure_category"] == rp_["failure_category"]
    assert proj_b[1]["failure_category"] == "dispatch_error"
    assert proj_p[1]["failure_category"] == "dispatch_error"
