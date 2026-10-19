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


def test_target_na(tmp_path):
    # replay instrument n/a (non-pytest runner) + green suite -> target = n/a,
    # and the required code_fix vector publishes.
    rd = _run_dir(
        tmp_path,
        summary={"status": "implemented", "files_changed": ["mod.py"]},
        test_payload=_test_evidence({
            "verifier": "test", "pass": False, "post_rc": 0, "base_rc": "",
            "replay_unverified": True, "replay_applicable": False,
        }),
        name="na",
    )
    vector, reasons = L.derive_levels(str(rd))
    assert vector["target"] == NA
    assert reasons["target"] == "verifier_test.json: replay instrument n/a for this test runner"
    assert vector["applies"] == PROVEN
    assert vector["executes"] == PROVEN
    assert vector["preserve"] == PROVEN
    assert L.publish_decision(vector, required=L.required_levels("code_fix")) == "publish"

    # replay_applicable absent (a pytest replay that abstained for another
    # reason) -> still UNVERIFIED, still abstain, even with the other three
    # required levels PROVEN.
    rd2 = _run_dir(
        tmp_path,
        summary={"status": "implemented", "files_changed": ["mod.py"]},
        test_payload=_test_evidence({
            "verifier": "test", "pass": False, "post_rc": 0, "base_rc": "",
            "replay_unverified": True,
        }),
        name="na2",
    )
    vector2, _ = L.derive_levels(str(rd2))
    assert vector2["target"] == UNVERIFIED
    assert L.publish_decision(vector2, required=L.required_levels("code_fix")) == "abstain"

    # post_rc != 0 + replay_applicable false -> no n/a, no publish.
    rd3 = _run_dir(
        tmp_path,
        test_payload=_test_evidence({
            "verifier": "test", "pass": False, "post_rc": 1, "base_rc": "0",
            "replay_unverified": True, "replay_applicable": False,
        }),
        name="na3",
    )
    vector3, _ = L.derive_levels(str(rd3))
    assert vector3["target"] == UNVERIFIED


def test_target_waiver(tmp_path, monkeypatch):
    """A verifier that turned its own instrument off names it; only an explicit
    operator waiver (MO_ALLOW_UNPROVEN_TARGET=1) turns that into publishable n/a.
    """
    payload = {
        "verifier": "test", "pass": True, "post_rc": 0, "base_rc": "",
        "target_waiver": "replay disabled by MO_CODEFIX_REPLAY=0",
    }

    def run(name):
        rd = _run_dir(
            tmp_path,
            summary={"status": "implemented", "files_changed": ["mod.py"]},
            test_payload=_test_evidence(payload),
            name=name,
        )
        return L.derive_levels(str(rd))

    # Default: the disabled instrument still abstains, but the reason is
    # actionable and names the switch — not the old opaque "not proven".
    monkeypatch.delenv("MO_ALLOW_UNPROVEN_TARGET", raising=False)
    vector, reasons = run("waiver-off")
    assert vector["target"] == UNVERIFIED
    assert "target instrument disabled" in reasons["target"]
    assert "MO_CODEFIX_REPLAY=0" in reasons["target"]
    assert "MO_ALLOW_UNPROVEN_TARGET=1" in reasons["target"]
    assert L.publish_decision(vector, required=L.required_levels("code_fix")) == "abstain"

    # Explicit waiver: publishable n/a, and the verdict records that it was waived.
    monkeypatch.setenv("MO_ALLOW_UNPROVEN_TARGET", "1")
    vector2, reasons2 = run("waiver-on")
    assert vector2["target"] == NA
    assert "waived by MO_ALLOW_UNPROVEN_TARGET=1" in reasons2["target"]
    assert L.publish_decision(vector2, required=L.required_levels("code_fix")) == "publish"

    # A waiver can never launder a failing suite into a publish.
    failing = {**payload, "pass": False}
    rd = _run_dir(
        tmp_path,
        summary={"status": "implemented", "files_changed": ["mod.py"]},
        test_payload=_test_evidence(failing),
        name="waiver-failing",
    )
    vector3, _ = L.derive_levels(str(rd))
    assert vector3["target"] != NA


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


