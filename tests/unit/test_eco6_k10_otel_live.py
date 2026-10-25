"""K10: live OTel/Langfuse export after every run (hermetic, no network)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

import mini_ork
from mini_ork import otel_export

REPO_ROOT = Path(next(iter(mini_ork.__path__))).resolve().parent

PK = "pk-stub-4f7a11"
SK = "sk-stub-8c2e93"
ENDPOINT = "https://otel.example.test/v1/traces"


class StubEnv:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def resolve(self, key):
        return self._values.get(key)


def stub(*, pk=None, sk=None, endpoint=None):
    values = {}
    if pk is not None:
        values["LANGFUSE_PUBLIC_KEY"] = pk
    if sk is not None:
        values["LANGFUSE_SECRET_KEY"] = sk
    if endpoint is not None:
        values["LANGFUSE_OTLP_ENDPOINT"] = endpoint
    return StubEnv(values)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_OTLP_ENDPOINT",
        "MO_OTEL_EXPORT",
        "MINI_ORK_SECRETS",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    from mini_ork.stores import migrate as mig

    db = tmp_path_factory.mktemp("otel") / "state.db"
    rc, _out, err = mig.init_db(str(db), root=str(REPO_ROOT))
    assert rc == 0, f"init_db failed: {err}"
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status,"
        " created_at, updated_at, cost_usd)"
        " VALUES ('run-x', 'code_fix', 'code-fix', 'k.md', 'published',"
        " 1700000000, 1700000005, 0.42)"
    )
    con.commit()
    con.close()
    return str(db)


def _spans(payload):
    return [
        s
        for rs in payload["resourceSpans"]
        for ss in rs["scopeSpans"]
        for s in ss["spans"]
    ]


def _attr(span, key):
    for a in span.get("attributes", []):
        if a["key"] == key:
            return a["value"].get("stringValue")
    return None


def test_both_keys_one_post_with_root_span(monkeypatch, seeded_db):
    recorded = []

    def fake_deliver(payload, *args, **kwargs):
        recorded.append((payload, args, kwargs))
        return True

    monkeypatch.setattr(otel_export, "_deliver", fake_deliver)
    assert (
        otel_export.export_after_run(
            "run-x", seeded_db, environment=stub(pk=PK, sk=SK)
        )
        is True
    )
    assert len(recorded) == 1
    roots = [s for s in _spans(recorded[0][0]) if "parentSpanId" not in s]
    assert any(_attr(s, "mini-ork.task_run_id") == "run-x" for s in roots)


def test_neither_key_no_build_no_post(monkeypatch):
    called = {"build": False, "deliver": False}

    def no_build(*a, **k):
        called["build"] = True
        raise AssertionError("build_payload must not be called")

    def no_deliver(*a, **k):
        called["deliver"] = True
        raise AssertionError("_deliver must not be called")

    monkeypatch.setattr(otel_export, "build_payload", no_build)
    monkeypatch.setattr(otel_export, "_deliver", no_deliver)
    assert otel_export.export_after_run("run-x", "unused.db", environment=stub()) is False
    assert called == {"build": False, "deliver": False}


def test_one_key_no_post_and_stderr_line(monkeypatch, capsys):
    called = {"deliver": False}

    def no_deliver(*a, **k):
        called["deliver"] = True
        raise AssertionError("_deliver must not be called")

    monkeypatch.setattr(otel_export, "_deliver", no_deliver)
    assert (
        otel_export.export_after_run("run-x", "unused.db", environment=stub(pk=PK))
        is False
    )
    assert called["deliver"] is False
    err = capsys.readouterr().err
    assert "only one of" in err
    assert PK not in err


def test_opt_out_env_zero_skips_credentials(monkeypatch):
    called = {"creds": False, "build": False}

    def no_creds(*a, **k):
        called["creds"] = True
        raise AssertionError("_credentials must not be called")

    def no_build(*a, **k):
        called["build"] = True
        raise AssertionError("build_payload must not be called")

    monkeypatch.setattr(otel_export, "_credentials", no_creds)
    monkeypatch.setattr(otel_export, "build_payload", no_build)
    monkeypatch.setenv("MO_OTEL_EXPORT", "0")
    assert (
        otel_export.export_after_run(
            "run-x", "unused.db", environment=stub(pk=PK, sk=SK)
        )
        is False
    )
    assert called == {"creds": False, "build": False}


def test_never_raises_on_deliver_oserror(monkeypatch, seeded_db, capsys):
    def broken(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(otel_export, "_deliver", broken)
    assert (
        otel_export.export_after_run(
            "run-x", seeded_db, environment=stub(pk=PK, sk=SK)
        )
        is False
    )
    assert "otel_export: export of run-x failed: OSError" in capsys.readouterr().err


def _quiet_channels(monkeypatch):
    for var in (
        "MO_IMPL_REWARD",
        "MO_GRADE_RUN_REWARD",
        "MO_LEARNING_WRITEBACK",
        "MO_LANE_ROUTER",
        "MO_AUTO_APPLY",
        "MO_APPLY_ENABLED",
    ):
        monkeypatch.setenv(var, "0")


def test_post_run_learning_calls_export_once(monkeypatch, tmp_path):
    from mini_ork.cli import execute as execute_mod

    calls = []

    def recorder(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    _quiet_channels(monkeypatch)
    monkeypatch.setattr(otel_export, "export_after_run", recorder)
    db = str(tmp_path / "state.db")
    execute_mod._post_run_learning(db, str(tmp_path), "run-x", "k10", fail_count=0)
    assert calls == [(("run-x", db), {})]


def test_post_run_learning_swallows_raising_export(monkeypatch, tmp_path):
    from mini_ork.cli import execute as execute_mod

    def boom(*args, **kwargs):
        raise RuntimeError("telemetry down")

    _quiet_channels(monkeypatch)
    monkeypatch.setattr(otel_export, "export_after_run", boom)
    db = str(tmp_path / "state.db")
    execute_mod._post_run_learning(db, str(tmp_path), "run-x", "k10", fail_count=0)


def test_dead_modules_are_gone():
    for name in (
        "mini_ork.observability.otel",
        "mini_ork.observability.langfuse_score_mapper",
    ):
        sys.modules.pop(name, None)
        assert importlib.util.find_spec(name) is None, f"{name} still exists"


def test_jsonl_helpers_are_gone():
    assert not hasattr(otel_export, "export_jsonl")
    assert not hasattr(otel_export, "build_payload_from_jsonl")
