"""Hermetic tests for the correctness level vector (VT6, G08-T05).

The level vector derives a five-level (applies / executes / target / preserve /
contract) verdict from evidence a run already writes, so a run can only be
published when every REQUIRED level is PROVEN. The levels are NON-NESTED:
passing a shallow level does not imply a deeper one.

Two sections:

1. Pure tests — each writes run-dir files in the producer's shape and drives
   ``mini_ork.verify.levels`` directly. ``verifier_test.json`` is the
   stderr-prefixed evidence shape (a ``[test] running: x`` line plus a one-line
   payload); ``verifier_behavioral.json`` is ``BehavioralVerdict.to_json()``.

2. Real-entrypoint tests — drive ``ex.main`` with the REAL
   ``recipes/code-fix/verifiers/test.py`` through ``_handle_verifier`` (no
   stubbed verifier, no seeded ``verifier_test.json``), then assert the
   publisher gate's commit/abstain/refute outcome.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import execute as ex
from mini_ork.verify import levels as L
from mini_ork.verify.behavioral import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    BehavioralVerdict,
)

NA = L.NA

# ── shared helpers ───────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.check_call(
        ["git", *args], cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _git_text(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def _make_repo(parent: Path, *, mod_src: str, test_src: str = "", extra=None) -> Path:
    repo = parent / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "mod.py").write_text(mod_src)
    if test_src:
        (repo / "test_mod.py").write_text(test_src)
    for name, content in (extra or {}).items():
        (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _sql(db: str, s: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sqlite3", db, s], capture_output=True, text=True)


def _seed_task_run(db: str, rid: str = "r1", status: str = "planned") -> None:
    _sql(db, f"INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,"
             f"cost_usd,created_at,updated_at) VALUES ('{rid}','code_fix','v1','k.md','{status}',"
             f"0.0,strftime('%s','now','-60 seconds'),strftime('%s','now'));")


def _fake(response, rc=0):
    def d(_task_class, _node_type, _prompt):
        return rc, response
    return d


def _run_dir(tmp_path: Path, *, summary=None, test_payload=None, behavioral=None,
             name: str = "run") -> Path:
    rd = tmp_path / name
    rd.mkdir(exist_ok=True)
    if summary is not None:
        (rd / "implementer-summary.json").write_text(json.dumps(summary))
    if test_payload is not None:
        (rd / "verifier_test.json").write_text(test_payload)
    if behavioral is not None:
        (rd / "verifier_behavioral.json").write_text(behavioral)
    return rd


def _test_evidence(payload: dict) -> str:
    """The stderr-prefixed evidence shape: a ``[test] running:`` line, then JSON."""
    return "[test] running: x\n" + json.dumps(payload) + "\n"


# ── 1. pure tests ────────────────────────────────────────────────────────────


def test_applies_proven_refuted_unverified(tmp_path):
    # implemented + 1 file -> PROVEN
    rd = _run_dir(tmp_path, summary={"status": "implemented", "files_changed": ["mod.py"]})
    vector, _ = L.derive_levels(str(rd))
    assert vector["applies"] == PROVEN

    # real _write_implementer_summary on an untouched repo -> no_changes -> REFUTED
    repo = _make_repo(tmp_path, mod_src="def add(a, b):\n    return a - b\n")
    rd2 = tmp_path / "run2"
    rd2.mkdir()
    ex._write_implementer_summary(str(rd2), str(repo), "impl.log")
    vector2, _ = L.derive_levels(str(rd2))
    assert vector2["applies"] == REFUTED

    # file missing -> UNVERIFIED
    rd3 = _run_dir(tmp_path, name="run3")
    vector3, _ = L.derive_levels(str(rd3))
    assert vector3["applies"] == UNVERIFIED

    # implemented + [] -> UNVERIFIED (git derivation fallback edge)
    rd4 = _run_dir(tmp_path, summary={"status": "implemented", "files_changed": []},
                   name="run4")
    vector4, _ = L.derive_levels(str(rd4))
    assert vector4["applies"] == UNVERIFIED


def test_executes_preserve_nonnested(tmp_path):
    cases = [
        ({"post_rc": 0, "base_rc": ""}, PROVEN, PROVEN),
        ({"post_rc": 1, "base_rc": "0"}, PROVEN, REFUTED),   # the non-nested case
        ({"post_rc": 2, "base_rc": "0"}, REFUTED, REFUTED),
        ({"post_rc": 2, "base_rc": "2"}, UNVERIFIED, UNVERIFIED),
        ({}, UNVERIFIED, UNVERIFIED),                        # no post_rc
    ]
    for i, (extra, exp_exec, exp_pres) in enumerate(cases):
        payload = {"verifier": "test", "pass": True, **extra}
        rd = _run_dir(tmp_path, test_payload=_test_evidence(payload), name=f"run{i}")
        vector, _ = L.derive_levels(str(rd))
        assert vector["executes"] == exp_exec, (i, extra)
        assert vector["preserve"] == exp_pres, (i, extra)


def test_target(tmp_path):
    cases = [
        ({"pass": True, "post_rc": 0, "replay": {"overlap": ["t::a"]}}, PROVEN),
        ({"pass": False, "post_rc": 0, "replay": {"overlap": []}}, REFUTED),
        ({"pass": False, "post_rc": 0, "replay_unverified": True,
          "replay": {"overlap": []}}, UNVERIFIED),
        ({"pass": False, "post_rc": 0, "adequacy_unverified": True,
          "replay": {"overlap": ["t::a"]}}, UNVERIFIED),
        ({"pass": True, "post_rc": 0}, UNVERIFIED),   # rc 0 + pass + no replay
        ({"pass": True, "post_rc": 1}, UNVERIFIED),   # rc 1
    ]
    for i, (extra, exp_target) in enumerate(cases):
        payload = {"verifier": "test", "base_rc": "", **extra}
        rd = _run_dir(tmp_path, test_payload=_test_evidence(payload), name=f"run{i}")
        vector, _ = L.derive_levels(str(rd))
        assert vector["target"] == exp_target, (i, extra)

    # adequacy_unverified downgrades target AND preserve
    rd = _run_dir(tmp_path, test_payload=_test_evidence(
        {"verifier": "test", "pass": False, "post_rc": 0, "base_rc": "",
         "adequacy_unverified": True, "replay": {"overlap": ["t::a"]}}),
        name="au")
    vector, _ = L.derive_levels(str(rd))
    assert vector["target"] == UNVERIFIED
    assert vector["preserve"] == UNVERIFIED


def test_contract(tmp_path):
    # absent -> n/a
    rd = _run_dir(tmp_path, name="absent")
    vector, _ = L.derive_levels(str(rd))
    assert vector["contract"] == NA

    # real multi-line BehavioralVerdict REFUTED -> REFUTED
    rd2 = _run_dir(tmp_path, name="refuted",
                   behavioral=BehavioralVerdict(status=REFUTED, surface="api").to_json())
    vector2, _ = L.derive_levels(str(rd2))
    assert vector2["contract"] == REFUTED

    # PROVEN twin -> PROVEN
    rd3 = _run_dir(tmp_path, name="proven",
                   behavioral=BehavioralVerdict(status=PROVEN, surface="api").to_json())
    vector3, _ = L.derive_levels(str(rd3))
    assert vector3["contract"] == PROVEN

    # garbage file -> UNVERIFIED
    rd4 = _run_dir(tmp_path, name="garbage", behavioral="garbage\nnot json\n")
    vector4, _ = L.derive_levels(str(rd4))
    assert vector4["contract"] == UNVERIFIED


def test_empty_run_dir(tmp_path):
    rd = _run_dir(tmp_path, name="empty")
    vector, _ = L.derive_levels(str(rd))
    for level in ("applies", "executes", "target", "preserve"):
        assert vector[level] == UNVERIFIED
    assert vector["contract"] == NA
    assert not any(value == PROVEN for value in vector.values())
    assert L.publish_decision(vector, required=L.required_levels("code_fix")) == "abstain"


def test_predicate():
    req = L.required_levels("code_fix")
    base = {k: PROVEN for k in req}
    base["contract"] = NA

    # target PROVEN + preserve REFUTED -> refute
    v = dict(base)
    v["preserve"] = REFUTED
    assert L.publish_decision(v, required=req) == "refute"
    assert L.all_levels_ok(v, required=req) is False

    # preserve UNVERIFIED -> abstain
    v2 = dict(base)
    v2["preserve"] = UNVERIFIED
    assert L.publish_decision(v2, required=req) == "abstain"

    # four PROVEN + contract n/a -> publish
    assert L.publish_decision(base, required=req) == "publish"
    assert L.all_levels_ok(base, required=req) is True

    # required=("contract",) with n/a -> abstain
    assert L.publish_decision(base, required=("contract",)) == "abstain"

    # required_levels("docs") == () -> publish
    assert L.required_levels("docs") == ()
    assert L.publish_decision(base, required=()) == "publish"


def test_enabled_knob():
    assert L.enabled({}) is False
    assert L.enabled({"MO_LEVEL_VECTOR": "0"}) is False
    assert L.enabled({"MO_LEVEL_VECTOR": "true"}) is False
    assert L.enabled({"MO_LEVEL_VECTOR": "1"}) is True


def test_read_verifier_payload(tmp_path):
    # trailing non-JSON line + object with another verifier are skipped bottom-up
    rd = _run_dir(tmp_path, name="parser")
    (rd / "verifier_test.json").write_text(
        "[test] running: x\n"
        '{"verifier": "test", "pass": true, "post_rc": 0}\n'
        '{"verifier": "other", "pass": false}\n'
        "garbage line\n"
    )
    payload = L.read_verifier_payload(str(rd / "verifier_test.json"), "test")
    assert payload == {"verifier": "test", "pass": True, "post_rc": 0}

    # multi-line indent=2 behavioral payload
    rd2 = _run_dir(tmp_path, name="parser2",
                   behavioral=BehavioralVerdict(status=REFUTED, surface="api").to_json())
    b = L.read_verifier_payload(str(rd2 / "verifier_behavioral.json"), "behavioral")
    assert b["status"] == REFUTED

    # absent file -> None
    assert L.read_verifier_payload(str(rd / "nope.json"), "test") is None


def test_emit_run_verdict_bytes(tmp_path, monkeypatch):
    old_bytes = b'{"verdict":"pass","failed_nodes":0,"dispatched":3,"source":"execute@run-level"}\n'

    monkeypatch.delenv("MO_LEVEL_VECTOR", raising=False)
    rd = tmp_path / "off"; rd.mkdir()
    ex._emit_run_verdict(str(rd), 0, 3)
    assert (rd / "verdict.json").read_bytes() == old_bytes

    monkeypatch.setenv("MO_LEVEL_VECTOR", "0")
    rd0 = tmp_path / "zero"; rd0.mkdir()
    ex._emit_run_verdict(str(rd0), 0, 3)
    assert (rd0 / "verdict.json").read_bytes() == old_bytes

    monkeypatch.setenv("MO_LEVEL_VECTOR", "1")
    rd1 = tmp_path / "one"; rd1.mkdir()
    ex._emit_run_verdict(str(rd1), 0, 3)
    text = (rd1 / "verdict.json").read_text()
    obj = json.loads(text)
    assert text.startswith(
        '{"verdict":"pass","failed_nodes":0,"dispatched":3,"source":"execute@run-level",')
    assert list(obj.keys())[:4] == ["verdict", "failed_nodes", "dispatched", "source"]
    for key in ("levels", "levels_reasons", "levels_required", "levels_ok", "levels_decision"):
        assert key in obj
    assert obj["levels_ok"] is True

    # dry_run with the knob on -> no levels key
    monkeypatch.setenv("MO_LEVEL_VECTOR", "1")
    rdd = tmp_path / "dry"; rdd.mkdir()
    ex._emit_run_verdict(str(rdd), 0, 3, dry_run=True)
    assert (rdd / "verdict.dryrun.json").read_bytes() == old_bytes


# ── 2. real-entrypoint tests ─────────────────────────────────────────────────

MOD_BUG = "def add(a, b):\n    return a - b\n"
MOD_FIX = "def add(a, b):\n    return a + b\n"

TEST_UNITTEST = (
    "import unittest\n"
    "from mod import add\n\n"
    "class TestAdd(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)

PYTEST_CMD = f"{sys.executable} -m pytest -p no:cacheprovider"
UNITTEST_CMD = f"{sys.executable} -m unittest"
assert "pytest" not in UNITTEST_CMD  # the abstention case relies on a non-pytest runner

WORKFLOW = (
    "dispatch_mode: serial\n"
    "nodes:\n"
    "  - {name: test, type: verifier, description: t, verifier_ref: verifiers/test.py}\n"
    "  - {name: publisher, type: publisher, description: p}\n"
)


def _drive_main(tmp_path, monkeypatch, *, repo, run_id, test_cmd, knob,
                extra_env=None):
    """Stand up a real home/db/run-dir/workflow, run the REAL verifier + publisher
    through ``ex.main``, and return ``(rc, db, rd)``."""
    home = tmp_path / "mo-home"
    home.mkdir(exist_ok=True)
    db = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        capture_output=True, text=True, check=True,
    )
    rd = home / "runs" / run_id
    rd.mkdir(parents=True)
    plan = rd / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "task_class": "code_fix",
                                "artifact_contract": {"outputs": []}}))
    wf = tmp_path / "wf.yaml"
    wf.write_text(WORKFLOW)
    _seed_task_run(db, rid=run_id)

    # The real git-derived implementer summary the publisher commit gate reads,
    # and a reviewer stand-in (the vector never reads it).
    ex._write_implementer_summary(str(rd), str(repo), "impl.log")
    (rd / "review-verdict.json").write_text(json.dumps({"verdict": "pass"}))

    env = {
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_WORKFLOW": str(wf),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": db,
        "MINI_ORK_PLAN_PATH": str(plan),
        "MINI_ORK_RUN_DIR": str(rd),
        "MINI_ORK_RUN_ID": run_id,
        "MINI_ORK_RECIPE": "code-fix",
        "MO_TARGET_CWD": str(repo),
        "MINI_ORK_TEST_CMD": test_cmd,
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MO_ORACLE_GATES_AUTO": "0",
        "MO_GRADE_RUN_REWARD": "0",
        "MO_LEARNING_WRITEBACK": "0",
        "MO_LANE_ROUTER": "0",
    }
    if knob:
        env["MO_LEVEL_VECTOR"] = "1"
    if extra_env:
        env.update(extra_env)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("MINI_ORK_RECIPE_ROOT", "MO_CODEFIX_REPLAY", "MO_TEST_BASELINE"):
        monkeypatch.delenv(key, raising=False)

    rc = ex.main([], root=str(REPO), dispatch_fn=_fake(""))
    return rc, db, rd


def _status(db, rid):
    return _sql(db, f"SELECT status FROM task_runs WHERE id='{rid}';").stdout.strip()


def test_real_strong_knob_on(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, mod_src=MOD_BUG, test_src=TEST_UNITTEST)
    (repo / "mod.py").write_text(MOD_FIX)  # uncommitted fix
    rc, db, rd = _drive_main(tmp_path, monkeypatch, repo=repo, run_id="r9",
                             test_cmd=PYTEST_CMD, knob=True)
    assert rc == 0

    verdict = json.loads((rd / "verdict.json").read_text())
    assert verdict["failed_nodes"] == 0
    for level in ("applies", "executes", "target", "preserve"):
        assert verdict["levels"][level] == PROVEN, (level, verdict["levels"])
    assert verdict["levels"]["contract"] == NA
    assert verdict["levels_ok"] is True

    head_msg = _git_text(repo, "log", "-1", "--pretty=%s")
    assert head_msg.startswith("mini-ork(code-fix): ")
    committed = [ln for ln in
                 _git_text(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
                 .splitlines() if ln]
    assert committed == ["mod.py"]
    assert _status(db, "r9") == "published"


def test_real_replay_abstain_knob_on(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, mod_src=MOD_BUG, test_src=TEST_UNITTEST)
    (repo / "mod.py").write_text(MOD_FIX)
    head_before = _git_text(repo, "rev-parse", "HEAD")
    rc, db, rd = _drive_main(tmp_path, monkeypatch, repo=repo, run_id="r10",
                             test_cmd=UNITTEST_CMD, knob=True)
    assert rc == 0

    payload = L.read_verifier_payload(str(rd / "verifier_test.json"), "test")
    assert payload["replay_unverified"] is True
    assert payload["pass"] is False

    verdict = json.loads((rd / "verdict.json").read_text())
    assert verdict["failed_nodes"] == 0
    assert verdict["levels"]["target"] == UNVERIFIED
    assert verdict["levels_ok"] is False
    assert verdict["levels_decision"] == "abstain"

    # no rollback, no commit
    assert _git_text(repo, "rev-parse", "HEAD") == head_before
    assert (repo / "mod.py").read_text() == MOD_FIX
    assert _status(db, "r10") == "failed"


def test_real_replay_abstain_knob_off(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, mod_src=MOD_BUG, test_src=TEST_UNITTEST)
    (repo / "mod.py").write_text(MOD_FIX)
    rc, db, rd = _drive_main(tmp_path, monkeypatch, repo=repo, run_id="r11",
                             test_cmd=UNITTEST_CMD, knob=False)
    assert rc == 0

    # verdict.json is the old literal bytes (today's gap: the abstention publishes)
    assert (rd / "verdict.json").read_bytes() == (
        b'{"verdict":"pass","failed_nodes":0,"dispatched":2,"source":"execute@run-level"}\n')
    assert _git_text(repo, "log", "-1", "--pretty=%s").startswith("mini-ork(code-fix): ")
    assert _status(db, "r11") == "published"


def test_real_prered_knob_on(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, mod_src=MOD_BUG, test_src=TEST_UNITTEST,
                      extra={"test_env.py": "def test_env():\n    assert False\n"})
    (repo / "mod.py").write_text(MOD_FIX)
    head_before = _git_text(repo, "rev-parse", "HEAD")
    rc, db, rd = _drive_main(tmp_path, monkeypatch, repo=repo, run_id="r12",
                             test_cmd=PYTEST_CMD, knob=True)
    assert rc == 0

    payload = L.read_verifier_payload(str(rd / "verifier_test.json"), "test")
    assert payload["pass"] is True
    assert payload["post_rc"] == 1
    assert payload["base_rc"] == "1"

    verdict = json.loads((rd / "verdict.json").read_text())
    assert verdict["levels"]["executes"] == PROVEN
    assert verdict["levels"]["target"] == UNVERIFIED
    assert verdict["levels"]["preserve"] == UNVERIFIED

    assert _git_text(repo, "rev-parse", "HEAD") == head_before
    assert _status(db, "r12") == "failed"


def test_real_adequacy_knob_on(tmp_path, monkeypatch):
    mod_head = (
        "def add(a, b):\n    return a - b\n\n\n"
        "def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n\n\n"
        "def is_even(n):\n    return n % 2 == 0\n"
    )
    mod_fix = mod_head.replace("return a - b", "return a + b")
    repo = _make_repo(tmp_path, mod_src=mod_head, test_src=TEST_UNITTEST)
    (repo / "mod.py").write_text(mod_fix)
    head_before = _git_text(repo, "rev-parse", "HEAD")
    rc, db, rd = _drive_main(tmp_path, monkeypatch, repo=repo, run_id="r13",
                             test_cmd=PYTEST_CMD, knob=True,
                             extra_env={"MO_SUITE_ADEQUACY": "1"})
    assert rc == 0

    payload = L.read_verifier_payload(str(rd / "verifier_test.json"), "test")
    assert payload["adequacy_unverified"] is True
    assert payload["suite_adequacy"]["verdict"] == "INADEQUATE"

    verdict = json.loads((rd / "verdict.json").read_text())
    assert verdict["levels_decision"] == "abstain"

    assert _git_text(repo, "rev-parse", "HEAD") == head_before
    assert _status(db, "r13") == "failed"


def test_publisher_refute(tmp_path, monkeypatch):
    # untouched repo -> the real writer derives a no_changes summary
    repo = _make_repo(tmp_path, mod_src=MOD_BUG, test_src=TEST_UNITTEST)
    rd = tmp_path / "rd"
    rd.mkdir()
    ex._write_implementer_summary(str(rd), str(repo), "impl.log")
    # test-9 evidence: pass + replay overlap
    (rd / "verifier_test.json").write_text(_test_evidence(
        {"verifier": "test", "pass": True, "post_rc": 0, "base_rc": "",
         "replay": {"overlap": ["t::a"]}}))
    monkeypatch.setenv("MO_LEVEL_VECTOR", "1")

    statuses = []
    monkeypatch.setattr(ex, "set_status",
                        lambda db, run_id, status: statuses.append(status))

    head_before = _git_text(repo, "rev-parse", "HEAD")
    rc, reason = ex.publisher_node(str(REPO), str(rd), "", "r", "code-fix", "code_fix")
    assert (rc, reason) == (1, "levels_refuted")
    assert "published" not in statuses
    assert _git_text(repo, "rev-parse", "HEAD") == head_before
