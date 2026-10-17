"""Tests for the pre-run lane-health gate (:mod:`mini_ork.cli.lane_preflight`).

The gate refuses a run whose DAG would dispatch against a starved lane —
``opus`` with no credit, ``deepseek`` 402 — before any node is started.
Composition (kickoff lane-health-gate §2):

  1. :func:`resolve_dispatch_lanes` — alias → routed → family.
  2. :func:`gate` — pure decision over ``providers.preflight``.
  3. :func:`enforce` — pure + abort side effects (artifact, DB, stderr).

These tests guard the boundary the gate exists for: a healthy gate surfaces
the failed lane, names the starved node, attaches a non-empty suggestion list,
and stays pure (no DB writes, no printing) so it is itself unit-testable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import lane_preflight  # noqa: E402


# Field tuple layout mirrors ``WorkflowNode.dispatch_fields``: field[6] is
# ``model_lane or type`` and field[1] is the node type. Real workflows build
# these from ``(e.split(_SEP) + [""] * 8)[:8]``; the tuple below matches
# ``execute.py:879``.
def _field(node_id, node_type, model_lane=""):
    empty = ""
    return (node_id, node_type, empty, empty, empty, empty,
            model_lane or node_type, empty)


# ── resolve chain ────────────────────────────────────────────────────────────


def test_resolve_dispatch_lanes_walks_alias_to_routed_to_family(tmp_path, monkeypatch):
    """A node field tuple is mapped to a :class:`DispatchLane` whose
    ``model`` is the routing-policy output resolved through
    :func:`resolve_lane_family` (alias → routed → family). The test process
    inherits the project's ``MINI_ORK_HOME``, so the resolver reads the
    team template; this test pins that the structure (workflow_lane,
    routed_lane, model) is populated end-to-end, without depending on a
    particular overlay file in the dev tree."""
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))

    fields = [_field("r1", "researcher", "codex_lens"),
              _field("r2", "researcher", "opus_lens"),
              _field("r3", "reviewer", "reviewer")]

    out = lane_preflight.resolve_dispatch_lanes(
        fields, root=REPO, home="", task_class="")

    assert [dl.node_id for dl in out] == ["r1", "r2", "r3"]
    for dl in out:
        assert dl.workflow_lane  # always populated by ``field[6] or type``
        assert dl.routed_lane     # routing policy produced a value
        assert dl.model           # resolve_lane_family produced a model name


def test_pinned_node_short_circuits_routing(tmp_path, monkeypatch):
    """A node whose ``model_lane`` is NOT the node type is an explicit recipe
    pin. ``policy_route_lane`` records ``source=pinned`` and returns the lane
    verbatim. The gate MUST honour it so a recipe pin is not silently
    re-routed by a learning-governed policy."""
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.delenv("MO_ROUTING_POLICY", raising=False)

    # ``minimax`` is a real lane in providers.yaml; pinning it on an
    # ``implementer`` test fixture forces the pin path.
    fields = [_field("pinned", "implementer", "minimax")]
    out = lane_preflight.resolve_dispatch_lanes(
        fields, root=REPO, home="", task_class="code-fix")

    assert out[0].node_id == "pinned"
    assert out[0].workflow_lane == "minimax"
    # Resolved model must contain the pin (either identity-resolved by the
    # alias or kept verbatim by the family resolver).
    assert "minimax" in out[0].model


# ── gate(): pure decision ────────────────────────────────────────────────────


def test_gate_is_pure_no_db_no_print(tmp_path, monkeypatch, capsys):
    """``gate()`` must be pure by contract: no ``task_runs`` mutation, no
    printing, no file writes. Calling it twice on the same input is
    deterministic and side-effect free (acceptance criterion 4)."""
    home = tmp_path / "home"; home.mkdir()
    db = home / "state.db"
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ,
                        "MINI_ORK_HOME": str(home),
                        "MINI_ORK_DB": str(db)},
                   capture_output=True, text=True, check=True)

    # A healthy compat lane should produce no failures AND not touch the DB.
    fields = [_field("n1", "researcher", "glm")]
    env = {"GLM_API_KEY": "sk-test", "MINI_ORK_HOME": str(home),
           "MINI_ORK_DB": str(db), "MINI_ORK_ROOT": str(REPO),
           "MO_LANE_HEALTH_GATE": "1"}

    a = lane_preflight.gate(fields, root=REPO, home=str(home),
                            task_class="", environ=env)
    b = lane_preflight.gate(fields, root=REPO, home=str(home),
                            task_class="", environ=env)
    assert a == b == []

    # No DB mutation, no stdout/stderr noise: the gate stays a pure function.
    con = sqlite3.connect(db)
    rows = con.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    con.close()
    assert rows == 0
    assert capsys.readouterr().out == ""
    assert capsys.readouterr().err == ""


def test_gate_skips_enforcement_when_env_var_is_zero(tmp_path, monkeypatch):
    """``MO_LANE_HEALTH_GATE=0`` is the documented escape hatch. It MUST
    short-circuit before ``preflight`` is consulted so a starved lane does
    not burn the probe round-trip."""
    calls = []

    import mini_ork.dispatch.providers as providers_mod

    def spy(models, root=None, environment=None):
        calls.append(list(models))
        return {}

    monkeypatch.setattr(providers_mod, "preflight", spy)

    env = {"GLM_API_KEY": "set", "MINI_ORK_HOME": str(tmp_path / "h"),
           "MINI_ORK_ROOT": str(REPO), "MO_LANE_HEALTH_GATE": "0"}
    fields = [_field("n1", "researcher", "glm")]

    out = lane_preflight.gate(fields, root=REPO, home=str(tmp_path / "h"),
                              environ=env)
    assert out == []
    assert calls == []  # preflight MUST NOT have been called


def test_gate_yields_one_failure_per_unhealthy_lane_per_node(tmp_path, monkeypatch):
    """The fix-from-the-message contract: a starvation names every waiting
    node, not just one. Two nodes sharing the starved ``glm`` lane produce
    two failures, each carrying the same ``reason``."""
    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth

    def fake_preflight(models, root=None, environment=None):
        return {"glm": LaneHealth(False,
            "glm: out of credit (HTTP 402) at https://gw/v1/messages"
            " — every dispatch on this lane would fail")}
    monkeypatch.setattr(providers_mod, "preflight", fake_preflight)
    # The suggestion list is drawn from the effective providers.yaml, whose
    # resolution order is $MINI_ORK_PROVIDERS → <home>/config/providers.yaml →
    # $MINI_ORK_ROOT/config/providers.yaml (lane_suggest._providers). ``home``
    # here is an empty tmp dir, so without this the lane universe is whatever
    # the ambient shell happens to export — empty under a bare pytest, which
    # silently emptied ``suggestions`` and made this test env-dependent. Pin
    # the root the same way the sibling resolve test above does.
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))

    fields = [_field("n1", "researcher", "glm"),
              _field("n2", "verifier", "glm")]

    out = lane_preflight.gate(fields, root=REPO, home=str(tmp_path / "h"))

    assert len(out) == 2
    assert {f.node_id for f in out} == {"n1", "n2"}
    for f in out:
        assert f.lane == "glm"
        assert "out of credit" in f.reason
        assert isinstance(f.suggestions, tuple)
        assert f.suggestions  # non-empty


def test_gate_dedups_preflight_probe_by_model(tmp_path, monkeypatch):
    """``preflight`` is called with the *deduped* model set, while every
    waiting node survives on the returned ``LaneFailure`` list. Three nodes
    on the same lane MUST probe once and emit three failures."""
    seen = []

    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth

    def spy(models, root=None, environment=None):
        seen.append(list(models))
        return {m: LaneHealth(False, f"{m}: out of credit") for m in models}

    monkeypatch.setattr(providers_mod, "preflight", spy)

    fields = [_field(f"n{i}", "researcher", "glm") for i in range(3)]
    out = lane_preflight.gate(fields, root=REPO, home=str(tmp_path / "h"))

    assert seen == [["glm"]]
    assert len(out) == 3
    assert {f.node_id for f in out} == {"n0", "n1", "n2"}


# ── enforce(): side-effect half ──────────────────────────────────────────────


def test_enforce_returns_empty_string_when_healthy(tmp_path, monkeypatch):
    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth

    def fake_preflight(models, root=None, environment=None):
        return {m: LaneHealth(True, "ok") for m in models}
    monkeypatch.setattr(providers_mod, "preflight", fake_preflight)

    out = lane_preflight.enforce(
        [_field("n1", "researcher", "glm")],
        root=REPO, home=str(tmp_path / "h"),
    )
    assert out == ""


def test_enforce_writes_lane_preflight_json(tmp_path, monkeypatch):
    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth

    def fake_preflight(models, root=None, environment=None):
        return {"glm": LaneHealth(False,
            "glm: out of credit (HTTP 402) at https://gw"
            " — every dispatch on this lane would fail")}
    monkeypatch.setattr(providers_mod, "preflight", fake_preflight)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    rc = lane_preflight.enforce(
        [_field("n1", "researcher", "glm")],
        root=REPO, home=str(tmp_path / "h"),
        run_dir=str(run_dir),
    )
    assert rc == "lane_unhealthy"
    payload = json.loads((run_dir / "lane-preflight.json").read_text())
    assert payload["verdict"] == "blocked"
    assert payload["blocked_reason"] == "lane_unhealthy"
    assert len(payload["failures"]) == 1
    assert payload["failures"][0]["lane"] == "glm"
    assert payload["failures"][0]["node_id"] == "n1"
    assert isinstance(payload["failures"][0]["suggestions"], list)


def test_enforce_marks_task_runs_failed_and_writes_run_event(tmp_path):
    """The abort contract: ``task_runs.status='failed'``, ``verdict`` falls
    back to ``'ESCALATE'`` (the only permitted enum), plus one
    ``run_events`` row ``event_type='lane_preflight_blocked'``. Mirrors the
    shape :func:`_execute_gate_check` writes (execute.py:3435-3446)."""
    home = tmp_path / "home"; home.mkdir()
    db = home / "state.db"
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ,
                        "MINI_ORK_HOME": str(home),
                        "MINI_ORK_DB": str(db)},
                   capture_output=True, text=True, check=True)
    run_id = "lane-test-run-001"
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO task_runs(id, task_class, kickoff_path, status, verdict, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, "code-fix", "/tmp/kick.md", "executing", None, 0, 0),
    )
    con.commit()
    con.close()

    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth
    import unittest.mock as mock

    def fake_preflight(models, root=None, environment=None):
        return {m: LaneHealth(False, f"{m}: out of credit (HTTP 402)") for m in models}
    with mock.patch.object(providers_mod, "preflight", side_effect=fake_preflight):
        rc = lane_preflight.enforce(
            [_field("n1", "researcher", "glm")],
            root=REPO, home=str(home),
            db=str(db), run_id=run_id,
            run_dir=str(home / "runs" / run_id),
        )
    assert rc == "lane_unhealthy"

    con = sqlite3.connect(db)
    row = con.execute("SELECT status, verdict, notes FROM task_runs WHERE id=?",
                      (run_id,)).fetchone()
    con.close()
    assert row[0] == "failed"
    assert row[1] == "ESCALATE"
    assert "lane preflight" in (row[2] or "")

    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT event_type, payload_json FROM run_events "
        "WHERE run_id=? AND event_type='lane_preflight_blocked'",
        (run_id,),
    ).fetchall()
    con.close()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert payload["blocked_reason"] == "lane_unhealthy"


def test_enforce_emits_machine_lines_on_stdout(tmp_path, monkeypatch, capsys):
    """The lifecycle scrapes ``blocked_reason=…`` and ``verdict=…`` from the
    child stdout; the gate MUST print both plus the fold-able
    ``lane_preflight=<json>`` so rubric/verify/reflect skip cleanly (same
    shape as the budget preflight at main.py:1074)."""
    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth

    def fake_preflight(models, root=None, environment=None):
        return {m: LaneHealth(False, f"{m}: out of credit") for m in models}
    monkeypatch.setattr(providers_mod, "preflight", fake_preflight)

    lane_preflight.enforce(
        [_field("n1", "researcher", "glm")],
        root=REPO, home=str(tmp_path / "h"),
    )
    captured = capsys.readouterr()
    assert "blocked_reason=lane_unhealthy" in captured.out
    assert "verdict=blocked" in captured.out
    assert "lane_preflight=" in captured.out


def test_enforce_writes_no_artifact_when_run_dir_is_blank(tmp_path, monkeypatch):
    """A missing run dir must not crash the gate — same OSError discipline
    :func:`_execute_gate_check` uses for ``blocked.json``."""
    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth

    def fake_preflight(models, root=None, environment=None):
        return {m: LaneHealth(False, f"{m}: out of credit") for m in models}
    monkeypatch.setattr(providers_mod, "preflight", fake_preflight)

    # ``run_dir=""`` (or ``"."``) — no file written, no exception.
    out = lane_preflight.enforce(
        [_field("n1", "researcher", "glm")],
        root=REPO, home=str(tmp_path / "h"),
        run_dir="",
    )
    assert out == "lane_unhealthy"


# ── secret-store credential merge (regression) ───────────────────────────────


def test_gate_probes_with_credentials_from_secret_store(tmp_path, monkeypatch):
    """Regression: the gate MUST see credentials written to the persisted
    secret store (``<home>/config/secrets.local.sh`` — the file
    ``mini-ork providers configure`` writes via
    :func:`write_secret_exports`). ``dispatch_model`` reads that file at
    line providers.py:1438, so the gate's probe MUST too — otherwise a
    live lane the dispatcher would serve is pronounced dead and the run
    never starts (the failure mode the kickoff exists to close).

    The test sets up a real on-disk secret store, passes a test ``environ``
    that does NOT carry ``GLM_API_KEY``, spies on ``providers.preflight``,
    and asserts the environment argument ``preflight`` saw carried the
    secret-store credential."""
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    from mini_ork.dispatch.secrets import write_secret_exports
    write_secret_exports(
        {"GLM_API_KEY": "sk-from-store"},
        path=home / "config" / "secrets.local.sh",
    )

    captured: dict = {}

    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth

    def spy(models, root=None, environment=None):
        captured["env"] = dict(environment or {})
        return {m: LaneHealth(True, "ok") for m in models}

    monkeypatch.setattr(providers_mod, "preflight", spy)

    env = {"MINI_ORK_HOME": str(home)}  # NO GLM_API_KEY
    out = lane_preflight.gate(
        [_field("n1", "researcher", "glm")],
        root=REPO, home=str(home), environ=env,
    )
    assert out == []  # healthy lane does not block
    # The credential from the secret store MUST reach the probe; before the
    # fix the gate called ``context_env_snapshot()`` and only saw
    # ``MINI_ORK_HOME`` here.
    assert captured["env"].get("GLM_API_KEY") == "sk-from-store"


def test_gate_user_env_overrides_secret_store(tmp_path, monkeypatch):
    """A user-exported shell var deliberately wins over the persisted
    equivalent (mirrors :func:`provider_environment`'s contract verbatim —
    the docstring of which is the gate's spec). Smoke rotation / a one-off
    override must not be silently overwritten by a stale stored value."""
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    from mini_ork.dispatch.secrets import write_secret_exports
    write_secret_exports(
        {"GLM_API_KEY": "sk-stale"},
        path=home / "config" / "secrets.local.sh",
    )

    captured: dict = {}

    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth

    def spy(models, root=None, environment=None):
        captured["env"] = dict(environment or {})
        return {m: LaneHealth(True, "ok") for m in models}

    monkeypatch.setattr(providers_mod, "preflight", spy)

    env = {"MINI_ORK_HOME": str(home), "GLM_API_KEY": "sk-fresh"}
    lane_preflight.gate(
        [_field("n1", "researcher", "glm")],
        root=REPO, home=str(home), environ=env,
    )
    assert captured["env"].get("GLM_API_KEY") == "sk-fresh"


def test_gate_fails_closed_when_secret_store_is_unreadable(tmp_path):
    """``provider_environment`` raises :class:`SecretStoreError` when the
    resolved credential store is malformed/perm-wrong/a symlink.
    ``dispatch_model`` refuses every dispatch with rc=2 in this state — the
    gate MUST also block (a closed-listing run that cannot read its own
    credentials must not start). One failure per waiting node names the
    credential store as the reason; :func:`enforce` then writes the same
    rc=75 artifact shape as a starved lane."""
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    # A symlinked store trips the reader's "no symlinks" invariant —
    # the cheapest reproducible ``SecretStoreError`` trigger.
    real = tmp_path / "real-secrets.sh"
    real.write_text("export GLM_API_KEY=sk-test\n", encoding="utf-8")
    os.chmod(real, 0o600)
    (home / "config" / "secrets.local.sh").symlink_to(real)

    env = {"MINI_ORK_HOME": str(home)}
    out = lane_preflight.gate(
        [_field("n1", "researcher", "glm"),
         _field("n2", "verifier", "glm")],
        root=REPO, home=str(home), environ=env,
    )

    assert len(out) == 2  # one failure per waiting node
    assert {f.node_id for f in out} == {"n1", "n2"}
    for f in out:
        assert "credential store failed" in f.reason


# ── unverified(): the lanes nobody actually checked ──────────────────────────
# ``LaneHealth.ok`` means "not known bad". A native lane the CLI authenticates,
# a probe switched off, or an endpoint that never answered are all ok — and all
# unchecked. Those lanes are named here rather than silently read as verified.

RUN_ID = "lp-run-1"
UNVER_REASON = "ok (unprobed: MO_LANE_PROBE is off — key presence only)"


def _fake_preflight(monkeypatch, health_by_model):
    """Replace providers.preflight (the seam gate/unverified call) with a fake."""
    import mini_ork.dispatch.providers as providers_mod

    def fake(models, root=None, environment=None):
        return {m: health_by_model[m] for m in models if m in health_by_model}

    monkeypatch.setattr(providers_mod, "preflight", fake)


def _make_min_db(tmp_path):
    """A DB with just the two tables _persist_blocked touches."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT, "
            "verdict TEXT, notes TEXT, updated_at INTEGER, ended_at INTEGER)")
        con.execute(
            "CREATE TABLE run_events (event_id TEXT, run_id TEXT, "
            "event_type TEXT, payload_json TEXT, created_at INTEGER)")
        con.execute(
            "INSERT INTO task_runs (id, status) VALUES (?, 'executing')", (RUN_ID,))
        con.commit()
    finally:
        con.close()
    return str(db)


