"""Unit tests for the green-but-unverified ABSTENTION rule (2026-10-08).

A suite that ran GREEN after the patch is real verification, even when the
instrument could not read a replay delta (a Rust/jest build it has no adapter
for) and so reported ``status="unverified"``. Treating that as a failure zeroed
the eval reward on every green Rust/IDE run (``needs_revision`` on a delivered
change) and poisoned the GRPO signal.

Three defects are pinned here:

1. ``_verifier_passed`` mapped ``{"pass": false, "status": "unverified"}`` to
   False — an abstention read as a refutation.
2. ``_read_run_trajectory`` parsed the raw evidence file with ``json.loads(body)``
   — the merged ``[x] running: …`` banner line made EVERY verifier unparsable
   (the root cause: ``r_exec`` came out ``null``, not ``0.5``).
3. ``_stage_checks`` verify stage did not credit ``suite_green`` + ``post_rc==0``.

Plus kickoff change 3: a command that is the literal ``true`` is vacuous (test
theater) and must not count — the banner names the command, so the eval path
marks it ``status: "vacuous"`` (``probe_validity._VACUOUS_PROBES``) before it can
earn an execution pass or claim a green suite.

The end-to-end cases drive ``_handle_eval`` through the real on-disk banner
format — copying ``_write_verifier``'s pure-JSON shape would leave the parse fix
uncovered, which is exactly how this bug survived.

Known conflict, surfaced rather than silently worked around (see
``test_no_verifiers_claimed_pass_is_not_gated``): the kickoff's change 4 claims
"the gate still fires when no verifier ran". It does not — with no verifier
evidence ``_stage_checks`` returns a tri-valued ``verify=None`` and ``coherence``
fails OPEN to 1.0. Making it fire means ``verify=False`` for "no verifiers", which
breaks ``test_eval_node_judge_only_when_no_execution_signal``'s documented
fail-open contract — a file outside this task's scope.

No LLM/DB beyond a migrated tmp store; mirrors tests/unit/test_eval_judge.py."""

import json
from pathlib import Path

import pytest

from mini_ork.learning import eval_judge as ej

REPO = Path(__file__).resolve().parents[2]


# ── _verifier_passed: unverified is an abstention, not a refutation ──────────
def test_unverified_status_is_abstention_even_with_pass_false():
    # the F2a shape: the field order must not matter — abstention wins.
    assert ej._verifier_passed(
        {"pass": False, "status": "unverified", "suite_green": True}) is None


def test_unverified_status_is_abstention_even_with_pass_true():
    # "whatever its `pass` field says" — a claimed pass that the instrument
    # could not confirm still carries no real execution signal.
    assert ej._verifier_passed({"pass": True, "status": "unverified"}) is None


def test_unverified_verdict_string_is_abstention():
    assert ej._verifier_passed({"verdict": "unverified"}) is None


def test_plain_pass_false_stays_false_and_pass_true_stays_true():
    assert ej._verifier_passed({"pass": False}) is False
    assert ej._verifier_passed({"pass": True}) is True


def test_vacuous_payloads_abstain():
    assert ej._verifier_passed({}) is None
    assert ej._verifier_passed({"status": "vacuous"}) is None
    assert ej._verifier_passed({"ran": True}) is None


def test_unverified_abstention_drops_out_of_execution_reward():
    """Change 4 (unchanged): an abstention is neither a pass nor a failure — it
    leaves the r_exec denominator, so a lone green-abstention verifier gives no
    execution signal (None → judge-only fallback), not 0.0."""
    r_exec, detail = ej.execution_reward(
        {"test": {"pass": False, "status": "unverified", "suite_green": True}})
    assert r_exec is None
    assert detail == {"test": None}
    # a real pass alongside an abstention still scores over the concrete verifier
    r_exec, _ = ej.execution_reward(
        {"test": {"pass": False, "status": "unverified", "suite_green": True},
         "typecheck": {"pass": True}})
    assert r_exec == 1.0


# ── _read_run_trajectory: banner-tolerant parse (the root cause) ─────────────
def test_read_run_trajectory_parses_banner_prefixed_payload(tmp_path):
    from mini_ork.cli.execute_handlers import _read_run_trajectory
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "verifier_test.json").write_text(
        "[test] running: script/mini-ork-build\n"
        + json.dumps({"pass": False, "status": "unverified",
                      "suite_green": True, "post_rc": 0}))
    _, verdicts = _read_run_trajectory("", "", str(run_dir))
    assert verdicts["test"]["suite_green"] is True
    assert verdicts["test"]["status"] == "unverified"   # parsed, not {"raw": …}


