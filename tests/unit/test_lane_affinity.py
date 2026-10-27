"""Tests for B3 lane affinity (``mini_ork/dispatch/lane_affinity.py`` + the
``llm_dispatch()`` integration).

Hermetic: tmp dirs only, the dispatcher is a fake 5-arg ``dispatch_fn`` (the
historic contract), and ``MINI_ORK_DB`` points at a path that does not exist so
the cost circuit, both lane-fuse passes and every ``llm_calls`` write early
return. No LLM, no network, no credentials.
"""
from __future__ import annotations

import json
import os

import pytest

from mini_ork.dispatch import lane_affinity
from mini_ork.dispatch.llm_dispatch import llm_dispatch

CHAIN = "glm,deepseek_flash"


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Never inherit a wrapping mini-ork launcher's run dir / node id."""
    for var in ("MINI_ORK_RUN_DIR", "MO_NODE_ID", "MO_LANE_AFFINITY",
                "MO_TARGET_CWD", "MO_DISPATCH_MAX_ATTEMPTS",
                "MO_GLM_MAX_ATTEMPTS", "MO_FUSE_ENABLED"):
        monkeypatch.delenv(var, raising=False)


def _affinity_path(run_dir) -> str:
    return os.path.join(str(run_dir), lane_affinity.AFFINITY_REL)


# ── unit contract ──────────────────────────────────────────────────────────

def test_record_then_preferred_keyed_on_lead(tmp_path):
    lane_affinity.record(str(tmp_path), "implementer", "glm", "deepseek_flash")
    assert lane_affinity.preferred(str(tmp_path), "implementer", "glm") == "deepseek_flash"
    assert lane_affinity.preferred(str(tmp_path), "implementer", "opus") is None
    assert lane_affinity.preferred(str(tmp_path), "reviewer", "glm") is None


def test_second_record_does_not_overwrite(tmp_path):
    lane_affinity.record(str(tmp_path), "implementer", "glm", "deepseek_flash")
    # neither a different lead nor a same-lead re-record redefines the affinity
    lane_affinity.record(str(tmp_path), "implementer", "kimi", "minimax")
    lane_affinity.record(str(tmp_path), "implementer", "glm", "minimax")
    assert lane_affinity.preferred(str(tmp_path), "implementer", "glm") == "deepseek_flash"
    assert lane_affinity.preferred(str(tmp_path), "implementer", "kimi") is None


def test_apply_moves_lane_first_and_is_identity_otherwise():
    assert lane_affinity.apply(["a", "b", "c"], "c") == ["c", "a", "b"]
    assert lane_affinity.apply(["a", "b", "c"], "a") == ["a", "b", "c"]
    chain = ["a", "b", "c"]
    for absent in ("zzz", None):
        result = lane_affinity.apply(chain, absent)
        assert result == ["a", "b", "c"]
        assert result is not chain


def test_corrupt_file_preferred_none_and_record_starts_fresh(tmp_path):
    with open(_affinity_path(tmp_path), "wb") as fh:
        fh.write(b"\x00 not json at all")
    assert lane_affinity.preferred(str(tmp_path), "implementer", "glm") is None
    lane_affinity.record(str(tmp_path), "implementer", "glm", "deepseek_flash")
    assert lane_affinity.preferred(str(tmp_path), "implementer", "glm") == "deepseek_flash"


def test_enabled_default_on_and_zero_off(monkeypatch):
    monkeypatch.delenv("MO_LANE_AFFINITY", raising=False)
    assert lane_affinity.enabled()
    monkeypatch.setenv("MO_LANE_AFFINITY", "0")
    assert not lane_affinity.enabled()
    monkeypatch.setenv("MO_LANE_AFFINITY", "1")
    assert lane_affinity.enabled()
    assert not lane_affinity.enabled({"MO_LANE_AFFINITY": "0"})


# ── through llm_dispatch ───────────────────────────────────────────────────