def _run_row(db):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT status, verdict, notes FROM task_runs WHERE id=?", (RUN_ID,)
        ).fetchone()
    finally:
        con.close()


def _events(db):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT event_type, payload_json FROM run_events").fetchall()
    finally:
        con.close()


def test_unverified_names_every_waiting_node(tmp_path, monkeypatch):
    """One ``LaneUnverified`` per waiting node, naming lane, role and node id —
    and ``gate`` still sees nothing (unverified is not a failure)."""
    import mini_ork.dispatch.providers as providers_mod
    _fake_preflight(monkeypatch, {
        "glm": providers_mod.LaneHealth(True, UNVER_REASON, False),
    })
    fields = [_field("n1", "researcher", "glm"), _field("n2", "verifier", "glm")]

    entries = lane_preflight.unverified(fields, root=REPO, home=str(tmp_path / "h"))

    assert [e.node_id for e in entries] == ["n1", "n2"]
    assert [e.role for e in entries] == ["researcher", "verifier"]
    assert all(e.lane == "glm" and e.reason == UNVER_REASON for e in entries)
    assert lane_preflight.gate(fields, root=REPO, home=str(tmp_path / "h")) == []


def test_unverified_missing_preflight_result(tmp_path, monkeypatch):
    """A model the probe returned no entry for is unverified, not dead."""
    _fake_preflight(monkeypatch, {})
    entries = lane_preflight.unverified(
        [_field("n1", "researcher", "glm")], root=REPO, home=str(tmp_path / "h"),
    )
    assert len(entries) == 1
    assert entries[0].reason == "no preflight result for this lane"


