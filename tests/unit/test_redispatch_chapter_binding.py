"""Hermetic branch coverage for the goal-loop redispatch binding.

kickoffs/book-goal-loop/binding/redispatch_chapter.py shells out to psql (job
resolution) and tsx (sanctioned forceResumeJob). Those seams are monkeypatched
here so every decision branch — especially the FSM!='failed' no-op that cannot
be exercised against the live DB while the run is failed — is proven without any
side effect.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_BINDING = (
    Path(__file__).resolve().parents[2]
    / "kickoffs"
    / "book-goal-loop"
    / "binding"
    / "redispatch_chapter.py"
)
_GOOD_BOOK = "d0df3cdb-8164-450e-b841-2c9354ea0423"


def _load():
    spec = importlib.util.spec_from_file_location("redispatch_chapter_binding", _BINDING)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load()


def test_no_argv_is_usage_error(mod):
    assert mod.main([]) == 2


def test_bad_chapter_id_rejected(mod, monkeypatch):
    monkeypatch.setenv("BOOK_UUID", _GOOD_BOOK)
    assert mod.main(["../etc"]) == 2
    assert mod.main(["1; DROP TABLE"]) == 2


def test_missing_or_bad_book_uuid_rejected(mod, monkeypatch):
    monkeypatch.delenv("BOOK_UUID", raising=False)
    assert mod.main(["1"]) == 2
    monkeypatch.setenv("BOOK_UUID", "not-a-uuid")
    assert mod.main(["1"]) == 2


def test_no_run_resolved_is_exit_3(mod, monkeypatch):
    monkeypatch.setenv("BOOK_UUID", _GOOD_BOOK)
    monkeypatch.setattr(mod, "_resolve_job", lambda book: (None, None, None))
    assert mod.main(["1"]) == 3


def test_fsm_not_failed_is_idempotent_noop(mod, monkeypatch, capsys):
    """The per-wave idempotency path: a run already progressing -> exit 0, no resume.

    ``generating`` is no longer a blanket no-op — it first tries to free a
    stranded dispatch claim — so the blanket path is pinned behind its own
    switch. Without it the probe would reach psql, which this suite is
    deliberately free of.
    """
    monkeypatch.setenv("BOOK_UUID", _GOOD_BOOK)
    monkeypatch.setenv("MO_GOAL_REDISPATCH_BREAK_GENERATING", "0")
    monkeypatch.setattr(mod, "_resolve_job", lambda book: ("job_x", "run-uuid", "generating"))
    called = {"resume": False}
    monkeypatch.setattr(
        mod, "_force_resume", lambda *a, **k: called.__setitem__("resume", True) or (True, "x")
    )
    rc = mod.main(["2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert called["resume"] is False  # never touched the sanctioned CLI
    assert "noop" in out and "generating" in out


def test_failed_fsm_dry_records_plan_without_resuming(mod, monkeypatch, capsys):
    """DRY surfaces the resume AND the runner-model reconcile plan, executing neither."""
    monkeypatch.setenv("BOOK_UUID", _GOOD_BOOK)
    monkeypatch.setenv("MO_GOAL_REDISPATCH_DRY", "1")
    monkeypatch.setattr(mod, "_resolve_job", lambda book: ("job_x", "run-uuid", "failed"))
    monkeypatch.setattr(mod, "_runner_model", lambda dirs: ("glm-5.3", "live runner serves 'glm-5.3'"))
    called = {"resume": False, "prov": False}
    monkeypatch.setattr(
        mod, "_force_resume", lambda *a, **k: called.__setitem__("resume", True) or (True, "x")
    )
    monkeypatch.setattr(
        mod, "_reconcile_provenance_model", lambda *a, **k: called.__setitem__("prov", True) or (True, "x")
    )
    rc = mod.main(["1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert called == {"resume": False, "prov": False}  # nothing executed
    assert "DRY" in out and "job_x" in out
    assert "glm-5.3" in out and "provenance" in out  # reconcile plan surfaced


def test_failed_fsm_live_reconciles_both_channels_is_exit_0(mod, monkeypatch, capsys):
    """The happy path aligns BOTH model channels to the live runner: the persisted
    provenance (jsonb_set) AND the resume env pin (CHAPTER_PRIMARY_MODEL)."""
    monkeypatch.setenv("BOOK_UUID", _GOOD_BOOK)
    monkeypatch.delenv("MO_GOAL_REDISPATCH_DRY", raising=False)
    monkeypatch.setattr(mod, "_resolve_job", lambda book: ("job_x", "run-uuid", "failed"))
    monkeypatch.setattr(mod, "_runner_model", lambda dirs: ("glm-5.3", "serves glm-5.3"))
    prov = {}
    monkeypatch.setattr(
        mod,
        "_reconcile_provenance_model",
        lambda ru, m: prov.update(run=ru, model=m) or (True, "provenance.chapter_dispatch.model -> 'glm-5.3'"),
    )
    seen = {}
    monkeypatch.setattr(
        mod,
        "_force_resume",
        lambda job_id, rdir, model=None: seen.update(job=job_id, dir=rdir, model=model)
        or (True, "resumed reset=10 pending=10"),
    )
    rc = mod.main(["1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert seen["job"] == "job_x"  # resolved job_id flows through to the resume
    assert seen["model"] == "glm-5.3"  # runner model threads into the resume env pin
    assert prov == {"run": "run-uuid", "model": "glm-5.3"}  # provenance reconciled to the same model
    assert "ok" in out


def test_failed_fsm_unreachable_runner_still_resumes(mod, monkeypatch, capsys):
    """A probe miss is best-effort: no provenance write, no env pin, but the resume
    still proceeds (an aligned-by-default run can regen; drift is the exception)."""
    monkeypatch.setenv("BOOK_UUID", _GOOD_BOOK)
    monkeypatch.delenv("MO_GOAL_REDISPATCH_DRY", raising=False)
    monkeypatch.setattr(mod, "_resolve_job", lambda book: ("job_x", "run-uuid", "failed"))
    monkeypatch.setattr(mod, "_runner_model", lambda dirs: (None, "runner /readyz probe failed; left as-is"))
    prov = {"called": False}
    monkeypatch.setattr(
        mod, "_reconcile_provenance_model", lambda *a, **k: prov.__setitem__("called", True) or (True, "x")
    )
    seen = {}
    monkeypatch.setattr(
        mod, "_force_resume", lambda job_id, rdir, model=None: seen.update(model=model) or (True, "resumed")
    )
    rc = mod.main(["1"])
    assert rc == 0
    assert prov["called"] is False  # no provenance write without a resolved model
    assert seen["model"] is None  # no env pin -> worker's own env model stands


def test_failed_fsm_live_resume_refused_is_exit_1(mod, monkeypatch, capsys):
    """worker_not_ready / any refusal -> non-zero so the wave surfaces it."""
    monkeypatch.setenv("BOOK_UUID", _GOOD_BOOK)
    monkeypatch.delenv("MO_GOAL_REDISPATCH_DRY", raising=False)
    monkeypatch.setattr(mod, "_resolve_job", lambda book: ("job_x", "run-uuid", "failed"))
    monkeypatch.setattr(mod, "_runner_model", lambda dirs: (None, "left as-is"))
    monkeypatch.setattr(
        mod, "_force_resume", lambda *a, **k: (False, "force-resume refused: worker_not_ready: ...")
    )
    rc = mod.main(["1"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAILED" in out and "worker_not_ready" in out


# ── capability 2: reconcile runner model drift (probe + provenance + env pin) ────
class _FakeReadyz:
    """A context-manager stand-in for urlopen's response."""

    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        import json as _json

        return _json.dumps(self._payload).encode()