SCHEMA_EVIDENCE_PASS = (
    "VERIFY OK: stable verified-artifact envelope passed\n"
    '{\n  "pass": true,\n  "checks": [\n'
    '    {"id": "envelope", "pass": true, "gating": true, "reason": ""},\n'
    '    {"id": "title_matches_manifest", "pass": true, "gating": false, "reason": ""}\n'
    "  ]\n}\n"
)


def test_contract_from_recipe_verifier_evidence(tmp_path):
    """The artifact recipe's own deterministic verifier publishes its envelope
    to <run_dir>/verifier_schema.json; that evidence PROVES contract for
    verified_artifact (run-1791736854 published with every level UNVERIFIED
    while this evidence held a full pass)."""
    rd = _run_dir(tmp_path, name="recipe_proven")
    (rd / "verifier_schema.json").write_text(SCHEMA_EVIDENCE_PASS, encoding="utf-8")
    vector, reasons = L.derive_levels(str(rd), "verified_artifact")
    assert vector["contract"] == PROVEN
    assert "verifier_schema.json" in reasons["contract"]
    # The tree-change quartet is n/a for a class whose recipe owns none of
    # those instruments — not three abstentions on a publish.
    assert vector["applies"] == NA
    assert vector["executes"] == NA
    assert vector["target"] == NA
    assert vector["preserve"] == NA
    assert L.publish_decision(vector, required=L.required_levels("verified_artifact")) == "publish"

    # The evidence scores the same WITHOUT a task class (an instrument that
    # ran is scored whatever the caller knew about the recipe).
    vector_legacy, _ = L.derive_levels(str(rd))
    assert vector_legacy["contract"] == PROVEN


def test_contract_refuted_by_recipe_verifier_evidence(tmp_path):
    # envelope pass:false -> REFUTED (publisher rc 1 -> rollback/reject)
    rd = _run_dir(tmp_path, name="recipe_refuted")
    (rd / "verifier_schema.json").write_text(
        'VERIFY FAIL\n{"pass": false, "checks": [{"id": "envelope", "pass": false, "gating": true}]}\n',
        encoding="utf-8",
    )
    vector, _ = L.derive_levels(str(rd), "verified_artifact")
    assert vector["contract"] == REFUTED
    assert L.publish_decision(vector, required=L.required_levels("verified_artifact")) == "refute"

    # envelope pass:true but a GATING check failed -> still REFUTED; a
    # non-gating failure alone does not refute.
    rd2 = _run_dir(tmp_path, name="recipe_gating")
    (rd2 / "verifier_schema.json").write_text(
        '{"pass": true, "checks": ['
        '{"id": "required_h2", "pass": false, "gating": true}, '
        '{"id": "title_matches_manifest", "pass": false, "gating": false}]}\n',
        encoding="utf-8",
    )
    vector2, _ = L.derive_levels(str(rd2), "verified_artifact")
    assert vector2["contract"] == REFUTED

    rd3 = _run_dir(tmp_path, name="recipe_nongating")
    (rd3 / "verifier_schema.json").write_text(
        '{"pass": true, "checks": ['
        '{"id": "title_matches_manifest", "pass": false, "gating": false}]}\n',
        encoding="utf-8",
    )
    vector3, _ = L.derive_levels(str(rd3), "verified_artifact")
    assert vector3["contract"] == PROVEN


