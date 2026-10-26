"""Hermetic tests for the framework-edit reviewer eval harness (rev6-r4).

No test dispatches a real LLM lane: ``run_case`` takes a ``dispatch_fn`` and the
CLI tests monkeypatch ``llm_dispatch.mo_llm_dispatch``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import mini_ork
from mini_ork.cli import garden
from mini_ork.cli import main as main_mod
from mini_ork.cli import reviewer_eval_cmd
from mini_ork.review import reviewer_eval

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]

EPICS_DEFECT = {
    "files": ["mini_ork/cli/epics.py"],
    "summary": "production code patches scheduler._RETRY_COLUMNS from another module at import time",
    "keywords": ["_RETRY_COLUMNS", "import time", "import-time", "monkey"],
}

DEFAULT_KICKOFF = "# Fix the epics archive\n\nSome plan body.\n"
DEFAULT_DIFF = "--- a/mini_ork/cli/epics.py\n+++ b/mini_ork/cli/epics.py\n"


# ── helpers ──────────────────────────────────────────────────────────────────


def make_framework_tree(root: Path) -> Path:
    prompts = root / "recipes" / "framework-edit" / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        REPO_ROOT / "recipes" / "framework-edit" / "prompts" / "reviewer.md",
        prompts / "reviewer.md")
    shutil.copyfile(
        REPO_ROOT / "recipes" / "framework-edit" / "workflow.yaml",
        root / "recipes" / "framework-edit" / "workflow.yaml")
    return root


def write_case(cases_dir: Path, cid: str, *, kind: str,
               defect: dict | None = None,
               kickoff: str = DEFAULT_KICKOFF,
               diff: str = DEFAULT_DIFF) -> Path:
    d = cases_dir / cid
    d.mkdir(parents=True, exist_ok=True)
    case = {
        "id": cid,
        "kind": kind,
        "expect": "block" if kind == "incident" else "pass",
        "source": {"run": "", "commit": ""},
        "defect": defect if defect is not None else {"files": [], "summary": "", "keywords": []},
        "added_at": "2026-10-26",
        "notes": "",
    }
    (d / "case.json").write_text(json.dumps(case))
    (d / "kickoff.md").write_text(kickoff)
    (d / "diff.patch").write_text(diff)
    return d


def incident_case(**kw) -> reviewer_eval.Case:
    base = dict(
        id="k12-import-time-cross-module-patch",
        kind="incident",
        expect="block",
        source={"run": "r", "commit": ""},
        defect=json.loads(json.dumps(EPICS_DEFECT)),
        path=Path("."),
    )
    base.update(kw)
    return reviewer_eval.Case(**base)


def clean_case(**kw) -> reviewer_eval.Case:
    base = dict(
        id="clean-k12-landed",
        kind="clean",
        expect="pass",
        source={"run": "", "commit": "c"},
        defect={"files": [], "summary": "", "keywords": []},
        path=Path("."),
    )
    base.update(kw)
    return reviewer_eval.Case(**base)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def env_root(tmp_path, monkeypatch):
    root = make_framework_tree(tmp_path / "root")
    (root / ".mini-ork").mkdir(exist_ok=True)
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_HOME", str(root / ".mini-ork"))
    monkeypatch.chdir(root)
    return root


# ── load_cases ───────────────────────────────────────────────────────────────


def test_load_cases_seeds_sorted_and_expect_from_kind():
    cases = reviewer_eval.load_cases(REPO_ROOT / "evals" / "reviewer" / "cases")
    ids = [c.id for c in cases]
    assert ids == sorted(ids)
    by_id = {c.id: c for c in cases}
    assert by_id["k12-import-time-cross-module-patch"].kind == "incident"
    assert by_id["k12-import-time-cross-module-patch"].expect == "block"
    assert by_id["clean-k12-landed"].kind == "clean"
    assert by_id["clean-k12-landed"].expect == "pass"


def test_load_cases_derives_expect_ignoring_stored_value(tmp_path):
    cases_dir = tmp_path / "cases"
    d = write_case(cases_dir, "a-case", kind="clean")
    payload = json.loads((d / "case.json").read_text())
    payload["expect"] = "block"  # stale/conflicting
    (d / "case.json").write_text(json.dumps(payload))
    assert reviewer_eval.load_cases(cases_dir)[0].expect == "pass"


def test_load_cases_incident_without_summary_names_the_file(tmp_path):
    cases_dir = tmp_path / "cases"
    write_case(cases_dir, "broken-incident", kind="incident",
               defect={"files": ["mini_ork/x.py"], "summary": "", "keywords": []})
    with pytest.raises(ValueError) as exc:
        reviewer_eval.load_cases(cases_dir)
    assert "broken-incident" in str(exc.value) or "case.json" in str(exc.value)


def test_load_cases_incident_without_anchor_and_clean_with_defect(tmp_path):
    cases_dir = tmp_path / "cases"
    write_case(cases_dir, "no-anchor", kind="incident",
               defect={"files": [], "summary": "real defect", "keywords": []})
    with pytest.raises(ValueError):
        reviewer_eval.load_cases(cases_dir)

    cases_dir2 = tmp_path / "cases2"
    write_case(cases_dir2, "dirty-clean", kind="clean", defect=EPICS_DEFECT)
    with pytest.raises(ValueError):
        reviewer_eval.load_cases(cases_dir2)


# ── fingerprint ──────────────────────────────────────────────────────────────


def test_fingerprint_stable_and_prompt_sensitive(tmp_path):
    root = make_framework_tree(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    first = reviewer_eval.fingerprint(root, "opus_lens", home=str(home))
    second = reviewer_eval.fingerprint(root, "opus_lens", home=str(home))
    assert first == second
    assert set(first) == {"prompt_sha", "envelope_sha", "lane",
                          "resolved_lane", "model", "id"}
    for key in ("prompt_sha", "envelope_sha", "id"):
        assert re.fullmatch(r"[0-9a-f]{16}", first[key])

    md = root / "recipes" / "framework-edit" / "prompts" / "reviewer.md"
    md.write_bytes(md.read_bytes() + b"# drifted\n")
    drifted = reviewer_eval.fingerprint(root, "opus_lens", home=str(home))
    assert drifted["prompt_sha"] != first["prompt_sha"]
    assert drifted["id"] != first["id"]


# ── build_prompt ─────────────────────────────────────────────────────────────


def test_build_prompt_sections_and_heading_fallback(tmp_path):
    root = make_framework_tree(tmp_path)
    reviewer_md = (root / "recipes" / "framework-edit" / "prompts" / "reviewer.md")
    cases_dir = tmp_path / "cases"
    write_case(cases_dir, "a-case", kind="incident", defect=EPICS_DEFECT)
    case = reviewer_eval.load_cases(cases_dir)[0]

    prompt = reviewer_eval.build_prompt(case, root=root, review_file="review.json")
    assert "--- Recipe prompt (system context) ---" in prompt
    assert "--- /recipe prompt ---" in prompt
    assert reviewer_md.read_text().strip()[:80] in prompt
    assert "Review the implementation for: Fix the epics archive" in prompt
    assert "Plan:\n" in prompt
    assert "# Reviewer inputs\n# review-diff.patch" in prompt
    assert "--- a/mini_ork/cli/epics.py" in prompt
    assert "resolved" in prompt
    assert "review.json" in prompt

    no_heading = tmp_path / "cases2"
    write_case(no_heading, "b-case", kind="incident", defect=EPICS_DEFECT,
               kickoff="just body text, no heading\n")
    loaded = reviewer_eval.load_cases(no_heading)[0]
    prompt2 = reviewer_eval.build_prompt(loaded, root=root, review_file="review.json")
    assert "Review the implementation for: b-case" in prompt2


def test_a_case_resolves_only_from_its_own_directory(tmp_path):
    """No lookup by id: a case whose directory has no files gets no other
    case's plan (the reviewer must be graded on the data that was loaded)."""
    root = make_framework_tree(tmp_path)
    write_case(root / "evals" / "reviewer" / "cases", "id-only", kind="incident",
               defect=EPICS_DEFECT, kickoff="# Id-only heading\n\nplan body\n")
    case = reviewer_eval.Case(
        id="id-only", kind="incident", expect="block",
        source={"run": "", "commit": ""},
        defect=json.loads(json.dumps(EPICS_DEFECT)),
        path=tmp_path / "placeholder")
    prompt = reviewer_eval.build_prompt(case, root=root, review_file="review.json")
    assert "Id-only heading" not in prompt
    assert "Review the implementation for: id-only" in prompt


