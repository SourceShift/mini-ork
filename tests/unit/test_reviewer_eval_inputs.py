"""Hermetic tests for the reviewer eval's in-run reviewer inputs (R4b).

The eval must show the reviewer the same block a real run assembles: the
implementer summary, the verifier evidence, the verdict and the diff, framed by
the same markers and REVIEWER NOTE. The renderer is the eval's own copy (the
real ``_assemble_reviewer_inputs`` lives in ``execute.py``, held by another
worktree), so a drift test pins it byte-for-byte to the real assembler.

No test dispatches a real LLM lane: everything is tmp dirs, a tmp git repo and
pure functions.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import mini_ork
from mini_ork.cli import reviewer_eval_cmd
from mini_ork.review import reviewer_eval as rev

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]

_DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-1\n+2\n"


# ── fixture helpers ──────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull)
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    """A tmp git repo with one committed file; returns ``(repo, base sha)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "spec@example.invalid")
    _git(repo, "config", "user.name", "Spec Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


def _fixture_run(tmp_path: Path) -> tuple[Path, Path, str]:
    """A run dir the real assembler can render; returns (run_dir, repo, diff)."""
    repo, base = _make_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "pre-implementer-ref").write_text(base + "\n", encoding="utf-8")
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"worktree_path": str(repo), "files_changed": ["a.py"]},
                   indent=2) + "\n",
        encoding="utf-8")
    (run_dir / "verifier_test.json").write_text(
        '{"status": "green", "marker": "vt-mark"}\n', encoding="utf-8")
    (run_dir / "verifier_static-check.json").write_text(
        '{"status": "green"}\n', encoding="utf-8")
    (run_dir / "verdict.json").write_text('{"verdict": "pass"}\n',
                                          encoding="utf-8")
    (repo / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    return run_dir, repo, ""


# ── 1. drift: the eval renderer == the real assembler ───────────────────────


def test_render_matches_real_assembler_byte_for_byte(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MO_CTX_SECTION_MAX_CHARS", raising=False)
    from mini_ork.cli import execute

    run_dir, _repo, _ = _fixture_run(tmp_path)
    real = execute._assemble_reviewer_inputs(str(run_dir))
    diff = (run_dir / "review-diff.patch").read_text(
        encoding="utf-8", errors="replace")

    # Sanity: the fixture really exercised the diff and the missing-verifier slot.
    assert "+    return 2" in diff
    assert real.startswith(
        "--- Reviewer inputs (assembled by mini-ork-execute) ---\n")
    assert "\n# verifier_typecheck.json\n(not available)\n" in real

    evidence = tmp_path / "evidence"
    assert rev.copy_evidence(run_dir, evidence) == 4
    assert rev.render_reviewer_inputs(evidence, diff) == real


def test_render_matches_real_assembler_full_order(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MO_CTX_SECTION_MAX_CHARS", raising=False)
    from mini_ork.cli import execute

    run_dir, _repo, _ = _fixture_run(tmp_path)
    # Optional-but-present parity report, a sorted discovered verifier pair, an
    # empty discovered verifier, and the remaining optional reports.
    (run_dir / "pre-retirement-parity.json").write_text(
        '{"parity": "green"}\n', encoding="utf-8")
    (run_dir / "verifier_aaa.json").write_text('{"discovered": "aaa"}\n',
                                               encoding="utf-8")
    (run_dir / "verifier_zzz.json").write_text("", encoding="utf-8")
    (run_dir / "integration-map.json").write_text('{"nodes": []}\n',
                                                  encoding="utf-8")
    (run_dir / "static-feature-ledger.json").write_text(
        '{"features": []}\n', encoding="utf-8")
    (run_dir / "self-migrate.diff").write_text(
        "diff --git a/legacy b/legacy\n", encoding="utf-8")

    real = execute._assemble_reviewer_inputs(str(run_dir))
    diff = (run_dir / "review-diff.patch").read_text(
        encoding="utf-8", errors="replace")

    headers = [ln[2:] for ln in real.splitlines() if ln.startswith("# ")]
    assert headers == [
        "implementer-summary.json",
        "verifier_test.json",
        "verifier_typecheck.json",
        "pre-retirement-parity.json",
        "verifier_aaa.json",
        "verifier_static-check.json",
        "verifier_zzz.json",
        "integration-map.json",
        "static-feature-ledger.json",
        "verdict.json",
        "self-migrate.diff",
        "review-diff.patch",
    ]

    evidence = tmp_path / "evidence"
    rev.copy_evidence(run_dir, evidence)
    assert rev.render_reviewer_inputs(evidence, diff) == real


# ── render: empty evidence / empty diff ─────────────────────────────────────


def test_render_empty_evidence_dir(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    block = rev.render_reviewer_inputs(evidence, _DIFF)
    assert block.startswith(
        "--- Reviewer inputs (assembled by mini-ork-execute) ---\n")
    assert "\n# implementer-summary.json\n(not available)\n" in block
    assert "\n# verifier_test.json\n(not available)\n" in block
    assert "\n# verifier_typecheck.json\n(not available)\n" in block
    assert f"\n# review-diff.patch\n{_DIFF}\n" in block
    assert "--- End reviewer inputs ---" in block
    assert "REVIEWER NOTE:" in block


def test_render_missing_evidence_dir_is_empty_not_an_error(tmp_path):
    block = rev.render_reviewer_inputs(tmp_path / "does-not-exist", "")
    assert "\n# implementer-summary.json\n(not available)\n" in block
    assert "\n# review-diff.patch\n(no diff)\n" in block
    assert "review-diff-noop" not in block
    assert "the implementer declared" not in block


# ── 3. build_prompt sends the assembled block ───────────────────────────────


def _case(tmp_path: Path, evidence: dict[str, str]) -> rev.Case:
    case_dir = tmp_path / "case"
    (case_dir / "evidence").mkdir(parents=True)
    for name, text in evidence.items():
        (case_dir / "evidence" / name).write_text(text, encoding="utf-8")
    (case_dir / "kickoff.md").write_text("# Fix the frob\n\nBody.\n",
                                         encoding="utf-8")
    (case_dir / "diff.patch").write_text(_DIFF, encoding="utf-8")
    return rev.Case(id="spec-case", kind="clean", expect="pass",
                    source={"run": "some-run", "commit": ""},
                    defect={"files": [], "summary": "", "keywords": []},
                    path=case_dir)


def test_build_prompt_contains_assembled_inputs(tmp_path):
    case = _case(tmp_path, {
        "implementer-summary.json": '{"files_changed": ["a.py"]}\n',
        "verifier_test.json": '{"status": "green", "probe": "spec-probe-17"}\n',
    })
    prompt = rev.build_prompt(case, root=REPO_ROOT, review_file="review.json")
    assert "--- Reviewer inputs (assembled by mini-ork-execute) ---" in prompt
    assert "spec-probe-17" in prompt
    assert "--- End reviewer inputs ---" in prompt
    assert "REVIEWER NOTE:" in prompt
    assert _DIFF in prompt
    # The old bare-diff fragment is gone.
    assert "# Reviewer inputs\n# review-diff.patch\n" not in prompt


# ── 4. fingerprint carries the eval-inputs version ──────────────────────────


def test_fingerprint_hashes_eval_inputs_version(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    fp = rev.fingerprint(REPO_ROOT, "spec-lane", home=str(home))

    calls: list = []
    real_dumps = json.dumps

    def _spy(obj, *args, **kwargs):
        calls.append((obj, args, kwargs))
        return real_dumps(obj, *args, **kwargs)

    monkeypatch.setattr(json, "dumps", _spy)
    again = rev.fingerprint(REPO_ROOT, "spec-lane", home=str(home))

    matching = [c for c in calls
                if isinstance(c[0], dict) and "prompt_sha" in c[0]]
    assert matching, "fingerprint hashed object was not observed"
    obj, args, kwargs = matching[-1]
    assert obj.get("eval_inputs") == rev._EVAL_INPUTS_VERSION

    with_version = real_dumps(obj, *args, **kwargs)
    without = {key: val for key, val in obj.items() if key != "eval_inputs"}
    without_version = real_dumps(without, *args, **kwargs)

    assert hashlib.sha256(with_version.encode()).hexdigest()[:16] == fp["id"]
    assert fp["id"] != hashlib.sha256(without_version.encode()).hexdigest()[:16]
    assert fp["id"] == again["id"]
    # The returned shape is unchanged for status/garden and the existing tests.
    assert set(fp) == {"prompt_sha", "envelope_sha", "lane",
                       "resolved_lane", "model", "id"}


# ── 5. add-case --run copies the evidence ───────────────────────────────────


def _cli_env(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    home = tmp_path / "home"
    (root / "evals" / "reviewer" / "cases").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return root, home


def _write_case(root: Path, cid: str, source: dict) -> Path:
    case_dir = root / "evals" / "reviewer" / "cases" / cid
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case.json").write_text(json.dumps({
        "id": cid,
        "kind": "clean",
        "expect": "pass",
        "source": source,
        "defect": {"files": [], "summary": "", "keywords": []},
    }, indent=2) + "\n", encoding="utf-8")
    return case_dir


def test_add_case_from_run_copies_evidence(tmp_path, monkeypatch, capsys):
    root, home = _cli_env(monkeypatch, tmp_path)
    run_dir = home / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "framework-edit.diff").write_text(_DIFF, encoding="utf-8")
    (run_dir / "implementer-summary.json").write_text(
        '{"files_changed": ["f.py"]}\n', encoding="utf-8")
    (run_dir / "verifier_x.json").write_text(
        '{"status": "green", "marker": "vx"}\n', encoding="utf-8")
    (run_dir / "verdict.json").write_text('{"verdict": "pass"}\n',
                                          encoding="utf-8")
    # Not evidence names: these must not be copied.
    (run_dir / "run_profile.json").write_text('{"run_id": "r1"}\n',
                                              encoding="utf-8")
    (run_dir / "notes.txt").write_text("operator scratch\n", encoding="utf-8")
    kickoff = tmp_path / "kick.md"
    kickoff.write_text("# Fix the thing\n\nBody.\n", encoding="utf-8")

    rc = reviewer_eval_cmd.main(
        ["add-case", "--id", "run-case", "--kind", "clean", "--run", "r1",
         "--kickoff", str(kickoff)])
    assert rc == 0, capsys.readouterr().err

    evidence = root / "evals" / "reviewer" / "cases" / "run-case" / "evidence"
    assert (evidence / "verifier_x.json").read_text(encoding="utf-8") \
        == '{"status": "green", "marker": "vx"}\n'
    assert (evidence / "implementer-summary.json").is_file()
    assert (evidence / "verdict.json").is_file()
    assert not (evidence / "framework-edit.diff").exists()
    assert not (evidence / "run_profile.json").exists()
    assert not (evidence / "notes.txt").exists()


# ── 6. refresh-evidence: the three outcomes ─────────────────────────────────


def test_refresh_evidence_three_outcomes(tmp_path, monkeypatch, capsys):
    root, home = _cli_env(monkeypatch, tmp_path)
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "implementer-summary.json").write_text(
        '{"files_changed": ["a.py"]}\n', encoding="utf-8")
    (run_dir / "verifier_test.json").write_text('{"status": "green"}\n',
                                                encoding="utf-8")
    (run_dir / "verifier_x.json").write_text('{"status": "green"}\n',
                                             encoding="utf-8")
    _write_case(root, "case-run", {"run": "run-1", "commit": ""})
    _write_case(root, "case-commit", {"run": "", "commit": "deadbeef"})
    _write_case(root, "case-gone", {"run": "gone-run", "commit": ""})

    rc = reviewer_eval_cmd.main(["refresh-evidence"])
    assert rc == 0
    lines = {ln.strip() for ln in capsys.readouterr().out.splitlines()
             if ln.strip()}
    assert "case-run: 3 evidence file(s)" in lines
    assert "case-commit: commit case, no evidence" in lines
    assert "case-gone: no run dir" in lines
    evidence = root / "evals" / "reviewer" / "cases" / "case-run" / "evidence"
    assert (evidence / "verifier_x.json").is_file()
    assert (evidence / "verifier_test.json").is_file()
    assert (evidence / "implementer-summary.json").is_file()


def test_refresh_evidence_recopies_and_filters(tmp_path, monkeypatch, capsys):
    root, home = _cli_env(monkeypatch, tmp_path)
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "verifier_test.json").write_text('{"status": "new"}\n',
                                                encoding="utf-8")
    case_dir = _write_case(root, "case-run", {"run": "run-1", "commit": ""})
    stale = case_dir / "evidence"
    stale.mkdir()
    (stale / "verifier_test.json").write_text('{"status": "stale"}\n',
                                              encoding="utf-8")
    _write_case(root, "case-commit", {"run": "", "commit": "deadbeef"})

    rc = reviewer_eval_cmd.main(["refresh-evidence", "--case", "case-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "case-run: 1 evidence file(s)" in out
    assert "case-commit" not in out
    assert (stale / "verifier_test.json").read_text(encoding="utf-8") \
        == '{"status": "new"}\n'


def test_refresh_evidence_in_parser_help(capsys):
    assert reviewer_eval_cmd.main(["refresh-evidence", "--help"]) == 0
    assert "refresh-evidence" in capsys.readouterr().out
    args = reviewer_eval_cmd.build_parser().parse_args(["refresh-evidence"])
    assert args.command == "refresh-evidence"
    # main.py's usage line is pinned and must not be edited for this verb.
    text = (REPO_ROOT / "mini_ork" / "cli" / "main.py").read_text(
        encoding="utf-8")
    assert "reviewer-eval [list|add-case|run|status|score]" in text
    assert "refresh-evidence" not in text


def test_copy_evidence_drops_files_the_run_no_longer_has(tmp_path):
    from mini_ork.review import reviewer_eval as rev

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "verifier_test.json").write_text('{"pass": true}')
    dest = tmp_path / "case" / "evidence"
    dest.mkdir(parents=True)
    (dest / "verifier_static-check.json").write_text('{"pass": false}')
    assert rev.copy_evidence(run_dir, dest) == 1
    assert sorted(p.name for p in dest.iterdir()) == ["verifier_test.json"]


# ── 5. the round block a case's run received is replayed ────────────────────

_FEEDBACK = "the cache key ignores the file mtime"


def _round_run(tmp_path: Path, records: list[dict]) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "revise").mkdir(parents=True)
    feedback = run_dir / "revise" / "round-2.md"
    feedback.write_text(_FEEDBACK, encoding="utf-8")
    (run_dir / "revise" / "current.json").write_text(json.dumps(
        {"round": 2, "max_rounds": 2, "feedback": str(feedback)}), encoding="utf-8")
    (run_dir / "review-round-aware.json").write_text(
        json.dumps(records), encoding="utf-8")
    return run_dir


