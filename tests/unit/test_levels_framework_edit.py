"""RBF-02b — the level vector reads the framework-edit test verdict.

framework-edit's test verifier publishes an envelope (``{"verifier": "test",
"pass": bool}``) rather than code-fix's posix ``post_rc``, so ``executes`` used
to abstain on every framework-edit run and a failed test verdict could not
withhold publish. These cases pin the three mechanism clauses of the kickoff:
the envelope decides ``executes`` when no ``post_rc`` exists, the class declares
``executes`` required without withholding runs whose instrument never ran, and
the contract reader accepts a bare ``{"pass": bool}`` last object as a verdict.

Hermetic: each case builds a run dir under ``tmp_path`` and calls the pure
functions in ``mini_ork.verify.levels`` directly.
"""
from mini_ork.verify.behavioral import PROVEN, REFUTED, UNVERIFIED
from mini_ork.verify.levels import (
    NA,
    REQUIRED_LEVELS,
    derive_levels,
    level_report,
    publish_decision,
    required_levels,
)

# `_run_verifier_ref` merges stdout+stderr, so real evidence starts with
# non-JSON lines like this one.
WARNING_LINE = (
    "/venv/lib/python3.12/site-packages/foo/bar.py:12: "
    "DeprecationWarning: foo.bar is deprecated\n"
)

SUMMARY = '{"status": "implemented", "files_changed": ["mini_ork/verify/levels.py"]}\n'


def _run_dir(tmp_path, name, files=None):
    run_dir = tmp_path / name
    run_dir.mkdir()
    for rel, text in (files or {}).items():
        (run_dir / rel).write_text(text, encoding="utf-8")
    return str(run_dir)


# ── REQUIRED_LEVELS ─────────────────────────────────────────────────────────


def test_framework_edit_declares_executes_required():
    assert REQUIRED_LEVELS["framework_edit"] == ("executes",)
    assert required_levels("framework_edit") == ("executes",)


# ── executes ← envelope pass ────────────────────────────────────────────────


def test_failed_test_envelope_refutes_executes(tmp_path):
    run_dir = _run_dir(tmp_path, "refuted", {
        "verifier_test.json": WARNING_LINE + '{"verifier": "test", "pass": false}\n',
    })
    vector, reasons = derive_levels(run_dir, "framework_edit")
    assert vector["executes"] == REFUTED
    assert reasons["executes"].lower() == "verifier_test.json: envelope pass=false"
    assert publish_decision(
        vector, required=REQUIRED_LEVELS["framework_edit"]) == "refute"

    report = level_report(run_dir, "framework_edit")
    assert report["levels_decision"] == "refute"
    assert report["levels_required"] == ["executes"]
    assert report["levels_ok"] is False


def test_passing_test_envelope_proves_executes(tmp_path):
    run_dir = _run_dir(tmp_path, "proven", {
        "verifier_test.json": WARNING_LINE + '{"verifier": "test", "pass": true}\n',
    })
    vector, reasons = derive_levels(run_dir, "framework_edit")
    assert vector["executes"] == PROVEN
    assert reasons["executes"].lower() == "verifier_test.json: envelope pass=true"
    assert publish_decision(
        vector, required=REQUIRED_LEVELS["framework_edit"]) == "publish"

    report = level_report(run_dir, "framework_edit")
    assert report["levels_decision"] == "publish"
    assert report["levels_ok"] is True


def test_envelope_falls_back_to_single_verifier_payload(tmp_path):
    # No `verifier` field: only the single-object fallback can read the file.
    failed = _run_dir(tmp_path, "fallback-false", {
        "verifier_test.json": WARNING_LINE + '{"pass": false}\n',
    })
    vector, reasons = derive_levels(failed, "framework_edit")
    assert vector["executes"] == REFUTED
    assert reasons["executes"].lower() == "verifier_test.json: envelope pass=false"

    passed = _run_dir(tmp_path, "fallback-true", {
        "verifier_test.json": WARNING_LINE + '{"pass": true}\n',
    })
    vector, _ = derive_levels(passed, "framework_edit")
    assert vector["executes"] == PROVEN