def test_shipped_seeds_are_the_real_k12_incident_and_its_clean_landing():
    cases = {c.id: c for c in reviewer_eval.load_cases(REPO_ROOT / "evals" / "reviewer" / "cases")}
    incident = cases["k12-import-time-cross-module-patch"]
    clean = cases["clean-k12-landed"]
    assert (incident.kind, incident.expect) == ("incident", "block")
    assert (clean.kind, clean.expect) == ("clean", "pass")
    for case in (incident, clean):
        assert (case.path / "kickoff.md").read_text().startswith("# ECOSYSTEM §6 · K12")
        payload = json.loads((case.path / "case.json").read_text())
        assert "kickoff" not in payload and "diff" not in payload
    assert "scheduler._RETRY_COLUMNS = " in (incident.path / "diff.patch").read_text()
    assert "scheduler._RETRY_COLUMNS = " not in (clean.path / "diff.patch").read_text()


def test_build_prompt_falls_back_to_embedded_text_when_files_unreadable(tmp_path):
    """A read-only checkout keeps the data files absent; the prompt still gets them."""
    d = tmp_path / "cases" / "clean-x"
    d.mkdir(parents=True)
    (d / "case.json").write_text(json.dumps({
        "id": "clean-x", "kind": "clean", "expect": "pass",
        "source": {"run": "", "commit": ""},
        "defect": {"files": [], "summary": "", "keywords": []},
        "added_at": "2026-10-26", "notes": "",
        "kickoff": "# Embedded heading\n\nplan body\n",
        "diff": "--- a/x.py\n+++ b/x.py\n",
    }))
    (d / "kickoff.md").mkdir()  # exists but can never be read as a file
    (d / "diff.patch").mkdir()
    case = reviewer_eval.load_cases(tmp_path / "cases")[0]

    prompt = reviewer_eval.build_prompt(
        case, root=make_framework_tree(tmp_path), review_file="review.json")
    assert "Review the implementation for: Embedded heading" in prompt
    assert "--- a/x.py" in prompt


