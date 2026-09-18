"""Deterministic step rules (VPRMs) and the gate that carries them.

``mini_ork.gates.step_rules`` adds the rule-based counterpart to the mutation
campaign: two lookups on the intermediate artifact — a patch that will not
apply, a verifier that names a file the target does not have — evaluated before
any judge sees the diff. The technique's own tradeoff is honest partial
coverage, so the tests below pin both halves of it: a rule that fires returns
``fail``, and a rule with nothing to check returns ``defer`` rather than a free
``pass``. The second half is the one that decays, because a rule that quietly
passes on absent input looks exactly like a rule that held.

The rules are asserted against real git rather than a stubbed subprocess. A
hand-rolled hunk whose context lines or counts are wrong fails ``git apply``
for reasons that have nothing to do with the rule, so the fixtures ask git for
the diff — a failure here then means the rule is wrong, not the fixture.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import gate_bootstrap as gb  # noqa: E402
from mini_ork.gates import gate_registry as gr  # noqa: E402
from mini_ork.gates import step_rules as sr  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# fixtures
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def ws(tmp_path):
    """A committed git worktree holding ``m.py`` — the patch's base."""
    d = tmp_path / "ws"
    d.mkdir()
    (d / "m.py").write_text(_SOURCE)

    def git(*a):
        return subprocess.run(["git", "-C", str(d), *a], check=True,
                              capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "init")
    return d


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return dbp


def _patch(ws: Path, path: str, old: str, new: str, out: Path) -> str:
    """A real unified diff for one file, written to ``out``."""
    p = ws / path
    original = p.read_text()
    p.write_text(original.replace(old, new))
    diff = subprocess.run(["git", "-C", str(ws), "diff", "--", path],
                          capture_output=True, text=True).stdout
    p.write_text(original)
    assert diff, "fixture produced an empty diff"
    out.write_text(diff)
    return str(out)


def _plan(tmp_path: Path, *, command: str = "", verifiers: list[str] | None = None,
          name: str = "plan.json") -> str:
    plan: dict = {"task_class": "code_fix"}
    if command:
        plan["verifier_contract"] = {"checks": [{"id": "c1", "command": command}]}
    if verifiers:
        plan["artifact_contract"] = {"success_verifiers": verifiers}
    p = tmp_path / name
    p.write_text(json.dumps(plan))
    return str(p)


def _row(db: str, gate_id: str) -> tuple:
    import sqlite3

    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT gate_type, condition, task_class_filter, safety, active "
            "FROM gate_registry WHERE gate_id=?", (gate_id,)).fetchone()
    finally:
        con.close()


def _verdict_of(db: str, ctx: dict) -> tuple[dict, dict]:
    """Run the whole registry for ``code_fix``; return (gate_id → verdict, summary)."""
    summary = gr.gate_run_all(db, "code_fix", json.dumps(ctx),
                              mini_ork_root=str(REPO))
    return {g["gate_id"]: g["verdict"] for g in summary["gates"]}, summary


# ─────────────────────────────────────────────────────────────────────────────
# rule 1 — the patch applies
# ─────────────────────────────────────────────────────────────────────────────


def test_a_patch_that_applies_passes(ws, tmp_path):
    patch = _patch(ws, "m.py", "return a + b", "return b + a", tmp_path / "p.diff")
    assert sr.rule_patch_applies_cleanly(patch, str(ws)) == (
        "pass", "git apply --check")


def test_a_patch_that_cannot_apply_fails(ws, tmp_path):
    """A hunk whose base text is not in the tree: the 77% failure class."""
    good = _patch(ws, "m.py", "return a + b", "return b + a", tmp_path / "g.diff")
    # Retarget the hunk at text the target file does not contain, keeping the
    # diff syntactically valid — exactly the shape a model-authored patch takes
    # when it guesses at a base it never read.
    bogus = (Path(good).read_text()
             .replace(" return a + b", " return a - b")
             .replace("+    return b + a", "+    return b + a"))
    bad = tmp_path / "bad.diff"
    bad.write_text(bogus)
    verdict, detail = sr.rule_patch_applies_cleanly(str(bad), str(ws))
    assert verdict == "fail"
    assert "git apply" in detail


def test_an_already_applied_patch_is_not_reported_as_broken(ws, tmp_path):
    """Reverse-applies cleanly means the patch is in the tree, not that it is bad.

    Failing this would punish a re-run of a healthy step, which is how a good
    rule turns into a veto of good work.
    """
    patch = _patch(ws, "m.py", "return a + b", "return b + a", tmp_path / "p.diff")
    subprocess.run(["git", "-C", str(ws), "apply", patch], check=True)
    verdict, detail = sr.rule_patch_applies_cleanly(patch, str(ws))
    assert verdict == "pass"
    assert "already applied" in detail


