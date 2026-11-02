"""A revise round continues the lane's conversation instead of cold-starting.

The revise loop re-dispatches the same node against the same target with a
delta-sized fix instruction, and its prompt deliberately drops the first
attempt's background so the round does not re-pay for it. That slimming is only
correct when the lane still holds the background — which is exactly the case
this wires up: the previous attempt's session id, persisted in the run dir, is
handed to ``claude --resume``.

Three properties matter here and each is tested directly:

* **the trigger** — revise rounds and recoveries resume, everything else does
  not, and ``MO_REVISE_RESUME=0`` opts out;
* **the source of the id** — the per-node sidecar, which exists for *failed*
  attempts too (durable ``node_attempts`` rows are written only on success, so a
  revise round could never have used them);
* **fail-soft** — an id whose transcript cannot be resolved yields "" (cold
  start), never a ``--resume`` the lane will reject.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.cli import execute_handlers  # noqa: E402
from mini_ork.recovery import resume_prep  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("MINI_ORK_RECOVERY_CLOSURE", "MINI_ORK_RECOVERY_FROM",
                "MO_REVISE_RESUME", "MO_RESUME_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)


def _run_dir_with_revise(tmp_path: Path) -> Path:
    (tmp_path / "revise").mkdir()
    (tmp_path / "revise" / "current.json").write_text(
        json.dumps({"round": 2, "max_rounds": 3, "feedback": ""}), encoding="utf-8")
    return tmp_path


# ── the trigger ────────────────────────────────────────────────────────────


def test_a_plain_dispatch_does_not_resume(tmp_path: Path):
    assert execute_handlers._resume_trigger(str(tmp_path)) == ""


def test_a_revise_round_resumes(tmp_path: Path):
    assert execute_handlers._resume_trigger(str(_run_dir_with_revise(tmp_path))) == "revise"


def test_a_recovery_resumes_for_its_own_reason(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_RECOVERY_CLOSURE", "1")
    # Recovery wins even while a revise round is pending: its trigger has
    # durable state behind it.
    assert execute_handlers._resume_trigger(str(_run_dir_with_revise(tmp_path))) == "recovery"


def test_the_revise_opt_out_is_honoured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MO_REVISE_RESUME", "0")
    assert execute_handlers._resume_trigger(str(_run_dir_with_revise(tmp_path))) == ""


def test_a_malformed_revise_file_is_not_a_revise_round(tmp_path: Path):
    (tmp_path / "revise").mkdir()
    (tmp_path / "revise" / "current.json").write_text("{not json", encoding="utf-8")
    assert execute_handlers._resume_trigger(str(tmp_path)) == ""


# ── the id comes from the per-node sidecar ─────────────────────────────────


def test_no_sidecar_means_no_resume(tmp_path: Path):
    assert resume_prep.prepare_round_resume(str(tmp_path), "implementer") == ""


def test_a_non_claude_lane_never_resumes(tmp_path: Path, monkeypatch):
    """codex/gemini run their own session model — same split as recovery."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    _plant_live_transcript(tmp_path, "implementer", "sid-codex")
    assert resume_prep.prepare_round_resume(
        str(tmp_path), "implementer", model="codex") == ""


def test_a_live_transcript_resumes_and_is_persisted(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    _plant_live_transcript(tmp_path, "implementer", "sid-abc")

    sid = resume_prep.prepare_round_resume(str(tmp_path), "implementer", model="sonnet")

    assert sid == "sid-abc"
    # The round dir now holds its own copy, so a later round still resumes if
    # the claude home is rebuilt or the run moves.
    assert (tmp_path / "sessions" / "sid-abc.jsonl").is_file()


def test_a_sidecar_with_no_transcript_fails_soft(tmp_path: Path, monkeypatch):
    """The dangerous case: an id we cannot honour must yield "", not an id that
    makes ``claude --resume`` fail and sinks a round the agent could have won."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    _write_sidecar(tmp_path, "implementer", "sid-missing")

    assert resume_prep.prepare_round_resume(str(tmp_path), "implementer", model="sonnet") == ""


def _write_sidecar(run_dir: Path, node_id: str, session_id: str) -> None:
    d = run_dir / ".sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{node_id}.session").write_text(session_id, encoding="utf-8")


def _plant_live_transcript(run_dir: Path, node_id: str, session_id: str,
                           cwd: str | None = None) -> None:
    _write_sidecar(run_dir, node_id, session_id)
    from mini_ork.stores.session_store import claude_projects_dir, project_slug

    target = claude_projects_dir() / project_slug(cwd) / f"{session_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"type":"summary"}\n', encoding="utf-8")


# ── the ledger records that the saving happened ────────────────────────────


@pytest.fixture(scope="module")
def db(tmp_path_factory) -> Path:
    from mini_ork.stores import migrate as mig

    path = tmp_path_factory.mktemp("ledger") / "state.db"
    rc, _, err = mig.init_db(db=str(path), root=str(REPO))
    assert rc == 0, err
    return path


def _write_row(db: Path) -> dict:
    from mini_ork.dispatch.llm_dispatch import write_llm_calls_row

    write_llm_calls_row(
        str(db), "anthropic-native", "sonnet", "default", "mini-ork:implementer",
        None, "success", 1200, 0.01, None, input_tokens=900, output_tokens=100,
    )
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT metadata_json FROM llm_calls ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0])


def test_a_resumed_dispatch_is_stamped_in_the_ledger(db: Path, monkeypatch):
    monkeypatch.setenv("MO_RESUME_SESSION_ID", "sid-resumed")
    assert _write_row(db).get("resumed") == "sid-resumed"


def test_a_cold_dispatch_carries_no_resume_marker(db: Path):
    assert "resumed" not in _write_row(db)
