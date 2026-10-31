"""RRL-5: revise a measured no-gain, and order research techniques by credit.

Two behaviours are pinned here, both hermetic: a temp DB migrated from the
repo's own ``db/migrations`` and a fake ``Runtime``, so no model, git or network
is touched.

* **Revise instead of giving up.** When G12 is the *only* failing deterministic
  gate and its rejection is a measured no-gain, the cycle re-dispatches the child
  with dev-only feedback. The sealed split must never reach the prompt.
* **Try the most promising technique family first.** ``_order_research`` is a
  permutation of the queue's technique positions driven by clade Thompson
  sampling, with a collapse override toward an untried family.

Each test names the failure it prevents. A cycle that cannot tell "the change
did nothing" from "the change broke a gate" either abandons a fixable technique
or pays forever for one that will never land.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_ork
from mini_ork.rsi import cycle
from mini_ork.rsi.candidates import Candidate
from mini_ork.rsi.config import (
    BudgetConfig,
    CycleConfig,
    DecideConfig,
    GatesConfig,
    ImpactConfig,
)
from mini_ork.rsi.gates import GateEvidence, GateResult, ValidationReport
from mini_ork.rsi.harvest import HarvestReport
from mini_ork.rsi.queue import QueueItem, QueuePlan
from mini_ork.rsi.select import Proposal, Selection
from mini_ork.stores.migrate import migrate_apply

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
#: Inside the verifier-replay benchmark surface, allowlisted, exists.
SURFACE_FILE = "recipes/code-fix/verifiers/test.py"
CYCLE_ID = "rsi-c900-revise"

_REJECTED_DEV = {
    "status": "rejected", "stage": "dev", "reason": "dev_net_loss",
    "dev": {"wins": 0, "losses": 1, "both_fail": 3,
            "items": [{"item": "h01", "stratum": "hard_negative",
                       "base": 0.0, "head": 0.0, "reason": "accepted"}]},
}
_ACCEPTED = {"status": "accepted", "stage": "sealed", "reason": "accepted"}


# --------------------------------------------------------------------------
# Hermetic scaffolding
# --------------------------------------------------------------------------


def _migrated_db(tmp_path: Path) -> Path:
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    db = home / "rsi.db"
    rc, lines = migrate_apply(str(REPO_ROOT / "db" / "migrations"),
                              dry_run=False, db=str(db))
    assert rc == 0, ("migrations failed", lines)
    return db


def _technique_candidate() -> Candidate:
    return Candidate.build(
        domain="verification", source="research:G01", kind="technique_unadopted",
        title="Adopt technique X", evidence_key="research:tech-x:1",
        now=1_800_000_000, detail="technique from a research review",
        files=(SURFACE_FILE,), fix_hint="apply the technique",
        success_metric="verifier replay", success_command="bench:verifier-replay",
        evidence={"priority": 5},
    )


def _cycle_config(*, max_revisions: int = 2,
                  per_cycle_cap_usd: float = 10.0) -> CycleConfig:
    return CycleConfig(
        budget=BudgetConfig(daily_cap_usd=50.0,
                            per_cycle_cap_usd=per_cycle_cap_usd),
        decide=DecideConfig(auto_merge=True),
        gates=GatesConfig(pytest_paths=("tests/unit/test_rsi_cycle_py.py",)),
        impact=ImpactConfig(max_revisions=max_revisions),
    )


def _run_cycle(tmp_path, monkeypatch, *, impacts, costs=None, max_revisions=2,
               per_cycle_cap_usd=10.0, fail_g6_first=False, second_impl_empty=False,
               clean_error=None):
    """One full cycle over a fake Runtime; returns the outcome plus an event log."""
    db = _migrated_db(tmp_path)
    monkeypatch.delenv("MO_RSI_CYCLE_DISABLED", raising=False)
    monkeypatch.setenv("MINI_ORK_WORKTREES_DIR", str(tmp_path / "worktrees"))

    cand = _technique_candidate()
    req = cycle.CycleRequest(
        root=REPO_ROOT, db=db,
        config=_cycle_config(max_revisions=max_revisions,
                             per_cycle_cap_usd=per_cycle_cap_usd),
        cycle_id=CYCLE_ID, candidate_id=cand.candidate_id)

    events: list = []
    selections: list = []
    costs = list(costs or [1.0])
    counters = {"impl": 0, "measure": 0}

    def probes(req):
        return ()

    def harvest(req, con, now, pr):
        return HarvestReport(candidates=(cand,), probes=(), harvest_ok=True,
                             skipped=(), errors=(), closed=())

    def propose(req, c):
        return Proposal(candidate_id=c.candidate_id, files=(SURFACE_FILE,),
                        summary="apply the technique", root_cause="wire it in")

    def implement(req, c, selection, **kw):
        n = counters["impl"]
        counters["impl"] += 1
        events.append("implement")
        selections.append(selection)
        if second_impl_empty and n == 1:
            return cycle.Implementation(
                SimpleNamespace(path=str(tmp_path)), None, changed_files=(),
                diff="", verdict={"pass": False}, child_run_id=f"r{n}",
                cost_usd=0.0, detail="the child abstained")
        return cycle.Implementation(
            SimpleNamespace(path=str(tmp_path)), None,
            changed_files=(SURFACE_FILE,),
            diff=f"diff --git a/{SURFACE_FILE} b/{SURFACE_FILE}\n"
                 f"--- attempt-{n}-diff-marker ---\n",
            verdict={"pass": True, "files_changed": 1},
            child_run_id=f"{CYCLE_ID}-fe-r{n}",
            cost_usd=costs[n] if n < len(costs) else costs[-1], detail="")

    def measure(req, c, impl, selection):
        n = counters["measure"]
        counters["measure"] += 1
        events.append(("measure", float(impl.cost_usd)))
        imp = impacts[n] if n < len(impacts) else impacts[-1]
        return GateEvidence(
            lint_ok=True, pytest_ok=not (fail_g6_first and n == 0),
            static_ran=True, probes_ran=True, impact=imp)

    def critics(req, c, impl, selection, report):
        from mini_ork.rsi.gates import CriticVerdict
        return (CriticVerdict(alias="minimax_lens", lane="minimax_lens",
                              approve=True),
                CriticVerdict(alias="deepseek_lens", lane="deepseek_lens",
                              approve=True))

    def merge(req, impl):
        events.append("merge")
        return SimpleNamespace(landed=True, sha="deadbeef00", status="merged",
                               detail="")

    def clean(req, impl):
        events.append("clean")
        if clean_error is not None:
            raise clean_error

    rt = cycle.Runtime(now=lambda: 1_800_000_000, probes=probes, harvest=harvest,
                       propose=propose, implement=implement, measure=measure,
                       critics=critics, spend_24h=lambda _db: 0.0, merge=merge,
                       clean=clean)
    outcome = cycle.run_cycle(req, runtime=rt)
    return SimpleNamespace(
        outcome=outcome, events=events, selections=selections,
        implement_calls=counters["impl"],
        measured_costs=[e[1] for e in events if isinstance(e, tuple)],
        candidate=cand)


# --------------------------------------------------------------------------
# End to end: revise then accept
# --------------------------------------------------------------------------


def test_a_measured_no_gain_is_revised_and_the_second_attempt_merges(
        tmp_path, monkeypatch, capsys):
    res = _run_cycle(tmp_path, monkeypatch,
                     impacts=[_REJECTED_DEV, _ACCEPTED], costs=[1.0, 2.0])

    assert res.outcome.status == "merged", res.outcome.detail
    assert res.implement_calls == 2
    # The first worktree is cleaned between attempts; the merged one is not.
    assert res.events.count("clean") == 1
    first_impl = [i for i, e in enumerate(res.events) if e == "implement"]
    clean_at = res.events.index("clean")
    assert first_impl[0] < clean_at < first_impl[1]

    second = res.selections[1]
    assert "## Revision 2 of 3" in second.kickoff
    assert "h01" in second.kickoff
    assert "--- attempt-0-diff-marker ---" in second.kickoff
    # The original kickoff appears once; the revision is appended, not stacked.
    assert second.kickoff.count("<!-- generated by rsi.select@v1") == 1
    assert second.kickoff.startswith(res.selections[0].kickoff.rstrip())
    assert second.kickoff_sha == hashlib.sha256(
        second.kickoff.encode("utf-8")).hexdigest()
    assert second.files == res.selections[0].files
    assert second.candidate_id == res.selections[0].candidate_id
    assert "[rsi] revision 1/2" in capsys.readouterr().err


def test_the_revised_kickoff_carries_no_sealed_counts(tmp_path, monkeypatch):
    leaked = dict(_REJECTED_DEV)
    leaked["sealed"] = {"wins": 9, "losses": 0}
    leaked["guards"] = {"ok": True, "violations": []}
    res = _run_cycle(tmp_path, monkeypatch, impacts=[leaked, _ACCEPTED])

    assert res.outcome.status == "merged", res.outcome.detail
    revised = res.selections[1].kickoff
    assert '"wins": 9' not in revised
    assert not re.search(r"sealed:\s*\d", revised)
    assert "9 fixed" not in revised


def test_an_empty_revision_child_is_implement_empty_with_the_revision_prefix(
        tmp_path, monkeypatch):
    res = _run_cycle(tmp_path, monkeypatch, impacts=[_REJECTED_DEV, _ACCEPTED],
                     second_impl_empty=True)

    assert res.implement_calls == 2
    assert res.outcome.status == "rejected"
    assert "implement_empty" in res.outcome.halt_reason
    assert "revision 1:" in res.outcome.halt_reason


def test_a_cleanup_failure_quarantines_as_revise_cleanup_failed(
        tmp_path, monkeypatch):
    res = _run_cycle(tmp_path, monkeypatch, impacts=[_REJECTED_DEV, _ACCEPTED],
                     clean_error=RuntimeError("clean exploded"))

    assert res.implement_calls == 1
    assert res.outcome.status == "quarantined"
    assert res.outcome.halt_reason == "revise_cleanup_failed"
    assert "clean exploded" in res.outcome.detail


def test_cumulative_cost_is_what_the_second_measurement_sees(tmp_path, monkeypatch):
    res = _run_cycle(tmp_path, monkeypatch, impacts=[_REJECTED_DEV, _ACCEPTED],
                     costs=[1.5, 2.0])

    assert res.outcome.status == "merged", res.outcome.detail
    assert res.measured_costs == [1.5, 3.5]


# --------------------------------------------------------------------------
# Not revisable
# --------------------------------------------------------------------------


def test_cap_exhaustion_quarantines_with_no_measured_gain(tmp_path, monkeypatch):
    res = _run_cycle(tmp_path, monkeypatch, impacts=[_REJECTED_DEV],
                     max_revisions=1)

    assert res.implement_calls == 2
    assert res.outcome.status in ("rejected", "quarantined")
    assert "no_measured_gain" in res.outcome.detail
    assert res.outcome.gates is not None
    assert res.outcome.gates.codes == ("no_measured_gain",)


def test_another_failing_gate_forbids_the_revision(tmp_path, monkeypatch):
    res = _run_cycle(tmp_path, monkeypatch, impacts=[_REJECTED_DEV],
                     fail_g6_first=True)

    assert res.implement_calls == 1
    assert res.outcome.status == "quarantined"
    assert "clean" not in res.events


@pytest.mark.parametrize("impact", [
    {"status": "rejected", "stage": "sealed",
     "reason": "sealed_budget_exhausted"},
    {"status": "error", "stage": "setup", "reason": "measure_error: boom"},
    {"status": "accepted", "stage": "sealed", "reason": "accepted"},
    None,
], ids=["budget-exhausted", "errored", "accepted", "absent"])
def test_a_non_measured_no_gain_forbids_the_revision(
        tmp_path, monkeypatch, impact):
    res = _run_cycle(tmp_path, monkeypatch, impacts=[impact, _ACCEPTED])

    assert res.implement_calls == 1
    assert "clean" not in res.events


def test_a_total_cost_at_the_cycle_cap_forbids_the_revision(tmp_path, monkeypatch):
    # Attempt 0 costs 4.0 (< 5.0 cap): revisable. Attempt 1 costs 1.0 more,
    # landing exactly on the cap: no further revision.
    res = _run_cycle(tmp_path, monkeypatch, impacts=[_REJECTED_DEV],
                     costs=[4.0, 1.0], per_cycle_cap_usd=5.0)

    assert res.implement_calls == 2
    assert res.outcome.status == "quarantined"
    assert res.outcome.gates is not None
    assert res.outcome.gates.codes == ("no_measured_gain",)


# --------------------------------------------------------------------------
# _revisable
# --------------------------------------------------------------------------


def _req(max_revisions=2, cap=10.0):
    return SimpleNamespace(config=SimpleNamespace(
        impact=SimpleNamespace(max_revisions=max_revisions),
        budget=SimpleNamespace(per_cycle_cap_usd=cap)))


def _report(failing):
    names = {"G6": ("pytest_scoped", "tests_failed"),
             "G12": ("impact", "no_measured_gain")}
    results = [GateResult(gate, name, False, code, "spec fixture")
               for gate, (name, code) in ((g, names[g]) for g in failing)]
    if not results:
        results.append(GateResult("G1", "harvest_complete", True))
    return ValidationReport(tuple(results))


def _measurement(impact):
    return SimpleNamespace(impact=impact)


def _revisable(report, impact, attempt=0, total=1.0):
    return cycle._revisable(_req(), _report(report), _measurement(impact),
                            attempt, total)


def test_revisable_true_when_every_condition_holds():
    assert _revisable(["G12"], _REJECTED_DEV) is True


@pytest.mark.parametrize(("report", "impact", "attempt", "total"), [
    (["G12"], _REJECTED_DEV, 2, 1.0),              # attempt < max_revisions
    (["G6", "G12"], _REJECTED_DEV, 0, 1.0),        # exactly G12 may fail
    (["G12"], {"status": "rejected", "stage": "sealed",
               "reason": "sealed_budget_exhausted"}, 0, 1.0),
    (["G12"], {"status": "error", "stage": "setup",
               "reason": "measure_error: X"}, 0, 1.0),
    (["G12"], {"status": "accepted"}, 0, 1.0),
    (["G12"], None, 0, 1.0),
    (["G12"], _REJECTED_DEV, 0, 10.0),             # at the cap
    (["G12"], _REJECTED_DEV, 0, 11.0),             # over the cap
], ids=["attempt-at-cap", "g6-with-g12", "sealed-budget-exhausted", "status-error",
        "status-accepted", "impact-none", "total-at-cap", "total-over-cap"])
def test_revisable_false_when_any_condition_fails(report, impact, attempt, total):
    assert _revisable(report, impact, attempt, total) is False


# --------------------------------------------------------------------------
# _attempt_record / _archive_impact
# --------------------------------------------------------------------------


def test_attempt_record_redacts_the_sealed_view_and_truncates_the_diff():
    impl = SimpleNamespace(changed_files=("a.py", "b.py"), diff="D" * 7000)
    measurement = _measurement({
        "status": "rejected", "stage": "dev", "reason": "dev_net_loss",
        "sealed": {"wins": 9, "losses": 0}, "guards": {"ok": True},
        "flaky": ["x1"], "dev": {},
    })

    record = cycle._attempt_record(0, impl, measurement)

    assert set(record) == {"attempt", "impact", "changed", "diff"}
    assert record["attempt"] == 1
    assert record["changed"] == ["a.py", "b.py"]
    assert len(record["diff"]) == 6000
    assert record["impact"]["sealed"] is None
    assert record["impact"]["guards"] is None
    assert "flaky" not in record["impact"]
    assert "wins: 9" not in str(record)


def test_archive_impact_copies_each_attempt_and_is_a_noop_without_a_file(tmp_path):
    home = tmp_path / "home"
    directory = home / "rsi" / CYCLE_ID
    directory.mkdir(parents=True)
    (directory / "impact.json").write_text('{"status": "rejected"}\n',
                                           encoding="utf-8")
    req = SimpleNamespace(home=home, cycle_id=CYCLE_ID)

    cycle._archive_impact(req, 0)
    snapshot = directory / "impact-attempt-1.json"
    assert snapshot.is_file()
    assert snapshot.read_text(encoding="utf-8") == '{"status": "rejected"}\n'
    assert (directory / "impact.json").is_file(), "the operator's copy stays"
    cycle._archive_impact(req, 1)
    assert (directory / "impact-attempt-2.json").is_file()

    empty = SimpleNamespace(home=tmp_path / "empty", cycle_id=CYCLE_ID)
    cycle._archive_impact(empty, 0)  # must not raise
    assert not (tmp_path / "empty" / "rsi" / CYCLE_ID
                / "impact-attempt-1.json").exists()


# --------------------------------------------------------------------------
# _dispatch_child
# --------------------------------------------------------------------------


def test_dispatch_child_names_each_attempt(tmp_path, monkeypatch):
    captured: list = []

    def fake_run(argv, **kwargs):
        captured.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cycle.subprocess, "run", fake_run)

    req = SimpleNamespace(
        root=REPO_ROOT, db=tmp_path / "state.db", home=tmp_path / "home",
        cycle_id=CYCLE_ID,
        config=SimpleNamespace(implement=SimpleNamespace(
            child_timeout_s=600, static_recipe_plan=True,
            runtime_backend="local")))
    child_home = tmp_path / "child-home"
    child_home.mkdir(parents=True)
    wt = SimpleNamespace(path=str(tmp_path / "wt"))

    run_id, rc, _ = cycle._dispatch_child(req, wt, "# kickoff body", child_home)
    assert run_id == f"{CYCLE_ID}-fe"
    assert rc == 0
    assert (child_home / "kickoff.md").read_text(encoding="utf-8") == "# kickoff body"
    argv, kwargs = captured[-1]
    assert str(child_home / "kickoff.md") in argv
    assert kwargs["env"]["MINI_ORK_RUN_ID"] == f"{CYCLE_ID}-fe"

    run_id2, _rc2, _ = cycle._dispatch_child(req, wt, "# revision body",
                                             child_home, attempt=2)
    assert run_id2 == f"{CYCLE_ID}-fe-r2"
    assert (child_home / "kickoff-r2.md").read_text(
        encoding="utf-8") == "# revision body"
    assert captured[-1][1]["env"]["MINI_ORK_RUN_ID"] == f"{CYCLE_ID}-fe-r2"


# --------------------------------------------------------------------------
# _revise_section / _revise_selection
# --------------------------------------------------------------------------


_SECTION_REQ = SimpleNamespace(config=SimpleNamespace(
    impact=SimpleNamespace(max_revisions=2)))


def _history_entry(attempt, impact, changed=(SURFACE_FILE,), diff="- a change\n"):
    return {"attempt": attempt, "impact": impact, "changed": list(changed),
            "diff": diff}


def test_revise_section_has_the_documented_structure():
    section = cycle._revise_section(_SECTION_REQ,
                                    [_history_entry(1, _REJECTED_DEV)])

    assert "## Revision 2 of 3 — the previous attempt showed no measured gain" in section
    assert ("The harness measured each attempt on a held-out benchmark. "
            "You see only its dev half; the sealed half reports a single bit."
            ) in section
    assert ("- attempt 1: stopped at dev: dev_net_loss. Dev split: 0 fixed, "
            "1 broken, 3 still wrong.") in section
    assert ("- `h01` (hard_negative): base 0.0, now 0.0. "
            "Verifier said: accepted") in section
    assert f"- `{SURFACE_FILE}`" in section
    assert "```diff" in section
    assert "- a change" in section
    assert "### Rules for this revision" in section
    assert "do not resubmit the same change" in section
    assert ("A change that special-cases the item ids above will fail the sealed "
            "half") in section
    assert "First check that the change implements the Mechanism under Expected Edit" in section
    assert "If the dev split shows 0 fixed and 0 broken" in section


def test_revise_section_prints_missing_numbers_as_zero_and_none_items():
    bare = {"status": "rejected", "stage": "dev", "reason": "dev_net_loss"}
    section = cycle._revise_section(_SECTION_REQ, [_history_entry(1, bare)])

    assert ("- attempt 1: stopped at dev: dev_net_loss. Dev split: "
            "0 fixed, 0 broken, 0 still wrong.") in section
    dev_block = section.split(
        "### Dev items the last attempt still gets wrong or broke", 1)[1]
    dev_block = dev_block.split("###", 1)[0]
    assert "- (none)" in dev_block


def test_revise_section_is_one_line_per_attempt_and_uses_only_the_last_items():
    second = {"status": "rejected", "stage": "sealed", "reason": "net_loss",
              "dev": {"wins": 2, "losses": 0, "both_fail": 1,
                      "items": [{"item": "h09", "stratum": "core", "base": 0.0,
                                 "head": 1.0, "reason": "fixed"}]}}
    section = cycle._revise_section(
        _SECTION_REQ,
        [_history_entry(1, _REJECTED_DEV), _history_entry(2, second)])

    assert "## Revision 3 of 3 — the previous attempt showed no measured gain" in section
    assert "- attempt 1: " in section and "- attempt 2: " in section
    assert ("- attempt 2: stopped at sealed: net_loss. Dev split: "
            "2 fixed, 0 broken, 1 still wrong.") in section
    assert "`h09`" in section
    assert "`h01` (" not in section


def test_revise_section_caps_dev_items_at_fifteen():
    items = [{"item": f"h{i:02d}", "stratum": "s", "base": 0.0, "head": 1.0,
              "reason": "fixed"} for i in range(20)]
    impact = {"status": "rejected", "stage": "dev", "reason": "dev_net_loss",
              "dev": {"wins": 20, "losses": 0, "both_fail": 0, "items": items}}
    section = cycle._revise_section(_SECTION_REQ, [_history_entry(1, impact)])

    item_lines = [line for line in section.splitlines()
                  if re.match(r"- `h\d+`", line)]
    assert len(item_lines) == 15
    assert "`h19`" not in section


def _selection() -> Selection:
    return Selection(
        candidate_id="rsi-spec", accepted=True, files=(SURFACE_FILE,),
        verification_command="bench:verifier-replay",
        kickoff="# RSI fix: original\n\n## Goal\n\noriginal body\n",
        kickoff_sha="0" * 64)


def test_revise_selection_appends_once_and_rehashes():
    original = _selection()
    revised = cycle._revise_selection(_SECTION_REQ, original,
                                      [_history_entry(1, _REJECTED_DEV)])

    assert revised is not original
    assert revised.kickoff.startswith(original.kickoff.rstrip())
    assert revised.kickoff.endswith("\n")
    assert revised.kickoff.count("## Revision 2 of 3") == 1
    assert revised.kickoff_sha == hashlib.sha256(
        revised.kickoff.encode("utf-8")).hexdigest()
    assert revised.files == original.files
    assert revised.candidate_id == original.candidate_id
    assert revised.accepted is True


def test_revise_selection_does_not_stack_sections():
    original = _selection()
    second = {"status": "rejected", "stage": "dev", "reason": "net_loss",
              "dev": {"wins": 0, "losses": 2, "both_fail": 0, "items": []}}
    # The second revision appends to the ORIGINAL kickoff with a 2-entry history.
    again = cycle._revise_selection(
        _SECTION_REQ, original,
        [_history_entry(1, _REJECTED_DEV), _history_entry(2, second)])

    assert again.kickoff.count("## Revision ") == 1
    assert "## Revision 3 of 3" in again.kickoff
    assert again.kickoff.count("# RSI fix: original") == 1


# --------------------------------------------------------------------------
# _order_research
# --------------------------------------------------------------------------


def _tech(candidate_id, family, priority):
    return Candidate(
        candidate_id=candidate_id, domain="verification",
        source=f"research:{family}", kind="technique_unadopted",
        title=f"technique {candidate_id}", detail="", severity=50,
        files=(SURFACE_FILE,), fix_hint="", success_metric="",
        success_command="bench:verifier-replay",
        evidence_key=f"research:{candidate_id}", signature=candidate_id,
        first_seen_at=1_800_000_000, evidence={"priority": priority})


def _gap(candidate_id):
    return Candidate(
        candidate_id=candidate_id, domain="learning", source="probe:tests",
        kind="signal_not_propagated", title=f"gap {candidate_id}", detail="",
        severity=50, files=("tests/unit/test_x.py",), fix_hint="",
        success_metric="", success_command="tests",
        evidence_key=f"file:x.py:{candidate_id}", signature=candidate_id,
        first_seen_at=1_800_000_000, evidence={"priority": 1})


def _plan(cands):
    return QueuePlan(selected=tuple(
        QueueItem(candidate=c, effective_severity=50, rank=i)
        for i, c in enumerate(cands)))


def _order_req(*, first=False):
    return SimpleNamespace(cycle_id=CYCLE_ID,
                           config=SimpleNamespace(
                               research=SimpleNamespace(first=first)))


def _credit_db(rows):
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE rsi_credit ("
        " id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL, candidate_id TEXT,"
        " source TEXT NOT NULL, lens TEXT, domain TEXT NOT NULL,"
        " outcome TEXT NOT NULL, severity INTEGER NOT NULL DEFAULT 0,"
        " utility REAL NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,"
        " created_at INTEGER NOT NULL)")
    for i, (candidate_id, source, outcome) in enumerate(rows):
        con.execute(
            "INSERT INTO rsi_credit (cycle_id, candidate_id, source, domain,"
            " outcome, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("rsi-c0", candidate_id, source, "verification", outcome, 1000 + i))
    con.commit()
    return con


def _ids(plan):
    return [item.candidate.candidate_id for item in plan.selected]


G01_FAILURES = [("techA", "research:G01", "rejected")] * 5
G02_MERGES = [("techB", "research:G02", "merged")] * 5


def test_credit_orders_techniques_within_their_positions(capsys):
    plan = _plan([_gap("gap"), _tech("techA", "G01", 3),
                  _gap("gap2"), _tech("techB", "G02", 7)])
    # A NULL candidate_id row becomes "" and harms nothing.
    con = _credit_db(G01_FAILURES + G02_MERGES
                     + [(None, "research:G01", "rejected")])

    out = cycle._order_research(_order_req(), con, plan)

    assert _ids(out) == ["gap", "techB", "gap2", "techA"]
    assert [item.rank for item in out.selected] == [1, 2, 3, 4]
    assert sorted(_ids(out)) == sorted(_ids(plan))
    assert all(out.find(cid) is not None for cid in _ids(plan))
    assert "[rsi] research order:" in capsys.readouterr().err


def test_research_first_moves_techniques_to_the_front():
    plan = _plan([_gap("gap"), _tech("techA", "G01", 3),
                  _gap("gap2"), _tech("techB", "G02", 7)])
    con = _credit_db(G01_FAILURES + G02_MERGES)

    out = cycle._order_research(_order_req(first=True), con, plan)

    assert _ids(out) == ["techB", "techA", "gap", "gap2"]
    assert [item.rank for item in out.selected] == [1, 2, 3, 4]


def test_collapse_forces_the_untried_family_first():
    # G01 carries a HIGHER prior, so only the collapse override can put the
    # untried family's technique first.
    plan = _plan([_gap("gap"), _tech("techG01", "G01", 9),
                  _gap("gap2"), _tech("techG03", "G03", 1)])
    con = _credit_db([("techG01", "research:G01", "rejected")] * 6)

    out = cycle._order_research(_order_req(), con, plan)

    assert _ids(out) == ["gap", "techG03", "gap2", "techG01"]
    assert [item.rank for item in out.selected] == [1, 2, 3, 4]


def test_the_same_cycle_id_yields_the_same_order():
    def plan():
        return _plan([_gap("gap"), _tech("techA", "G01", 3),
                      _gap("gap2"), _tech("techB", "G02", 7)])

    con = _credit_db(G01_FAILURES + G02_MERGES)
    first = cycle._order_research(_order_req(), con, plan())
    second = cycle._order_research(_order_req(), con, plan())

    assert _ids(first) == _ids(second)


def test_a_plan_without_techniques_is_returned_unchanged():
    plan = _plan([_gap("gap"), _gap("gap2")])
    con = _credit_db(G01_FAILURES + G02_MERGES)

    assert cycle._order_research(_order_req(), con, plan) is plan


def test_a_sqlite_error_reads_as_no_attempts():
    plan = _plan([_gap("gap"), _tech("techA", "G01", 3),
                  _gap("gap2"), _tech("techB", "G02", 7)])

    class Broken:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("no such table: rsi_credit")

    out = cycle._order_research(_order_req(), Broken(), plan)

    assert isinstance(out, QueuePlan)
    assert sorted(_ids(out)) == sorted(_ids(plan))
    assert [item.rank for item in out.selected] == [1, 2, 3, 4]


# --------------------------------------------------------------------------
# The ordering is wired into _body
# --------------------------------------------------------------------------


def test_order_research_is_called_once_right_after_the_queue(tmp_path, monkeypatch):
    real = cycle._order_research
    seen: list = []

    def spy(req, con, plan):
        seen.append(plan)
        return real(req, con, plan)

    monkeypatch.setattr(cycle, "_order_research", spy)
    res = _run_cycle(tmp_path, monkeypatch, impacts=[_ACCEPTED])

    assert res.outcome.status == "merged", res.outcome.detail
    assert len(seen) == 1
    assert isinstance(seen[0], QueuePlan)
    assert res.candidate.candidate_id in [i.candidate.candidate_id
                                          for i in seen[0].selected]
