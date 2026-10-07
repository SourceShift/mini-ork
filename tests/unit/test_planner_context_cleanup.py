"""Planner context hygiene (audit of live runs, 2026-10-07).

The planner must get only *verified, run-relevant* context:

- raw gradients (the graph-context block) are off unless ``MO_INJECT_UNVERIFIED=1``
  — the same opt-in the node learned blocks already use;
- other sessions'/projects' material (role pack, ContextNest attention inbox +
  recent sessions, the global active-state index) is off unless
  ``MO_PLANNER_SHARED_CONTEXT=1`` — and its producers are not even called;
- whatever is withheld is named in the injection ledger (``skipped_blocks``);
- the *full* success command reaches nodes (`context_v2._constraint_items`).
"""
from __future__ import annotations

import json

import pytest

from mini_ork import context_assembler, context_v2
from mini_ork.cli import plan as plan_mod

KICKOFF = """# Planner context: no unverified gradients, no other sessions' material

## Files in scope (touch ONLY these)

- `mini_ork/cli/plan.py`

Do NOT modify any other file.

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_planner_context_cleanup.py
```
"""
BASE = "PLANNER PROMPT"

# Markers carry the real block headers so an accidental injection is detectable
# by substring, not only by the call counters.
FM_MARKER = "V1-FM"
PRIOR_MARKER = "V1-PRIOR"
GRAPH_MARKER = "Learned graph context (failure-linked)"
ROLE_MARKER = "--- ContextNest capsule (kind-ordered substrate digest) ---"
RECENT_MARKER = "--- ContextNest: recent sessions for relevant files ---"
ACTIVE_MARKER = "ACTIVE STATE INDEX"


@pytest.fixture
def env(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(KICKOFF, encoding="utf-8")
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "1")
    # Keep the arm deterministic: this suite tests the *gating*, not v1-vs-v2.
    monkeypatch.setenv("MO_CONTEXT_V2", "off")
    monkeypatch.delenv("MO_INJECT_UNVERIFIED", raising=False)
    monkeypatch.delenv("MO_PLANNER_SHARED_CONTEXT", raising=False)
    monkeypatch.delenv("MO_USE_ROLE_PACKS", raising=False)
    return tmp_path, str(kickoff)


def _counted(calls, key, value):
    def fn(*_a, **_k):
        calls[key] = calls.get(key, 0) + 1
        return value
    return fn


def _stub_producers(monkeypatch, calls):
    """Replace every context producer with a counter + a marked string."""
    monkeypatch.setattr(context_assembler, "failure_modes_md", _counted(calls, "fm", FM_MARKER))
    monkeypatch.setattr(context_assembler, "prior_runs_md", _counted(calls, "prior", PRIOR_MARKER))
    monkeypatch.setattr(context_assembler, "graph_context_md", _counted(calls, "graph", GRAPH_MARKER))
    monkeypatch.setattr(context_assembler, "context_assemble", lambda *_a, **_k: {"v1": True})
    monkeypatch.setattr(plan_mod, "_contextnest_atoms_md", _counted(calls, "atoms", ROLE_MARKER))
    monkeypatch.setattr(plan_mod, "_contextnest_recent_sessions_md",
                        _counted(calls, "recent", RECENT_MARKER))
    import mini_ork.steering.context_role_packs as crp
    monkeypatch.setattr(crp, "role_pack_md", _counted(calls, "role", ROLE_MARKER))
    import mini_ork.orchestration.active_state_index as asi
    monkeypatch.setattr(asi, "render_active_state_block", _counted(calls, "active", ACTIVE_MARKER))


def _plan(tmp_path, kickoff, name):
    run_dir = tmp_path / name
    run_dir.mkdir()
    out = plan_mod._inject_context(BASE, kickoff, "framework_edit",
                                   str(tmp_path / "state.db"),
                                   str(run_dir / "plan.json"), False)
    rec = json.loads((run_dir / "learned" / "planner.json").read_text())
    return out, rec


# ── defaults: unverified + shared material are withheld ─────────────────────

def test_defaults_drop_unverified_and_shared_blocks(env, monkeypatch):
    tmp_path, kickoff = env
    calls: dict[str, int] = {}
    _stub_producers(monkeypatch, calls)

    out, rec = _plan(tmp_path, kickoff, "defaults")

    # None of the withheld material reaches the prompt.
    assert GRAPH_MARKER not in out
    assert "ContextNest" not in out
    assert ACTIVE_MARKER not in out
    # …and their producers were never called (no ContextNest HTTP, no DB scan).
    for key in ("graph", "role", "atoms", "recent", "active"):
        assert calls.get(key, 0) == 0, key

    # The verified v1 failure-modes block is still injected.
    assert FM_MARKER in out and PRIOR_MARKER in out
    assert calls["fm"] == 1 and calls["prior"] == 1

    # The ledger names every block left out, with why.
    sb = rec["skipped_blocks"]
    assert sb["graph_context"] == "unverified"
    assert sb["role_pack"] == "shared_context_off"
    assert sb["contextnest_recent"] == "shared_context_off"
    assert sb["active_state"] == "shared_context_off"


def test_verified_failure_modes_block_survives_the_cleanup(env, monkeypatch):
    tmp_path, kickoff = env
    calls: dict[str, int] = {}
    _stub_producers(monkeypatch, calls)
    out, _ = _plan(tmp_path, kickoff, "fm")
    assert FM_MARKER in out
    assert calls["fm"] == 1


# ── opt-ins restore only the block they cover ───────────────────────────────

def test_mo_inject_unverified_restores_only_the_graph_context(env, monkeypatch):
    tmp_path, kickoff = env
    calls: dict[str, int] = {}
    _stub_producers(monkeypatch, calls)
    monkeypatch.setenv("MO_INJECT_UNVERIFIED", "1")

    out, rec = _plan(tmp_path, kickoff, "unverified")

    assert GRAPH_MARKER in out
    assert calls["graph"] == 1
    assert "graph_context" not in rec["skipped_blocks"]
    # Shared-session material stays off.
    assert "role_pack" in rec["skipped_blocks"]
    assert ACTIVE_MARKER not in out


def test_mo_planner_shared_context_restores_the_shared_blocks(env, monkeypatch):
    tmp_path, kickoff = env
    calls: dict[str, int] = {}
    _stub_producers(monkeypatch, calls)
    monkeypatch.setenv("MO_PLANNER_SHARED_CONTEXT", "1")

    out, rec = _plan(tmp_path, kickoff, "shared")

    assert ROLE_MARKER in out
    assert ACTIVE_MARKER in out
    assert calls["role"] == 1 and calls["active"] == 1
    assert "role_pack" not in rec["skipped_blocks"]
    # Graph context is still withheld (it is unverified, not merely shared).
    assert rec["skipped_blocks"]["graph_context"] == "unverified"
    assert GRAPH_MARKER not in out


# ── the full success command reaches nodes ──────────────────────────────────

def test_constraint_keeps_a_long_verification_command_intact():
    cmd = "python3.11 -m pytest -q " + " ".join(
        f"tests/unit/test_case_{i}.py" for i in range(12))
    assert 240 < len(" ".join(cmd.split())) <= 2000

    items = context_v2._constraint_items({"verification": [cmd]})
    text = items[0]["text"]

    assert " ".join(cmd.split()) in text
    assert "(truncated)" not in text


def test_constraint_truncates_only_absurd_commands():
    long_cmd = "x" * 2500
    items = context_v2._constraint_items({"verification": [long_cmd]})
    text = items[0]["text"]

    assert text.endswith("…(truncated)`")
    assert "x" * 2000 in text
    assert "x" * 2001 not in text