def test_non_boolean_pass_is_not_a_verdict(tmp_path):
    run_dir = _run_dir(tmp_path, "string-pass", {
        "verifier_test.json": WARNING_LINE + '{"verifier": "test", "pass": "false"}\n',
    })
    vector, _ = derive_levels(run_dir, "framework_edit")
    assert vector["executes"] == UNVERIFIED
    assert publish_decision(
        vector, required=REQUIRED_LEVELS["framework_edit"]) == "abstain"


# ── a missing instrument must stay n/a ──────────────────────────────────────


def test_framework_edit_without_test_evidence_is_n_a(tmp_path):
    # Empty run dir: the instrument never ran.
    empty = _run_dir(tmp_path, "empty")
    vector, _ = derive_levels(empty, "framework_edit")
    assert vector["executes"] == NA
    assert publish_decision(
        vector, required=REQUIRED_LEVELS["framework_edit"]) != "abstain"

    # A real framework-edit run writes implementer-summary.json but no test
    # instrument; the new required level must not withhold that publish.
    with_summary = _run_dir(tmp_path, "summary-only", {
        "implementer-summary.json": SUMMARY,
    })
    vector, _ = derive_levels(with_summary, "framework_edit")
    assert vector["executes"] == NA
    assert publish_decision(
        vector, required=REQUIRED_LEVELS["framework_edit"]) != "abstain"


def test_missing_evidence_n_a_does_not_leak_to_code_fix(tmp_path):
    run_dir = _run_dir(tmp_path, "codefix-no-test", {
        "implementer-summary.json": SUMMARY,
    })
    report = level_report(run_dir, "code_fix")
    assert report["levels"]["executes"] == UNVERIFIED
    assert report["levels_decision"] == "abstain"


def test_present_test_file_without_verdict_still_abstains(tmp_path):
    # The n/a branch is keyed on the file being ABSENT, not on the class.
    run_dir = _run_dir(tmp_path, "unreadable", {
        "implementer-summary.json": SUMMARY,
        "verifier_test.json": WARNING_LINE + '{"verifier": "test", "status": "unknown"}\n',
    })
    vector, _ = derive_levels(run_dir, "framework_edit")
    assert vector["executes"] == UNVERIFIED
    assert publish_decision(
        vector, required=REQUIRED_LEVELS["framework_edit"]) == "abstain"


# ── code-fix is decided exactly as today ────────────────────────────────────


def test_code_fix_post_rc_paths_unchanged(tmp_path):
    green = _run_dir(tmp_path, "codefix-green", {
        "implementer-summary.json": SUMMARY,
        "verifier_test.json": (
            WARNING_LINE
            + '{"verifier": "test", "post_rc": 0, "base_rc": "0", "pass": true, '
            '"replay": {"overlap": ["test_levels_framework_edit"]}}\n'
        ),
    })
    report = level_report(green, "code_fix")
    assert report["levels"]["executes"] == PROVEN
    assert report["levels_reasons"]["executes"] == "verifier_test.json: post_rc=0"
    assert report["levels_decision"] == "publish"

    crashed = _run_dir(tmp_path, "codefix-crash", {
        "verifier_test.json": '{"verifier": "test", "post_rc": 2, "base_rc": "0"}\n',
    })
    vector, reasons = derive_levels(crashed, "code_fix")
    assert vector["executes"] == REFUTED
    assert reasons["executes"] == "verifier_test.json: post_rc=2 on green base"

    reached = _run_dir(tmp_path, "codefix-ran", {
        "verifier_test.json": '{"verifier": "test", "post_rc": 1}\n',
    })
    vector, reasons = derive_levels(reached, "code_fix")
    assert vector["executes"] == PROVEN
    assert reasons["executes"] == "verifier_test.json: post_rc=1"


