"""Harness card (mini_ork.observability.harness_card) and ``mini-ork harness-card``.

Hermetic: a fresh migrated state.db, a temporary run dir with per-node lane
records, ledger rows written through the real ledger writer, and conformance
probes run through an injected fake harness. No provider, no network.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from mini_ork.cli import harness_card as harness_card_cli
from mini_ork.dispatch import conformance, harness_identity
from mini_ork.dispatch.llm_dispatch import write_llm_calls_row
from mini_ork.dispatch.models import DispatchResult, TokenUsage
from mini_ork.observability import harness_card
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-card-1"


def _harness(version: str) -> dict:
    return {"harness": "claude-code", "kind": "anthropic-compat", "binary": "claude", "version": version}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "runs" / RUN).mkdir(parents=True)
    db = home / "state.db"
    rc, _, err = mig.init_db(db=str(db), root=str(REPO))
    assert rc == 0, err
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_ID", RUN)
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    return home


def _call(home, lane, actor, *, version="2.1.312 (Claude Code)", status="success", cost="0.01", harness=True):
    metadata = json.dumps({"harness": _harness(version)}) if harness else "{}"
    write_llm_calls_row(str(home / "state.db"), "p", lane, "default", "mini-ork:researcher", actor,
                        status, 1000, cost, "", 100, 20, metadata)


def _node(home, node, requested, served):
    (home / "runs" / RUN / f".lane-{node}.json").write_text(
        json.dumps({"requested": requested, "served": served, "fallback": requested != served, "at": 1}))


def test_a_silent_fallback_that_collapses_a_panel_is_disclosed(home):
    # The reply-evidence panel of 2026-10-31: codex and kimi were broken, and
    # both lenses fell back to minimax — a four-family panel ran as three.
    for node, requested, served in [("steelman_lens", "glm", "glm"),
                                    ("counter_lens", "deepseek_flash", "deepseek_flash"),
                                    ("factcheck_lens", "codex", "minimax"),
                                    ("voice_lens", "kimi", "minimax")]:
        _node(home, node, requested, served)
        _call(home, served, node)
    card = harness_card.build(str(home / "runs" / RUN), RUN, db=str(home / "state.db"))

    fallbacks = [w for w in card["warnings"] if "requested" in w and "served by" in w]
    assert len(fallbacks) == 2
    assert any(w.startswith("factcheck_lens: requested codex") for w in fallbacks)
    collapse = [w for w in card["warnings"] if w.startswith("family collapse")]
    assert len(collapse) == 1
    assert "factcheck_lens, voice_lens" in collapse[0] and "served by minimax" in collapse[0]
    assert card["totals"]["calls"] == 4
    assert card["totals"]["families_served"] == sorted({n["served_family"] for n in card["nodes"]})
    minimax = [row for row in card["lanes"] if row["lane"] == "minimax"][0]
    assert (minimax["calls"], minimax["actors"]) == (2, ["factcheck_lens", "voice_lens"])


def test_no_warning_when_every_node_got_the_lane_it_asked_for(home):
    _node(home, "steelman_lens", "glm", "glm")
    _call(home, "glm", "steelman_lens")
    card = harness_card.build(str(home / "runs" / RUN), RUN, db=str(home / "state.db"))
    assert card["warnings"] == []


def _probe(home, lane, version, ok, monkeypatch):
    monkeypatch.setattr(harness_identity, "identity", lambda *_a, **_k: _harness(version))

    def run(request, root):
        token = Path(request.cwd, conformance.PROBE_FILE).read_text().strip()
        return DispatchResult(ok=True, rc=0, text=json.dumps({"token": token}) if ok else "no",
                              model=request.model, usage=TokenUsage(input_tokens=5, output_tokens=2))

    conformance.probe(lane, db=str(home / "state.db"), dispatch=run)


def test_conformance_is_reported_for_the_release_that_served(home, monkeypatch):
    _probe(home, "glm", "2.1.312 (Claude Code)", True, monkeypatch)
    _probe(home, "kimi", "2.1.312 (Claude Code)", False, monkeypatch)
    _probe(home, "minimax", "2.1.200 (Claude Code)", True, monkeypatch)
    for lane in ("glm", "kimi", "minimax", "deepseek_flash"):
        _call(home, lane, f"{lane}_node")
    card = harness_card.build(str(home / "runs" / RUN), RUN, db=str(home / "state.db"))
    status = {row["lane"]: row["conformance"] for row in card["lanes"]}
    assert card["totals"]["calls"] == 4, "probe calls are not the run's work"
    assert status["glm"] == "conformant"
    assert status["kimi"].startswith("probe failed: ")
    assert status["minimax"] == "probed under 2.1.200 (Claude Code), not the release that served"
    assert status["deepseek_flash"] == "never probed"


def test_calls_without_a_harness_identity_are_flagged(home, monkeypatch):
    # Rows written before harness stamping existed.
    monkeypatch.setattr(harness_identity, "identity", lambda *_a, **_k: None)
    _call(home, "glm", "old_node", harness=False)
    card = harness_card.build(str(home / "runs" / RUN), RUN, db=str(home / "state.db"))
    assert card["lanes"][0]["harness"] == "unrecorded"
    assert "1 call(s) carry no harness identity (written before harness stamping)" in card["warnings"]


def test_write_stores_json_and_markdown(home):
    _node(home, "voice_lens", "kimi", "minimax")
    _call(home, "minimax", "voice_lens")
    run_dir = home / "runs" / RUN
    harness_card.write(str(run_dir), RUN, db=str(home / "state.db"))
    stored = json.loads((run_dir / "harness-card.json").read_text())
    assert stored["schema"] == harness_card.CARD_SCHEMA
    md = (run_dir / "harness-card.md").read_text()
    assert "| voice_lens | kimi" in md and "## Warnings" in md


def test_cli_prints_the_card_and_rejects_an_unknown_run(home):
    _call(home, "glm", "steelman_lens")
    out = io.StringIO()
    with redirect_stdout(out):
        assert harness_card_cli.main([RUN, "--json"]) == 0
    assert json.loads(out.getvalue())["run_id"] == RUN
    assert harness_card_cli.main(["no-such-run"]) == 2
