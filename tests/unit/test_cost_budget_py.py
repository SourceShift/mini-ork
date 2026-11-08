"""Per-run budget policy: the declared ceiling, and the gate that honours it.

The three failures these tests pin:

* a ceiling that is declared but unreadable must REFUSE, never quietly become
  "unlimited" (the fail-open direction the module exists to prevent);
* the ceiling is per RUN — a run at $11 of $12 parks while a sibling run with
  the same spend and a $50 ceiling keeps going;
* the marker the IDE and the attention inbox read is kept truthful, so a run
  that fits again after a ceiling raise stops asking for a person.
"""
from __future__ import annotations

import json
import sqlite3
import sys

import pytest


def _db(tmp_path, run_id="r1", rows=(10.0,)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS llm_calls (provider TEXT, model_id TEXT, "
        "tier TEXT, feature_name TEXT, run_id TEXT, cost_usd REAL, status TEXT)")
    for cost in rows:
        con.execute(
            "INSERT INTO llm_calls (provider,model_id,tier,feature_name,run_id,"
            "cost_usd,status) VALUES ('anthropic','opus','smart','panel',?,?,'success')",
            (run_id, cost))
    con.commit()
    con.close()
    return path


def _cost_budget():
    from mini_ork.cost import budget

    return budget


def test_no_ceiling_declared_is_unlimited(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_RUN_BUDGET_USD", raising=False)
    monkeypatch.delenv("MO_BUDGET_POLICY", raising=False)
    db = _db(tmp_path)
    v = _cost_budget().verdict(db, "r1", tmp_path / "run")
    assert v.budget is None
    assert v.exhausted is False
    assert v.spent == 10.0
    assert "no per-run budget" in v.detail


def test_under_the_ceiling_proceeds(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_RUN_BUDGET_USD", "25")
    db = _db(tmp_path)
    v = _cost_budget().verdict(db, "r1", tmp_path / "run")
    assert (v.spent, v.budget, v.exhausted) == (10.0, 25.0, False)


def test_at_or_over_the_ceiling_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_RUN_BUDGET_USD", "10")
    db = _db(tmp_path)
    v = _cost_budget().verdict(db, "r1", tmp_path / "run")
    assert v.exhausted is True
    assert v.budget == 10.0
    assert "spent $10.00 of a $10.00 budget" in v.detail


def test_the_ceiling_is_per_run(tmp_path, monkeypatch):
    """Same ledger, same ceiling knob — but each run is answered by its own rows."""
    monkeypatch.setenv("MO_RUN_BUDGET_USD", "12")
    db = _db(tmp_path, run_id="r1", rows=(6.0, 6.5))
    assert _cost_budget().verdict(db, "r1", None).exhausted is True
    db2 = _db(tmp_path / "other", run_id="r2", rows=(6.0,))
    assert _cost_budget().verdict(db2, "r2", None).exhausted is False


def test_an_unparseable_ceiling_refuses_fail_closed(tmp_path, monkeypatch):
    """A typo is a refusal, not a silent 'unlimited'."""
    monkeypatch.setenv("MO_RUN_BUDGET_USD", "twelve")
    db = _db(tmp_path)
    v = _cost_budget().verdict(db, "r1", tmp_path / "run")
    assert v.exhausted is True
    assert v.budget is None
    assert "MO_RUN_BUDGET_USD='twelve'" in v.detail


@pytest.mark.parametrize("bad", ["0", "-5", "nan", "inf"])
def test_a_non_positive_or_infinite_ceiling_refuses(bad, tmp_path, monkeypatch):
    monkeypatch.setenv("MO_RUN_BUDGET_USD", bad)
    db = _db(tmp_path)
    v = _cost_budget().verdict(db, "r1", tmp_path / "run")
    assert v.exhausted is True, bad


def test_the_run_profile_carries_the_ceiling_and_the_env_overrides_it(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_RUN_BUDGET_USD", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({"budget_usd": 5}))
    db = _db(tmp_path)
    assert _cost_budget().verdict(db, "r1", run_dir).exhausted is True
    monkeypatch.setenv("MO_RUN_BUDGET_USD", "50")
    v = _cost_budget().verdict(db, "r1", run_dir)
    assert (v.budget, v.exhausted) == (50.0, False)


def test_a_broken_profile_ceiling_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_RUN_BUDGET_USD", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({"budget_usd": "a lot"}))
    db = _db(tmp_path)
    v = _cost_budget().verdict(db, "r1", run_dir)
    assert v.exhausted is True
    assert "budget_usd='a lot'" in v.detail


def test_an_unreadable_ledger_reads_as_zero(tmp_path, monkeypatch):
    """No DB yet is a fresh home, not a refusal — same choice the daily cap makes."""
    monkeypatch.setenv("MO_RUN_BUDGET_USD", "1")
    v = _cost_budget().verdict(tmp_path / "missing.db", "r1", None)
    assert (v.spent, v.exhausted) == (0.0, False)


def test_a_registered_policy_is_asked(tmp_path, monkeypatch):
    budget = _cost_budget()
    asked: list[tuple] = []

    def _policy(db, run_id, run_dir):
        asked.append((run_id, run_dir))
        return budget.Verdict(spent=99.0, budget=100.0, exhausted=True,
                              detail="the vendor quota says stop")

    budget.register_budget_policy("test-quota", _policy)
    monkeypatch.setenv("MO_BUDGET_POLICY", "test-quota")
    monkeypatch.delenv("MO_RUN_BUDGET_USD", raising=False)
    db = _db(tmp_path)
    v = budget.verdict(db, "r1", tmp_path / "run")
    assert v.exhausted is True and v.detail == "the vendor quota says stop"
    assert asked == [("r1", tmp_path / "run")]
    # The name is unselected again for the rest of the session.
    monkeypatch.delenv("MO_BUDGET_POLICY")
    del budget.BUDGET_POLICIES["test-quota"]


def test_an_unknown_policy_refuses_rather_than_falling_back_to_unlimited(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_BUDGET_POLICY", "no-such-policy")
    db = _db(tmp_path)
    v = _cost_budget().verdict(db, "r1", None)
    assert v.exhausted is True
    assert "unknown budget policy 'no-such-policy'" in v.detail


def test_a_raising_policy_refuses(tmp_path, monkeypatch):
    budget = _cost_budget()

    def _boom(db, run_id, run_dir):
        raise RuntimeError("quota service unreachable")

    budget.register_budget_policy("test-boom", _boom)
    monkeypatch.setenv("MO_BUDGET_POLICY", "test-boom")
    try:
        v = budget.verdict(_db(tmp_path), "r1", None)
    finally:
        del budget.BUDGET_POLICIES["test-boom"]
    assert v.exhausted is True
    assert "quota service unreachable" in v.detail


def test_the_marker_is_written_then_removed_when_the_run_fits_again(tmp_path, monkeypatch):
    budget = _cost_budget()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_RUN_BUDGET_USD", "5")
    v = budget.verdict(_db(tmp_path), "r1", run_dir)
    budget.record(run_dir, v)
    payload = json.loads((run_dir / ".budget-exhausted").read_text())
    assert payload["spent_usd"] == 10.0 and payload["budget_usd"] == 5.0
    assert budget.read_marker(run_dir)["detail"] == v.detail

    monkeypatch.setenv("MO_RUN_BUDGET_USD", "500")  # the operator raised it
    budget.record(run_dir, budget.verdict(_db(tmp_path), "r1", run_dir))
    assert not (run_dir / ".budget-exhausted").exists()
    assert budget.read_marker(run_dir) is None


def test_record_is_a_noop_without_a_run_dir():
    budget = _cost_budget()
    budget.record(None, budget.Verdict(spent=1.0, budget=1.0, exhausted=True, detail="x"))


# ── the gate ───────────────────────────────────────────────────────────────


def _gate(tmp_path, monkeypatch, *, budget_usd, rows=(10.0,)):
    """Run the dispatch gate's helper and return what it decided."""
    from mini_ork.cli import execute_handlers as eh

    monkeypatch.setenv("MO_NODE_FINISH_REASON", "")  # let monkeypatch restore it
    monkeypatch.setenv("MO_RUN_BUDGET_USD", budget_usd)
    monkeypatch.delenv("MO_BUDGET_POLICY", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    return eh._budget_gate(_db(tmp_path), "r1", str(run_dir), "implementer"), run_dir


def test_the_gate_parks_the_run_and_records_why(tmp_path, monkeypatch, capsys):
    from mini_ork.gates import node_review

    decided, run_dir = _gate(tmp_path, monkeypatch, budget_usd="5")
    assert decided == (node_review.REVIEW_PAUSE_RC, "budget_exhausted")
    assert (run_dir / ".budget-exhausted").is_file()
    err = capsys.readouterr().err
    assert "[budget]" in err and "MO_RUN_BUDGET_USD" in err
    assert os_environ("MO_NODE_FINISH_REASON") == "budget_exhausted"


def test_the_gate_lets_a_run_under_its_ceiling_through(tmp_path, monkeypatch):
    decided, run_dir = _gate(tmp_path, monkeypatch, budget_usd="50")
    assert decided is None
    assert not (run_dir / ".budget-exhausted").exists()


def test_the_gate_clears_a_marker_left_by_a_raised_ceiling(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".budget-exhausted").write_text("{}")
    decided, _ = _gate(tmp_path, monkeypatch, budget_usd="50")
    assert decided is None
    assert not (run_dir / ".budget-exhausted").exists()


def os_environ(key: str) -> str:
    import os

    return os.environ.get(key, "")


# ── the surfaces that read the marker ──────────────────────────────────────


def test_attention_reports_the_budget_before_the_cost_pause(tmp_path):
    from mini_ork import attention

    home = tmp_path / "home"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _cost_budget().record(run_dir, _cost_budget().Verdict(
        spent=12.0, budget=12.0, exhausted=True, detail="spent $12.00 of a $12.00 budget"))
    (run_dir / ".cost-pause").write_text("")  # both holds present

    stage = attention.stage_of(home, "run-1")
    assert stage.key == "budget_exhausted"
    assert stage.state == attention.STATE_PERSON
    assert "12.00" in stage.why


def test_the_budget_row_has_a_command_that_exists():
    from mini_ork import attention

    row = attention.taxonomy("budget_exhausted")
    assert row.state == attention.STATE_PERSON
    assert row.command == "mini-ork resume {run}"
    # the first row of the table is the most consequential
    assert attention.TAXONOMY[0].key == "budget_exhausted"


def test_the_finish_reason_maps_to_an_input_required_checkpoint():
    from mini_ork.stores import checkpoints

    assert checkpoints.failure_class_for_finish_reason("budget_exhausted") == "input_required"


def test_the_package_is_importable_from_the_runtime_root():
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
    from mini_ork.cost import budget

    assert budget.BUDGET_ENV == "MO_RUN_BUDGET_USD"