def test_post_rc_outranks_envelope_pass(tmp_path):
    # post_rc present ⇒ the post_rc branches decide; an envelope pass neither
    # rescues a red rc nor refutes a green one.
    green = _run_dir(tmp_path, "rc0-pass-false", {
        "verifier_test.json": (
            '{"verifier": "test", "post_rc": 0, "base_rc": "0", "pass": false}\n'
        ),
    })
    vector, reasons = derive_levels(green, "code_fix")
    assert vector["executes"] == PROVEN
    assert reasons["executes"] == "verifier_test.json: post_rc=0"

    crashed = _run_dir(tmp_path, "rc2-pass-true", {
        "verifier_test.json": (
            '{"verifier": "test", "post_rc": 2, "base_rc": "0", "pass": true}\n'
        ),
    })
    vector, reasons = derive_levels(crashed, "code_fix")
    assert vector["executes"] == REFUTED
    assert reasons["executes"] == "verifier_test.json: post_rc=2 on green base"


# ── contract reader accepts a bare pass ─────────────────────────────────────


def test_bare_pass_false_refutes_contract(tmp_path):
    run_dir = _run_dir(tmp_path, "bare-false", {
        "verifier_schema.json": "[schema] validating artifact contract\n" + '{"pass": false}\n',
    })
    report = level_report(run_dir, "verified_artifact")
    assert report["levels"]["contract"] == REFUTED
    assert "REFUTED" in report["levels_reasons"]["contract"]
    assert report["levels_decision"] == "refute"


def test_bare_pass_true_proves_contract(tmp_path):
    run_dir = _run_dir(tmp_path, "bare-true", {
        "verifier_schema.json": WARNING_LINE + '{"pass": true}\n',
    })
    report = level_report(run_dir, "verified_artifact")
    assert report["levels"]["contract"] == PROVEN
    assert report["levels_decision"] == "publish"


def test_check_row_is_not_a_bare_verdict(tmp_path):
    # Inner check rows carry `id` (or `name`); they are not envelopes.
    for name, row in (("by-id", '{"id": "x", "pass": false}'),
                      ("by-name", '{"name": "x", "pass": false}')):
        run_dir = _run_dir(tmp_path, name, {
            "verifier_schema.json": row + "\n",
        })
        report = level_report(run_dir, "verified_artifact")
        assert report["levels"]["contract"] == UNVERIFIED
        assert report["levels_decision"] == "abstain"


def test_bare_pass_must_be_the_last_object(tmp_path):
    run_dir = _run_dir(tmp_path, "bare-then-row", {
        "verifier_schema.json": '{"pass": false}\n{"id": "row", "pass": true}\n',
    })
    report = level_report(run_dir, "verified_artifact")
    assert report["levels"]["contract"] == UNVERIFIED
    assert report["levels_decision"] == "abstain"


def test_full_envelope_keeps_priority(tmp_path):
    # A full envelope followed by a bare pass: the envelope decides.
    envelope_then_bare = _run_dir(tmp_path, "envelope-priority", {
        "verifier_schema.json": (
            '{"pass": true, "checks": [{"id": "c1", "pass": true}]}\n'
            '{"pass": false}\n'
        ),
    })
    report = level_report(envelope_then_bare, "verified_artifact")
    assert report["levels"]["contract"] == PROVEN
    assert report["levels_decision"] == "publish"

    # A trailing check row must not hide the envelope above it.
    envelope_then_row = _run_dir(tmp_path, "envelope-then-row", {
        "verifier_schema.json": (
            '{"pass": true, "checks": [{"id": "c1", "pass": true}]}\n'
            '{"id": "row", "pass": false}\n'
        ),
    })
    report = level_report(envelope_then_row, "verified_artifact")
    assert report["levels"]["contract"] == PROVEN
    assert report["levels_decision"] == "publish"


def test_bare_pass_true_with_skip_marker_is_not_proven(tmp_path):
    # A verifier that examined nothing still publishes pass:true with an
    # error_summary; that must not mint a PROVEN contract level.
    run_dir = _run_dir(tmp_path, "skip", {
        "verifier_typecheck.json": (
            '{"verifier": "typecheck", "pass": true, "evidence_path": null, '
            '"error_summary": "no typecheck tool detected — skipped"}\n'
        ),
    })
    report = level_report(run_dir, "verified_artifact")
    assert report["levels"]["contract"] == UNVERIFIED
    assert report["levels_decision"] == "abstain"