def _write_runner_env(root, url="http://REDACTED-INTERNAL-IP:7910", token="x" * 40):
    (root / "server").mkdir(parents=True, exist_ok=True)
    (root / "server" / ".env").write_text(
        f"CHAPTER_MICROVM_RUNNER_URL={url}\nCHAPTER_MICROVM_RUNNER_TOKEN={token}\n"
    )


def test_runner_model_aligns_from_readyz(mod, monkeypatch, tmp_path):
    """A ready runner's advertised model is what we reconcile both channels to."""
    _write_runner_env(tmp_path)
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return _FakeReadyz({"status": "ready", "model": "glm-5.3"})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    model, why = mod._runner_model([str(tmp_path)])
    assert model == "glm-5.3" and "glm-5.3" in why
    assert seen["url"] == "http://REDACTED-INTERNAL-IP:7910/readyz"
    assert seen["auth"] == "Bearer " + "x" * 40


def test_runner_model_prefers_first_env_dir_with_config(mod, monkeypatch, tmp_path):
    """The worktree's server/.env is preferred; a later checkout is the fallback."""
    wt = tmp_path / "wt"
    primary = tmp_path / "primary"
    _write_runner_env(primary)  # only the fallback has config
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda *a, **k: _FakeReadyz({"status": "ready", "model": "MiniMax-M3"})
    )
    model, why = mod._runner_model([str(wt), str(primary)])  # wt lacks server/.env
    assert model == "MiniMax-M3" and str(primary) in why  # fell back to the checkout that has it


def test_runner_model_absent_config_never_touches_network(mod, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("must not probe without runner config")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    model, why = mod._runner_model([str(tmp_path)])  # no server/.env anywhere
    assert model is None and "absent" in why


def test_runner_model_probe_failure_is_best_effort(mod, monkeypatch, tmp_path):
    _write_runner_env(tmp_path)
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("conn refused"))
    )
    model, why = mod._runner_model([str(tmp_path)])
    assert model is None and "probe failed" in why


