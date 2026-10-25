"""Unit tests: the pre-dispatch filter and the kickoff it writes.

Three properties, each with the failure it prevents:

* **Every rejection has one named code.** An operator reading ``selection.json``
  learns why the cycle stopped, and the ledger can count which filter fires.
* **A rejected proposal costs nothing.** All checks are pure — no worktree, no
  child dispatch, no git write.
* **An accepted kickoff lints clean.** ``kickoff_lint`` counts a warning as a
  failed node in this pipeline, so the writer is pinned by the lint itself
  rather than by a template someone forgot to update.
"""
from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

import mini_ork
from mini_ork.kickoff_lint import lint
from mini_ork.rsi.candidates import Candidate
from mini_ork.rsi.harvest import ALL_RULES, SYNTHETIC_PREFIX
from mini_ork.rsi.queue import LedgerView
from mini_ork.rsi.select import (
    SELECT_CODES,
    Proposal,
    SelectConfig,
    SelectContext,
    Selection,
    build_kickoff,
    normalize_repo_path,
    paths_overlap,
    read_claims,
    registered_success_commands,
    select,
    write_selection,
)

NOW = 1_760_000_000
ROOT = Path(mini_ork.__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mini_ork_worktree.py"


def _cand(
    *,
    file: str = "mini_ork/learning/ledger.py",
    domain: str = "learning",
    kind: str = "data_path_dead",
    severity: int | None = None,
    source: str | None = None,
    command: str | None = None,
) -> Candidate:
    cand = Candidate.build(
        domain=domain,
        source=source or "db:L1",
        kind=kind,
        title="learning pass stats are never written",
        evidence_key=f"{file}:{kind}",
        files=(file,),
        now=NOW,
        success_command="db:L1" if command is None else command,
    )
    return cand if severity is None else replace(cand, severity=severity)


def _ctx(**kw) -> SelectContext:
    return SelectContext(root=ROOT, now=NOW, **kw)


def _run(cand: Candidate, proposal: Proposal | None = None, **kw) -> Selection:
    proposal = proposal or Proposal(candidate_id=cand.candidate_id,
                                    files=cand.files or ("mini_ork/learning/ledger.py",),
                                    summary="write the stat")
    return select({cand.candidate_id: cand}, proposal, _ctx(**kw))


# ── the accepted path ───────────────────────────────────────────────────────


def test_accepted_proposal_emits_a_kickoff_that_lints_with_zero_findings():
    cand = _cand()
    sel = _run(cand, Proposal(candidate_id=cand.candidate_id,
                              files=("mini_ork/learning/ledger.py",),
                              summary="Call record_pass_stat from the queue."))
    assert sel.accepted, (sel.code, sel.detail)
    assert sel.files == ("mini_ork/learning/ledger.py",)
    assert sel.kickoff_sha == hashlib.sha256(sel.kickoff.encode()).hexdigest()

    findings = lint(sel.kickoff, project=ROOT, recipe="framework-edit", home=None)
    assert findings == [], findings


def test_kickoff_names_the_scope_files_and_the_success_command():
    cand = _cand()
    sel = _run(cand)
    assert "## Files in scope" in sel.kickoff
    assert "`mini_ork/learning/ledger.py`" in sel.kickoff
    assert "## Verification command" in sel.kickoff
    assert f"`{cand.success_command}`" in sel.kickoff


def test_kickoff_is_deterministic_and_identity_carries_the_candidate():
    cand = _cand()
    a = _run(cand)
    b = _run(cand)
    assert a.kickoff == b.kickoff
    assert a.kickoff_sha == b.kickoff_sha
    assert cand.candidate_id in a.kickoff
    other = replace(cand, title="a different gap", evidence_key="other")
    assert _run(other).kickoff_sha != a.kickoff_sha


def test_paths_are_normalised_deduplicated_and_ordered():
    cand = _cand()
    sel = _run(cand, Proposal(candidate_id=cand.candidate_id, files=(
        "./mini_ork/learning/ledger.py",
        "mini_ork/learning/ledger.py",
        "mini_ork/learning/lesson_conflict.py",
    )))
    assert sel.accepted, sel.code
    assert sel.files == ("mini_ork/learning/ledger.py",
                         "mini_ork/learning/lesson_conflict.py")


# ── identity and severity ───────────────────────────────────────────────────


def test_unknown_candidate_is_candidate_not_in_harvest():
    cand = _cand()
    sel = select({}, Proposal(candidate_id=cand.candidate_id, files=cand.files), _ctx())
    assert sel.code == "candidate_not_in_harvest"


def test_below_min_severity_is_low_severity():
    cand = _cand(severity=10)
    assert _run(cand).code == "low_severity"


# ── the file set ────────────────────────────────────────────────────────────


def test_absolute_escaping_and_glob_paths_are_bad_path():
    cand = _cand()
    for bad in ("/etc/passwd", "../outside.py", "mini_ork/**/*.py", "~/x.py"):
        sel = _run(cand, Proposal(candidate_id=cand.candidate_id, files=(bad,)))
        assert sel.code == "bad_path", bad


def test_no_files_is_empty_scope():
    cand = _cand()
    sel = _run(cand, Proposal(candidate_id=cand.candidate_id, files=()))
    assert sel.code == "empty_scope"


def test_a_file_the_panel_invented_is_rejected():
    cand = _cand()
    sel = _run(cand, Proposal(candidate_id=cand.candidate_id,
                              files=("mini_ork/learning/does_not_exist.py",)))
    assert sel.code == "panel_invented_file"
    assert "does_not_exist" in sel.detail


def test_frozen_path_is_rejected_before_blast_and_allowlist():
    cand = _cand(file="config/kernel-frozen.yaml")
    sel = _run(cand, frozen_paths=("config/kernel-frozen.yaml",))
    assert sel.code == "frozen_path"


def test_a_cycle_may_not_edit_the_gate_that_validates_it():
    cand = _cand(file="mini_ork/rsi/select.py")
    sel = _run(cand)
    assert sel.code == "self_edit_of_gate"
    assert "validates it" in sel.detail


def test_the_self_edit_floor_wins_over_the_allowlist_but_not_over_frozen():
    cand = _cand(file="mini_ork/rsi/select.py")
    sel = _run(cand, frozen_paths=("mini_ork/rsi/*.py",))
    assert sel.code == "frozen_path"


def test_six_files_is_blast_radius():
    cand = _cand()
    six = ("mini_ork/learning/__init__.py", "mini_ork/learning/ledger.py",
           "mini_ork/learning/lesson_conflict.py", "mini_ork/dispatch/routing.py",
           "mini_ork/gates/gate_registry.py",
           "mini_ork/verify/panel_heterogeneity.py")
    sel = _run(cand, Proposal(candidate_id=cand.candidate_id, files=six))
    assert sel.code == "blast_radius"
    assert "6 files" in sel.detail


def test_file_outside_the_fix_surface_is_outside_allowlist():
    cand = _cand()
    sel = _run(cand, Proposal(candidate_id=cand.candidate_id, files=("README.md",)))
    assert sel.code == "outside_allowlist"


def test_config_may_widen_the_surface():
    cand = _cand()
    cfg = SelectConfig(allow_prefixes=("README.md",))
    sel = _run(cand, Proposal(candidate_id=cand.candidate_id, files=("README.md",)),
               config=cfg)
    assert sel.accepted, sel.code


# ── the success command ─────────────────────────────────────────────────────


def test_unregistered_success_command_is_rejected():
    cand = _cand(command="please make it better")
    sel = _run(cand)
    assert sel.code == "missing_success_command"


def test_a_proposal_may_override_the_command_but_not_invent_one():
    cand = _cand()
    ok = _run(cand, Proposal(candidate_id=cand.candidate_id,
                             files=cand.files, verification_command="garden"))
    assert ok.accepted, ok.code
    assert ok.verification_command == "garden"
    bad = _run(cand, Proposal(candidate_id=cand.candidate_id, files=cand.files,
                              verification_command="ls -la"))
    assert bad.code == "missing_success_command"


def test_every_harvest_rule_has_a_registered_synthetic_probe():
    commands = registered_success_commands()
    for _, rules in ALL_RULES:
        for rule_id, _ in rules:
            assert f"{SYNTHETIC_PREFIX}{rule_id}" in commands, rule_id
    for probe_id in ("garden", "validate", "tests", "verifier-audit", "lane-preflight"):
        assert probe_id in commands
    assert f"{SYNTHETIC_PREFIX}L8" not in commands  # a probe, not a rule
    assert "please make it better" not in commands


# ── already handled ─────────────────────────────────────────────────────────


def test_merged_signature_is_reported_as_already_fixed():
    cand = _cand()
    view = LedgerView(merged_ancestor_signatures=frozenset({cand.signature}))
    sel = _run(cand, view=view)
    assert sel.code == "already_fixed"
    assert "origin/main" in sel.detail


def test_a_probe_that_now_passes_means_the_gap_is_fixed():
    cand = _cand(source="probe:garden", kind="quality_red", command="garden",
                 file="mini_ork/learning/ledger.py")
    sel = _run(cand, probe_status={"garden": "ok"})
    assert sel.code == "already_fixed"
    assert "garden" in sel.detail


def test_a_probe_that_still_reports_is_not_fixed():
    cand = _cand(source="probe:garden", kind="quality_red", command="garden")
    sel = _run(cand, probe_status={"garden": "findings"})
    assert sel.accepted, sel.code


def test_claimed_candidate_is_already_claimed():
    cand = _cand()
    sel = _run(cand, view=LedgerView(claimed_ids=frozenset({cand.candidate_id})))
    assert sel.code == "already_claimed"


def test_recently_rejected_signature_is_failed_signature():
    cand = _cand()
    view = LedgerView(by_signature={cand.signature: {
        "status": "rejected", "updated_at": NOW - 3600, "severity": cand.severity,
    }})
    sel = _run(cand, view=view)
    assert sel.code == "failed_signature"


# ── claims ──────────────────────────────────────────────────────────────────


def test_overlapping_live_claim_is_worktree_claim_conflict():
    cand = _cand(file="mini_ork/learning/ledger.py")
    sel = _run(cand, claims=(("eco6-k14", "mini_ork/learning"),))
    assert sel.code == "worktree_claim_conflict"
    assert "eco6-k14" in sel.detail


def test_non_overlapping_claim_does_not_block():
    cand = _cand(file="mini_ork/learning/ledger.py")
    sel = _run(cand, claims=(("other", "recipes"),))
    assert sel.accepted, sel.code


def test_the_cycles_own_claim_slug_is_ignored():
    cand = _cand(file="mini_ork/learning/ledger.py")
    sel = _run(cand, claims=((f"rsi-{cand.candidate_id}", "mini_ork/learning"),))
    assert sel.accepted, sel.code


def test_paths_overlap_matches_the_worktree_script():
    spec = importlib.util.spec_from_file_location("mo_worktree_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pairs = [
        ("mini_ork/rsi", "mini_ork/rsi/queue.py"),
        ("mini_ork/rsi/", "mini_ork/rsi"),
        ("mini_ork/rsi", "mini_ork/rsi-extra/x.py"),
        ("mini_ork/rsi/queue.py", "mini_ork/rsi/queue.py"),
        ("recipes", "mini_ork/rsi"),
    ]
    for a, b in pairs:
        assert paths_overlap(a, b) == module.paths_overlap(a, b), (a, b)


def test_read_claims_tolerates_a_missing_or_messy_registry(tmp_path):
    assert read_claims(tmp_path) == ()
    (tmp_path / ".ownership").write_text(
        "good\tmini_ork/rsi\n\njunk-line\nother\t\nb\t tests/unit\n", encoding="utf-8"
    )
    assert read_claims(tmp_path) == (("good", "mini_ork/rsi"), ("b", "tests/unit"))


# ── proposal + config normalisation ─────────────────────────────────────────


def test_normalize_repo_path_keeps_only_real_relative_files():
    assert normalize_repo_path("./mini_ork//rsi/queue.py") == "mini_ork/rsi/queue.py"
    assert normalize_repo_path(" mini_ork/rsi/queue.py ") == "mini_ork/rsi/queue.py"
    for bad in ("", "/abs/x.py", "C:\\x.py", "~/x.py", "a/../../b.py", "x/*.py", "."):
        assert normalize_repo_path(bad) is None, bad


def test_proposal_reads_the_aliases_the_prompts_emit():
    p = Proposal.from_mapping({
        "candidate_id": "rsi-abc",
        "files_to_touch": "mini_ork/rsi/queue.py",
        "change_summary": "write the stat",
        "why": "no caller exists",
        "acceptance_ids": "AC1",
        "constraints": "do not touch probes.py",
        "success_command": "db:L1",
    })
    assert p.files == ("mini_ork/rsi/queue.py",)
    assert p.summary == "write the stat"
    assert p.root_cause == "no caller exists"
    assert p.ac_ids == ("AC1",)
    assert p.do_not_touch == ("do not touch probes.py",)
    assert p.verification_command == "db:L1"


def test_select_config_validates_and_reads_one_mapping():
    cfg = SelectConfig.from_mapping({"max_files": 2, "allow_prefixes": ["mini_ork/"],
                                     "min_severity": 55})
    assert cfg.max_files == 2
    assert cfg.allow_prefixes == ("mini_ork/",)
    assert cfg.queue.min_severity == 55
    with pytest.raises(ValueError):
        SelectConfig(max_files=0)
    with pytest.raises(ValueError):
        SelectConfig(allow_prefixes=())


def test_generated_kickoff_respects_max_files_from_config():
    cand = _cand()
    cfg = SelectConfig(max_files=1)
    sel = _run(cand, Proposal(candidate_id=cand.candidate_id,
                              files=("mini_ork/learning/ledger.py",
                                     "mini_ork/learning/lesson_conflict.py")),
               config=cfg)
    assert sel.code == "blast_radius"


# ── record shapes ───────────────────────────────────────────────────────────


def test_selection_rejects_inconsistent_records():
    with pytest.raises(ValueError):
        Selection(candidate_id="x", accepted=True)  # no kickoff
    with pytest.raises(ValueError):
        Selection(candidate_id="x", accepted=True, kickoff="k", code="frozen_path")
    with pytest.raises(ValueError):
        Selection(candidate_id="x", accepted=False, code="not_a_code")


def test_every_rejection_code_is_declared():
    cand = _cand()
    cases = [
        ("bad_path", Proposal(candidate_id=cand.candidate_id, files=("/abs.py",))),
        ("empty_scope", Proposal(candidate_id=cand.candidate_id, files=())),
        ("panel_invented_file", Proposal(candidate_id=cand.candidate_id,
                                         files=("mini_ork/learning/nope.py",))),
        ("self_edit_of_gate", Proposal(candidate_id=cand.candidate_id,
                                       files=("mini_ork/rsi/select.py",))),
    ]
    for code, proposal in cases:
        rejection = _run(cand, proposal)
        assert rejection.code == code
        assert code in SELECT_CODES


def test_write_selection_refuses_to_overwrite(tmp_path):
    cand = _cand()
    sel = _run(cand)
    target = tmp_path / "kickoff.md"
    assert write_selection(sel, target) == target
    assert target.read_text(encoding="utf-8") == sel.kickoff
    with pytest.raises(FileExistsError):
        write_selection(sel, target)
    with pytest.raises(ValueError):
        write_selection(Selection(candidate_id="x", accepted=False, code="empty_scope"),
                        tmp_path / "nope.md")


def test_build_kickoff_omits_an_empty_do_not_touch_line():
    cand = _cand()
    text = build_kickoff(cand, Proposal(candidate_id=cand.candidate_id),
                         verification_command=cand.success_command,
                         files=("mini_ork/learning/ledger.py",))
    assert "Do not touch:" not in text


def test_the_lint_assertion_is_not_vacuous():
    """Prove the zero-findings assertion above actually exercises the lint."""
    cand = _cand()
    sel = _run(cand)
    broken = sel.kickoff.replace("## Files in scope", "## Stuff")
    findings = lint(broken, project=ROOT, recipe="framework-edit", home=None)
    assert findings, "kickoff_lint agreed with a kickoff it should reject"