def test_read_run_trajectory_keeps_raw_fallback_for_garbage(tmp_path):
    from mini_ork.cli.execute_handlers import _read_run_trajectory
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "verifier_broken.json").write_text("not json at all\nno braces here")
    _, verdicts = _read_run_trajectory("", "", str(run_dir))
    assert "raw" in verdicts["broken"]


def test_read_run_trajectory_ignores_a_nested_object(tmp_path):
    """The parser must take the TOP-LEVEL payload, not a nested `{"pass": true}`
    (the previous bottom-up scan returned a sub-check and flipped fail → pass)."""
    from mini_ork.cli.execute_handlers import _read_run_trajectory
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "verifier_behavioral.json").write_text(
        "[behavioral] running: pytest\n" + json.dumps(
            {"pass": False, "checks": [{"name": "a", "pass": True}]}, indent=2))
    _, verdicts = _read_run_trajectory("", "", str(run_dir))
    assert verdicts["behavioral"]["pass"] is False
    assert verdicts["behavioral"]["checks"] == [{"name": "a", "pass": True}]


@pytest.mark.parametrize("cmd", ["true", ":", "exit 0", "/usr/bin/true"])
def test_read_run_trajectory_marks_a_vacuous_command_as_no_signal(tmp_path, cmd):
    """Kickoff change 3: a command that is the literal `true` proves nothing, so
    its `pass: true` must not become an execution pass (test theater)."""
    from mini_ork.cli.execute_handlers import _read_run_trajectory
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "verifier_typecheck.json").write_text(
        f"[typecheck] running: {cmd}\n"
        + json.dumps({"verifier": "typecheck", "pass": True}))
    _, verdicts = _read_run_trajectory("", "", str(run_dir))
    assert verdicts["typecheck"]["status"] == "vacuous"
    assert "pass" not in verdicts["typecheck"]
    assert ej._verifier_passed(verdicts["typecheck"]) is None


def test_a_vacuous_command_cannot_claim_a_green_suite(tmp_path):
    """A literal-`true` test command whose payload says suite_green/post_rc 0
    must not satisfy the green-suite verify rule (review finding)."""
    from mini_ork.cli.execute_handlers import _read_run_trajectory, _stage_checks
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "verifier_test.json").write_text(
        "[test] running: true\n" + json.dumps(
            {"verifier": "test", "pass": False, "status": "unverified",
             "suite_green": True, "post_rc": 0}))
    _, verdicts = _read_run_trajectory("", "", str(run_dir))
    assert "suite_green" not in verdicts["test"]
    assert "post_rc" not in verdicts["test"]
    checks = _stage_checks(True, "artifact", [], verdicts, None)
    assert checks["verify"] is not True


def test_read_run_trajectory_keeps_a_real_command(tmp_path):
    """The vacuity rule is limited to the no-op vocabulary — a real command's
    pass still counts."""
    from mini_ork.cli.execute_handlers import _read_run_trajectory
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "verifier_typecheck.json").write_text(
        "[typecheck] running: tsc --noEmit\n"
        + json.dumps({"verifier": "typecheck", "pass": True}))
    _, verdicts = _read_run_trajectory("", "", str(run_dir))
    assert verdicts["typecheck"] == {"verifier": "typecheck", "pass": True}
    assert ej._verifier_passed(verdicts["typecheck"]) is True


# ── _stage_checks: a green suite verifies, a vacuous one does not ────────────
def _verify_stage(verifier_verdicts, r_exec=None):
    from mini_ork.cli.execute_handlers import _stage_checks
    return _stage_checks(True, "", [], verifier_verdicts, r_exec)


def test_stage_checks_green_abstention_is_verify_true():
    """The isolated new rule: the ONLY verifier abstained on the delta but its
    suite ran green (post_rc==0) → verify is True (real, non-vacuous proof)."""
    checks = _verify_stage({"test": {
        "pass": False, "status": "unverified",
        "suite_green": True, "post_rc": 0, "replay_unverified": True}})
    assert checks["verify"] is True


def test_stage_checks_f2a_shaped_verifiers_verify_true():
    checks = _verify_stage({
        "test": {"pass": False, "status": "unverified",
                 "suite_green": True, "post_rc": 0, "replay_unverified": True},
        "typecheck": {"pass": True}})
    assert checks["verify"] is True