def test_a_non_patch_artifact_defers(tmp_path):
    md = tmp_path / "report.md"
    md.write_text("# findings\n\nnothing to apply here\n")
    assert sr.rule_patch_applies_cleanly(str(md), str(tmp_path))[0] == "defer"


def test_no_workspace_defers(ws, tmp_path):
    patch = _patch(ws, "m.py", "return a + b", "return b + a", tmp_path / "p.diff")
    assert sr.rule_patch_applies_cleanly(patch, "")[0] == "defer"
    assert sr.rule_patch_applies_cleanly(patch, str(tmp_path / "nope"))[0] == "defer"


# ─────────────────────────────────────────────────────────────────────────────
# rule 2 — the paths the plan names exist
# ─────────────────────────────────────────────────────────────────────────────


def test_named_paths_that_exist_pass(ws, tmp_path):
    (ws / "tests").mkdir()
    (ws / "tests" / "test_m.py").write_text("def test_x():\n    assert True\n")
    plan = _plan(tmp_path, command="python3 -m pytest tests/test_m.py")
    assert sr.rule_named_test_path_exists(plan, str(ws), str(REPO))[0] == "pass"


def test_a_named_path_that_is_missing_fails_and_names_it(ws, tmp_path):
    plan = _plan(tmp_path, command="python3 -m pytest tests/unit/test_ghost.py")
    verdict, detail = sr.rule_named_test_path_exists(plan, str(ws), str(REPO))
    assert verdict == "fail"
    assert "tests/unit/test_ghost.py" in detail


def test_a_plan_that_names_no_path_defers(ws, tmp_path):
    """`pytest -q` has nothing to look up; deferring is the honest answer."""
    plan = _plan(tmp_path, command="python3 -m pytest -q")
    assert sr.rule_named_test_path_exists(plan, str(ws), str(REPO))[0] == "defer"


def test_a_plan_with_no_verifier_command_defers(ws, tmp_path):
    plan = _plan(tmp_path)
    assert sr.rule_named_test_path_exists(plan, str(ws), str(REPO))[0] == "defer"


def test_output_paths_are_not_treated_as_named_inputs(ws, tmp_path):
    """`--junitxml=…` and `> log` name where a result goes, not what must exist."""
    plan = _plan(tmp_path,
                 command="python3 -m pytest -q --junitxml=reports/out.xml > run.log")
    assert sr.rule_named_test_path_exists(plan, str(ws), str(REPO))[0] == "defer"


def test_success_verifiers_are_checked_too(ws, tmp_path):
    plan = _plan(tmp_path, verifiers=["verifiers/absent-check.py"])
    verdict, detail = sr.rule_named_test_path_exists(plan, str(ws), str(REPO))
    assert verdict == "fail"
    assert "verifiers/absent-check.py" in detail


def test_a_recipe_verifier_resolves_the_way_the_dispatcher_resolves_it(ws, tmp_path,
                                                                       monkeypatch):
    """`verifiers/typecheck.py` lives under `recipes/<recipe>/verifiers`, not the root.

    Resolving it as a plain path would fail on the most common plan shape in the
    repo — a rule vetoing healthy runs while looking correct.
    """
    assert (Path(REPO) / "recipes" / "code-fix" / "verifiers" / "typecheck.py").is_file()
    plan = _plan(tmp_path, verifiers=["verifiers/typecheck.py"])
    monkeypatch.setenv("MINI_ORK_RECIPE", "code-fix")
    assert sr.rule_named_test_path_exists(plan, str(ws), str(REPO))[0] == "pass"


def test_a_recipe_verifier_that_is_genuinely_absent_still_fails(ws, tmp_path,
                                                               monkeypatch):
    plan = _plan(tmp_path, verifiers=["verifiers/not-a-real-verifier.py"])
    monkeypatch.setenv("MINI_ORK_RECIPE", "code-fix")
    assert sr.rule_named_test_path_exists(plan, str(ws), str(REPO))[0] == "fail"


def test_a_missing_plan_defers(ws):
    assert sr.rule_named_test_path_exists("", str(ws), str(REPO))[0] == "defer"


# ─────────────────────────────────────────────────────────────────────────────
# aggregation — partial coverage is a pass, no coverage is a defer
# ─────────────────────────────────────────────────────────────────────────────


