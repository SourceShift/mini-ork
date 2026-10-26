"""The per-run review findings ledger (kickoff rev6-r5a).

Covers the contract in ``mini_ork/workflow/review_ledger.py``: stable ids,
re-reports, fixes, regressions, out-of-delta findings, the reviewer's prior
block and the implementer's feedback lines.
"""
import json
import os

from mini_ork.workflow import review_ledger


def _run_dir(tmp_path):
    run = tmp_path / "run"
    (run / "revise").mkdir(parents=True, exist_ok=True)
    return run


def _finding(**kw):
    base = {"file": "a.py", "line": 1, "severity": "medium", "issue": "a bug"}
    base.update(kw)
    return base


def test_ledger_rel_and_attempt_no(tmp_path):
    assert review_ledger.LEDGER_REL == "revise/findings.json"
    run = _run_dir(tmp_path)
    assert review_ledger.attempt_no(str(run)) == 1
    (run / "revise" / "current.json").write_text(json.dumps({"round": 2}))
    assert review_ledger.attempt_no(str(run)) == 3
    (run / "revise" / "current.json").write_text("{not json")
    assert review_ledger.attempt_no(str(run)) == 1


def test_first_review_assigns_ids(tmp_path):
    run = _run_dir(tmp_path)
    review = {"verdict": "needs_revision",
              "findings": [_finding(file="path.py", line=42, severity="high",
                                    issue="off by one"),
                           _finding(file="b.py", line=7, severity="low",
                                    issue="typo")],
              "resolved": []}
    ledger = review_ledger.record_review(str(run), 1, review, now=1000)
    assert [f["id"] for f in ledger["findings"]] == ["F1", "F2"]
    assert all(f["status"] == "open" for f in ledger["findings"])
    assert all(f["first_seen"] == f["last_seen"] == 1 for f in ledger["findings"])
    # ids are rewritten in place so the caller can persist them
    assert [f["id"] for f in review["findings"]] == ["F1", "F2"]
    summary = ledger["rounds"][0]
    assert summary["new"] == 2 and summary["reported_again"] == 0
    assert summary["fixed"] == 0 and summary["still_open"] == 2
    assert summary["attempt"] == 1 and summary["verdict"] == "needs_revision"
    assert summary["at"] == 1000


def test_second_review_rereport_resolve_and_new(tmp_path):
    run = _run_dir(tmp_path)
    review_ledger.record_review(str(run), 1, {
        "verdict": "needs_revision",
        "findings": [_finding(file="path.py", line=42, severity="high",
                              issue="off by one"),
                     _finding(file="b.py", line=7, severity="low",
                              issue="typo")],
        "resolved": []}, now=1)
    second = {"verdict": "needs_revision",
              "findings": [
                  {"id": "F1", "file": "path.py", "line": 43,
                   "severity": "medium", "issue": "off by one still",
                   "instances": [{"file": "q.py", "line": 5}]},
                  _finding(file="c.py", line=9, severity="low",
                           issue="new thing")],
              "resolved": ["F2"]}
    ledger = review_ledger.record_review(str(run), 2, second, now=2)
    by_id = {f["id"]: f for f in ledger["findings"]}
    assert by_id["F1"]["status"] == "open"
    assert by_id["F1"]["last_seen"] == 2
    assert by_id["F1"]["severity"] == "medium"
    assert by_id["F1"]["line"] == 43
    assert by_id["F1"]["instances"] == [{"file": "q.py", "line": 5}]
    assert by_id["F2"]["status"] == "fixed"
    assert by_id["F2"]["fixed_in"] == 2
    assert by_id["F3"]["status"] == "open"
    assert by_id["F3"]["first_seen"] == 2
    assert second["findings"][1]["id"] == "F3"
    summary = ledger["rounds"][1]
    assert summary["reported_again"] == 1
    assert summary["fixed"] == 1
    assert summary["new"] == 1
    assert summary["still_open"] == 2


def test_resolved_and_rereported_stays_open(tmp_path):
    run = _run_dir(tmp_path)
    review_ledger.record_review(str(run), 1, {
        "verdict": "needs_revision",
        "findings": [_finding(issue="one"), _finding(file="b.py", issue="two")],
        "resolved": []}, now=1)
    ledger = review_ledger.record_review(str(run), 2, {
        "verdict": "needs_revision",
        "findings": [{"id": "F1", "file": "a.py", "line": 1,
                      "severity": "high", "issue": "one"}],
        "resolved": ["F1"]}, now=2)
    by_id = {f["id"]: f for f in ledger["findings"]}
    assert by_id["F1"]["status"] == "open"
    assert ledger["rounds"][1]["fixed"] == 0
    assert ledger["rounds"][1]["reported_again"] == 1


def test_new_finding_matching_fixed_is_reopened(tmp_path):
    run = _run_dir(tmp_path)
    review_ledger.record_review(str(run), 1, {
        "verdict": "needs_revision",
        "findings": [_finding(issue="Null pointer deref!")],
        "resolved": []}, now=1)
    review_ledger.record_review(str(run), 2,
                                {"verdict": "pass", "findings": [],
                                 "resolved": ["F1"]}, now=2)
    ledger = review_ledger.record_review(str(run), 3, {
        "verdict": "needs_revision",
        "findings": [_finding(line=2, issue="null   pointer: deref!")],
        "resolved": []}, now=3)
    reopened = [f for f in ledger["findings"] if f.get("reopened")]
    assert [f["id"] for f in reopened] == ["F2"]
    assert reopened[0]["status"] == "open"
    assert ledger["rounds"][2]["reopened"] == 1


