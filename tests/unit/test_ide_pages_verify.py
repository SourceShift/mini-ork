"""IDE page ``verify`` — Verification & safety, built from real sources only."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import verify
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _sql(home: Path, *statements: tuple[str, tuple]) -> None:
    con = sqlite3.connect(home / "state.db")
    for sql, params in statements:
        con.execute(sql, params)
    con.commit()
    con.close()


def _node_end(run_id: str, node_type: str, node_id: str, reason: str, **extra) -> tuple[str, tuple]:
    payload = {"node_id": node_id, "node_type": node_type, "finish_reason": reason, **extra}
    return ("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"ev-{run_id}-{node_id}-{reason}", run_id, "node_end", json.dumps(payload), int(time.time())))


def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def test_every_tab_builds_with_the_design_structure(home: Path) -> None:
    for key, _label in verify.TABS:
        page = build_page(home, "verify", key, {})
        assert page["ok"] is True, page
        assert page["errors"] == {}, page["errors"]
        assert page["title"] == "Verification & safety"
        assert page["tab"] == key
        assert [(t["key"], t["label"]) for t in page["tabs"]] == [
            ("certify", "Certify"), ("gates", "Gates & verifiers"), ("panels", "Panel independence"),
            ("inbox", "Human gates"), ("autonomy", "Autonomy & probes"), ("bugs", "Bug reports")]
        assert page["sections"]
        json.dumps(page)


def test_unknown_tab_falls_back_to_certify(home: Path) -> None:
    assert build_page(home, "verify", "nope", {})["tab"] == "certify"


def test_certify_reads_the_certificates_folder(home: Path) -> None:
    empty = build_page(home, "verify", "certify", {"claim": "dark mode is lost on reload"})
    form = _section(empty, "Certify")
    assert form["actions"][0]["do"] == {"thread": "/certify dark mode is lost on reload"}
    recent = _section(empty, "Recent certificates")
    assert "No certificates yet" in recent["rows"][0]["cells"][0]["t"]

    (home / "certificates").mkdir()
    cert = {"schema": "mini-ork.certificate/v1", "verdict": "PROVEN", "reason": "probe flips",
            "claim": {"summary": "dark mode is lost on reload"},
            "method": {"model": "sonnet"}, "cost": {"usd": 0.12}, "digest": "9c1f00000000e07a",
            "evidence": {"probe": "assert read() == 'dark'",
                         "invariants": [{"mr": "persists", "holds": True, "outcome": "holds"},
                                        {"mr": "second tab", "holds": False, "on_patch": "fail"}]}}
    (home / "certificates" / "c1.json").write_text(json.dumps(cert))
    page = build_page(home, "verify", "certify", {})
    kv = {i["k"]: i for i in _section(page, "Certificate v1")["items"]}
    assert kv["Verdict"]["v"] == "PROVEN" and kv["Verdict"]["c"] == "green"
    assert kv["Invariants"]["v"] == "1 of 2 hold"
    assert kv["Cost"]["v"] == "$0.12"
    assert _section(page, "Reproduction probe")["lines"][0]["t"] == "assert read() == 'dark'"
    marks = [i["m"] for i in _section(page, "Adversarial invariants")["items"]]
    assert marks == ["✓", "!"]
    row = _section(page, "Recent certificates")["rows"][0]
    assert row["cells"][0]["t"] == "dark mode is lost on reload"
    assert row["do"]["path"].endswith("c1.json")


def test_gate_outcomes_count_node_end_events(home: Path) -> None:
    _sql(home,
         _node_end("run-a", "verifier", "test", "done"),
         _node_end("run-a", "verifier", "typecheck", "error"),
         _node_end("run-a", "reviewer", "reviewer", "verdict_fail", verdict="fail"),
         _node_end("run-b", "publisher", "publisher", "done"),
         _node_end("run-b", "implementer", "implementer", "cost_limit"),
         ("INSERT INTO mo_inbox_gates (gate_id, feature, context_json, status, enqueued_at) "
          "VALUES (?,?,?,?,?)", ("human_sign_off", "promote", "{}", "pending", int(time.time()))))
    page = build_page(home, "verify", "gates", {})
    table = _section(page, "Gate outcomes · last 24 h")
    assert table["head"] == ["gate", "pass", "fail", "pending", "latest"]
    rows = {r["cells"][0]["t"]: [c["t"] for c in r["cells"][1:]] for r in table["rows"]}
    assert rows["deterministic_verifier"][:3] == ["1", "1", "0"]
    assert "typecheck" in rows["deterministic_verifier"][3]
    assert rows["reviewer_gate"][:2] == ["0", "1"]
    assert rows["deployment_gate"][:2] == ["1", "0"]
    assert rows["human_gate"][2] == "1"
    # Budget records only its trips: a pass count would be invented.
    assert rows["budget_gate"][:2] == ["—", "1"]
    registry = _section(page, "Verifier registry")
    assert any(r["cells"][0]["t"] == "test.py" for r in registry["rows"])


def test_a_broken_source_costs_one_section(home: Path, monkeypatch) -> None:
    def boom(_home):
        raise RuntimeError("db locked")

    monkeypatch.setattr(verify, "_gate_outcomes", boom)
    page = build_page(home, "verify", "gates", {})
    assert page["ok"] is True
    assert "db locked" in page["errors"]["Gate outcomes · last 24 h"]
    assert _section(page, "Gate outcomes · last 24 h")["items"][0]["m"] == "✗"
    assert _section(page, "Specialised gates")


def test_human_gates_list_pending_items_or_say_nothing_waits(home: Path) -> None:
    page = build_page(home, "verify", "inbox", {})
    assert _section(page, "Pending human gates")["items"][0]["t"] == "Nothing waiting for you"
    _sql(home, ("INSERT INTO mo_inbox_gates (gate_id, feature, phase, context_json, status, enqueued_at) "
                "VALUES (?,?,?,?,?,?)", ("deployment_gate", "release", "staging", "{}", "pending",
                                         int(time.time()) - 120)))
    page = build_page(home, "verify", "inbox", {})
    item = _section(page, "Pending human gates")["items"][0]
    assert item["t"] == "deployment_gate" and item["m"] == "⚑"
    assert [a["label"] for a in item["acts"]] == ["Approve", "Reject"]
    approve, reject = item["acts"]
    assert approve["do"]["cli"] == ["board", "gate", "approve", "1"]
    assert approve["do"]["confirm"].startswith("Approve")
    assert reject["do"]["cli"] == ["board", "gate", "reject", "1"]
    assert reject["do"]["confirm"].startswith("Reject")
    kv = {i["k"]: i["v"] for i in _section(page, "Oversight calibration · 30 days")["items"]}
    assert kv["Decisions"] == "0"


def test_autonomy_ladder_comes_from_safety_md_and_counts_candidates(home: Path) -> None:
    _sql(home, ("INSERT INTO workflow_candidates (candidate_id, base_workflow_version_id, mutations, status) "
                "VALUES (?,?,?,?)", ("wc-1", "wf-1", json.dumps([{"kind": "prompt_change"}]), "candidate")))
    page = build_page(home, "verify", "autonomy", {})
    ladder = _section(page, "Bounded autonomy ladder")
    assert [r["cells"][0]["t"] for r in ladder["rows"]] == ["1", "2", "3", "4", "5", "6", "7"]
    assert ladder["rows"][0]["cells"][3]["t"] == "1"
    probes = [i["t"] for i in _section(page, "Safety probes")["items"]]
    assert "RSP tripwires" in probes
    assert _section(page, "audit_log")["lines"][0]["t"] == "No audit events recorded yet."


def test_panel_independence_selects_a_recipe_and_reads_its_families(home: Path) -> None:
    page = build_page(home, "verify", "panels", {})
    chips = _section(page, "Recipe")["items"]
    on = [c["t"] for c in chips if c["on"]]
    assert on == ["code-fix"]
    assert page["args"]["recipe"] == "code-fix"
    assert all(c["do"] == {"set": {"recipe": c["t"]}} for c in chips)
    kv = {i["k"]: i for i in _section(page, "Coalition verdict")["items"]}
    assert kv["Verdict"]["v"] in {"heterogeneous", "low", "medium", "high"}
    assert kv["ρ realised"]["v"] == "not measured"
    receipt = _section(page, "Receipt · who judged this")
    assert receipt["head"] == ["node", "family", "role", "lane"]
    assert receipt["rows"]


def test_bugs_tab_lists_reports_with_both_actions(home: Path) -> None:
    _sql(home, ("INSERT INTO bug_reports (fingerprint, agent_role, title, observed_in, confidence, status, "
                "first_seen_at, last_seen_at, updated_at) VALUES ('fp', 'scheduler', 'verdict.json mismatch', "
                "'bin/mini-ork-scheduler', 0.95, 'open', ?, ?, ?)",
                (int(time.time()), int(time.time()), int(time.time()))))
    table = _section(build_page(home, "verify", "bugs", {}), "Bug reports")
    assert table["head"] == ["id", "report", "source", "score"]
    assert [c["t"] for c in table["rows"][0]["cells"]][1:] == [
        "verdict.json mismatch", "mini-ork-scheduler", "0.95"]
    actions = {a["label"]: a["do"] for a in table["actions"]}
    assert actions["Sweep runs"]["cli"] == ["bugs", "sweep"]
    assert actions["Sweep runs"]["home"] is False
    assert actions["Promote top 3"]["cli"] == ["bugs", "promote", "--top", "3"]
    assert actions["Promote top 3"]["home"] is False