def test_failure_and_unverified_lists_are_disjoint(tmp_path, monkeypatch):
    """ok=False is a failure; ok=True + verified=False is unverified. Never
    both."""
    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth
    _fake_preflight(monkeypatch, {
        "glm": LaneHealth(False, "glm: out of credit (HTTP 402)"),
        "cod": LaneHealth(True, UNVER_REASON, False),
    })
    fields = [_field("n1", "researcher", "glm"), _field("n2", "planner", "cod")]

    failures = lane_preflight.gate(fields, root=REPO, home=str(tmp_path / "h"))
    entries = lane_preflight.unverified(fields, root=REPO, home=str(tmp_path / "h"))

    assert [f.node_id for f in failures] == ["n1"]
    assert [e.node_id for e in entries] == ["n2"]


def test_compat_lane_with_key_but_probe_off_is_unverified(tmp_path, monkeypatch):
    """The explicit compat fall-through: the key is present but no probe ran, so
    the lane is ok yet unverified — its reason says so."""
    from mini_ork.dispatch import providers as providers_mod

    reg = tmp_path / "providers.yaml"
    reg.write_text(
        "providers:\n"
        "  spec-compat-lane:\n"
        "    kind: anthropic-compat\n"
        "    base_url: http://127.0.0.1:9/v1\n"
        "    api_key_env: SPEC_COMPAT_KEY\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(reg))

    health = providers_mod.lane_health("spec-compat-lane", str(REPO), environment={
        "MINI_ORK_HOME": str(tmp_path / "home"),
        "SPEC_COMPAT_KEY": "sk-spec", "MO_LANE_PROBE": "0",
    })

    assert health.ok is True
    assert health.verified is False
    assert health.reason == UNVER_REASON


def test_enforce_warns_and_proceeds_on_unverified_lane(tmp_path, monkeypatch, capsys):
    """Default posture: an unverified lane warns, records the artifact with
    ``verdict: proceed``, and starts the run — no machine line, no DB write."""
    import mini_ork.dispatch.providers as providers_mod
    monkeypatch.delenv("MO_LANE_REQUIRE_VERIFICATION", raising=False)
    _fake_preflight(monkeypatch, {
        "glm": providers_mod.LaneHealth(True, UNVER_REASON, False),
    })
    db = _make_min_db(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rc = lane_preflight.enforce(
        [_field("n1", "researcher", "glm")],
        root=REPO, home=str(tmp_path / "h"),
        db=db, run_id=RUN_ID, run_dir=str(run_dir),
    )

    assert rc == ""
    payload = json.loads((run_dir / "lane-preflight.json").read_text(encoding="utf-8"))
    assert payload["verdict"] == "proceed"
    assert payload["blocked_reason"] == ""
    assert [e["node_id"] for e in payload["unverified"]] == ["n1"]
    captured = capsys.readouterr()
    assert any(
        ln.startswith("warn:") and "cannot verify:" in ln
        for ln in captured.err.splitlines()
    ), captured.err
    assert not any(
        ln.startswith(("blocked_reason=", "verdict=", "lane_preflight="))
        for ln in captured.out.splitlines()
    ), captured.out
    assert _run_row(db) == ("executing", None, None)
    assert _events(db) == []


def test_enforce_blocks_on_unverified_when_verification_required(
    tmp_path, monkeypatch, capsys
):
    """``MO_LANE_REQUIRE_VERIFICATION=1`` turns an unverifiable lane into an
    abort: rc string, artifact, task_runs failure, one run event, and the
    machine lines carrying the new reason."""
    import mini_ork.dispatch.providers as providers_mod
    monkeypatch.setenv("MO_LANE_REQUIRE_VERIFICATION", "1")
    _fake_preflight(monkeypatch, {
        "glm": providers_mod.LaneHealth(True, UNVER_REASON, False),
    })
    db = _make_min_db(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rc = lane_preflight.enforce(
        [_field("n1", "researcher", "glm")],
        root=REPO, home=str(tmp_path / "h"),
        db=db, run_id=RUN_ID, run_dir=str(run_dir),
    )

    assert rc == "lane_unverified"
    payload = json.loads((run_dir / "lane-preflight.json").read_text(encoding="utf-8"))
    assert payload["verdict"] == "blocked"
    assert payload["blocked_reason"] == "lane_unverified"
    status, verdict, notes = _run_row(db)
    assert (status, verdict) == ("failed", "ESCALATE")
    assert "lane_unverified" in (notes or "")
    events = _events(db)
    assert len(events) == 1 and events[0][0] == "lane_preflight_blocked"
    captured = capsys.readouterr()
    out_lines = captured.out.splitlines()
    assert "blocked_reason=lane_unverified" in out_lines
    assert "verdict=blocked" in out_lines
    assert any(
        ln.startswith("fix:") and "MO_LANE_REQUIRE_VERIFICATION=0" in ln
        for ln in captured.err.splitlines()
    ), captured.err


def test_enforce_dead_lane_blocks_before_unverified(tmp_path, monkeypatch, capsys):
    """A dead lane still blocks as ``lane_unhealthy`` even when unverified lanes
    are present and verification is required."""
    import mini_ork.dispatch.providers as providers_mod
    LaneHealth = providers_mod.LaneHealth
    monkeypatch.setenv("MO_LANE_REQUIRE_VERIFICATION", "1")
    _fake_preflight(monkeypatch, {
        "glm": LaneHealth(False, "glm: out of credit (HTTP 402)"),
        "cod": LaneHealth(True, UNVER_REASON, False),
    })

    rc = lane_preflight.enforce(
        [_field("n1", "researcher", "glm"), _field("n2", "planner", "cod")],
        root=REPO, home=str(tmp_path / "h"),
    )

    assert rc == "lane_unhealthy"
    assert "blocked_reason=lane_unhealthy" in capsys.readouterr().out


def test_persist_blocked_takes_reason_from_payload_with_fallback(tmp_path):
    """The reason is read from the payload, falling back to ``lane_unhealthy``
    — so both block reasons land in the ``task_runs`` notes."""
    first = tmp_path / "first"; first.mkdir()
    db = _make_min_db(first)
    lane_preflight._persist_blocked(db, RUN_ID, {"blocked_reason": "lane_unverified"})
    assert "lane_unverified" in (_run_row(db)[2] or "")

    second = tmp_path / "second"; second.mkdir()
    db2 = _make_min_db(second)
    lane_preflight._persist_blocked(db2, RUN_ID, {"verdict": "blocked"})
    assert "lane_unhealthy" in (_run_row(db2)[2] or "")


def test_print_machine_lines_reads_reason_from_payload(capsys):
    """Both block reasons print a correct ``blocked_reason=`` line, keeping the
    ``lane_preflight=<json>`` / ``verdict=blocked`` shape."""
    lane_preflight._print_machine_lines({
        "verdict": "blocked", "blocked_reason": "lane_unverified", "unverified": [],
    })
    out = capsys.readouterr().out.splitlines()
    assert "blocked_reason=lane_unverified" in out
    assert "verdict=blocked" in out
    assert any(ln.startswith("lane_preflight=") for ln in out)