def test_contract_recipe_verifier_toolchain_failure_abstains(tmp_path):
    """A recipe verifier that could not run its tool (``failure_kind: toolchain``)
    is UNVERIFIED — an abstention — never REFUTED (audit 2026-10-11: npm refusing
    to start on a pnpm repo before eslint ran). Fail-closed: ``code`` and an
    absent key keep the verdict exactly as it was (REFUTED)."""
    rd = _run_dir(tmp_path, name="recipe_toolchain")
    (rd / "verifier_schema.json").write_text(
        '{"pass": false, "failure_kind": "toolchain", '
        '"checks": [{"id": "envelope", "pass": false, "gating": true}]}\n',
        encoding="utf-8",
    )
    vector, reasons = L.derive_levels(str(rd), "verified_artifact")
    assert vector["contract"] == UNVERIFIED
    assert "could not run" in reasons["contract"]
    assert L.publish_decision(
        vector, required=L.required_levels("verified_artifact")) == "abstain"

    # failure_kind: code -> the tool ran and rejected the input -> REFUTED
    rd2 = _run_dir(tmp_path, name="recipe_code")
    (rd2 / "verifier_schema.json").write_text(
        '{"pass": false, "failure_kind": "code", '
        '"checks": [{"id": "envelope", "pass": false, "gating": true}]}\n',
        encoding="utf-8",
    )
    vector2, _ = L.derive_levels(str(rd2), "verified_artifact")
    assert vector2["contract"] == REFUTED

    # key absent -> REFUTED (nothing changes for a verifier that never declared)
    rd3 = _run_dir(tmp_path, name="recipe_absent")
    (rd3 / "verifier_schema.json").write_text(
        '{"pass": false, "checks": [{"id": "envelope", "pass": false, "gating": true}]}\n',
        encoding="utf-8",
    )
    vector3, _ = L.derive_levels(str(rd3), "verified_artifact")
    assert vector3["contract"] == REFUTED


_CODEFIX_VERIFIERS = Path(__file__).resolve().parents[2] / "recipes" / "code-fix" / "verifiers"