def test_copy_evidence_records_the_last_round(tmp_path):
    run_dir = _round_run(tmp_path, [
        {"arm": "on", "attempt": 1, "total": 3, "final": False},
        {"arm": "on", "attempt": 3, "total": 3, "final": True},
    ])
    dest = tmp_path / "evidence"
    rev.copy_evidence(run_dir, dest)
    assert json.loads((dest / "review-round.json").read_text(encoding="utf-8")) == {
        "arm": "on", "attempt": 3, "total": 3, "feedback": _FEEDBACK}
    # a second copy keeps it rather than sweeping it as a stale file
    rev.copy_evidence(run_dir, dest)
    assert (dest / "review-round.json").is_file()


def test_copy_evidence_keeps_the_feedback_the_last_review_saw(tmp_path):
    """An auto-repair round after the last review moves ``current.json`` on to
    new feedback; the eval must replay the round-<attempt-1> file instead."""
    run_dir = _round_run(tmp_path, [{"arm": "on", "attempt": 3, "total": 3}])
    later = run_dir / "revise" / "round-3.md"
    later.write_text("repair round 3: feedback no reviewer saw", encoding="utf-8")
    (run_dir / "revise" / "current.json").write_text(json.dumps(
        {"round": 3, "max_rounds": 3, "feedback": str(later)}), encoding="utf-8")
    dest = tmp_path / "evidence"
    rev.copy_evidence(run_dir, dest)
    record = json.loads((dest / "review-round.json").read_text(encoding="utf-8"))
    assert record["feedback"] == _FEEDBACK


