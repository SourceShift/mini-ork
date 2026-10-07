"""``mini-ork.ide_pages.node_changes.build_changes_view`` — node detail's
``changes`` view: per-node result table, run-cumulative files + diff, run
commits.

The fixture mirrors the kickoff's "Tests" §: a temp home with a five-node
DAG (planner → implementer → verifier_node → reviewer → publisher), an
``acp-diffs.json`` with two files, a verifier JSON + log, a review JSON
with two findings (``file:line``), a ``plan.json``, and a real git
workspace record with two commits on the run branch.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages.node import build_node
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-changes"
T0 = 1_791_000_000


WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: verifier_node, type: verifier, verifier_ref: verifiers/test.py}
  - {name: static_check_verifier, type: verifier, verifier_ref: verifiers/static-check.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher}
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: demo\n")
    return h


def _seed_git_repo(project: Path) -> tuple[Path, str]:
    """Init a tiny git repo at ``project`` (the run's parent dir). One base
    commit, then a linked worktree at ``project/wt`` with two extra commits.

    Returns ``(worktree_path, base_sha)``. The worktree is a linked worktree
    so ``git log`` runs there; the project's main checkout has the same
    files (because they're tracked), so ``_project_file`` maps diff paths
    back onto ``project`` and returns a non-None ``abs``.
    """
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "x@x"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=project, check=True)
    (project / "README.md").write_text("# demo\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=project, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True, check=True
    ).stdout.strip()
    wt_path = project / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "wt/test", str(wt_path)], cwd=project, check=True)
    (wt_path / "a.py").write_text("def a(): return 1\n")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add a"], cwd=wt_path, check=True)
    (wt_path / "b.py").write_text("def b(): return 2\n")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add b"], cwd=wt_path, check=True)
    return wt_path, base_sha


def _seed(home: Path, *, repo: Path, base_sha: str) -> Path:
    """One run with five completed nodes + the artefacts the kickoff lists."""
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# demo\n")

    # Workspace record so ``run.workspace`` is populated (the changes view
    # reads ``base_sha`` / ``branch`` / ``path`` from it).
    (home / "worktrees").mkdir(parents=True, exist_ok=True)
    ws_record = home / "worktrees" / f"{RUN}.json"
    ws_record.write_text(
        json.dumps(
            {
                "run_id": RUN,
                "path": str(repo),
                "branch": "wt/test",
                "base_branch": "main",
                "base_sha": base_sha,
                "project": str(home.absolute().parent),
                "adopted": False,
                "clean_at_start": True,
                "home": str(home),
            }
        )
    )

    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            RUN,
            "demo-recipe",
            "executing",
            0.0,
            T0,
            T0 + 100,
            T0 + 90,
            "demo",
            str(kickoff),
            "latest",
            "tr-demo-1",
        ),
    )

    # Six completed nodes (planner, implementer, verifier_node,
    # static_check_verifier, reviewer, publisher). Each emits node_start
    # then node_end so the run's DAG is fully attributed.
    events = [
        ("planner", "planner", "planner", T0 + 10, T0 + 20),
        ("implementer", "implementer", "worker", T0 + 20, T0 + 50),
        ("verifier_node", "verifier", "verifier", T0 + 50, T0 + 60),
        ("static_check_verifier", "verifier", "verifier", T0 + 60, T0 + 65),
        ("reviewer", "reviewer", "reviewer", T0 + 65, T0 + 70),
        ("publisher", "publisher", "publisher", T0 + 70, T0 + 80),
    ]
    for i, (node, ntype, lane, ts, end) in enumerate(events):
        for event_kind, ts_emit in (("node_start", ts - 1), ("node_end", end)):
            payload = {"node_id": node, "node_type": ntype, "model_lane": lane, "finish_reason": "done"}
            con.execute(
                "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (f"ev-{i}-{event_kind}", RUN, event_kind, json.dumps(payload), ts_emit),
            )
    con.commit()
    con.close()

    # plan.json — the planner's two step entries (objective + a step).
    (run_dir / "plan.json").write_text(
        json.dumps(
            {
                "objective": "demo planner objective",
                "steps": [
                    {"id": "implementer", "type": "implementer", "lane": "worker"},
                    {"id": "verifier_node", "type": "verifier", "lane": "verifier"},
                ],
            }
        )
    )

    # acp-diffs.json: two files (a.py + b.py) the implementer wrote.
    (run_dir / "acp-diffs.json").write_text(
        json.dumps(
            [
                {"path": str(repo / "a.py"), "old_text": "", "new_text": "def a(): return 1\n"},
                {"path": str(repo / "b.py"), "old_text": "", "new_text": "def b(): return 2\n"},
            ]
        )
    )

    # Verifier JSON + log.
    log_path = run_dir / "verifier_verifier_node.log"
    (run_dir / "verifier_verifier_node.json").write_text(
        json.dumps(
            {
                "verifier": "verifier_node",
                "pass": True,
                "post_rc": 0,
                "evidence_path": str(log_path),
                "error_summary": "",
            }
        )
    )
    log_path.write_text("[verifier] running\n[ok] verifier pass\n")
    # Fix #2 — recipe verifier stem-based artefacts. ``node.prompt`` ends
    # in ``verifiers/test.py`` → stem ``test``. The reader tries the stem
    # first, then the node id.
    (run_dir / "verifier_test.json").write_text(
        json.dumps(
            {
                "verifier": "test",
                "pass": True,
                "checks": [
                    {"name": "lint", "rc": 0, "log": "verifier_verifier_node.log"},
                    {"name": "compile", "rc": 0, "log": "verifier_verifier_node.log"},
                ],
            }
        )
    )
    (run_dir / "evidence").mkdir(exist_ok=True)
    (run_dir / "evidence" / "test.log").write_text(
        "[lint] running\n[lint] ok\n[compile] running\n[compile] ok\n"
    )

    # Review JSON: needs_revision + 2 findings with file:line.
    (run_dir / "review-reviewer.json").write_text(
        json.dumps(
            {
                "verdict": "needs_revision",
                "notes": [
                    {"title": "issue one", "file": str(repo / "a.py"), "line": 1, "severity": "high"},
                    {"title": "issue two", "file": str(repo / "b.py"), "line": 1, "severity": "medium"},
                ],
            }
        )
    )

    return run_dir


# ── per-node assertions (kickoff §"Tests") ─────────────────────────────────


def test_changes_view_planner_has_plan_items_no_files(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "planner", view="changes")
    assert out["ok"] is True
    assert out["view"] == "changes"
    items = out["result"]["items"]
    # Objective line plus the two step rows.
    titles = [str(it.get("t") or "") for it in items]
    assert any("demo planner objective" in t for t in titles)
    assert any("implementer" in t for t in titles)
    assert any("verifier_node" in t for t in titles)
    # Planner runs first → no code changes yet.
    assert out["files"] == []
    assert "No code changed" in out["diff_note"]


def test_changes_view_implementer_has_files_diff_commits(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "implementer", view="changes")
    assert out["ok"] is True
    assert len(out["files"]) == 2
    paths = {f["path"] for f in out["files"]}
    assert "wt/a.py" in paths
    assert "wt/b.py" in paths
    # Each file has an absolute path (it exists in the seeded worktree).
    for f in out["files"]:
        assert f["abs"] is not None and Path(f["abs"]).is_file()
    # The diff text is non-empty and contains both file paths.
    assert out["diff"]
    assert "a.py" in out["diff"]
    assert "b.py" in out["diff"]
    assert out["diff_note"] == ""
    # Commits: the two extra commits on the run branch.
    assert len(out["commits"]) == 2
    subjects = [c["subject"] for c in out["commits"]]
    assert "add a" in subjects
    assert "add b" in subjects
    # Each commit carries a sha, when, author, and at least one file entry.
    for c in out["commits"]:
        assert len(c["sha"]) == 40
        assert c["when"]
        assert c["author"]
        assert c["files"]


def test_changes_view_verifier_has_checks_with_log_paths(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "verifier_node", view="changes")
    items = out["result"]["items"]
    # Fix #2 contract: per-check rows (no leading verdict row). Each check
    # has ``t`` = check name, ``sub`` = "rc <n> · <log name>" and
    # ``path`` = the absolute log path.
    cids = [str(it.get("t") or "") for it in items]
    assert "lint" in cids
    assert "compile" in cids
    # Each item's ``path`` points at an existing log file (either the
    # run-dir log or ``evidence/<stem>*.log``).
    paths_blob = json.dumps([it.get("path") for it in items])
    assert "verifier_verifier_node.log" in paths_blob or "evidence/test.log" in paths_blob
    for it in items:
        if it.get("path"):
            assert Path(it["path"]).is_file(), it["path"]
        # ``sub`` carries "rc <n>" + the log name.
        sub = it.get("sub") or ""
        assert "rc 0" in sub, sub
    # No leading verdict row.
    assert not str(items[0].get("t") or "").startswith("pass")


def test_changes_view_reviewer_verdict_first_then_findings_with_paths(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "reviewer", view="changes")
    items = out["result"]["items"]
    # Verdict row first.
    assert "needs_revision" in str(items[0].get("t") or "")
    # Then the two findings.
    titles = [str(items[1].get("t") or ""), str(items[2].get("t") or "")]
    assert any("issue one" in t for t in titles)
    assert any("issue two" in t for t in titles)
    # Each finding has ``file:line`` in `sub`.
    subs = [items[1].get("sub") or "", items[2].get("sub") or ""]
    assert any("a.py:1" in s for s in subs)
    assert any("b.py:1" in s for s in subs)
    # Each finding has an Open action pointing at the absolute file path.
    for it in items[1:3]:
        acts_blob = json.dumps(it.get("acts") or [])
        assert "/a.py" in acts_blob or "/b.py" in acts_blob


def test_changes_view_publisher_has_commits(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "publisher", view="changes")
    assert len(out["commits"]) == 2
    assert out["commits_note"] == ""


# ── error / CLI path ───────────────────────────────────────────────────────


def test_changes_view_unknown_node_returns_ok_false(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "no-such-node", view="changes")
    assert out["ok"] is False
    assert "no node no-such-node" in out["error"]


def test_changes_view_runs_via_cli(home: Path, capsys) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    rc = board_cmd.main(["node", RUN, "implementer", "--view", "changes", "--home", str(home)], "")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["view"] == "changes"
    assert len(out["files"]) == 2
    assert len(out["commits"]) == 2


def test_changes_view_diff_capped_when_run_diff_huge(home: Path) -> None:
    """Synthetic diff > 300 KB → ``diff_note`` flags the cap; ``diff`` ≤ cap."""
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    run_dir_path = _seed(home, repo=repo, base_sha=base_sha)
    # Stuff a > 300 KB single file into acp-diffs.json.
    huge = "x\n" * 200_000  # ~400 KB of single-line content
    (run_dir_path / "acp-diffs.json").write_text(
        json.dumps(
            [
                {"path": str(repo / "a.py"), "old_text": "", "new_text": huge},
            ]
        )
    )
    out = build_node(home, RUN, "implementer", view="changes")
    assert len(out["diff"]) <= 300_000
    # diff_note flags the cap when the cap fires.
    if len(out["diff"]) == 300_000:
        assert "capped" in out["diff_note"].lower() or "300" in out["diff_note"]


def test_changes_view_no_workspace_records_commits_note(home: Path) -> None:
    """No workspace + no publish/verdict/run-verdict commit → commits_note."""
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    # Delete the workspace record so the git-log path falls through.
    (home / "worktrees" / f"{RUN}.json").unlink()
    # Reload by deleting the in-process cache would require re-importing
    # _load; the Run was loaded in the seed step but no test in this file
    # shares a Run object across cases (each test is fresh). The fixture
    # recreates everything; we just delete the workspace record before
    # calling build_node so the view reads it as missing.
    out = build_node(home, RUN, "publisher", view="changes")
    assert out["commits"] == []
    assert "No commits" in out["commits_note"]


# ── kickoff r2 (ide-node-changes-r2) — fail-before / pass-after evidence ──


def test_changes_view_planner_with_start_none_gets_no_diff(home: Path) -> None:
    """Fix #1: a planner with ``start=None`` returns no diff even when a
    later code-changing node has one.

    The kickoff's `_show_diff_for` switches to a workflow-order index
    check; the planner (always index 0) is ``False`` and the implementer
    (later index) is ``True``. This fails on ``4ff6a5fe`` (the old
    start-time heuristic returned ``True`` defensively when either side
    was missing) and passes once the index check lands.

    Fixture: planner has no ``node_start`` event in the events table; the
    loader leaves ``node.start = None``. With the OLD code the missing
    start trips the defensive ``else`` branch (``return True``) and the
    planner gets the implementer's diff.
    """
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    # Delete the planner's node_start event so its loader-resolved
    # ``start`` is ``None``.
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "DELETE FROM run_events WHERE run_id = ? AND event_type = 'node_start' "
        "AND json_extract(payload_json, '$.node_id') = 'planner'",
        (RUN,))
    con.commit(); con.close()

    # Wipe the in-process fleet cache so ``fleet.run_card`` rebuilds from
    # the cleared events table.
    from mini_ork.acp import fleet as _fleet
    cache = getattr(_fleet, "_RUN_CARD_CACHE", None)
    if isinstance(cache, dict):
        cache.clear()

    # Sanity: the loaded planner has no start time.
    from mini_ork.ide_pages.run import _load as _load_run
    run_obj = _load_run(home, RUN)
    assert run_obj is not None, "loader returned None"
    planner = next(n for n in run_obj.nodes if n.id == "planner")
    assert planner.start is None, (
        f"loader did not honour missing node_start; got {planner.start}")

    planner_out = build_node(home, RUN, "planner", view="changes")
    assert planner_out["files"] == [], planner_out["files"]
    assert "No code changed" in planner_out["diff_note"], planner_out["diff_note"]

    impl_out = build_node(home, RUN, "implementer", view="changes")
    assert len(impl_out["files"]) == 2, impl_out["files"]


def test_changes_view_lens_node_reads_report(home: Path) -> None:
    """Fix #3: a ``researcher`` node whose id ends in ``_lens`` is treated
    as a review node, and its ``lens-code_impact.md`` is read via
    ``Node._report_paths`` (lazy-imported, strips ``_lens``).

    Before the fix, the lens researcher fell through to ``_other_node_items``
    (filename match only) and missed the ``lens-<id>.md`` content.
    """
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    run_dir = home / "runs" / RUN
    # Lens researcher node: id ``code_impact_lens``, type ``researcher``.
    con = sqlite3.connect(home / "state.db")
    for kind, ts in (("node_start", T0 + 5), ("node_end", T0 + 12)):
        payload = {"node_id": "code_impact_lens", "node_type": "researcher",
                   "model_lane": "minimax_lens"}
        if kind == "node_end":
            payload["finish_reason"] = "done"
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"ev-cil-{kind}", RUN, kind, json.dumps(payload), ts))
    con.commit(); con.close()
    # Lens report — must be ``lens-code_impact.md`` (not ``lens-code_impact_lens.md``).
    (run_dir / "lens-code_impact.md").write_text(
        "# Code impact\n\n## Findings\n\n- finding A\n- finding B\n"
    )

    out = build_node(home, RUN, "code_impact_lens", view="changes")
    items = out["result"]["items"]
    # Heading rows surface from ``_items_from_markdown``.
    titles = [str(it.get("t") or "") for it in items]
    assert any("Code impact" in t for t in titles), items
    # The lens researcher has NO ``review-<id>.json``, so the kickoff's
    # logic must read the markdown — not fall through to "No review
    # artefacts found".
    assert not any("No review artefacts" in (it.get("t") or "") for it in items), items


def test_changes_view_patch_only_returns_patch_text_and_counts(home: Path) -> None:
    """Fix #4: when only ``framework-edit.diff`` (or ``review-diff.patch``)
    is present (no acp diffs, no workspace git), the view shows the
    patch text and per-file ``+``/``-`` counts from the hunks.

    The existing ``_parse_unified_diff_into_entries`` returned only the
    file paths and skipped line counts; the new parser stores them so
    ``files[*].added``/``.removed`` reflect the hunks.
    """
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    run_dir = home / "runs" / RUN
    # Remove acp-diffs.json and the workspace record so only the patch
    # file remains.
    (run_dir / "acp-diffs.json").unlink()
    # Write a small patch with known ``+``/``-`` line counts.
    patch_text = (
        "diff --git a/foo.py b/foo.py\n"
        "index 0000..1111 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+line one\n"
        "+line two\n"
        "+line three\n"
        "@@ -1,1 +1,0 @@\n"
        "-old line\n"
    )
    (run_dir / "framework-edit.diff").write_text(patch_text)

    out = build_node(home, RUN, "implementer", view="changes")
    files = out["files"]
    assert len(files) == 1, files
    f = files[0]
    assert f["path"].endswith("foo.py"), f
    assert f["added"] == 3, f
    assert f["removed"] == 1, f
    # The diff text is the patch itself (capped), not a re-derived
    # ``unified_diff`` from empty old/new text.
    assert "+line one" in out["diff"]
    assert "-old line" in out["diff"]
    assert out["diff_note"] == "" or "capped" in out["diff_note"]


def test_changes_view_cached_or_computed_called_once(home: Path) -> None:
    """Fix #5: ``cached_or_computed`` is invoked once per
    ``build_changes_view`` (the changes view threads the entries through).

    Before the fix, both ``_files_and_note`` and ``_diff_text`` called it
    independently, so a single build dropped two cache reads.
    """
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    from mini_ork.acp import diffs as _diffs

    original = _diffs.cached_or_computed
    calls = {"n": 0}

    def spy(run_dir):
        calls["n"] += 1
        return original(run_dir)

    _diffs.cached_or_computed = spy
    try:
        out = build_node(home, RUN, "implementer", view="changes")
        assert out["ok"] is True
        assert calls["n"] == 1, (
            f"expected 1 cached_or_computed call per build, got {calls['n']}")
    finally:
        _diffs.cached_or_computed = original


def test_changes_view_finding_path_resolves_against_project_root(home: Path,
                                                                 monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    """Fix #6: reviewer finding with a repo-relative ``file`` resolves to
    an absolute path against the project root, not the process cwd.

    The kickoff's note: ``Path(file_path).is_file()`` against process
    cwd was the bug; ``_resolve_finding_path`` now joins with the
    project root and (when present) the workspace path.
    """
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    run_dir = home / "runs" / RUN
    # Inject a finding with a REPO-RELATIVE path (``wt/a.py`` — exists
    # in the seeded worktree, NOT in the test runner's cwd).
    (run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "needs_revision",
        "notes": [
            {"title": "issue one",
             "file": "wt/a.py",
             "line": 1,
             "severity": "high"},
        ],
    }))

    # Run from a different cwd so a stale ``Path(...).is_file()`` against
    # cwd would have been False.
    other = tmp_path / "elsewhere"
    other.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(other)

    out = build_node(home, RUN, "reviewer", view="changes")
    items = out["result"]["items"]
    # The finding row carries ``path`` and an Open act pointing at the
    # absolute file (the project-root-joined ``wt/a.py``).
    finding = next((it for it in items if "issue one" in (it.get("t") or "")), None)
    assert finding is not None, items
    assert finding.get("path"), finding
    assert Path(finding["path"]).is_file(), finding["path"]
    # The Open act is the absolute path.
    acts_blob = json.dumps(finding.get("acts") or [])
    assert finding["path"] in acts_blob


# ── kickoff r3 (ide-node-changes-r3) — six fixes ──────────────────────


def _seed_static_check(run_dir: Path) -> Path:
    """Seed the ``static_check_verifier`` artefacts (kickoff r3 §3).

    Writes ``verifier_static-check.json`` with a leading
    ``DeprecationWarning`` line, ``verifier-static-check.checks.tsv``
    (mirroring the real recipe columns ``cid\\tdesc\\tpassed``) and
    ``evidence/static-check.log``. Returns the log path.
    """
    log_path = run_dir / "evidence" / "static-check.log"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "evidence").mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "[static-check] running\n[static-check] [lint] running\n"
        "[static-check] [lint] ok\n[static-check] [artifact] running\n"
        "[static-check] [artifact] ok\n"
    )
    json_text = json.dumps(
        {
            "verifier": "static-check",
            "pass": False,
            "checks": [
                {"name": "lint", "expected": "ok", "actual": "ok", "pass": True},
                {"name": "artifact-diff-exists", "expected": "exists", "actual": "missing", "pass": False},
            ],
        }
    )
    # Add a leading DeprecationWarning line so fix #2 is exercised.
    (run_dir / "verifier_static-check.json").write_text(
        "DeprecationWarning: pkg_resources is deprecated\n" + json_text
    )
    # TSV columns match the real recipe: cid \t desc \t passed (no rc).
    (run_dir / "verifier-static-check.checks.tsv").write_text(
        "lint\tstatic-check: lint ok\ttrue\n"
        "artifact-diff-exists\tframework-edit.diff exists\tfalse\n"
    )
    return log_path


def test_changes_view_executor_shape_verdict_row_and_log_tail(home: Path) -> None:
    """Fix #1: executor-shape verifier JSON (``pass`` / ``post_rc`` /
    ``error_summary``) renders a red row + log tail, never "No verifier
    artefacts found".

    Before the fix, ``_verifier_items`` only knew the recipe ``checks[]``
    shape and the TSV. A flat executor verdict was silently dropped. The
    new executor-shape branch emits one row whose ``t`` is
    ``error_summary``, ``sub`` carries the rc + log basename, and
    ``log_tail`` carries the last lines of the evidence log.
    """
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    run_dir = home / "runs" / RUN
    # Wipe any seed-time static-check artefacts so this fixture owns the row.
    for rel in ("verifier_static-check.json", "verifier-static-check.checks.tsv"):
        p = run_dir / rel
        if p.exists():
            p.unlink()
    log = run_dir / "verifier_static-check.log"
    log.write_text("[verifier] [pytest] running\n[pytest] FAILED\n")
    (run_dir / "verifier_static-check.json").write_text(
        json.dumps({"pass": False, "post_rc": 2, "error_summary": "pytest failed",
                    "evidence_path": str(log)})
    )

    out = build_node(home, RUN, "static_check_verifier", view="changes")
    items = out["result"]["items"]
    # Exactly one verdict row (no leading "No verifier artefacts found").
    assert len(items) == 1, items
    row = items[0]
    assert row["t"] == "pytest failed", row
    assert "rc 2" in row["sub"], row["sub"]
    assert "verifier_static-check.log" in row["sub"], row["sub"]
    assert row["mc"] == "red", row
    assert row["passed"] is False
    # log_tail carries the file tail.
    assert "FAILED" in (row.get("log_tail") or ""), row.get("log_tail")
    # The absolute log path is wired up.
    assert row.get("path"), row
    assert Path(row["path"]).is_file(), row["path"]


def test_changes_view_tolerant_json_parses_with_deprecation_warning(home: Path) -> None:
    """Fix #2: ``verifier_<stem>.json`` with a leading
    ``DeprecationWarning`` line still parses.

    Before the fix, raw ``json.loads`` raised and the reader returned
    ``None`` — falling through to "No verifier artefacts found". The new
    reader finds the first ``{`` and parses from there to EOF.
    """
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    _seed_static_check(home / "runs" / RUN)

    out = build_node(home, RUN, "static_check_verifier", view="changes")
    items = out["result"]["items"]
    # Two checks (lint, artifact-diff-exists) — JSON parsed despite the
    # leading DeprecationWarning.
    titles = [str(it.get("t") or "") for it in items]
    assert "lint" in titles, titles
    assert "artifact-diff-exists" in titles, titles
    # No silent "No verifier artefacts found".
    assert not any("No verifier artefacts" in t for t in titles), titles


def test_changes_view_static_check_fixture_rows_have_sub_and_path(home: Path) -> None:
    """Fix #3: TSV/JSON check rows carry ``sub = rc <n> · <log name>``
    and ``path`` = absolute log path.

    Fixture mirrors the kickoff's recipe shape: JSON with a leading
    ``DeprecationWarning`` line (parsed), TSV with recipe columns
    ``cid/desc/passed`` (no rc), and ``evidence/<stem>.log``. Asserts
    names, sub carries rc + log name, and ``path`` points at the
    evidence log.
    """
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    run_dir = home / "runs" / RUN
    log_path = _seed_static_check(run_dir)

    out = build_node(home, RUN, "static_check_verifier", view="changes")
    items = out["result"]["items"]
    by_name = {it["t"]: it for it in items}
    assert "lint" in by_name, items
    assert "artifact-diff-exists" in by_name, items
    # Real checks[] name no log: every row points at the verifier's own
    # evidence/<stem>.log for both sub and path.
    for name, passed in (("lint", True), ("artifact-diff-exists", False)):
        row = by_name[name]
        assert row.get("sub") == "static-check.log", row
        assert Path(row["path"]).resolve() == log_path.resolve(), row["path"]
        assert row["passed"] is passed, row


def test_check_rows_fall_back_to_the_newest_timestamped_evidence_log(home: Path) -> None:
    """No ``evidence/<stem>.log`` → the newest ``evidence/<stem>-*.log``."""
    import os

    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    run_dir = home / "runs" / RUN
    _seed_static_check(run_dir).unlink()
    old = run_dir / "evidence" / "static-check-100-1-aaa.log"
    new = run_dir / "evidence" / "static-check-200-1-bbb.log"
    old.write_text("old\n")
    new.write_text("new\n")
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))

    out = build_node(home, RUN, "static_check_verifier", view="changes")
    rows = [it for it in out["result"]["items"] if it.get("t") == "lint"]
    assert rows and rows[0]["sub"] == new.name, rows
    assert Path(rows[0]["path"]).resolve() == new.resolve()


def test_read_json_skips_a_warning_line_that_contains_a_brace(tmp_path: Path) -> None:
    from mini_ork.ide_pages.node_changes import _read_json

    p = tmp_path / "v.json"
    p.write_text('Warning: dict {x} is deprecated\n{"pass": true, "checks": []}\n')
    assert _read_json(p) == {"pass": True, "checks": []}


def test_a_large_complete_review_patch_is_kept(tmp_path: Path) -> None:
    """Size alone is not truncation: a complete 60 KB review-diff.patch wins."""
    from mini_ork.ide_pages.node_changes import _select_patch_text

    body = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n" + ("+x\n" * 20_000)
    (tmp_path / "review-diff.patch").write_text(body)
    (tmp_path / "framework-edit.diff").write_text(body + "+y\n")
    text, name = _select_patch_text(tmp_path)
    assert name == "review-diff.patch" and text == body


def test_a_mid_hunk_review_patch_yields_to_the_longer_framework_diff(tmp_path: Path) -> None:
    from mini_ork.ide_pages.node_changes import _select_patch_text

    full = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n+a\n+b\n"
    (tmp_path / "review-diff.patch").write_text(full[:-2])  # cut inside the "+b" line, no trailing newline
    (tmp_path / "framework-edit.diff").write_text(full)
    text, name = _select_patch_text(tmp_path)
    assert name == "framework-edit.diff" and text == full


def test_changes_view_git_fallback_uses_correct_record(home: Path,
                                                       tmp_path: Path) -> None:
    """Fix #4: the git fallback reads ``run.workspace`` (loaded from
    ``<home>/worktrees/<run_id>.json``) and ignores every other record
    in ``<home>/worktrees/``.

    Before the fix, the fallback sorted ``<run_dir>.parent / "worktrees"``
    and took the first match — wrong directory, wrong record, silent
    empty result. The new code passes ``run`` and reads ``run.workspace``
    directly, so a second (bogus) record for a different run_id is
    ignored.
    """
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    run_dir = home / "runs" / RUN

    # Drop acp-diffs.json and both patch files so the git fallback is
    # the only source left.
    for rel in ("acp-diffs.json", "review-diff.patch", "framework-edit.diff"):
        p = run_dir / rel
        if p.exists():
            p.unlink()

    # Plant a bogus second record under a name that would sort first if
    # the OLD code enumerated ``home / "worktrees"``. Its fields point
    # at a non-existent path so the OLD code would return [] (or fail).
    bogus = home / "worktrees" / "AAAAA-other-run.json"
    bogus.write_text(json.dumps({
        "run_id": "other-run",
        "path": str(tmp_path / "does-not-exist"),
        "branch": "wt/test",
        "base_branch": "main",
        "base_sha": "deadbeef" * 5,
        "project": str(tmp_path),
        "adopted": False,
        "clean_at_start": True,
        "home": str(home),
    }))
    # The current run's record is the real one — left in place by _seed.

    out = build_node(home, RUN, "implementer", view="changes")
    files = out["files"]
    # The fallback produced the actual repo's commits (a.py, b.py), NOT
    # the bogus second record (which would have returned []). Paths
    # come through ``_project_file`` (the longest tail that exists in the
    # main checkout); the diff text is the raw ``git -C <ws> diff``
    # output — paths relative to the worktree (``<project>/wt``).
    paths = {f["path"] for f in files}
    assert len(files) == 2, paths
    assert any(p.endswith("a.py") for p in paths), paths
    assert any(p.endswith("b.py") for p in paths), paths
    # The diff text contains the git diff hunks for a.py / b.py.
    assert out["diff"]
    assert "a/a.py b/a.py" in out["diff"], out["diff"]
    assert "a/b.py b/b.py" in out["diff"], out["diff"]


def test_changes_view_one_diff_text_with_both_patch_files(home: Path) -> None:
    """Fix #5: when both ``review-diff.patch`` and ``framework-edit.diff``
    are present, the diff text holds exactly ONE ``diff --git`` per
    file — never concatenated.

    Before the fix, ``_read_patch_text`` concatenated both files so the
    view duplicated every ``diff --git`` (2 per file when both exist).
    The new code chooses one file (preferring ``review-diff.patch`` when
    it isn't truncated) and returns its text intact.
    """
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    run_dir = home / "runs" / RUN
    # Drop acp-diffs.json so the patch source wins.
    (run_dir / "acp-diffs.json").unlink()

    review_text = (
        "diff --git a/foo.py b/foo.py\n"
        "index 0000..1111 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+line one\n"
        "+line two\n"
    )  # ends with newline → NOT truncated → review-diff.patch wins.
    framework_text = (
        "diff --git a/bar.py b/bar.py\n"
        "index 0000..2222 100644\n"
        "--- a/bar.py\n"
        "+++ b/bar.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+other line\n"
    )
    (run_dir / "review-diff.patch").write_text(review_text)
    (run_dir / "framework-edit.diff").write_text(framework_text)

    out = build_node(home, RUN, "implementer", view="changes")
    # Exactly one ``diff --git`` per file (foo.py only — review won,
    # framework's bar.py is NOT concatenated).
    n_foo = out["diff"].count("foo.py b/")
    n_bar = out["diff"].count("bar.py b/")
    assert n_foo == 1, out["diff"]
    assert n_bar == 0, out["diff"]
    # The text matches the chosen source byte-for-byte.
    assert "+line one" in out["diff"]
    assert "+line two" in out["diff"]
    # The files[] counts reflect the chosen source (added=2, removed=0).
    foo_row = next(f for f in out["files"] if f["path"].endswith("foo.py"))
    assert foo_row["added"] == 2
    assert foo_row["removed"] == 0


def test_a_new_file_in_another_worktree_opens_where_it_is(tmp_path: Path) -> None:
    """A file the project lacks (new in a live worktree) keeps its real path."""
    from mini_ork.ide_pages.node_changes import _project_file_lazy

    project = tmp_path / "project"
    project.mkdir()
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    new_file = worktree / "pkg" / "new_mod.py"
    new_file.parent.mkdir()
    new_file.write_text("x = 1\n")

    display, absolute = _project_file_lazy(str(new_file), project)
    assert display == "pkg/new_mod.py"
    assert absolute == str(new_file)