def test_stage_checks_green_but_nonzero_rc_is_not_verify_true():
    # suite_green without a clean exit code is not proof — keep the strict rule.
    checks = _verify_stage({"test": {
        "status": "unverified", "suite_green": True, "post_rc": 1}})
    assert checks["verify"] is not True


@pytest.mark.parametrize("vacuous", [{}, {"status": "vacuous"}, {"ran": True}])
def test_stage_checks_vacuous_only_is_not_verify_true(vacuous):
    checks = _verify_stage({"smoke": vacuous})
    assert checks["verify"] is not True


def test_stage_checks_no_verifiers_is_none_not_false():
    # tri-valued: nothing ran → no signal (drops out), distinct from a vacuous run.
    assert _verify_stage({})["verify"] is None


# ── end to end through the coherence gate ────────────────────────────────────
def _migrated_db(home: Path) -> str:
    from mini_ork.stores.migrate import init_db
    home.mkdir(parents=True, exist_ok=True)
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\n{out}\n{err}"
    return dbp


def _make_ctx(run_dir: Path, db: str, dispatch_fn):
    from mini_ork.cli.execute import NodeDispatch
    return NodeDispatch(
        node_id="eval", node_type="eval", node_desc="grade the run",
        prompt_ref="", verifier_ref="", model_lane="reviewer",
        node_requires_capabilities="", root=str(REPO), run_dir=str(run_dir),
        plan_path="", task_class="code_fix", db=db, run_id="run-eval-1",
        recipe="code-fix", workflow="", lane="reviewer",
        run_dir_eff=str(run_dir), recipe_dir="", prompt_file="",
        plan_content="the plan", learned="",
        dispatch_fn=dispatch_fn, trace=lambda *a, **k: None,
        charge=lambda *a, **k: None,
    )


def _write_banner_verifier(run_dir, name, banner, obj):
    """Write the REAL evidence shape: a ``[x] running: …`` banner line, then the
    JSON payload — never bare ``json.dumps`` (that hides the parse bug)."""
    (run_dir / f"verifier_{name}.json").write_text(banner + "\n" + json.dumps(obj))


_JUDGE_PASS = '{"axes": {"safety": 1.0}, "verdict": "pass"}'


def test_green_abstention_run_is_not_gated(tmp_path, monkeypatch):
    """The F2a reproduction: a green Rust build whose delta instrument abstained
    plus a literal-`true` typecheck, with a claimed pass. The R2 coherence gate
    must NOT fire — verify is real (suite_green), so the run is coherent.

    Both new rules are load-bearing here: the green-suite rule makes verify True,
    and the vacuous-command rule keeps the no-op typecheck from supplying a bogus
    execution pass (without it this test's r_exec would be 1.0)."""
    monkeypatch.delenv("MO_EVAL_COHERENCE_GATE", raising=False)  # default ON
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_banner_verifier(run_dir, "test", "[test] running: script/mini-ork-build", {
        "pass": False, "status": "unverified", "suite_green": True,
        "post_rc": 0, "replay_unverified": True,
        "error_summary": "unverified: replay supports pytest, jest, vitest, "
                         "or a results file; none produced for this command"})
    _write_banner_verifier(run_dir, "typecheck", "[typecheck] running: true",
                           {"pass": True})
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, _JUDGE_PASS))
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["process"]["coherence"] == 1.0
    assert saved["process"]["overclaimed_success"] is True
    assert "gated_to" not in saved["process"]          # the gate did NOT fire
    assert saved["score"] > 0.0
    assert saved["verdict"] != "needs_revision"
    # the green suite is a VERIFY signal, not an execution pass: the abstention
    # dropped out and the literal-`true` typecheck is vacuous → no r_exec signal.
    assert saved["execution"]["r_exec"] is None
    assert saved["reward_source"] == "eval-judge@v1"


def test_literal_true_command_run_is_still_gated(tmp_path, monkeypatch):
    """Kickoff change 3, end to end: a run whose ONLY verifier is a literal-`true`
    typecheck reporting `{"pass": true}` is test theater — the gate must fire."""
    monkeypatch.delenv("MO_EVAL_COHERENCE_GATE", raising=False)  # default ON
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_banner_verifier(run_dir, "typecheck", "[typecheck] running: true",
                           {"verifier": "typecheck", "pass": True})
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, _JUDGE_PASS))
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["execution"]["r_exec"] is None                 # no real pass
    assert saved["process"]["coherence"] == 0.0
    assert saved["process"]["gated_to"] == 0.0
    assert saved["score"] == 0.0
    assert saved["verdict"] == "needs_revision"