def test_copy_evidence_removes_a_stale_round_record(tmp_path):
    run_dir = _round_run(tmp_path, [{"arm": "on", "attempt": 3, "total": 3}])
    dest = tmp_path / "evidence"
    rev.copy_evidence(run_dir, dest)
    assert (dest / "review-round.json").is_file()
    (run_dir / "review-round-aware.json").unlink()
    rev.copy_evidence(run_dir, dest)
    assert not (dest / "review-round.json").exists()


def test_build_prompt_replays_the_round_block_before_the_envelope(tmp_path):
    case = _case(tmp_path / "on", {"review-round.json": json.dumps(
        {"arm": "on", "attempt": 3, "total": 3, "feedback": _FEEDBACK})})
    prompt = rev.build_prompt(case, root=REPO_ROOT, review_file="review.json")
    block_at = prompt.index("## Review attempt 3 of 3")
    env_at = prompt.index("Respond with JSON:")
    assert block_at < env_at
    assert prompt[block_at:env_at].rstrip().endswith("Shipping test gaming always is.")

    holdout = _case(tmp_path / "holdout", {"review-round.json": json.dumps(
        {"arm": "holdout", "attempt": 3, "total": 3, "feedback": _FEEDBACK})})
    assert "## Review attempt" not in rev.build_prompt(
        holdout, root=REPO_ROOT, review_file="review.json")
