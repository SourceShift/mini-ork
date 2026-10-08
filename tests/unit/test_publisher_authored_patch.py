"""Authored-patch contract tests (kickoff `auto/publisher-authored-patch.md`).

An in-place publish must commit ONLY the run's authored change: never a peer's
in-window hunk in the same file, never a whole-file `git add`, and never through
the shared `.git/index`. Every test drives the real resolver/lander against a
throwaway git repo plus a synthetic run dir.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mini_ork.cli.publisher_authored_patch import (
    Abstain,
    AuthoredPatch,
    foreign_hunks,
    land_patch,
    report_foreign_hunks,
    resolve_authored_patch,
)

GIT_IDENTITY = ("-c", "user.name=mo-test", "-c", "user.email=mo-test@example.invalid")


def _git(repo: Path, *args: str, check: bool = True, input_text: str | None = None):
    """Run git in a throwaway repo with the index/dir env sanitized.

    `GIT_INDEX_FILE`/`GIT_DIR`/`GIT_WORK_TREE` are stripped so an ambient lane
    value can never make a test assertion read a different tree than the one the
    module under test used.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")}
    return subprocess.run(
        ["git", *GIT_IDENTITY, *args], cwd=repo, env=env,
        capture_output=True, text=True, check=check, timeout=60, input=input_text,
    )


BASE_A = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"


def _patch_a_line2() -> str:
    """A carry patch changing a.py line2 -> line2-run."""
    return (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,5 +1,5 @@\n"
        " line1\n"
        "-line2\n"
        "+line2-run\n"
        " line3\n"
        " line4\n"
        " line5\n"
    )


def _patch_line1() -> str:
    """A carry patch changing a.py line1 -> line1-salvage.

    Deliberately a DIFFERENT line from :func:`_patch_a_line2`, so a test can
    tell whether a composed patch came from the carry, the transcript replay, or
    both.
    """
    return (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-line1\n"
        "+line1-salvage\n"
        " line2\n"
        " line3\n"
    )