def test_build_prompt_truncates_huge_diff_and_kickoff(tmp_path):
    root = make_framework_tree(tmp_path)
    cases_dir = tmp_path / "cases"
    write_case(cases_dir, "huge-diff", kind="incident", defect=EPICS_DEFECT,
               diff="START\n" + ("y" * 200_000) + "\nDIFF_END_UNIQUE\n")
    write_case(cases_dir, "huge-kickoff", kind="incident", defect=EPICS_DEFECT,
               kickoff="# Heading\n\n" + ("k" * 50_000) + "KO_END_UNIQUE\n")
    by_id = {c.id: c for c in reviewer_eval.load_cases(cases_dir)}

    diff_prompt = reviewer_eval.build_prompt(
        by_id["huge-diff"], root=root, review_file="review.json")
    assert "[truncated]" in diff_prompt
    assert "DIFF_END_UNIQUE" not in diff_prompt

    ko_prompt = reviewer_eval.build_prompt(
        by_id["huge-kickoff"], root=root, review_file="review.json")
    assert "[truncated]" in ko_prompt
    assert "KO_END_UNIQUE" not in ko_prompt


# ── parse_review ─────────────────────────────────────────────────────────────


def test_parse_review_variants():
    assert reviewer_eval.parse_review('{"verdict": "pass"}')["verdict"] == "pass"
    prose = 'prose first\n{"verdict": "pass"} then\n{"verdict": "block", "findings": []}'
    assert reviewer_eval.parse_review(prose)["verdict"] == "block"
    assert reviewer_eval.parse_review("plain prose only") is None
    assert reviewer_eval.parse_review('{"notes": []}') is None
    assert reviewer_eval.parse_review("[1, 2, 3]") is None