def test_any_failing_rule_makes_the_verdict_fail():
    assert sr.gate_verdict({"rules": [
        {"rule": "a", "verdict": "pass"}, {"rule": "b", "verdict": "fail"}]}) == "fail"


def test_one_held_rule_is_a_pass_even_when_another_deferred():
    """Partial coverage is the technique's whole premise — it is not a defer."""
    assert sr.gate_verdict({"rules": [
        {"rule": "a", "verdict": "pass"}, {"rule": "b", "verdict": "defer"}]}) == "pass"


def test_no_rule_firing_is_a_defer():
    assert sr.gate_verdict({"rules": [
        {"rule": "a", "verdict": "defer"}, {"rule": "b", "verdict": "defer"}]}) == "defer"


def test_a_report_with_no_rules_is_a_defer():
    assert sr.gate_verdict({"rules": []}) == "defer"
    assert sr.gate_verdict(None) == "defer"
    assert sr.gate_verdict("not a dict") == "defer"


def test_a_rule_that_raises_defers_rather_than_escaping():
    """A broken rule is an unrun rule, not a crashed gate run."""
    report = sr.run_rules(None, None, None, None)
    assert [r["verdict"] for r in report["rules"]] == ["defer", "defer"]


# ─────────────────────────────────────────────────────────────────────────────
# registration and the live gate path
# ─────────────────────────────────────────────────────────────────────────────


def test_the_gate_is_seeded_unscoped_and_unsafe(db):
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    assert _row(db, "step-rules-gate") == (
        "custom", "native:step-rules", None, 0, 1)


def test_bootstrap_is_idempotent_with_the_new_gate(db):
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    first = _row(db, "step-rules-gate")
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    assert _row(db, "step-rules-gate") == first


def test_a_run_with_no_artifact_defers_without_failing(db):
    """The common case: nothing to check is a defer, and a defer is not a fail."""
    gb.bootstrap_oracle_gates(db=db, root=str(REPO))
    verdicts, summary = _verdict_of(db, {"task_class": "code_fix"})
    assert verdicts["step-rules-gate"] == "defer"
    assert verdicts["mutation-adversary-gate"] == "defer"
    assert summary["any_fail"] is False
    assert summary["any_defer"] is True


def test_a_patch_that_cannot_apply_fails_the_run(db, ws, tmp_path):
    gb.bootstrap_oracle_gates(db=db, root=str(REPO))
    good = _patch(ws, "m.py", "return a + b", "return b + a", tmp_path / "g.diff")
    bad = tmp_path / "bad.diff"
    bad.write_text(Path(good).read_text().replace(" return a + b", " return x + y"))
    verdicts, summary = _verdict_of(db, {
        "task_class": "code_fix", "artifact_path": str(bad), "workspace": str(ws)})
    assert verdicts["step-rules-gate"] == "fail"
    assert summary["any_fail"] is True
    # safety=0: a finding the verdict must carry, not a publish blocker.
    assert summary["safety_violation"] is False


def test_a_plan_naming_an_absent_path_fails_the_run(db, ws, tmp_path):
    gb.bootstrap_oracle_gates(db=db, root=str(REPO))
    plan = _plan(tmp_path, command="python3 -m pytest tests/test_ghost.py")
    verdicts, _ = _verdict_of(db, {
        "task_class": "code_fix", "plan_path": plan, "workspace": str(ws)})
    assert verdicts["step-rules-gate"] == "fail"


def test_evaluate_defers_when_the_rules_are_turned_off(ws, tmp_path, monkeypatch):
    """MO_STEP_RULES=0 opts out — and says so, rather than reporting a pass."""
    patch = _patch(ws, "m.py", "return a + b", "return b + a", tmp_path / "p.diff")
    ctx = json.dumps({"artifact_path": patch, "workspace": str(ws)})
    assert sr.evaluate("native:step-rules", ctx, "", str(REPO)) == "pass"
    monkeypatch.setenv("MO_STEP_RULES", "0")
    assert sr.evaluate("native:step-rules", ctx, "", str(REPO)) == "defer"


def test_evaluate_routes_through_the_native_registry(ws, tmp_path):
    """The sentinel resolves to the rules, not to the legacy script fallback."""
    from mini_ork.gates import native_gates

    patch = _patch(ws, "m.py", "return a + b", "return b + a", tmp_path / "p.diff")
    ctx = json.dumps({"artifact_path": patch, "workspace": str(ws)})
    resolved = native_gates.resolve_native_evaluator("native:step-rules")
    assert resolved is not None
    assert resolved("native:step-rules", ctx, "", str(REPO)) == "pass"