def _iso(epoch: float) -> str:
    """An SDK-transcript ``timestamp`` (ISO-8601, Z) for ``epoch``."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _stamp_sessions(run_dir: Path, epoch: float) -> None:
    """Force the session transcripts' mtime so ``_edits_since`` admits them.

    ``_edits_since`` skips a session file whose mtime is not after the attempt
    start, so a transcript written "now" for an attempt timestamped later needs
    its mtime moved forward too.
    """
    for path in (run_dir / "sessions").glob("*.jsonl"):
        os.utime(path, (epoch, epoch))


def _seed_node_start(run_dir: Path, started_at: float) -> None:
    """One implementer ``node_start`` in the run's ``state.db`` at ``started_at``.

    ``_attempt_started`` reads the LATEST implementer ``node_start`` from the
    run's ``run_events``; the run dir is ``<home>/runs/<id>``.
    """
    home = run_dir.parent.parent
    run_id = run_dir.name
    con = sqlite3.connect(home / "state.db")
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS run_events (event_id TEXT PRIMARY KEY, "
            "run_id TEXT, event_type TEXT, payload_json TEXT, created_at INTEGER)")
        con.execute(
            "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"evt-{run_id}-{int(started_at)}", run_id, "node_start",
             json.dumps({"node_id": "implementer", "node_type": "implementer"}),
             int(started_at)),
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "a.py").write_text(BASE_A)
    (path / "b.py").write_text(BASE_A)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "base")
    return path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _make_run_dir(tmp_path: Path, repo: Path, base_ref: str | None, *,
                  scope=None, carry=None, carry_applied=True, rolled_back=False,
                  edits=None, records=None, impl_log=None, summary=None,
                  node_start=None) -> Path:
    run_dir = tmp_path / "home" / "runs" / "run"
    run_dir.mkdir(parents=True)
    if base_ref is not None:
        (run_dir / "pre-implementer-ref").write_text(base_ref + "\n")
    profile = {"roots": {"target": str(repo)}}
    if scope is not None:
        profile["scope_allow"] = [f"`{p}`" for p in scope]
    (run_dir / "run_profile.json").write_text(json.dumps(profile))
    if carry is not None:
        (run_dir / "salvage.patch").write_text(carry)
        if carry_applied:
            # A carry patch is a source only once the restore PROVED it landed
            # (``carry-applied.json``); a bare ``salvage.patch`` is not.
            (run_dir / "carry-applied.json").write_text(json.dumps({
                "patch": "salvage.patch",
                "sha256": hashlib.sha256(carry.encode("utf-8")).hexdigest(),
                "applied_at": 0.0,
                "target": str(repo),
            }))
    if rolled_back:
        (run_dir / "rolled-back.json").write_text(json.dumps({"at": 0}))
    if node_start is not None:
        _seed_node_start(run_dir, node_start)
    if edits is not None or records is not None:
        (run_dir / "sessions").mkdir()
        lines = []
        if edits is not None:
            lines.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": name, "input": payload}
                for name, payload in edits]}})
        if records is not None:
            lines.extend(records)
        (run_dir / "sessions" / "impl.jsonl").write_text(
            "\n".join(json.dumps(rec) for rec in lines) + "\n")
    if impl_log is not None:
        (run_dir / f"impl-{impl_log[0]}.log").write_text(impl_log[1])
    if summary is not None:
        (run_dir / "implementer-summary.json").write_text(json.dumps(summary))
    return run_dir


def _changed_lines(patch_text: str) -> list[str]:
    """The ``+``/``-`` payload lines of a patch, in order (headers excluded)."""
    out: list[str] = []
    in_hunk = False
    for line in patch_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and line[:1] in ("+", "-"):
            out.append(line)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# cross-check (pure)
# ─────────────────────────────────────────────────────────────────────────────


def test_foreign_hunks_are_the_ones_not_in_the_authored_patch():
    authored = _patch_a_line2()
    tree = authored + (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -7,4 +7,4 @@\n"
        " line7\n"
        "-line8\n"
        "+line8-peer\n"
        " line9\n"
        " line10\n"
    )
    assert foreign_hunks(tree, authored) == ["a.py: -line8", "a.py: +line8-peer"]
    assert foreign_hunks(authored, authored) == []


# ─────────────────────────────────────────────────────────────────────────────
# carry patch (a recover)
# ─────────────────────────────────────────────────────────────────────────────


def test_commits_only_the_run_hunk_and_leaves_the_foreign_hunk(tmp_path, repo):
    base = _head(repo)
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"], carry=_patch_a_line2())
    # The run's own edit is in the tree, and a peer's in-window edit lands in a
    # DIFFERENT hunk of the same file.
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run").replace("line9", "line9-peer"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert authored.source == "carry-patch:salvage.patch"

    # A fresh tree delta that is NOT the authored patch must be left alone.
    peer_bytes = (repo / "a.py").read_bytes()
    foreign = report_foreign_hunks(str(repo), str(run_dir), authored.patch_text)

    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha

    committed = _git(repo, "show", f"{sha}:a.py").stdout
    assert "line2-run" in committed
    assert "line9-peer" not in committed  # the foreign hunk was never committed
    assert foreign == ["a.py: -line9", "a.py: +line9-peer"]
    listed = (run_dir / "publish-foreign-hunks.txt").read_text()
    assert listed.splitlines() == ["a.py: -line9", "a.py: +line9-peer"]

    # The working tree is left exactly as it was: relative to the publish, only
    # the peer's line differs, and it is still uncommitted.
    assert (repo / "a.py").read_bytes() == peer_bytes
    vs_head = _git(repo, "diff", "HEAD", "--", "a.py").stdout
    assert "-line9" in vs_head and "+line9-peer" in vs_head
    assert "line2-run" not in vs_head  # the run's own change IS committed


def test_out_of_scope_section_and_foreign_file_are_not_committed(tmp_path, repo):
    base = _head(repo)
    carry = _patch_a_line2() + (
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1,5 +1,5 @@\n"
        " line1\n"
        "-line2\n"
        "+line2-run\n"
        " line3\n"
        " line4\n"
        " line5\n"
    )
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"], carry=carry)
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run"))
    (repo / "b.py").write_text(BASE_A.replace("line9", "line9-peer"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert "b.py" not in authored.patch_text  # scope dropped the out-of-scope section

    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha
    assert _git(repo, "show", "--name-only", "--format=", sha).stdout.split() == ["a.py"]
    # b.py's edit is untouched and still uncommitted.
    vs_head = _git(repo, "diff", "HEAD", "--", "b.py").stdout
    assert "-line9" in vs_head and "+line9-peer" in vs_head


def test_commit_is_parented_on_a_head_that_moved(tmp_path, repo):
    base = _head(repo)
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"], carry=_patch_a_line2())

    # An unrelated commit lands between the base and the publish.
    (repo / "b.py").write_text(BASE_A.replace("line1", "line1-unrelated"))
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-m", "unrelated")
    moved = _head(repo)

    authored = resolve_authored_patch(str(run_dir))
    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha

    # The publish is parented on the new HEAD, and the unrelated commit survives.
    assert _git(repo, "rev-parse", f"{sha}^").stdout.strip() == moved
    assert _git(repo, "merge-base", "--is-ancestor", moved, sha).returncode == 0
    assert "line1-unrelated" in _git(repo, "show", f"{sha}:b.py").stdout
    assert "line2-run" in _git(repo, "show", f"{sha}:a.py").stdout
    assert _head(repo) == sha


def test_conflicting_head_move_abstains_publish_conflict(tmp_path, repo):
    base = _head(repo)
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"], carry=_patch_a_line2())

    # HEAD moves in a way that collides with the patch's own hunk.
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-someone-else"))
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "conflicting move")
    moved = _head(repo)

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    landed = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(landed, Abstain), landed
    assert landed.reason == "publish-conflict"
    assert _head(repo) == moved  # HEAD unchanged, nothing committed


# ─────────────────────────────────────────────────────────────────────────────
# transcript replay (a fresh run)
# ─────────────────────────────────────────────────────────────────────────────


def test_transcript_replay_commits_the_implementers_own_edit(tmp_path, repo):
    base = _head(repo)
    run_dir = _make_run_dir(
        tmp_path, repo, base, scope=["a.py"],
        edits=[("Edit", {"file_path": str(repo / "a.py"),
                         "old_string": "line2\n", "new_string": "line2-run\n"})],
    )
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert authored.source == "transcript-replay"

    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha
    assert "line2-run" in _git(repo, "show", f"{sha}:a.py").stdout


def test_transcript_replay_mismatch_abstains_unattributable(tmp_path, repo):
    base = _head(repo)
    run_dir = _make_run_dir(
        tmp_path, repo, base, scope=["a.py"],
        edits=[("Write", {"file_path": str(repo / "a.py"),
                          "content": BASE_A.replace("line2", "line2-run")})],
    )
    # A peer edited a different hunk after the implementer's Write.
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run").replace("line9", "line9-peer"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, Abstain), authored
    assert authored.reason == "publish-unattributable"
    assert "a.py" in authored.detail


def test_run_dir_artifacts_are_never_replayed_into_the_patch(tmp_path, repo):
    """A run dir inside the target repo is gitignored; `git apply --cached` is not.

    The implementer's own Write of its log artifact under ``run_dir`` is not a
    change it authored in the repo. Replaying it would commit the gitignored run
    artifact — or, when the runtime later rewrites the log, abstain
    ``publish-unattributable`` and blame a peer. It must be dropped.
    """
    base = _head(repo)
    run_dir = repo / ".mini-ork" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (repo / ".gitignore").write_text(".mini-ork/\n")
    (run_dir / "pre-implementer-ref").write_text(base + "\n")
    (run_dir / "run_profile.json").write_text(json.dumps({"roots": {"target": str(repo)}}))
    log_path = run_dir / "impl-implementer.log"
    log_path.write_text("working\n")
    (run_dir / "sessions").mkdir()
    record = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-1", "name": "Write", "input": {
            "file_path": str(repo / "a.py"), "content": BASE_A.replace("line2", "line2-run")}},
        {"type": "tool_use", "id": "tu-2", "name": "Write", "input": {
            "file_path": str(log_path), "content": "working\n"}},
    ]}}
    (run_dir / "sessions" / "impl.jsonl").write_text(json.dumps(record) + "\n")
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert "line2-run" in authored.patch_text
    assert "impl-implementer.log" not in authored.patch_text
    assert ".mini-ork" not in authored.patch_text


def test_failed_tool_call_is_skipped_not_blamed_on_a_peer(tmp_path, repo):
    """An Edit that came back is_error changed nothing.

    A rejected "Found 2 matches …" Edit followed by a successful retry is
    ordinary; replaying the rejected call desynchronises the replay and the
    resolver abstains publish-unattributable against an innocent peer.
    """
    base = _head(repo)
    rejected = {"type": "tool_use", "id": "tu-1", "name": "Edit", "input": {
        "file_path": str(repo / "a.py"), "old_string": "line2\n", "new_string": "line2-run\n"}}
    retried = {"type": "tool_use", "id": "tu-2", "name": "Edit", "input": {
        "file_path": str(repo / "a.py"),
        "old_string": "line2\nline3\n", "new_string": "line2-run\nline3\n"}}
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"], records=[
        {"type": "assistant", "message": {"content": [rejected]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu-1", "is_error": True,
             "content": "Found 2 matches of the string to replace — not unique."}]}},
        {"type": "assistant", "message": {"content": [retried]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu-2", "is_error": False,
             "content": "edited"}]}},
    ])
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert authored.source == "transcript-replay"
    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha
    assert "line2-run" in _git(repo, "show", f"{sha}:a.py").stdout


def test_revise_round_chains_the_earlier_session(tmp_path, repo):
    """A revise round dispatches a FRESH session.

    Its edits sit on top of the previous round's, which the run's base does not
    hold, so replaying that session alone can never reconcile for a file an
    earlier round created. The run's sessions must replay in order.
    """
    base = _head(repo)
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"])
    sessions = run_dir / "sessions"
    sessions.mkdir()
    (run_dir / ".sessions").mkdir()
    (run_dir / ".sessions" / "implementer.session").write_text("round2")

    created = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-1", "name": "Write", "input": {
            "file_path": str(repo / "a.py"),
            "content": BASE_A.replace("line2", "line2-run")}}]}}
    revised = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-2", "name": "Edit", "input": {
            "file_path": str(repo / "a.py"),
            "old_string": "line2-run\nline3\n", "new_string": "line2-run\nline3-fix\n"}}]}}
    (sessions / "round1.jsonl").write_text(json.dumps(created) + "\n")
    (sessions / "round2.jsonl").write_text(json.dumps(revised) + "\n")
    now = time.time()
    os.utime(sessions / "round1.jsonl", (now - 10, now - 10))
    os.utime(sessions / "round2.jsonl", (now, now))

    (repo / "a.py").write_text(
        BASE_A.replace("line2", "line2-run").replace("line3", "line3-fix"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert authored.source == "transcript-replay"
    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha
    committed = _git(repo, "show", f"{sha}:a.py").stdout
    assert "line2-run" in committed and "line3-fix" in committed


def test_no_authored_source_abstains(tmp_path, repo):
    base = _head(repo)
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"])
    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, Abstain), authored
    assert authored.reason == "publish-no-authored-patch"


# ─────────────────────────────────────────────────────────────────────────────
# emitted diff (a text/codex lane) and declared files (last resort)
# ─────────────────────────────────────────────────────────────────────────────


def test_emitted_diff_in_the_implementer_log_is_an_authored_source(tmp_path, repo):
    """The diff `execute.apply_impl_output` applied IS the implementer's patch.

    A text lane prints a unified diff instead of calling Write/Edit, so the
    transcript has no edits at all; the log's own diff is what put the change on
    the tree.
    """
    base = _head(repo)
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"],
                            impl_log=("impl1", "I patched it:\n\n" + _patch_a_line2()))
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert authored.source == "emitted-diff:impl-impl1.log"

    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha
    assert "line2-run" in _git(repo, "show", f"{sha}:a.py").stdout
    assert _git(repo, "show", "--name-only", "--format=", sha).stdout.split() == ["a.py"]


def test_emitted_diff_for_an_out_of_repo_path_abstains(tmp_path, repo):
    outside = tmp_path / "outside.txt"
    outside.write_text("line1\n")
    base = _head(repo)
    escape = (
        "--- a/../outside.txt\n"
        "+++ b/../outside.txt\n"
        "@@ -1 +1,2 @@\n"
        " line1\n"
        "+line2\n"
    )
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"],
                            impl_log=("impl1", "```\n" + escape + "```\n"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, Abstain), authored
    assert authored.reason == "publish-unattributable"


def test_declared_files_fallback_commits_only_the_declared_files(tmp_path, repo):
    """LEGACY shape (no baseline): the declared files_changed, taken whole.

    A run that carries no ``pre-implementer-ref`` leaves neither a transcript edit
    nor an emitted diff, so the declared list is the only thing left to commit.
    Nothing outside the declared list (a peer's file here) is ever swept in.
    """
    run_dir = _make_run_dir(tmp_path, repo, None, scope=["a.py"],
                            summary={"files_changed": [str(repo / "a.py")]})
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run"))
    (repo / "b.py").write_text(BASE_A.replace("line9", "line9-peer"))  # a peer's edit

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert authored.source == "declared-files:implementer-summary.json"

    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha
    assert _git(repo, "show", "--name-only", "--format=", sha).stdout.split() == ["a.py"]
    assert "line9-peer" not in _git(repo, "show", f"{sha}:b.py").stdout
    assert "+line9-peer" in _git(repo, "diff", "HEAD", "--", "b.py").stdout


def test_declared_files_with_a_baseline_abstain_not_commit_the_tree_delta(tmp_path, repo):
    """With a pre-implementer-ref, the declared-files delta is a TREE delta.

    Every production implementer run writes ``pre-implementer-ref``. The declared
    list is then HEAD -> working tree, which cannot separate a peer's in-window hunk
    inside a declared file, so taking it would commit the peer (the
    ide-orca-b2b-story incident). It must abstain instead — a tree delta is not a
    source, not even as a last resort.
    """
    base = _head(repo)
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"],
                            summary={"files_changed": [str(repo / "a.py")]})
    # The implementer's own change (line2), plus a peer's in-window hunk (line9)
    # in the SAME declared file.
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run").replace("line9", "line9-peer"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, Abstain), authored
    assert authored.reason == "publish-no-authored-patch"
    assert _head(repo) == base  # nothing committed


def test_declared_files_with_a_stash_baseline_abstain(tmp_path, repo):
    """``pre-implementer-ref`` is a ``git stash create`` snapshot of the DIRTY tree.

    A declared-files diff against that snapshot is empty, so the fallback would
    abstain "nothing to commit" on exactly the runs it supposedly exists for — and
    worse, taken against HEAD it is a tree delta that carries any pre-existing or
    peer change. With a baseline present it must abstain, not commit either shape.
    """
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run"))
    stash = _git(repo, "stash", "create").stdout.strip()
    assert stash, "test needs a dirty tree to snapshot"
    run_dir = _make_run_dir(tmp_path, repo, None, scope=["a.py"],
                            summary={"files_changed": [str(repo / "a.py")]})
    (run_dir / "pre-implementer-ref").write_text(stash + "\n")

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, Abstain), authored
    assert authored.reason == "publish-no-authored-patch"


def test_transcript_evidence_blocks_the_declared_files_fallback(tmp_path, repo):
    """A reconciling-impossible transcript abstains — it never falls back.

    Regression for the whole-file sweep: once there IS authorship evidence, a
    mismatch is the peer-edit diagnosis and the fallback would commit the peer.
    """
    base = _head(repo)
    run_dir = _make_run_dir(
        tmp_path, repo, base, scope=["a.py"],
        edits=[("Write", {"file_path": str(repo / "a.py"),
                          "content": BASE_A.replace("line2", "line2-run")})],
        summary={"files_changed": [str(repo / "a.py")]},
    )
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run").replace("line9", "line9-peer"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, Abstain), authored
    assert authored.reason == "publish-unattributable"


def test_carry_patch_commit_equals_the_carry_patch_exactly(tmp_path, repo):
    """The kickoff test: a recover's commit is the carry patch, nothing else."""
    base = _head(repo)
    carry = _patch_a_line2()
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"], carry=carry)
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha

    committed = _git(repo, "show", "--format=", "--no-color", sha).stdout
    assert _changed_lines(committed) == _changed_lines(carry) == ["-line2", "+line2-run"]
    assert _git(repo, "show", "--name-only", "--format=", sha).stdout.split() == ["a.py"]


# ─────────────────────────────────────────────────────────────────────────────
# a recover: an unapplied carry is not a source; an applied one composes
# ─────────────────────────────────────────────────────────────────────────────


def _attempt_records(*, abandoned: dict | None, live: dict,
                     base: float) -> list[dict]:
    """Timestamped transcript records for a recovered implementer attempt.

    ``abandoned`` (if given) is stamped BEFORE ``base`` (the latest implementer
    ``node_start``); ``live`` is stamped after it.
    """
    out: list[dict] = []
    if abandoned is not None:
        out.append({"type": "assistant", "timestamp": _iso(base - 50),
                    "message": {"content": [abandoned]}})
    out.append({"type": "assistant", "timestamp": _iso(base + 60),
                "message": {"content": [live]}})
    return out


def test_unapplied_carry_publishes_the_transcript_replay_not_the_salvage(tmp_path, repo):
    """A ``salvage.patch`` that merely sits in the run dir is NOT a source.

    The rollback reset the tree to the base and the carry was never applied, so
    the authored patch is the revived implementer's own replay — committing the
    salvage would publish the unreviewed round-2 work (the ide-orca-f2b
    incident).
    """
    base = _head(repo)
    now = time.time()
    write = {"type": "tool_use", "id": "tu-1", "name": "Write", "input": {
        "file_path": str(repo / "a.py"), "content": BASE_A.replace("line2\n", "line2-run\n")}}
    run_dir = _make_run_dir(
        tmp_path, repo, base, scope=["a.py"],
        carry=_patch_line1(), carry_applied=False, rolled_back=True,
        records=_attempt_records(abandoned=None, live=write, base=now),
        node_start=now)
    _stamp_sessions(run_dir, now + 60)
    (repo / "a.py").write_text(BASE_A.replace("line2\n", "line2-run\n"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert authored.source == "transcript-replay"
    assert "line2-run" in authored.patch_text
    assert "line1-salvage" not in authored.patch_text  # the salvage never landed

    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha
    committed = _git(repo, "show", f"{sha}:a.py").stdout
    assert "line2-run" in committed and "line1-salvage" not in committed


def test_applied_carry_composes_with_the_revived_implementers_edits(tmp_path, repo):
    """Carry applied + a later implementer edit → the publish is BOTH.

    Committing the carry alone would publish less than the reviewer approved and
    leave the implementer's own change as "foreign" hunks; landing the composed
    patch on the base must reproduce the tree.
    """
    base = _head(repo)
    now = time.time()
    carry = _patch_line1()
    edit = {"type": "tool_use", "id": "tu-2", "name": "Edit", "input": {
        "file_path": str(repo / "a.py"),
        "old_string": "line2\n", "new_string": "line2-run\n"}}
    run_dir = _make_run_dir(
        tmp_path, repo, base, scope=["a.py"], carry=carry,
        records=_attempt_records(abandoned=None, live=edit, base=now),
        node_start=now)
    _stamp_sessions(run_dir, now + 60)
    # The tree holds the carry AND the implementer's own edit on top of it.
    (repo / "a.py").write_text(
        BASE_A.replace("line1\n", "line1-salvage\n").replace("line2\n", "line2-run\n"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert authored.source == "carry-patch:salvage.patch+transcript-replay"

    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha
    committed = _git(repo, "show", f"{sha}:a.py").stdout
    assert "line1-salvage" in committed and "line2-run" in committed


def test_edits_before_the_latest_attempt_are_ignored(tmp_path, repo):
    """An abandoned attempt's edits sit in the same resumed transcript.

    They were discarded from the tree, so only the calls after the LATEST
    implementer ``node_start`` belong to the change being published. Replaying
    the abandoned Write would desynchronise the replay from the tree.
    """
    base = _head(repo)
    now = time.time()
    abandoned = {"type": "tool_use", "id": "tu-0", "name": "Write", "input": {
        "file_path": str(repo / "a.py"),
        "content": BASE_A.replace("line3", "line3-abandoned")}}
    live = {"type": "tool_use", "id": "tu-1", "name": "Edit", "input": {
        "file_path": str(repo / "a.py"),
        "old_string": "line2\n", "new_string": "line2-run\n"}}
    run_dir = _make_run_dir(
        tmp_path, repo, base, scope=["a.py"], carry=_patch_line1(),
        records=_attempt_records(abandoned=abandoned, live=live, base=now),
        node_start=now)
    _stamp_sessions(run_dir, now + 60)
    (repo / "a.py").write_text(
        BASE_A.replace("line1\n", "line1-salvage\n").replace("line2\n", "line2-run\n"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert authored.source == "carry-patch:salvage.patch+transcript-replay"
    assert "line2-run" in authored.patch_text
    assert "line3-abandoned" not in authored.patch_text


# ─────────────────────────────────────────────────────────────────────────────
# the shared index
# ─────────────────────────────────────────────────────────────────────────────


def test_shared_index_only_refreshes_the_published_paths(tmp_path, repo):
    base = _head(repo)
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"], carry=_patch_a_line2())
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-run").replace("line9", "line9-peer"))
    # A peer has something staged in another file: that entry must survive.
    (repo / "staged.txt").write_text("peer staged\n")
    subprocess.run(["git", "-C", str(repo), "add", "staged.txt"], check=True,
                   capture_output=True)

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    report_foreign_hunks(str(repo), str(run_dir), authored.patch_text)
    sha = land_patch(str(repo), authored.patch_text, "")
    assert isinstance(sha, str), sha

    def git_out(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout

    # The published path's index entry is the new HEAD: no staged revert, so a
    # peer's next plain `git commit` cannot undo the publish.
    assert git_out("diff", "--cached", "--name-only", "--", "a.py") == ""
    # The peer's staged file is still staged, untouched.
    assert git_out("diff", "--cached", "--name-only").split() == ["staged.txt"]
    # The peer's in-window hunk is still an unstaged working-tree change.
    assert "line9-peer" in git_out("diff", "--", "a.py")
    # A peer commit now carries only its own staged file.
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=p", "-c", "user.email=p@x",
                    "commit", "-q", "-m", "peer"], check=True, capture_output=True)
    assert "line2-run" in git_out("show", "HEAD:a.py")


# ─────────────────────────────────────────────────────────────────────────────
# scope entries as kickoffs write them; an abstain is not "published"
# ─────────────────────────────────────────────────────────────────────────────


def test_scope_matches_paths_directories_and_bare_file_names():
    from mini_ork.cli.publisher_authored_patch import _in_scope

    rel = "crates/mini_ork_ui/src/page.rs"
    assert _in_scope(rel, [])
    assert _in_scope(rel, [rel])
    assert _in_scope(rel, ["crates/mini_ork_ui/src/"])
    assert _in_scope(rel, ["crates/mini_ork_ui"])
    assert _in_scope(rel, ["page.rs"])
    assert _in_scope(rel, ["src/page.rs"])
    assert not _in_scope(rel, ["flow.rs"])
    assert not _in_scope(rel, ["age.rs"])  # a segment match, not a substring
    assert not _in_scope("README.md", ["page.rs", "crates/mini_ork_ui/src/"])


def test_bare_file_name_scope_admits_the_nested_file(tmp_path, repo):
    # Live: a kickoff listed `page.rs` under "Files in scope (under
    # `crates/mini_ork_ui/src/`)"; scope_allow held the bare name, so every
    # in-repo edit was filtered out and the publish abstained.
    nested = repo / "pkg" / "src"
    nested.mkdir(parents=True)
    (nested / "page.rs").write_text(BASE_A)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "nested")
    base = _head(repo)
    run_dir = _make_run_dir(
        tmp_path, repo, base, scope=["page.rs"],
        edits=[("Edit", {"file_path": str(nested / "page.rs"),
                         "old_string": "line2\n", "new_string": "line2-run\n"})],
    )
    (nested / "page.rs").write_text(BASE_A.replace("line2", "line2-run"))

    authored = resolve_authored_patch(str(run_dir))
    assert isinstance(authored, AuthoredPatch), authored
    assert "pkg/src/page.rs" in authored.patch_text


def test_an_abstained_in_place_commit_is_recorded(tmp_path, repo):
    from mini_ork.cli import publisher

    base = _head(repo)
    run_dir = _make_run_dir(tmp_path, repo, base, scope=["a.py"])
    (repo / "a.py").write_text(BASE_A.replace("line2", "line2-peer"))
    committed = publisher._publisher_try_commit_files(
        "", str(repo), str(run_dir), "", "approve", "code-fix", "implementer", "r-1")
    assert committed is False
    record = json.loads((run_dir / "publish-abstain.json").read_text())
    assert record["reason"].startswith("publish-")
    assert _head(repo) == base