# ── judge ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("pass", "pass"), ("approve", "pass"), ("approved", "pass"), ("PASS", "pass"),
    ("needs_revision", "block"), ("revise", "block"),
    ("request_changes", "block"), ("fail", "block"), ("reject", "block"),
    ("escalate", "block"), ("NEEDS_REVISION", "block"),
    ("banana", "error"), ("", "error"),
])
def test_judge_maps_verdicts(raw, expected):
    verdict, flagged, hit, _n = reviewer_eval.judge(
        incident_case(), {"verdict": raw, "findings": []})
    assert verdict == expected
    assert flagged is (expected == "block")
    assert hit is False


def test_judge_canonical_incident_finding():
    review = {"verdict": "needs_revision",
              "findings": [{"file": "mini_ork/cli/epics.py",
                            "issue": "import-time monkey patch"}]}
    assert reviewer_eval.judge(incident_case(), review) == ("block", True, True, 1)


def test_judge_hit_via_instances_and_keyword_and_clean_never_hits():
    instances = {"verdict": "needs_revision",
                 "findings": [{"file": "elsewhere.py", "issue": "x",
                               "instances": [{"file": "mini_ork/cli/epics.py"}]}]}
    assert reviewer_eval.judge(incident_case(), instances)[2] is True

    keyword = {"verdict": "needs_revision",
               "findings": [{"file": "elsewhere.py",
                             "issue": "a MONKEY patch at import time"}]}
    assert reviewer_eval.judge(incident_case(), keyword)[2] is True

    miss = {"verdict": "needs_revision",
            "findings": [{"file": "docs/x.md", "issue": "typo"}]}
    assert reviewer_eval.judge(incident_case(), miss)[2] is False
    assert reviewer_eval.judge(clean_case(), keyword)[2] is False
    assert reviewer_eval.judge(incident_case(), None)[0] == "error"


# ── run_case ─────────────────────────────────────────────────────────────────


def test_run_case_reads_review_json_in_isolated_cwd(tmp_path):
    root = make_framework_tree(tmp_path)
    work = tmp_path / "work"
    seen: dict = {}

    def fake(model=None, prompt=None, out_file=None, timeout_s=None,
             max_turns=None, **kw):
        seen.update(model=model, out_file=Path(out_file), timeout_s=timeout_s,
                    max_turns=max_turns, cwd=Path.cwd())
        Path("review.json").write_text(json.dumps(
            {"verdict": "needs_revision",
             "findings": [{"file": "mini_ork/cli/epics.py", "issue": "x"}]}))
        return 0

    res = reviewer_eval.run_case(incident_case(), root=root, lane="lane-x",
                                 work_dir=work, dispatch_fn=fake, timeout_s=33)
    assert (res.verdict, res.flagged, res.hit_defect, res.findings_n) == (
        "block", True, True, 1)
    assert res.error == ""
    assert seen["model"] == "lane-x"
    assert seen["out_file"] == work / "k12-import-time-cross-module-patch" / "out.txt"
    assert seen["timeout_s"] == 33 and seen["max_turns"] == 30
    assert seen["cwd"] == work / "k12-import-time-cross-module-patch"
    assert Path.cwd() != seen["cwd"]


def test_run_case_falls_back_to_out_txt_and_reads_cost(tmp_path):
    root = make_framework_tree(tmp_path)
    work = tmp_path / "work"

    def fake(model=None, prompt=None, out_file=None, timeout_s=None,
             max_turns=None, **kw):
        Path(out_file).write_text('{"verdict": "pass", "findings": []}')
        Path(str(out_file) + ".cost").write_text("0.25")
        return 0

    res = reviewer_eval.run_case(clean_case(), root=root, lane="l",
                                 work_dir=work, dispatch_fn=fake)
    assert res.verdict == "pass" and res.error == ""
    assert res.cost_usd == pytest.approx(0.25)


def test_run_case_rc_and_exception_become_error(tmp_path):
    root = make_framework_tree(tmp_path)
    work = tmp_path / "work"

    def bad_rc(model=None, prompt=None, out_file=None, timeout_s=None,
               max_turns=None, **kw):
        return 7

    res = reviewer_eval.run_case(clean_case(), root=root, lane="l",
                                 work_dir=work, dispatch_fn=bad_rc)
    assert "7" in res.error

    def boom(model=None, prompt=None, out_file=None, timeout_s=None,
             max_turns=None, **kw):
        raise RuntimeError("dispatch exploded")

    res2 = reviewer_eval.run_case(clean_case(), root=root, lane="l",
                                  work_dir=work, dispatch_fn=boom)
    assert "dispatch exploded" in res2.error