def test_outside_delta_only_marks_new_findings_outside(tmp_path):
    run = _run_dir(tmp_path)
    review = {"verdict": "needs_revision",
              "findings": [_finding(file="b.py", issue="out of scope"),
                           _finding(file="a.py", issue="in scope")],
              "resolved": []}
    ledger = review_ledger.record_review(str(run), 1, review,
                                         delta_files=["a.py"], now=1)
    by_file = {f["file"]: f for f in ledger["findings"]}
    assert by_file["b.py"]["outside_delta"] is True
    assert not by_file["a.py"].get("outside_delta")
    assert ledger["rounds"][0]["outside_delta"] == 1

    other = _run_dir(tmp_path / "other")
    ledger = review_ledger.record_review(str(other), 1, dict(review), now=1)
    assert all(not f.get("outside_delta") for f in ledger["findings"])


def test_field_normalization_and_non_list_inputs(tmp_path):
    run = _run_dir(tmp_path)
    ledger = review_ledger.record_review(str(run), 1, {
        "verdict": "fail",
        "findings": [
            _finding(severity="HIGH", issue="upper"),
            _finding(file="c.py", severity="catastrophic", issue="weird"),
            _finding(file="d.py", issue="x" * 500),
        ],
        "resolved": "F1"}, now=1)
    by_file = {f["file"]: f for f in ledger["findings"]}
    assert by_file["a.py"]["severity"] == "high"
    assert by_file["c.py"]["severity"] == "medium"
    assert len(by_file["d.py"]["issue"]) <= 400
    assert all(f["instances"] == [] for f in ledger["findings"])
    # a non-list findings key is treated as no findings
    ledger = review_ledger.record_review(str(run), 2,
                                         {"verdict": "pass", "findings": "oops"},
                                         now=2)
    assert ledger["rounds"][1]["new"] == 0


def test_feedback_lines_still_open_only_after_two_reviews(tmp_path):
    run = _run_dir(tmp_path)
    review_ledger.record_review(str(run), 1, {
        "verdict": "needs_revision",
        "findings": [_finding(file="path.py", line=42, severity="high",
                              issue="the issue",
                              instances=[{"file": "a.py", "line": 3},
                                         {"file": "b.py", "line": 9}]),
                     _finding(file="gone.py", severity="low",
                              issue="fixed one")],
        "resolved": []}, now=1)
    lines = review_ledger.feedback_lines(str(run), 1)
    assert lines[0] == ("- [F1] high path.py:42 — the issue "
                        "(also at a.py:3, b.py:9)")
    assert "STILL OPEN" not in lines[0]
    review_ledger.record_review(str(run), 2, {
        "verdict": "needs_revision",
        "findings": [{"id": "F1", "file": "path.py", "line": 42,
                      "severity": "high", "issue": "the issue"}],
        "resolved": ["F2"]}, now=2)
    lines = review_ledger.feedback_lines(str(run), 2)
    assert len(lines) == 1
    assert lines[0].startswith("- [F1] high path.py:42 — the issue")
    assert (" — STILL OPEN after 2 reviews: the earlier fix did not work; "
            "change the approach for this finding instead of patching the "
            "same lines again.") in lines[0]


def test_prior_block_empty_then_open_before_fixed(tmp_path):
    run = _run_dir(tmp_path)
    assert review_ledger.prior_block(str(run)) == ""
    review_ledger.record_review(str(run), 1, {
        "verdict": "needs_revision",
        "findings": [_finding(file="a.py", line=1, severity="high",
                              issue="bug one"),
                     _finding(file="b.py", line=2, severity="low",
                              issue="bug two")],
        "resolved": []}, now=1)
    review_ledger.record_review(str(run), 2,
                                {"verdict": "needs_revision", "findings": [],
                                 "resolved": ["F2"]}, now=2)
    block = review_ledger.prior_block(str(run))
    assert block.startswith("## Your earlier findings (ids are stable across rounds)")
    open_line = "- [F1] high a.py:1 — bug one"
    fixed_line = "- [F2] low b.py:2 — bug two"
    assert open_line in block and fixed_line in block
    assert block.index(open_line) < block.index("Already fixed")
    assert block.index("Already fixed") < block.index(fixed_line)


def test_load_corrupt_returns_empty_shape(tmp_path):
    run = _run_dir(tmp_path)
    (run / "revise" / "findings.json").write_text("][ not json")
    assert review_ledger.load(str(run)) == {"findings": [], "rounds": []}
    # the write is atomic and leaves no temp file beside the ledger
    review_ledger.record_review(str(run), 1,
                                {"verdict": "pass", "findings": []}, now=1)
    with open(os.path.join(str(run), "revise", "findings.json"),
              encoding="utf-8") as handle:
        assert json.load(handle) == review_ledger.load(str(run))
    assert os.listdir(os.path.join(str(run), "revise")) == ["findings.json"]
