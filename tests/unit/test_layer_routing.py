"""Unit + integration tests for the layer-routing gate (RSI Tier-1 #5a).

Hermetic: tmp_path homes + sqlite, no network, no LLM. Covers
:mod:`mini_ork.learning.layer_routing` directly and drives the
reflection→store loop through ``reflection_pipeline.reflection_extract_gradients``.
"""
from __future__ import annotations

import json
import sqlite3
import time

from mini_ork.learning import layer_routing
from mini_ork.learning import reflection_pipeline
from mini_ork.recovery.diagnose import DIAGNOSIS_FILE, LAYER_CHANGE, LAYER_UNCLASSIFIED


def make_home(tmp_path, *, status="failed", layer="kickoff", routes_to="kickoff_repair",
              diagnosis="dict", task_runs=True, null_run_id_trace=False):
    """Build a fixture home: state.db + runs/run-1/<DIAGNOSIS_FILE>."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    db = home / "state.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE execution_traces (trace_id TEXT PRIMARY KEY, run_id TEXT, "
            "task_class TEXT, created_at TEXT)"
        )
        if task_runs:
            con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT)")
        con.execute(
            "INSERT INTO execution_traces VALUES ('tr-1', 'run-1', 'code_fix', "
            "'2026-10-27 10:00:00')"
        )
        if null_run_id_trace:
            con.execute(
                "INSERT INTO execution_traces VALUES ('tr-null', NULL, 'code_fix', "
                "'2026-10-27 10:00:01')"
            )
        if task_runs:
            con.execute("INSERT INTO task_runs VALUES ('run-1', ?)", (status,))
        con.commit()
    finally:
        con.close()
    if diagnosis != "none":
        run_dir = home / "runs" / "run-1"
        run_dir.mkdir(parents=True)
        payload = (
            {"layer": layer, "routes_to": routes_to, "rule_id": "R0", "source": "rule"}
            if diagnosis == "dict"
            else diagnosis
        )
        (run_dir / DIAGNOSIS_FILE).write_text(json.dumps(payload))
    return home, db


# ── Constants ────────────────────────────────────────────────────────────────

def test_constants():
    assert layer_routing.EDIT_LAYER_HARNESS == "harness"
    assert layer_routing.HARNESS_TARGET_PREFIXES == (
        "workflow.", "agent.", "verifier.", "harness.", "prompts/"
    )
    assert layer_routing.CROSS_CLASS_PREFIX == "cross_class:"
    assert layer_routing.HARNESS_FIXABLE_LAYERS == frozenset({LAYER_CHANGE})
    assert layer_routing.HARNESS_FIXABLE_LAYERS == frozenset({"change"})
    assert layer_routing.PASS_THROUGH_LAYERS == frozenset({"", LAYER_UNCLASSIFIED})
    assert layer_routing.PASS_THROUGH_LAYERS == frozenset({"", "unclassified"})
    assert layer_routing.REFUSALS_FILE == "layer-refusals.jsonl"


def test_functions_are_module_level():
    for name in ("mode", "edit_layer", "route", "trace_run_id", "run_diagnosis",
                 "check_gradient", "record_refusal"):
        assert callable(getattr(layer_routing, name)), name


# ── mode ─────────────────────────────────────────────────────────────────────

def test_mode_matrix(monkeypatch):
    cases = [
        ("off", "off"),
        ("OFF ", "off"),
        ("on", "on"),
        (" ON", "on"),
        ("shadow", "shadow"),
        ("nonsense", "shadow"),
    ]
    for raw, expected in cases:
        monkeypatch.setenv("MO_LAYER_ROUTING", raw)
        assert layer_routing.mode() == expected, (raw, expected)
    monkeypatch.delenv("MO_LAYER_ROUTING", raising=False)
    assert layer_routing.mode() == "shadow"


def test_mode_unset_is_shadow(monkeypatch):
    monkeypatch.delenv("MO_LAYER_ROUTING", raising=False)
    assert layer_routing.mode() == "shadow"


# ── edit_layer ───────────────────────────────────────────────────────────────

def test_edit_layer_harness_targets():
    for target in (
        "agent.reviewer.prompt",
        "workflow.edge.verifier_to_reviewer",
        "verifier.obs_smoke",
        "workflow.recipe.code-fix",
        "harness.code-fix.implementer",
        "prompts/reviewer.md",
        "cross_class:workflow.edge.x",
    ):
        assert layer_routing.edit_layer(target) == "harness", target


def test_edit_layer_untyped_targets():
    assert layer_routing.edit_layer("verifier@v1") == ""
    assert layer_routing.edit_layer("") == ""
    assert layer_routing.edit_layer("cross_class:verifier@v1") == ""


def test_edit_layer_strips_exactly_one_prefix():
    assert layer_routing.edit_layer("cross_class:agent.reviewer.prompt") == "harness"
    assert layer_routing.edit_layer("cross_class:cross_class:workflow.edge.x") == ""
    assert layer_routing.edit_layer("cross_class:") == ""


# ── route ────────────────────────────────────────────────────────────────────

def test_route_result_shape():
    verdict = layer_routing.route({"layer": "kickoff"}, "agent.x.prompt")
    assert set(verdict) == {"admit", "edit_layer", "diagnosed_layer", "reroute", "reason"}
    assert isinstance(verdict["admit"], bool)


def test_route_no_diagnosis():
    assert layer_routing.route(None, "agent.x.prompt") == {
        "admit": True, "edit_layer": "harness", "diagnosed_layer": "",
        "reroute": "", "reason": "no diagnosis",
    }
    assert layer_routing.route("kickoff", "agent.x.prompt")["reason"] == "no diagnosis"
    assert layer_routing.route(["kickoff"], "agent.x.prompt")["reason"] == "no diagnosis"


def test_route_untyped_target_wins():
    assert layer_routing.route({"layer": "model"}, "verifier@v1") == {
        "admit": True, "edit_layer": "", "diagnosed_layer": "model",
        "reroute": "", "reason": "untyped target",
    }


def test_route_unclassified_pass_through():
    for diagnosis in ({"layer": "unclassified"}, {"layer": ""}, {}, {"layer": None}):
        verdict = layer_routing.route(diagnosis, "agent.x.prompt")
        assert verdict["admit"] is True, diagnosis
        assert verdict["reason"] == "unclassified diagnosis", diagnosis


def test_route_layer_match():
    assert layer_routing.route({"layer": "change"}, "workflow.edge.x") == {
        "admit": True, "edit_layer": "harness", "diagnosed_layer": "change",
        "reroute": "", "reason": "layer match",
    }


def test_route_kickoff_refusal_exact():
    assert layer_routing.route(
        {"layer": "kickoff", "routes_to": "kickoff_repair"}, "agent.x.prompt"
    ) == {
        "admit": False, "edit_layer": "harness", "diagnosed_layer": "kickoff",
        "reroute": "kickoff_repair",
        "reason": "layer mismatch: a harness directive cannot fix a "
                  "kickoff-layer failure (routes_to=kickoff_repair)",
    }


def test_route_every_non_change_layer_refused():
    for layer in ("model", "provider", "environment", "engine", "user", "infra", "bogus"):
        verdict = layer_routing.route(
            {"layer": layer, "routes_to": "somewhere"}, "agent.reviewer.prompt"
        )
        assert verdict["admit"] is False
        assert verdict["edit_layer"] == "harness"
        assert verdict["diagnosed_layer"] == layer
        assert verdict["reroute"] == "somewhere"
        assert verdict["reason"].startswith(
            f"layer mismatch: a harness directive cannot fix a {layer}-layer failure"
        )


def test_route_refusal_without_routes_to():
    verdict = layer_routing.route({"layer": "engine"}, "verifier.obs_smoke")
    assert verdict["admit"] is False
    assert verdict["reroute"] == ""
    assert verdict["reason"].endswith("(routes_to=none)")


def test_route_diagnosed_layer_always_reported():
    for diagnosis in ({"layer": "kickoff", "routes_to": "r"}, {"layer": "model"},
                      {"layer": "change"}, {"layer": ""}):
        verdict = layer_routing.route(diagnosis, "agent.x.prompt")
        assert verdict["diagnosed_layer"] == (diagnosis.get("layer") or ""), diagnosis


# ── trace_run_id ─────────────────────────────────────────────────────────────

def test_trace_run_id(tmp_path):
    home, db = make_home(tmp_path, null_run_id_trace=True)
    assert layer_routing.trace_run_id(str(db), "tr-1") == "run-1"
    assert layer_routing.trace_run_id(str(db), "tr-missing") == ""
    assert layer_routing.trace_run_id(str(db), "tr-null") == ""
    assert layer_routing.trace_run_id(str(tmp_path / "no" / "such.db"), "tr-1") == ""


# ── run_diagnosis ────────────────────────────────────────────────────────────

def test_run_diagnosis_empty_run_id(tmp_path):
    home, db = make_home(tmp_path)
    assert layer_routing.run_diagnosis(str(home), "", str(db)) is None


def test_run_diagnosis_returns_dict_for_failed(tmp_path):
    home, db = make_home(tmp_path, status="failed", layer="kickoff",
                         routes_to="kickoff_repair")
    dx = layer_routing.run_diagnosis(str(home), "run-1", str(db))
    assert dx == {"layer": "kickoff", "routes_to": "kickoff_repair",
                  "rule_id": "R0", "source": "rule"}


def test_run_diagnosis_unsuccessful_statuses(tmp_path):
    for status in ("published", "superseded", "executing"):
        home, db = make_home(tmp_path / status, status=status)
        assert layer_routing.run_diagnosis(str(home), "run-1", str(db)) is None, status


def test_run_diagnosis_no_file(tmp_path):
    home, db = make_home(tmp_path, status="failed", diagnosis="none")
    assert layer_routing.run_diagnosis(str(home), "run-1", str(db)) is None


def test_run_diagnosis_non_object_and_unreadable(tmp_path):
    home, db = make_home(tmp_path, status="failed", diagnosis=["not", "an", "object"])
    assert layer_routing.run_diagnosis(str(home), "run-1", str(db)) is None

    home2, db2 = make_home(tmp_path=tmp_path / "b", status="failed", diagnosis="dict")
    (home2 / "runs" / "run-1" / DIAGNOSIS_FILE).write_text("{not json")
    assert layer_routing.run_diagnosis(str(home2), "run-1", str(db2)) is None


def test_run_diagnosis_unknown_run_and_no_table(tmp_path):
    home, db = make_home(tmp_path)
    assert layer_routing.run_diagnosis(str(home), "run-nobody", str(db)) is None
    home2, db2 = make_home(tmp_path / "b", task_runs=False)
    assert layer_routing.run_diagnosis(str(home2), "run-1", str(db2)) is None


# ── check_gradient ───────────────────────────────────────────────────────────

def test_check_gradient_refused_full_fixture(tmp_path):
    home, db = make_home(tmp_path, status="failed", layer="kickoff",
                         routes_to="kickoff_repair")
    verdict = layer_routing.check_gradient(
        {"target": "agent.reviewer.prompt"}, trace_id="tr-1", db=str(db), home=str(home)
    )
    assert set(verdict) == {
        "admit", "edit_layer", "diagnosed_layer", "reroute", "reason",
        "trace_id", "run_id", "target",
    }
    assert verdict["admit"] is False
    assert verdict["edit_layer"] == "harness"
    assert verdict["diagnosed_layer"] == "kickoff"
    assert verdict["reroute"] == "kickoff_repair"
    assert verdict["reason"] == (
        "layer mismatch: a harness directive cannot fix a kickoff-layer failure "
        "(routes_to=kickoff_repair)"
    )
    assert verdict["trace_id"] == "tr-1"
    assert verdict["run_id"] == "run-1"
    assert verdict["target"] == "agent.reviewer.prompt"


def test_check_gradient_missing_target_admits(tmp_path):
    home, db = make_home(tmp_path, status="failed", layer="kickoff")
    verdict = layer_routing.check_gradient(
        {"signal": "s"}, trace_id="tr-1", db=str(db), home=str(home)
    )
    assert verdict["admit"] is True
    assert verdict["target"] == ""
    assert verdict["reason"] == "untyped target"


def test_check_gradient_missing_trace_admits(tmp_path):
    home, db = make_home(tmp_path, status="failed", layer="kickoff")
    verdict = layer_routing.check_gradient(
        {"target": "agent.reviewer.prompt"}, trace_id="tr-nobody", db=str(db),
        home=str(home),
    )
    assert verdict["admit"] is True
    assert verdict["run_id"] == ""
    assert verdict["reason"] == "no diagnosis"


# ── record_refusal ───────────────────────────────────────────────────────────

def test_record_refusal_appends_sorted_lines(tmp_path):
    home = tmp_path / "home"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    ref_file = run_dir / "layer-refusals.jsonl"

    assert layer_routing.record_refusal(
        str(home), "run-1", {"z_last": 1, "a_first": 2, "mode": "on"}
    ) is True
    assert layer_routing.record_refusal(str(home), "run-1", {"mode": "shadow"}) is True

    text = ref_file.read_text()
    lines = text.splitlines()
    assert len(lines) == 2
    assert text.endswith("\n")
    for line in lines:
        obj = json.loads(line)
        assert list(obj.keys()) == sorted(obj.keys())
    assert json.loads(lines[0]) == {"z_last": 1, "a_first": 2, "mode": "on"}


def test_record_refusal_false_on_bad_run(tmp_path):
    home = tmp_path / "home"
    (home / "runs" / "run-1").mkdir(parents=True)
    assert layer_routing.record_refusal(str(home), "", {"a": 1}) is False
    assert layer_routing.record_refusal(str(home), "run-nobody", {"a": 1}) is False


def test_record_refusal_false_on_oserror(tmp_path):
    home = tmp_path / "home"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "layer-refusals.jsonl").mkdir()
    assert layer_routing.record_refusal(str(home), "run-1", {"a": 1}) is False


# ── Integration through reflection_extract_gradients ─────────────────────────

def seed(tmp_path, monkeypatch, *, status="failed", layer="kickoff",
         routes_to="kickoff_repair", diagnosis="dict"):
    """Build the fixture home and patch the gradient seams; return (home, stored)."""
    home, db = make_home(
        tmp_path, status=status, layer=layer, routes_to=routes_to, diagnosis=diagnosis
    )
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MO_LAYER_ROUTING", raising=False)
    monkeypatch.delenv("MO_REFLECTION_BATCH", raising=False)

    stored = []
    monkeypatch.setattr(reflection_pipeline, "_gradient_ensure_table", lambda: None)
    monkeypatch.setattr(reflection_pipeline, "_gradient_store", stored.append)
    monkeypatch.setattr(
        reflection_pipeline,
        "_gradient_extract",
        lambda tid: [json.dumps({
            "target": "agent.reviewer.prompt", "signal": "s",
            "suggested_change": "c", "evidence": tid, "confidence": 0.7,
        })],
    )
    return home, stored


def refusals(home):
    path = home / "runs" / "run-1" / "layer-refusals.jsonl"
    if not path.exists():
        return None
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_layer_routing_imported_at_module_top():
    assert reflection_pipeline.layer_routing is layer_routing


def test_layer_check_refuses_and_carries_home(tmp_path, monkeypatch):
    home, _ = seed(tmp_path, monkeypatch)
    verdict = reflection_pipeline._layer_check(
        json.dumps({"target": "agent.reviewer.prompt"}), "tr-1"
    )
    assert verdict is not None
    assert verdict["admit"] is False
    assert verdict["diagnosed_layer"] == "kickoff"
    assert verdict["reroute"] == "kickoff_repair"
    assert verdict["_home"] == str(home)


def test_layer_check_home_falls_back_to_db_parent(tmp_path, monkeypatch):
    home, _ = seed(tmp_path, monkeypatch)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    verdict = reflection_pipeline._layer_check(
        json.dumps({"target": "agent.reviewer.prompt"}), "tr-1"
    )
    assert verdict is not None
    assert verdict["_home"] == str(home)


def test_layer_check_none_when_db_unset(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert reflection_pipeline._layer_check(
        json.dumps({"target": "agent.reviewer.prompt"}), "tr-1"
    ) is None


def test_layer_check_requires_json_object(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    assert reflection_pipeline._layer_check(json.dumps(["not", "an", "object"]), "tr-1") is None
    assert reflection_pipeline._layer_check("{not json", "tr-1") is None


def test_layer_check_admits_matching_layer(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch, layer="change")
    verdict = reflection_pipeline._layer_check(
        json.dumps({"target": "agent.reviewer.prompt"}), "tr-1"
    )
    assert verdict is not None
    assert verdict["admit"] is True
    assert verdict["reason"] == "layer match"


def test_refused_when_on(tmp_path, monkeypatch, capsys):
    home, stored = seed(tmp_path, monkeypatch, status="failed", layer="kickoff")
    monkeypatch.setenv("MO_LAYER_ROUTING", "on")

    reflection_pipeline.reflection_extract_gradients(0)

    assert stored == []
    out, err = capsys.readouterr()
    assert "agent.reviewer.prompt" not in out
    assert "layer-refused agent.reviewer.prompt from tr-1" in err
    assert ("layer mismatch: a harness directive cannot fix a kickoff-layer failure "
            "(routes_to=kickoff_repair)") in err
    assert "reflection_extract_gradients: extracted 0 gradients since 0" in err

    records = refusals(home)
    assert records is not None and len(records) == 1
    rec = records[0]
    assert rec["admit"] is False
    assert rec["diagnosed_layer"] == "kickoff"
    assert rec["reroute"] == "kickoff_repair"
    assert rec["target"] == "agent.reviewer.prompt"
    assert rec["trace_id"] == "tr-1"
    assert rec["run_id"] == "run-1"
    assert rec["mode"] == "on"
    assert isinstance(rec["ts"], int)
    assert abs(rec["ts"] - int(time.time())) < 3600
    assert "_home" not in rec


def test_layer_refused_summary_line_and_order(tmp_path, monkeypatch, capsys):
    seed(tmp_path, monkeypatch, status="failed", layer="kickoff")
    monkeypatch.setenv("MO_LAYER_ROUTING", "on")

    reflection_pipeline.reflection_extract_gradients(0)

    _, err = capsys.readouterr()
    summary = "1 gradient(s) layer-refused (MO_LAYER_ROUTING=on)"
    assert summary in err
    final = "reflection_extract_gradients: extracted 0 gradients since 0"
    assert final in err
    assert err.index(summary) < err.index(final)


def test_shadow_stores_and_records(tmp_path, monkeypatch, capsys):
    home, stored = seed(tmp_path, monkeypatch, status="failed", layer="kickoff")
    monkeypatch.setenv("MO_LAYER_ROUTING", "shadow")

    reflection_pipeline.reflection_extract_gradients(0)

    assert len(stored) == 1
    out, err = capsys.readouterr()
    assert "agent.reviewer.prompt" in out
    assert "reflection_extract_gradients: extracted 1 gradients since 0" in err
    records = refusals(home)
    assert records is not None and len(records) == 1
    assert records[0]["mode"] == "shadow"
    assert records[0]["admit"] is False


def test_unset_is_shadow(tmp_path, monkeypatch):
    home, stored = seed(tmp_path, monkeypatch, status="failed", layer="kickoff")
    monkeypatch.delenv("MO_LAYER_ROUTING", raising=False)

    reflection_pipeline.reflection_extract_gradients(0)

    assert len(stored) == 1
    records = refusals(home)
    assert records is not None and len(records) == 1
    assert records[0]["mode"] == "shadow"


def test_off_does_nothing(tmp_path, monkeypatch):
    home, stored = seed(tmp_path, monkeypatch, status="failed", layer="kickoff")
    monkeypatch.setenv("MO_LAYER_ROUTING", "off")

    reflection_pipeline.reflection_extract_gradients(0)

    assert len(stored) == 1
    assert refusals(home) is None


def test_matching_layer_passes(tmp_path, monkeypatch):
    home, stored = seed(tmp_path, monkeypatch, status="failed", layer="change")
    monkeypatch.setenv("MO_LAYER_ROUTING", "on")

    reflection_pipeline.reflection_extract_gradients(0)

    assert len(stored) == 1
    assert refusals(home) is None


def test_published_run_passes(tmp_path, monkeypatch):
    home, stored = seed(tmp_path, monkeypatch, status="published", layer="kickoff")
    monkeypatch.setenv("MO_LAYER_ROUTING", "on")

    reflection_pipeline.reflection_extract_gradients(0)

    assert len(stored) == 1
    assert refusals(home) is None


def test_no_diagnosis_passes(tmp_path, monkeypatch):
    home, stored = seed(tmp_path, monkeypatch, status="failed", diagnosis="none")
    monkeypatch.setenv("MO_LAYER_ROUTING", "on")

    reflection_pipeline.reflection_extract_gradients(0)

    assert len(stored) == 1
    assert refusals(home) is None