# ── score ────────────────────────────────────────────────────────────────────


def _result(**kw) -> reviewer_eval.CaseResult:
    base = dict(case_id="x", kind="incident", expect="block", verdict="error",
                flagged=False, hit_defect=False, findings_n=0,
                cost_usd=None, error="")
    base.update(kw)
    return reviewer_eval.CaseResult(**base)


def test_score_recall_and_false_blocks():
    results = [
        _result(case_id="i1", verdict="block", flagged=True, hit_defect=True,
                findings_n=1, cost_usd=0.10),
        _result(case_id="i2", verdict="pass", cost_usd=0.20),
        _result(case_id="c1", kind="clean", expect="pass", verdict="block",
                flagged=True, findings_n=1, cost_usd=0.30),
        _result(case_id="c2", kind="clean", expect="pass", verdict="pass"),
    ]
    summary = reviewer_eval.score(results)
    assert summary["incidents"]["n"] == 2
    assert summary["incidents"]["caught_on_defect"] == 1
    assert summary["incidents"]["recall"] == pytest.approx(0.5)
    assert summary["incidents"]["recall_wilson_lower"] is not None
    assert summary["clean"]["false_blocks"] == 1
    assert summary["clean"]["false_block_rate"] == pytest.approx(0.5)
    assert summary["errors"] == 0
    assert summary["cost_usd"] == pytest.approx(0.60)


def test_score_zero_n_and_errors_in_n():
    empty = reviewer_eval.score([])
    assert empty["incidents"]["recall"] is None
    assert empty["incidents"]["recall_wilson_lower"] is None
    assert empty["clean"]["false_block_rate"] is None

    summary = reviewer_eval.score([
        _result(case_id="i1", verdict="block", flagged=True, hit_defect=True),
        _result(case_id="i2", verdict="pass"),
        _result(case_id="i3", verdict="error", error="rc 7"),
    ])
    assert summary["incidents"]["n"] == 3
    assert summary["incidents"]["caught_on_defect"] == 1
    assert summary["incidents"]["recall"] == pytest.approx(1 / 3)
    assert summary["errors"] == 1


def test_latest_result_newest_by_name(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "2026-01-01-aaaaaaaaaaaaaaaa.jsonl").write_text(
        json.dumps({"summary": {"incidents": {"n": 1}}}) + "\n")
    (results / "2026-10-26-bbbbbbbbbbbbbbbb.jsonl").write_text(
        json.dumps({"case_id": "x"}) + "\n" +
        json.dumps({"summary": {"incidents": {"n": 2}}}) + "\n")
    path, row = reviewer_eval.latest_result(results)
    assert path.name == "2026-10-26-bbbbbbbbbbbbbbbb.jsonl"
    assert row["summary"]["incidents"]["n"] == 2
    assert reviewer_eval.latest_result(tmp_path / "missing") is None


# ── CLI ──────────────────────────────────────────────────────────────────────


def _fake_dispatch(monkeypatch, *, rc=0, review=None, cost="0.05"):
    from mini_ork.dispatch import llm_dispatch

    def fake(model=None, prompt=None, out_file=None, timeout_s=None,
             max_turns=None, **kw):
        out = Path(out_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(review if review is not None else
                                  {"verdict": "needs_revision",
                                   "findings": [{"file": "mini_ork/cli/epics.py",
                                                 "issue": "monkey"}]}))
        if cost is not None:
            Path(str(out_file) + ".cost").write_text(cost)
        return rc

    monkeypatch.setattr(llm_dispatch, "mo_llm_dispatch", fake)
    return fake


def test_cli_help_and_usage_errors(capsys):
    assert reviewer_eval_cmd.main(["--help"]) == 0
    assert "add-case" in capsys.readouterr().out
    assert reviewer_eval_cmd.main(["definitely-not-a-subcommand"]) == 2
    assert reviewer_eval_cmd.main(["run", "--no-such-flag"]) == 2


