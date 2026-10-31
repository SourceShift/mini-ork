"""Lane conformance probes (mini_ork.dispatch.conformance) and the preflight gate.

Hermetic: the probe's dispatch is an injected fake harness (it reads the probe
file from the pinned cwd the way a real CLI would), the registry is temporary,
and the state.db is freshly migrated. No provider, no network.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.cli import providers as providers_cli
from mini_ork.dispatch import conformance, harness_identity
from mini_ork.dispatch.models import DispatchResult, TokenUsage
from mini_ork.dispatch.providers import lane_health
from mini_ork.stores import audit_chain
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
LANE = "http"
IDENTITY = {"harness": "mini-ork/openai-compat", "kind": "openai-compat", "binary": "",
            "version": "mini-ork 0.0.test"}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    registry = tmp_path / "providers.yaml"
    registry.write_text(
        "providers:\n  http:\n    kind: openai-compat\n"
        "    base_url: https://api.invalid/v1\n    api_key_env: HTTP_KEY\n")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(registry))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(harness_identity, "identity", lambda lane, root=None, **_: dict(IDENTITY))
    path = tmp_path / "state.db"
    rc, _, err = mig.init_db(db=str(path), root=str(REPO))
    assert rc == 0, err
    return str(path)


def fake_harness(*, reply="json", usage=True, write=False, read_cwd=True, ok=True, seen=None):
    def run(request, root):
        if seen is not None:
            seen.append(request)
        token = (Path(request.cwd, conformance.PROBE_FILE).read_text().strip()
                 if read_cwd else "0" * 32)
        if write:
            Path(request.cwd, "notes.md").write_text("scratch")
        text = {
            "json": json.dumps({"token": token}),
            "fenced": f'```json\n{{"token": "{token}"}}\n```',
            "prose": f"The file contains {token}.",
        }[reply]
        return DispatchResult(
            ok=ok, rc=0 if ok else 1, text=text if ok else "",
            error="" if ok else "lane preflight failed: key rejected", model=request.model,
            usage=TokenUsage(input_tokens=40 if usage else 0, output_tokens=9 if usage else 0),
            cost_usd=0.0012)
    return run


def test_a_conformant_harness_passes_and_the_verdict_is_recorded(db):
    seen = []
    verdict = conformance.probe(LANE, db=db, dispatch=fake_harness(seen=seen))

    assert verdict["ok"] is True, verdict
    assert verdict["checks"] == dict.fromkeys(conformance.CHECKS, True)
    assert verdict["harness"] == IDENTITY
    request = seen[0]
    assert request.env["MO_TARGET_CWD"] == request.cwd
    assert request.env["MO_REQUIRE_CONFORMANCE"] == "0"
    assert not Path(request.cwd).exists(), "the scratch directory is removed"

    con = sqlite3.connect(db)
    feature, cost = con.execute("SELECT feature_name, cost_usd FROM llm_calls").fetchone()
    con.close()
    assert (feature, cost) == ("mini-ork:conformance", pytest.approx(0.0012))
    record = conformance.latest(db, LANE)
    assert record["event_type"] == "conformance_probe"
    assert record["event"]["ok"] is True
    assert audit_chain.verify_chain(db)["ok"] is True
    assert conformance.passed(LANE, db=db) == (True, "conformant")


@pytest.mark.parametrize("harness, failing", [
    (fake_harness(reply="fenced"), []),
    (fake_harness(reply="prose"), ["json_contract"]),
    (fake_harness(read_cwd=False), ["cwd_and_tools", "json_contract"]),
    (fake_harness(usage=False), ["usage_reported"]),
    (fake_harness(write=True), ["read_only"]),
    (fake_harness(ok=False), ["answered", "cwd_and_tools", "json_contract"]),
])
def test_each_contract_breach_fails_its_own_check(db, harness, failing):
    verdict = conformance.probe(LANE, db=db, dispatch=harness)
    assert verdict["failures"] == failing
    assert verdict["ok"] is (not failing)


def test_a_write_is_named_in_the_verdict(db):
    verdict = conformance.probe(LANE, db=db, dispatch=fake_harness(write=True))
    assert verdict["leftovers"] == ["notes.md"]


def test_passed_needs_a_passing_probe_under_the_current_harness(db, monkeypatch):
    ok, reason = conformance.passed(LANE, db=db)
    assert not ok and "no conformance probe on record" in reason

    conformance.probe(LANE, db=db, dispatch=fake_harness(reply="prose"))
    ok, reason = conformance.passed(LANE, db=db)
    assert not ok and "failed (json_contract)" in reason

    conformance.probe(LANE, db=db, dispatch=fake_harness())
    assert conformance.passed(LANE, db=db)[0] is True

    upgraded = {**IDENTITY, "version": "mini-ork 9.9.9"}
    monkeypatch.setattr(harness_identity, "identity", lambda lane, root=None, **_: upgraded)
    ok, reason = conformance.passed(LANE, db=db)
    assert not ok and "harness changed" in reason and "9.9.9" in reason


def test_the_preflight_gate_refuses_a_lane_until_it_conforms(db):
    runtime = {"HTTP_KEY": "k", "MINI_ORK_DB": db}
    assert lane_health(LANE, environment=runtime).ok is True, "gate is opt-in"

    gated = {**runtime, "MO_REQUIRE_CONFORMANCE": "1"}
    health = lane_health(LANE, environment=gated)
    assert health.ok is False and "no conformance probe" in health.reason

    conformance.probe(LANE, db=db, dispatch=fake_harness())
    assert lane_health(LANE, environment=gated).ok is True


def test_the_gate_still_refuses_a_lane_without_credentials(db):
    health = lane_health(LANE, environment={"MINI_ORK_DB": db, "MO_REQUIRE_CONFORMANCE": "1"})
    assert health.ok is False and "HTTP_KEY" in health.reason


def test_providers_conform_cli(db, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setattr(conformance, "_default_dispatch", fake_harness())
    assert providers_cli.main(["conform", "--json", LANE], root=REPO) == 0
    verdicts = json.loads(capsys.readouterr().out)
    assert [v["lane"] for v in verdicts] == [LANE] and verdicts[0]["ok"] is True

    monkeypatch.setattr(conformance, "_default_dispatch", fake_harness(usage=False))
    assert providers_cli.main(["conform", LANE], root=REPO) == 1
    assert "FAIL usage_reported" in capsys.readouterr().out