class Round:
    """A scripted dispatch round. The fake owns the chain walk (the real
    fallback walker lives inside ``mo_llm_dispatch``): lanes in ``fail`` die
    with a non-retryable provider error, and the first survivor writes both the
    output file and the ``.model`` sidecar naming the lane that served."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls: list[list[str]] = []

    def fake(self, model, prompt_text, out_file, timeout_s, max_turns):
        lanes = [m.strip() for m in str(model).split(",") if m.strip()]
        self.calls.append(lanes)
        for lane in lanes:
            if lane in self.fail:
                with open(out_file + ".err.log", "a", encoding="utf-8") as fh:
                    fh.write(f"[{lane}] provider quota exceeded (terminal)\n")
                continue
            with open(out_file, "w", encoding="utf-8") as fh:
                fh.write(f"output served by {lane}\n")
            with open(out_file + ".model", "w", encoding="utf-8") as fh:
                fh.write(lane)
            return 0
        return 1

    def run(self, tmp_path, chain):
        return llm_dispatch(
            ["--node-type", "implementer", "--prompt-text", "test round",
             "--out", str(tmp_path / "out.txt"), "--model", chain,
             "--timeout", "30", "--max-turns", "4"],
            root=str(tmp_path), dispatch_fn=self.fake)


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    rd = tmp_path / "run"
    rd.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(rd))
    monkeypatch.setenv("MO_NODE_ID", "implementer")
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "no-state.db"))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    return rd


def _entry(run_dir) -> dict:
    with open(_affinity_path(run_dir), encoding="utf-8") as fh:
        return json.load(fh)["implementer"]


def test_round1_failed_lead_records_serving_lane(run_dir, tmp_path):
    r1 = Round(fail={"glm"})
    assert r1.run(tmp_path, CHAIN) == 0
    assert r1.calls == [["glm", "deepseek_flash"]]
    entry = _entry(run_dir)
    assert entry["lead"] == "glm"
    assert entry["served"] == "deepseek_flash"


def test_round2_same_chain_dispatches_affinity_lane_first(run_dir, tmp_path, capsys):
    assert Round(fail={"glm"}).run(tmp_path, CHAIN) == 0
    capsys.readouterr()
    r2 = Round()
    assert r2.run(tmp_path, CHAIN) == 0
    assert r2.calls[0] == ["deepseek_flash", "glm"]
    err = capsys.readouterr().err
    assert ("[lane_affinity] implementer: deepseek_flash first "
            "(served an earlier round; lead glm)") in err
    # the first success keeps defining the affinity
    entry = _entry(run_dir)
    assert entry["lead"] == "glm"
    assert entry["served"] == "deepseek_flash"


def test_round2_remapped_lead_wins(run_dir, tmp_path, capsys):
    assert Round(fail={"glm"}).run(tmp_path, CHAIN) == 0
    capsys.readouterr()
    r2 = Round()
    assert r2.run(tmp_path, "opus,deepseek_flash") == 0
    assert r2.calls[0] == ["opus", "deepseek_flash"]
    assert "[lane_affinity]" not in capsys.readouterr().err
    assert _entry(run_dir)["lead"] == "glm"


def test_disabled_knob_round2_keeps_routing_lead(run_dir, tmp_path, capsys, monkeypatch):
    assert Round(fail={"glm"}).run(tmp_path, CHAIN) == 0
    capsys.readouterr()
    monkeypatch.setenv("MO_LANE_AFFINITY", "0")
    r2 = Round()
    assert r2.run(tmp_path, CHAIN) == 0
    assert r2.calls[0] == ["glm", "deepseek_flash"]
    assert "[lane_affinity]" not in capsys.readouterr().err


# ── affinity reorders the FINAL (post-expansion) chain ─────────────────────

def _planner_chain(tmp_path, monkeypatch) -> list[str]:
    """The planner chain llm_dispatch actually composes under the harness."""
    from mini_ork.dispatch.routing import dispatch_chain
    monkeypatch.setenv("MO_FALLBACK_CODING", "minimax,codex,sonnet")
    return dispatch_chain("planner", "glm", str(tmp_path)).split(",")


def _seed_affinity(run_dir, lead="glm", served="minimax") -> None:
    with open(_affinity_path(run_dir), "w", encoding="utf-8") as fh:
        json.dump({"planner": {"lead": lead, "served": served}}, fh)


def _run_planner(tmp_path, monkeypatch, *, extra_env=None):
    """Dispatch the planner with no ``--model`` override (the incident path)."""
    monkeypatch.setenv("MO_NODE_ID", "planner")
    monkeypatch.setenv("MO_FALLBACK_CODING", "minimax,codex,sonnet")
    monkeypatch.delenv("MO_SHAPE_CHECK", raising=False)
    monkeypatch.delenv("MO_PLAN_ARTIFACT", raising=False)
    monkeypatch.setattr("mini_ork.dispatch.llm_dispatch.resolve_lane_model",
                        lambda *a, **k: "glm")
    monkeypatch.setattr("mini_ork.dispatch.llm_dispatch.resolve_lane_family",
                        lambda lane, *a, **k: lane)
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    calls: list[list[str]] = []

    def fake(model, prompt_text, out_file, timeout_s, max_turns):
        calls.append([m.strip() for m in str(model).split(",") if m.strip()])
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write("output")
        return 0

    rc = llm_dispatch(
        ["--node-type", "planner", "--prompt-text", "test round",
         "--out", str(tmp_path / "out.txt")],
        root=str(tmp_path), dispatch_fn=fake)
    return rc, calls


def test_affinity_reorders_the_expanded_planner_chain(run_dir, tmp_path,
                                                     monkeypatch, capsys):
    """The served lane only exists in the chain AFTER the shape-contract
    expansion; affinity must still put it first (kickoff defect 3)."""
    chain = _planner_chain(tmp_path, monkeypatch)
    assert len(chain) >= 2 and chain[0] == "glm", chain
    served = chain[1]
    _seed_affinity(run_dir, lead="glm", served=served)
    rc, calls = _run_planner(tmp_path, monkeypatch)
    assert rc == 0
    assert calls, "the fake dispatch must have been called"
    assert calls[0][0] == served, calls
    assert sorted(calls[0]) == sorted(set(chain)), calls
    assert f"[lane_affinity] planner: {served} first" in capsys.readouterr().err


def test_affinity_disabled_first_call_is_the_lead(run_dir, tmp_path,
                                                  monkeypatch):
    chain = _planner_chain(tmp_path, monkeypatch)
    served = chain[1]
    _seed_affinity(run_dir, lead="glm", served=served)
    rc, calls = _run_planner(tmp_path, monkeypatch,
                             extra_env={"MO_LANE_AFFINITY": "0"})
    assert rc == 0
    assert calls[0][0] == "glm", calls
    # the kill switch disables the reorder, not the shape-contract expansion
    assert calls[0] == chain, calls