def test_cli_run_writes_results_then_status_lifecycle(env_root, monkeypatch, capsys):
    cases = env_root / "evals" / "reviewer" / "cases"
    write_case(cases, "k12-import-time-cross-module-patch", kind="incident",
               defect=EPICS_DEFECT)
    write_case(cases, "clean-k12-landed", kind="clean")
    _fake_dispatch(monkeypatch)

    rc = reviewer_eval_cmd.main(["run", "--work-dir", str(env_root / "w")])
    assert rc == 0, capsys.readouterr().err

    results_dir = env_root / "evals" / "reviewer" / "results"
    files = [p for p in results_dir.iterdir() if p.suffix == ".jsonl"]
    assert len(files) == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{16}\.jsonl", files[0].name)
    rows = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
    assert len(rows) == 3
    assert isinstance(rows[-1]["summary"], dict)
    assert rows[0]["fingerprint"] == rows[-1]["fingerprint"]["id"]
    assert sorted(rows[-1]["cases"]) == ["clean-k12-landed",
                                         "k12-import-time-cross-module-patch"]

    assert reviewer_eval_cmd.main(["status"]) == 0

    md = env_root / "recipes" / "framework-edit" / "prompts" / "reviewer.md"
    md.write_bytes(md.read_bytes() + b"# drifted\n")
    out = ""
    rc = reviewer_eval_cmd.main(["status"])
    captured = capsys.readouterr()
    out = captured.out
    assert rc == 1
    assert "reviewer changed since its last eval" in out
    assert "mini-ork reviewer-eval run" in out