def test_runner_model_rejects_unsafe_model(mod, monkeypatch, tmp_path):
    """A runner advertising a model with SQL/injection metachars is refused (the
    allow-list guards both the env pin and the jsonb_set)."""
    _write_runner_env(tmp_path)
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeReadyz({"status": "ready", "model": "glm'; DROP TABLE x;--"}),
    )
    model, why = mod._runner_model([str(tmp_path)])
    assert model is None and "bad model" in why


class _FakeProc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_reconcile_provenance_updates_when_differs(mod, monkeypatch):
    """A RETURNING row means the value flipped -> (True, ...). The SQL carries the
    run uuid, the model, and the jsonb path — and only fires on a real difference."""
    seen = {}

    def _fake_q(sql):
        seen["sql"] = sql
        return _FakeProc(0, "glm-5.3\n")

    monkeypatch.setattr(mod, "_q", _fake_q)
    changed, why = mod._reconcile_provenance_model("11111111-2222-3333-4444-555555555555", "glm-5.3")
    assert changed is True and "glm-5.3" in why
    assert "jsonb_set" in seen["sql"] and "chapter_dispatch,model" in seen["sql"]
    assert "11111111-2222-3333-4444-555555555555" in seen["sql"]
    assert "IS DISTINCT FROM" in seen["sql"]  # idempotency guard present


def test_reconcile_provenance_noop_when_already_aligned(mod, monkeypatch):
    """No RETURNING row (0 rows updated) -> already aligned, (False, ...)."""
    monkeypatch.setattr(mod, "_q", lambda sql: _FakeProc(0, "\n"))
    changed, why = mod._reconcile_provenance_model("11111111-2222-3333-4444-555555555555", "glm-5.3")
    assert changed is False and "already aligned" in why


def test_reconcile_provenance_rejects_bad_run_uuid(mod, monkeypatch):
    """A non-uuid run id never reaches the DB (defense-in-depth, though run_uuid is
    DB-sourced)."""
    monkeypatch.setattr(mod, "_q", lambda sql: (_ for _ in ()).throw(AssertionError("must not query")))
    changed, why = mod._reconcile_provenance_model("not-a-uuid", "glm-5.3")
    assert changed is False and "invalid" in why


def test_reconcile_provenance_rejects_unsafe_model(mod, monkeypatch):
    monkeypatch.setattr(mod, "_q", lambda sql: (_ for _ in ()).throw(AssertionError("must not query")))
    changed, why = mod._reconcile_provenance_model("11111111-2222-3333-4444-555555555555", "x'; DROP--")
    assert changed is False and "unsafe" in why


def test_reconcile_provenance_db_error_is_best_effort(mod, monkeypatch):
    monkeypatch.setattr(mod, "_q", lambda sql: _FakeProc(1, "", "connection refused"))
    changed, why = mod._reconcile_provenance_model("11111111-2222-3333-4444-555555555555", "glm-5.3")
    assert changed is False and "db-error" in why


def test_force_resume_pins_model_into_subprocess_env(mod, monkeypatch, tmp_path):
    """A resolved model reaches the resume subprocess env as CHAPTER_PRIMARY_MODEL so
    the FRESH re-resolve writes it back to provenance."""
    rdir = tmp_path
    tsx = rdir / "node_modules" / ".bin" / "tsx"
    tsx.parent.mkdir(parents=True)
    tsx.write_text("#!/bin/sh\n")
    (rdir / "server" / "scripts").mkdir(parents=True)
    (rdir / "server" / "scripts" / "forceResumeChaptersMinimax.ts").write_text("//\n")
    seen = {}

    def fake_run(argv, cwd, capture_output, text, env):
        seen["env"] = env
        return _FakeProc(0, '{"jobId":"job_x","success":true,"resetChapters":[1],"pendingChapters":[1]}\n')

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    ok, why = mod._force_resume("job_x", str(rdir), "glm-5.3")
    assert ok is True and "resumed" in why
    assert seen["env"]["CHAPTER_PRIMARY_MODEL"] == "glm-5.3"  # pinned into the resume


def test_force_resume_without_model_inherits_env(mod, monkeypatch, tmp_path):
    """No model -> env=None (inherit os.environ), never a fabricated pin."""
    rdir = tmp_path
    tsx = rdir / "node_modules" / ".bin" / "tsx"
    tsx.parent.mkdir(parents=True)
    tsx.write_text("#!/bin/sh\n")
    (rdir / "server" / "scripts").mkdir(parents=True)
    (rdir / "server" / "scripts" / "forceResumeChaptersMinimax.ts").write_text("//\n")
    seen = {}

    def fake_run(argv, cwd, capture_output, text, env):
        seen["env"] = env
        return _FakeProc(0, '{"jobId":"job_x","success":true}\n')

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    ok, _why = mod._force_resume("job_x", str(rdir), None)
    assert ok is True
    assert seen["env"] is None  # inherits the parent env unchanged