def _run_recipe_verifier(script: str, tmp_path: Path, env_extra: dict) -> str:
    """One code-fix recipe verifier's stdout, run from a bare directory.

    A bare cwd is the point: ``detect_typecheck`` finds no `package.json`, no
    tsconfig marker and no mypy config, so the typecheck verifier takes its
    skip branch — the case that used to make the contract level unreachable.
    """
    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    env.update(env_extra)
    proc = subprocess.run([sys.executable, str(_CODEFIX_VERIFIERS / script)],
                          capture_output=True, text=True, cwd=tmp_path, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_both_code_fix_contract_envelopes_survive_the_shape_guard(tmp_path):
    """Every contract evidence file must parse, not just the one under test.

    The defect this pins: ``_read_verifier_evidence`` accepts a recipe
    verifier's envelope only with a bool ``pass`` *and* a list ``checks``, and
    the deriver returns UNVERIFIED if **any** evidence file is unparsable. For
    code-fix the contract level reads two files — ``verifier_typecheck.json``
    (no mypy/pyright on the target → the skip path) and
    ``verifier_metamorphic.json`` (no relation spec → vacuous) — and *both*
    failed the guard, so contract was unreachable. A test covering one of them
    would have passed while the level stayed UNVERIFIED.

    Parsing, though, is only half of it. Both of these outcomes are the
    verifier saying it examined nothing, so both must ALSO abstain — the
    engine's ``status: "unverified"`` marker, which ``eval_judge``,
    ``execute_handlers`` and ``run_flow`` already read that way. Without it the
    level mints PROVEN out of a skip, which is the failure this half pins: an
    envelope that carries ``pass: true`` must not be read as a judgement the
    tool never made.
    """
    empty = tmp_path / "target"
    empty.mkdir()
    rd = _run_dir(tmp_path, name="codefix_shapes")
    (rd / "verifier_typecheck.json").write_text(
        _run_recipe_verifier("typecheck.py", empty, {}), encoding="utf-8")
    (rd / "verifier_metamorphic.json").write_text(
        _run_recipe_verifier("metamorphic.py", empty, {"MO_METAMORPHIC_SPEC": ""}),
        encoding="utf-8")

    for name in ("verifier_typecheck.json", "verifier_metamorphic.json"):
        parsed = L._read_verifier_evidence(str(rd / name))
        assert parsed is not None, f"{name} fails the envelope shape guard"
        assert parsed["checks"], f"{name} carries an empty checks list"
        assert parsed["status"] == "unverified", f"{name} claims a judgement it did not make"

    vector, reasons = L.derive_levels(str(rd), "code_fix")
    assert vector["contract"] == UNVERIFIED, reasons["contract"]

    # The level must still be reachable: a metamorphic relation that actually
    # ran and held is a judgement, and PROVEN is the honest answer for it.
    spec = tmp_path / "meta_spec.py"
    spec.write_text(
        "from mini_ork.learning.metamorphic import commutativity\n"
        "def _add(x):\n"
        "    a, b = x\n"
        "    return a + b\n"
        "TARGET = _add\n"
        "SEED_INPUTS = [(2, 3), (10, 20)]\n"
        "RELATIONS = [commutativity()]\n",
        encoding="utf-8")
    ran = _run_dir(tmp_path, name="codefix_ran")
    (ran / "verifier_metamorphic.json").write_text(
        _run_recipe_verifier("metamorphic.py", empty, {"MO_METAMORPHIC_SPEC": str(spec)}),
        encoding="utf-8")
    ran_parsed = L._read_verifier_evidence(str(ran / "verifier_metamorphic.json"))
    assert ran_parsed["pass"] is True
    assert ran_parsed.get("status") is None, "a real pass is not an abstention"
    ran_vector, ran_reasons = L.derive_levels(str(ran), "code_fix")
    assert ran_vector["contract"] == PROVEN, ran_reasons["contract"]

    # Negative control — the exact shapes these replaced. Both fail the guard
    # (the metamorphic one on an ``int`` count, the typecheck one on a missing
    # ``checks`` key), which is what pinned the level at UNVERIFIED. Without
    # this the test could pass for the wrong reason.
    stale = _run_dir(tmp_path, name="codefix_stale")
    (stale / "verifier_typecheck.json").write_text(
        '{"verifier": "typecheck", "pass": true, "evidence_path": null, '
        '"error_summary": "no typecheck tool detected — skipped"}\n',
        encoding="utf-8")
    (stale / "verifier_metamorphic.json").write_text(
        '{"verdict": "vacuous", "checks": 0, "note": "no metamorphic relation ran"}\n',
        encoding="utf-8")
    for name in ("verifier_typecheck.json", "verifier_metamorphic.json"):
        assert L._read_verifier_evidence(str(stale / name)) is None, name
    stale_vector, _ = L.derive_levels(str(stale), "code_fix")
    assert stale_vector["contract"] == UNVERIFIED


def test_abstained_kinds_scan_and_fail_closed_accessors(tmp_path):
    """``abstained_kinds`` is the publisher guard's input: it scans every
    ``verifier_*.json`` and returns only genuine abstentions, and the accessors
    abstain only on an explicit ABSTAINING_KINDS member (fail-closed)."""
    rd = _run_dir(tmp_path, name="abstain_scan")
    (rd / "verifier_schema.json").write_text(
        '{"pass": false, "failure_kind": "toolchain", "checks": []}\n', encoding="utf-8")
    (rd / "verifier_lint.json").write_text(
        '{"pass": false, "failure_kind": "code", "checks": []}\n', encoding="utf-8")
    assert L.abstained_kinds(str(rd)) == ["toolchain"]

    rd2 = _run_dir(tmp_path, name="no_abstain")
    (rd2 / "verifier_schema.json").write_text('{"pass": true, "checks": []}\n', encoding="utf-8")
    assert L.abstained_kinds(str(rd2)) == []

    rd3 = _run_dir(tmp_path, name="empty_abstain")
    assert L.abstained_kinds(str(rd3)) == []

    # accessors
    assert L.abstain_kind({"pass": False, "failure_kind": "toolchain"}) == "toolchain"
    assert L.abstain_kind({"pass": False, "failure_kind": "infra"}) == ""   # declared, not abstaining
    assert L.abstain_kind({"pass": False, "failure_kind": "weird"}) == ""   # unrecognised
    assert L.abstain_kind({"pass": False}) == ""                           # absent
    assert L.abstain_kind({"pass": True, "failure_kind": "toolchain"}) == ""  # never over a pass


def test_contract_required_but_no_evidence_abstains(tmp_path):
    # verified_artifact with NO evidence at all: contract is UNVERIFIED (a
    # required level may never ride an absent instrument to publish), and the
    # decision abstains.
    rd = _run_dir(tmp_path, name="recipe_bare")
    vector, reasons = L.derive_levels(str(rd), "verified_artifact")
    assert vector["contract"] == UNVERIFIED
    assert "required" in reasons["contract"]
    assert L.publish_decision(vector, required=L.required_levels("verified_artifact")) == "abstain"

    # unparsable evidence -> UNVERIFIED, not a pass
    rd2 = _run_dir(tmp_path, name="recipe_unparsable")
    (rd2 / "verifier_schema.json").write_text("not json at all\n", encoding="utf-8")
    vector2, _ = L.derive_levels(str(rd2), "verified_artifact")
    assert vector2["contract"] == UNVERIFIED
    assert L.publish_decision(vector2, required=L.required_levels("verified_artifact")) == "abstain"

    # a class that does not require contract keeps the absent -> n/a reading
    rd3 = _run_dir(tmp_path, name="other_bare")
    vector3, _ = L.derive_levels(str(rd3), "ui_audit")
    assert vector3["contract"] == NA
    assert L.publish_decision(vector3, required=L.required_levels("ui_audit")) == "publish"


def test_tree_levels_na_only_for_non_code_edit_classes(tmp_path):
    # code_fix WITHOUT evidence still abstains on every tree level (legacy
    # semantics — the instruments exist for the class, they just proved nothing)
    rd = _run_dir(tmp_path, name="codefix_bare")
    vector, _ = L.derive_levels(str(rd), "code_fix")
    assert vector["applies"] == UNVERIFIED
    assert vector["executes"] == UNVERIFIED
    assert vector["target"] == UNVERIFIED
    assert vector["preserve"] == UNVERIFIED
    assert L.publish_decision(vector, required=L.required_levels("code_fix")) == "abstain"

    # and a non-code-edit class that DOES have verifier_test.json is scored,
    # never n/a'd away (a present instrument is read whatever the class)
    rd2 = _run_dir(
        tmp_path, name="generic_with_test",
        summary={"status": "implemented", "files_changed": ["a.py"]},
        test_payload=_test_evidence({"verifier": "test", "post_rc": 0, "pass": True,
                                      "replay": {"overlap": ["test_x"]}}),
    )
    vector2, _ = L.derive_levels(str(rd2), "ui_audit")
    assert vector2["applies"] == PROVEN
    assert vector2["executes"] == PROVEN
    assert vector2["preserve"] == PROVEN


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

    # required=("contract",) with n/a -> publish (n/a does not block)
    assert L.publish_decision(base, required=("contract",)) == "publish"

    # required_levels("docs") == () -> publish
    assert L.required_levels("docs") == ()
    assert L.publish_decision(base, required=()) == "publish"


def test_publish_decision_na():
    # n/a does not block publish
    assert L.publish_decision({"target": NA, "executes": PROVEN},
                              required=("target", "executes")) == "publish"
    # refute still wins over n/a
    assert L.publish_decision({"target": NA, "executes": REFUTED},
                              required=("target", "executes")) == "refute"
    # n/a + UNVERIFIED still abstains
    assert L.publish_decision({"target": NA, "executes": UNVERIFIED},
                              required=("target", "executes")) == "abstain"


def test_enabled_knob():
    assert L.enabled({}) is True
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

    monkeypatch.setenv("MO_LEVEL_VECTOR", "0")  # DEFAULT ON now; pin OFF
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

    # a run that abstained flips the verdict and appends failure_kinds (last),
    # but keeps the four byte-stable pairs at the head
    monkeypatch.setenv("MO_LEVEL_VECTOR", "1")
    rda = tmp_path / "abstain"; rda.mkdir()
    (rda / "verifier_schema.json").write_text(
        '{"pass": false, "failure_kind": "toolchain", "checks": []}\n', encoding="utf-8")
    ex._emit_run_verdict(str(rda), 0, 3)
    obj_a = json.loads((rda / "verdict.json").read_text())
    assert obj_a["verdict"] == "abstain"
    assert obj_a["failed_nodes"] == 0
    assert list(obj_a.keys())[:4] == ["verdict", "failed_nodes", "dispatched", "source"]
    assert obj_a["failure_kinds"] == ["toolchain"]

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


# The implementer's emitted unified diff (MOD_BUG → MOD_FIX). Written as
# impl-<node>.log so the publisher's authored-patch resolver has a source.
_AUTHORED_DIFF = (
    "--- a/mod.py\n"
    "+++ b/mod.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)


def _drive_main(tmp_path, monkeypatch, *, repo, run_id, test_cmd, knob,
                extra_env=None, authored=False):
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
    if authored:
        # The publisher commits ONLY the run's authored patch
        # (mini_ork/cli/publisher_authored_patch.py): with a pre-implementer-ref
        # baseline present, the declared files_changed is a whole-file TREE delta
        # and is not a source. Stand in the implementer's own emitted unified diff
        # (the text apply_impl_output applies with `git apply`) so a run whose
        # levels are PROVEN actually publishes.
        (rd / "impl-test.log").write_text(_AUTHORED_DIFF)
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
                             test_cmd=PYTEST_CMD, knob=True,
                             extra_env={"MO_SUITE_ADEQUACY": "0"}, authored=True)
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


def test_real_opaque_delta_knob_on(tmp_path, monkeypatch):
    """A non-pytest runner is proven by the opaque exit-code delta.

    The suite here is `python -m unittest` — no per-test adapter exists, and
    the old instrument called the replay "n/a" (target n/a, publish unblocked).
    The opaque fallback runs the command unchanged on both sides and proves the
    delta at suite grain: base (buggy HEAD) red, candidate green. target is now
    PROVEN rather than n/a.
    """
    repo = _make_repo(tmp_path, mod_src=MOD_BUG, test_src=TEST_UNITTEST)
    (repo / "mod.py").write_text(MOD_FIX)
    rc, db, rd = _drive_main(tmp_path, monkeypatch, repo=repo, run_id="r10",
                             test_cmd=UNITTEST_CMD, knob=True, authored=True)
    assert rc == 0

    payload = L.read_verifier_payload(str(rd / "verifier_test.json"), "test")
    assert payload["pass"] is True
    # `replay_applicable` is only written when False; absent == the instrument
    # applied to this command (the opaque fallback), so it is not n/a.
    assert payload.get("replay_applicable") is not False
    assert payload["replay"]["runner"] == "opaque"
    assert payload["replay"]["proven_by"] == "exit-code-delta"
    assert payload["replay"]["candidate_rc"] == 0
    assert payload["replay"]["base_rc"] != 0

    verdict = json.loads((rd / "verdict.json").read_text())
    assert verdict["failed_nodes"] == 0
    assert verdict["levels"]["target"] == PROVEN
    assert verdict["levels_ok"] is True
    assert verdict["levels_decision"] == "publish"

    # a green non-pytest suite whose delta is proven publishes
    assert _git_text(repo, "log", "-1", "--pretty=%s").startswith("mini-ork(code-fix): ")
    committed = [ln for ln in
                 _git_text(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
                 .splitlines() if ln]
    assert committed == ["mod.py"]
    assert _status(db, "r10") == "published"


def test_real_replay_abstain_knob_off(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, mod_src=MOD_BUG, test_src=TEST_UNITTEST)
    (repo / "mod.py").write_text(MOD_FIX)
    rc, db, rd = _drive_main(tmp_path, monkeypatch, repo=repo, run_id="r11",
                             test_cmd=UNITTEST_CMD, knob=False,
                             extra_env={"MO_LEVEL_VECTOR": "0"}, authored=True)
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
    # A red base no longer blanket-passes (the old BASE_RC != 0 rule). Here the
    # candidate strictly improves (mod.py's tests now pass) with no regression,
    # but the suite as a whole is still red, so the verifier ABSTAINS: pass=False.
    # Only MO_TEST_CERTIFY_ON_IMPROVEMENT=1 certifies a strict red-base
    # improvement — that is the "current contract" this test pins.
    assert payload["pass"] is False
    assert payload["red_base_unverified"] is True
    assert payload["status"] == "unverified"
    assert payload["post_rc"] == 1
    assert payload["base_rc"] == "1"

    verdict = json.loads((rd / "verdict.json").read_text())
    assert verdict["levels"]["executes"] == PROVEN
    assert verdict["levels"]["target"] == UNVERIFIED
    assert verdict["levels"]["preserve"] == UNVERIFIED
    assert verdict["levels_decision"] == "abstain"

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