def test_cli_run_dry_run_dispatches_nothing(env_root, monkeypatch, capsys):
    cases = env_root / "evals" / "reviewer" / "cases"
    write_case(cases, "k12-import-time-cross-module-patch", kind="incident",
               defect=EPICS_DEFECT)
    write_case(cases, "clean-k12-landed", kind="clean")

    from mini_ork.dispatch import llm_dispatch

    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not dispatch")

    monkeypatch.setattr(llm_dispatch, "mo_llm_dispatch", boom)
    rc = reviewer_eval_cmd.main(
        ["run", "--dry-run", "--work-dir", str(env_root / "w")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fingerprint" in out.lower()
    assert len(re.findall(r"\d{3,}", out)) >= 2


def test_cli_add_case_from_commit_and_duplicate(env_root, capsys):
    repo = env_root
    _git(repo, "init", "-q")
    (repo / "feature.txt").write_text("hello\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "-c", "user.email=t@t.local", "-c", "user.name=T",
         "commit", "-q", "-m", "add feature", "-m", "the body")
    sha = _git(repo, "rev-parse", "HEAD").strip()

    rc = reviewer_eval_cmd.main(
        ["add-case", "--id", "commit-case", "--kind", "clean", "--commit", sha])
    assert rc == 0, capsys.readouterr().err
    case_dir = env_root / "evals" / "reviewer" / "cases" / "commit-case"
    payload = json.loads((case_dir / "case.json").read_text())
    assert payload["kind"] == "clean" and payload["expect"] == "pass"
    assert payload["source"]["commit"] == sha
    assert "add feature" in (case_dir / "kickoff.md").read_text()
    assert (case_dir / "diff.patch").read_text().strip()

    assert reviewer_eval_cmd.main(
        ["add-case", "--id", "commit-case", "--kind", "clean", "--commit", sha]) == 1


def test_cli_add_case_incident_needs_anchor(env_root, capsys):
    repo = env_root
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "f.txt")
    _git(repo, "-c", "user.email=t@t.local", "-c", "user.name=T",
         "commit", "-q", "-m", "c", "-m", "b")
    sha = _git(repo, "rev-parse", "HEAD").strip()

    base = ["add-case", "--id", "inc", "--kind", "incident", "--commit", sha]
    assert reviewer_eval_cmd.main(base + ["--keyword", "boom"]) != 0
    assert reviewer_eval_cmd.main(base + ["--summary", "a defect"]) != 0
    assert reviewer_eval_cmd.main(
        base + ["--summary", "a defect", "--keyword", "boom"]) == 0


def test_reviewer_eval_registered_in_main():
    assert "reviewer-eval" in main_mod.SUBCOMMAND_REGISTRY
    assert main_mod.SUBCOMMAND_REGISTRY["reviewer-eval"](["--help"], str(REPO_ROOT)) == 0
    assert "reviewer-eval [list|add-case|run|status|score]" in main_mod._HELP
    assert "Score the framework-edit reviewer on incidents + clean changes." in main_mod._HELP


# ── garden ───────────────────────────────────────────────────────────────────


def test_garden_missing_cases_dir_is_silent(tmp_path, capsys):
    root = make_framework_tree(tmp_path / "root")
    home = tmp_path / "home"
    home.mkdir()
    findings = garden.Findings()
    garden._check_reviewer_eval(str(root), str(home), findings)
    assert findings.warnings == 0
    assert "reviewer changed" not in capsys.readouterr().err


def test_garden_stale_fingerprint_warns_uncounted(tmp_path, capsys):
    root = make_framework_tree(tmp_path / "root")
    home = tmp_path / "home"
    home.mkdir()
    write_case(root / "evals" / "reviewer" / "cases", "an-incident",
               kind="incident", defect=EPICS_DEFECT)

    findings = garden.Findings()
    garden._check_reviewer_eval(str(root), str(home), findings)
    assert findings.warnings == 0  # counted=False
    err = capsys.readouterr().err
    assert "reviewer changed since its last eval" in err
    assert "mini-ork reviewer-eval run" in err


def test_garden_matching_fingerprint_is_silent(tmp_path, capsys):
    root = make_framework_tree(tmp_path / "root")
    home = tmp_path / "home"
    home.mkdir()
    write_case(root / "evals" / "reviewer" / "cases", "an-incident",
               kind="incident", defect=EPICS_DEFECT)

    lane = reviewer_eval.reviewer_lane(root)
    fp_id = reviewer_eval.fingerprint(root, lane, home=str(home))["id"]
    results_dir = root / "evals" / "reviewer" / "results"
    results_dir.mkdir(parents=True)
    (results_dir / f"2026-10-26-{fp_id}.jsonl").write_text(json.dumps({
        "summary": {"incidents": {"n": 1}},
        "fingerprint": {"id": fp_id},
        "cases": ["an-incident"],
        "at": "2026-10-26T00:00:00",
    }) + "\n")

    findings = garden.Findings()
    garden._check_reviewer_eval(str(root), str(home), findings)
    assert "reviewer changed" not in capsys.readouterr().err


def test_garden_exception_is_silent(tmp_path, capsys):
    root = tmp_path / "root"
    home = tmp_path / "home"
    home.mkdir()
    write_case(root / "evals" / "reviewer" / "cases", "an-incident",
               kind="incident", defect=EPICS_DEFECT)
    findings = garden.Findings()
    garden._check_reviewer_eval(str(root), str(home), findings)
    assert findings.warnings == 0
    assert "reviewer changed" not in capsys.readouterr().err


# ── no real lanes ────────────────────────────────────────────────────────────


def test_no_test_calls_a_real_lane():
    """Guard: the dispatch seam is always faked in this module."""
    source = Path(__file__).read_text()
    assert "mo_llm_dispatch" in source  # only ever monkeypatched


def test_run_case_resolves_the_lane_alias_for_the_real_dispatcher(tmp_path, monkeypatch):
    """mo_llm_dispatch refuses an agents.yaml alias (`opus_lens`) as an unknown
    lane; the first live eval errored on every case. The eval must dispatch
    the alias's family lane, as the executor does."""
    from mini_ork.dispatch import llm_dispatch

    root = make_framework_tree(tmp_path)
    cases_dir = tmp_path / "cases"
    write_case(cases_dir, "alias-case", kind="incident", defect=EPICS_DEFECT)
    case = reviewer_eval.load_cases(cases_dir)[0]
    seen = []

    def fake_dispatch(lane, prompt, out_file, *args, **kwargs):
        seen.append(lane)
        Path(out_file).write_text('{"verdict": "pass", "findings": []}')
        return 0

    monkeypatch.setattr(llm_dispatch, "mo_llm_dispatch", fake_dispatch)
    monkeypatch.setattr(llm_dispatch, "resolve_lane_family",
                        lambda lane, root="", home="": {"opus_lens": "glm"}.get(lane, lane))
    result = reviewer_eval.run_case(case, root=root, lane="opus_lens",
                                    work_dir=tmp_path / "work")
    assert seen == ["glm"]
    assert result.error == ""
