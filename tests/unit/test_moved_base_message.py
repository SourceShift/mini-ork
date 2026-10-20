"""RBF-11d: the ``impl_moved_base`` message names the move, not the actor.

A moved HEAD may be caused by the implementer or by a concurrent session, so
the message must be actor-neutral, carry the target's reflog excerpt since the
run started (at most ten lines), and say "concurrent actor" only when no entry
falls inside the implementer's own subprocess window.

Every fixture is a hermetic temporary git repo with fixed commit timestamps;
nothing here touches a live run, DB, credential, or network.
"""

import os
import subprocess
from datetime import datetime

from mini_ork.cli.execute_handlers import _implementer_moved_base

# All instants are in the machine's local offset so naive and aware parses
# agree with ``git reflog --date=iso``.
_LOCAL_TZ = datetime.now().astimezone().tzinfo

T_START = datetime(2026, 1, 2, 10, 0, tzinfo=_LOCAL_TZ)
T_END = datetime(2026, 1, 2, 11, 0, tzinfo=_LOCAL_TZ)

BASE_SUBJECT = "base-before-window-77b1c2"
IN_WINDOW_SUBJECT = "in-window-commit-4ab1c2"
AFTER_WINDOW_SUBJECT = "after-window-commit-9d3e5f"


def _at(hour, minute):
    return datetime(2026, 1, 2, hour, minute, tzinfo=_LOCAL_TZ)


def _git(repo, *args, date=None):
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    # A pre-set GIT_DIR/GIT_WORK_TREE would redirect the fixture out of tmp.
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    if date is not None:
        stamp = date.isoformat()
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    proc = subprocess.run(
        ["git", "-C", str(repo),
         "-c", "user.name=moved-base-test",
         "-c", "user.email=moved-base-test@example.invalid",
         "-c", "commit.gpgsign=false",
         "-c", "core.hooksPath=/dev/null",
         *args],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"fixture git {args} failed: {proc.stderr.strip()}"
    return proc.stdout


def _commit(repo, subject, when):
    _git(repo, "commit", "-q", "--allow-empty", "-m", subject, date=when)
    return _git(repo, "rev-parse", "HEAD").strip()


def _target(tmp_path, commits):
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    shas = [_commit(repo, subject, when) for subject, when in commits]
    assert commits[-1][0] in _git(repo, "reflog", "--date=iso")
    return repo, shas


def _run_dir(tmp_path, base_sha, started):
    run = tmp_path / "run"
    run.mkdir()
    ref = run / "pre-implementer-ref"
    ref.write_text(base_sha + "\n", encoding="utf-8")
    stamp = started.timestamp()
    os.utime(ref, (stamp, stamp))
    return run


def _message(tmp_path, commits, start=T_START, end=T_END):
    repo, shas = _target(tmp_path, commits)
    run = _run_dir(tmp_path, shas[0], start)
    return _implementer_moved_base(
        str(run), str(repo), window_start=start, window_end=end), shas


def test_commit_inside_window_is_actor_neutral(tmp_path):
    """A commit made after the window start: reflog line present, no blame."""
    msg, shas = _message(tmp_path, [
        (BASE_SUBJECT, _at(9, 0)),
        (IN_WINDOW_SUBJECT, _at(10, 30)),
    ])
    assert msg, "a moved base must still be reported"
    assert "the implementer committed" not in msg
    assert "HEAD moved from" in msg
    assert "during the implementer window" in msg
    assert IN_WINDOW_SUBJECT in msg, "the reflog line of the move must be attached"
    assert shas[0][:7] in msg and shas[-1][:7] in msg
    assert "concurrent actor" not in msg, (
        "an entry inside the implementer window must not be blamed on another actor")


def test_commit_outside_window_gets_concurrent_actor_suffix(tmp_path):
    """A commit timestamped outside the implementer window: concurrent suffix."""
    msg, _ = _message(tmp_path, [
        (BASE_SUBJECT, _at(9, 0)),
        (AFTER_WINDOW_SUBJECT, _at(11, 30)),
    ])
    assert "not made by the implementer (concurrent actor)" in msg
    assert AFTER_WINDOW_SUBJECT in msg, "the reflog excerpt must still be attached"
    assert "HEAD moved from" in msg
    assert "the implementer committed" not in msg


def test_reflog_excerpt_is_capped_at_ten_lines(tmp_path):
    commits = [(BASE_SUBJECT, _at(9, 0))] + [
        (f"cap-commit-{i:02d}-dead", _at(10, 5 + i)) for i in range(12)
    ]
    msg, _ = _message(tmp_path, commits)
    shown = msg.count("HEAD@{")
    assert 1 <= shown <= 10, f"expected at most ten reflog lines, got {shown}"


def test_reflog_is_filtered_to_entries_since_run_start(tmp_path):
    msg, _ = _message(tmp_path, [
        (BASE_SUBJECT, _at(9, 0)),
        (IN_WINDOW_SUBJECT, _at(10, 30)),
    ])
    assert IN_WINDOW_SUBJECT in msg
    assert BASE_SUBJECT not in msg, "entries before the run started must be filtered out"
