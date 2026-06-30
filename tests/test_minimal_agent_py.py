"""Tests for mini_ork.agent.minimal — the three spec'd behaviors.

- completion via sentinel
- max_turns cap
- per-turn shell-state isolation (fresh subprocess per turn)
"""
from __future__ import annotations

import os
from pathlib import Path

# Step 3 fallback: pytest env does not export MINI_ORK_ROOT by default.
_REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("MINI_ORK_ROOT", str(_REPO_ROOT))

from mini_ork.agent import MinimalAgent, run_minimal  # noqa: E402


class FakeDispatch:
    """Returns scripted strings, in order, on each call."""

    def __init__(self, scripts: list[str]) -> None:
        self._scripts = list(scripts)
        self.calls: list[object] = []

    def __call__(self, req):
        self.calls.append(req)
        if not self._scripts:
            raise AssertionError("FakeDispatch exhausted — agent out-paced script")
        return self._scripts.pop(0)


import pytest  # noqa: E402


@pytest.fixture
def scripted_dispatch(monkeypatch):
    def _factory(scripts: list[str]) -> FakeDispatch:
        fake = FakeDispatch(scripts)
        monkeypatch.setattr("mini_ork.agent.minimal.dispatch_model", fake)
        return fake

    return _factory


def test_loop_completes_and_writes_file(scripted_dispatch, tmp_path):
    cwd = str(tmp_path)
    scripted_dispatch([
        "```bash\necho hi > out.txt\n```",
        "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT done",
    ])
    agent = MinimalAgent(cwd=cwd, max_turns=4)
    result = agent.run("write hi to out.txt")

    assert (tmp_path / "out.txt").read_text().strip() == "hi"
    assert result.turns == 2
    assert result.completed is True
    assert result.final_output == "done"
    assert result.exit_status == "completed"


def test_max_turns_cap(scripted_dispatch, tmp_path):
    cwd = str(tmp_path)
    scripted_dispatch(["```bash\ntrue\n```"] * 10)
    agent = MinimalAgent(cwd=cwd, max_turns=4)
    result = agent.run("loop forever")

    assert result.completed is False
    assert result.turns == 4
    assert result.exit_status == "max_turns_exceeded"


def test_independent_action_cwd(scripted_dispatch, tmp_path):
    """Turn 1 `cd sub && pwd` then turn 2 `pwd` must return the original cwd
    — proves each turn is its own subprocess (no shell-state leak)."""
    cwd = str(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    scripted_dispatch([
        "```bash\ncd sub && pwd\n```",
        "```bash\npwd\n```",
    ])
    agent = MinimalAgent(cwd=cwd, max_turns=4)
    result = agent.run("test isolation")

    assert result.turns == 2
    user_msgs = [m for m in result.messages if m["role"] == "user"]
    # initial task + per-turn stdout messages → at least 2 user-role entries
    assert len(user_msgs) >= 2
    last_user = user_msgs[-1]["content"]
    # The second `pwd` must report the original cwd, not the `sub` directory.
    assert cwd in last_user
    assert f"{cwd}/sub" not in last_user


def test_run_minimal_helper(scripted_dispatch, tmp_path):
    scripted_dispatch(["COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT hi"])
    result = run_minimal("any", cwd=str(tmp_path), max_turns=2)
    assert result.completed is True
    assert result.final_output == "hi"