def test_red_suite_plus_noop_typecheck_is_still_gated(tmp_path, monkeypatch):
    """The reviewer's exact repro for kickoff change 3: a RED suite (abstained,
    `suite_green: false`, `post_rc: 101`) alongside a literal-`true` typecheck
    reporting `{"pass": true}`. Before the vacuity rule the no-op supplied the
    only concrete pass → r_exec 1.0, verify True, coherence 1.0, gate silent. A
    red-suite run must never earn maximum execution reward from a no-op."""
    monkeypatch.delenv("MO_EVAL_COHERENCE_GATE", raising=False)  # default ON
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_banner_verifier(run_dir, "test", "[test] running: script/mini-ork-build", {
        "pass": False, "status": "unverified", "suite_green": False,
        "post_rc": 101, "replay_unverified": True})
    _write_banner_verifier(run_dir, "typecheck", "[typecheck] running: true",
                           {"verifier": "typecheck", "pass": True})
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, _JUDGE_PASS))
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["execution"]["r_exec"] is None       # no concrete pass survives
    assert saved["process"]["coherence"] == 0.0
    assert saved["process"]["gated_to"] == 0.0
    assert saved["score"] == 0.0
    assert saved["verdict"] == "needs_revision"


def test_vacuous_verifier_run_is_still_gated(tmp_path, monkeypatch):
    """Change 4 (unchanged): the gate still fires when the run verified nothing
    real. A claimed pass with only a VACUOUS payload (a real command, no signal)
    → verify False (non-empty payloads, no concrete signal, no green suite) →
    coherence 0 → blocked."""
    monkeypatch.delenv("MO_EVAL_COHERENCE_GATE", raising=False)  # default ON
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_banner_verifier(run_dir, "smoke", "[smoke] running: pytest -q",
                           {"status": "vacuous"})
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, _JUDGE_PASS))
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["process"]["coherence"] == 0.0
    assert saved["process"]["gated_to"] == 0.0
    assert saved["score"] == 0.0
    assert saved["verdict"] == "needs_revision"


def test_unverified_without_green_suite_is_still_gated(tmp_path, monkeypatch):
    """The boundary: an abstention with NO green suite is NOT proof — it is the
    same as verifying nothing, so the gate still fires."""
    monkeypatch.delenv("MO_EVAL_COHERENCE_GATE", raising=False)  # default ON
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_banner_verifier(run_dir, "test", "[test] running: cargo test", {
        "pass": False, "status": "unverified", "suite_green": False,
        "post_rc": None, "replay_unverified": True})
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, _JUDGE_PASS))
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["process"]["coherence"] == 0.0
    assert saved["process"]["gated_to"] == 0.0
    assert saved["score"] == 0.0


def test_no_verifiers_claimed_pass_is_not_gated(tmp_path, monkeypatch):
    """The kickoff's "no verifiers" case, asserted as it ACTUALLY behaves.

    Kickoff change 4 says the gate "still fires when no verifier ran" and its test
    bullet asks for "a claimed pass with NO verifiers is still gated". That is
    false today and this test records the real behaviour: with no verifier
    evidence at all, ``_stage_checks`` returns a tri-valued ``verify=None`` (a
    stage with no signal DROPS OUT, it is not a failure), ``decide_from_steps``
    then has no concrete step (D is None) and ``coherence`` fails OPEN to 1.0.

    Gating this case requires ``verify=False`` when ``verifier_verdicts`` is empty,
    which changes ``test_eval_node_judge_only_when_no_execution_signal``'s
    documented fail-open contract (a judge-only run stays at score 1.0) — a file
    outside this task's scope. Surfaced here rather than silently substituted
    away by a vacuous-verifier test."""
    monkeypatch.delenv("MO_EVAL_COHERENCE_GATE", raising=False)  # default ON
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()  # no verifier_*.json at all
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, _JUDGE_PASS))
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["reward_source"] == "eval-judge@v1"  # no execution signal
    assert saved["process"]["coherence"] == 1.0       # fails OPEN, NOT gated
    assert "gated_to" not in saved["process"]
