"""``mini_ork.ide_pages.outcome`` — fault attribution on the failed run card.

J24/C25: a failed run must say *whose* fault it is — a mini-ork bug (offer
``Open triage``), a task/consumer fault (retry the failure), or "unknown" (the
evidence is infra/timeout, so do not auto-fix). The verdict comes from
``mini_ork.triage.blame.attribute``; this page only feeds it the fields the run
carries and renders the sentence.

The fixture pattern mirrors ``test_ide_pages_outcome.py``: a temp mini-ork home
with an initialised DB, a ``task_runs`` row and the node lifecycle events that
``run_page._load`` folds into ``run.nodes``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.ide_pages import outcome
from mini_ork.ide_pages import run as run_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-blame1"
T0 = 1_791_000_000

CONFIRM = "File a bug and queue a framework-edit fix epic?"

# A traceback whose frames are mini-ork's own code → blame "mini_ork".
MINI_ORK_LOG = "\n".join([
    "node build dispatching",
    "Traceback (most recent call last):",
    '  File "/srv/checkout/mini_ork/dispatch/router.py", line 88, in send',
    "    raise RuntimeError('lane socket closed')",
    "RuntimeError: lane socket closed",
])

# A traceback in the consumer's repo, outside the framework tree → "consumer".
CONSUMER_LOG = "\n".join([
    "running consumer suite",
    "Traceback (most recent call last):",
    '  File "/srv/consumer-repo/tests/test_shim.py", line 12, in test_build',
    "    assert build() == 3",
    "AssertionError: build returned 4",
])


@pytest.fixture(autouse=True)
def _no_retry_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the card's action list to the code under test — no DB-derived hint."""
    import mini_ork.recovery.retry_hint as retry_hint

    monkeypatch.setattr(retry_hint, "load_or_compute", lambda *a, **k: None)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(
    home: Path,
    *,
    status: str = "failed",
    nodes: tuple[tuple[str, str, str], ...] = (("build", "implementer", "error"),),
    log: str | None = None,
    log_name: str = "build.log",
) -> Path:
    """One run row plus its node lifecycle events; optionally one node log."""
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# Make the demo pass\n\nDetails.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, "demo-recipe", status, 0.5, T0, T0 + 120, T0 + 100, "demo", str(kickoff),
         "latest", "tr-demo-1"))
    for i, (node_id, node_type, finish) in enumerate(nodes):
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"start-{i}", RUN, "node_start",
             json.dumps({"node_id": node_id, "node_type": node_type}), T0 + 10 + i))
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"end-{i}", RUN, "node_end",
             json.dumps({"node_id": node_id, "node_type": node_type,
                         "finish_reason": finish}), T0 + 20 + i))
    con.commit()
    con.close()
    if log is not None:
        (run_dir / log_name).write_text(log, encoding="utf-8")
    return run_dir


def _run(home: Path):
    run = run_page._load(home, RUN)
    assert run is not None
    return run


def _text(out: dict) -> str:
    """Every human-readable string the card carries, joined."""
    parts = [str(out.get("text") or ""), str(out.get("detail") or "")]
    for sec in out.get("callouts") or []:
        if isinstance(sec, dict):
            parts.append(str(sec.get("title") or ""))
            parts.append(str(sec.get("text_md") or ""))
    return "\n".join(parts)


def _buttons(out: dict) -> list[dict]:
    btns = list(out.get("actions") or []) + list(out.get("menu") or [])
    for sec in out.get("callouts") or []:
        if isinstance(sec, dict):
            btns += list(sec.get("actions") or [])
    return [b for b in btns if isinstance(b, dict)]


def _triage_buttons(out: dict) -> list[dict]:
    found = []
    for btn in _buttons(out):
        do = btn.get("do")
        cli = do.get("cli") if isinstance(do, dict) else None
        if isinstance(cli, list) and cli and str(cli[0]) == "triage":
            found.append(btn)
    return found


# ── mini_ork fault ─────────────────────────────────────────────────────────

def test_mini_ork_frame_blames_framework_and_offers_open_triage(home: Path) -> None:
    """A framework traceback → ``blame == "mini_ork"``, the evidence named, and a
    confirm-gated ``Open triage`` running ``triage --run <id> --promote``."""
    _seed(home, log=MINI_ORK_LOG)
    out = outcome.resolve(_run(home))

    assert out["state"] == "failed"
    assert out["blame"] == "mini_ork"
    text = _text(out)
    assert "Looks like a mini-ork fault" in text
    assert "fix it at the source" in text
    # The sentence names the first evidence line (the framework frame path).
    assert "mini_ork/dispatch/router.py" in text

    triage = [b for b in _triage_buttons(out) if b.get("label") == "Open triage"]
    assert len(triage) == 1, [b.get("label") for b in _buttons(out)]
    do = triage[0]["do"]
    assert do["cli"] == ["triage", "--run", RUN, "--promote"]
    # It writes — the confirm string is mandatory and verbatim.
    assert do["confirm"] == CONFIRM


def test_open_triage_is_the_only_new_action(home: Path) -> None:
    """The blame callout adds exactly one button on a mini-ork fault."""
    _seed(home, log=MINI_ORK_LOG)
    out = outcome.resolve(_run(home))
    blame = next(c for c in out["callouts"] if c.get("title") == "Whose fault is this")
    assert [b["label"] for b in blame["actions"]] == ["Open triage"]


# ── consumer / task fault ──────────────────────────────────────────────────

@pytest.mark.parametrize("finish", ["verdict_fail", "verdict_revise"])
def test_consumer_verdict_adds_no_action(home: Path, finish: str) -> None:
    """A legitimate artifact verdict → ``consumer``; the retry controls already
    exist, so the blame callout adds no second action."""
    _seed(home, nodes=(("review", "reviewer", finish),))
    out = outcome.resolve(_run(home))

    assert out["blame"] == "consumer"
    text = _text(out)
    assert "Looks like a task fault" in text
    assert "retry the failure, or adjust the kickoff" in text
    assert _triage_buttons(out) == []
    blame = next(c for c in out["callouts"] if c.get("title") == "Whose fault is this")
    assert blame["actions"] == []


def test_consumer_traceback_blames_consumer(home: Path) -> None:
    """An error raised in code outside the framework tree is a task fault."""
    _seed(home, log=CONSUMER_LOG)
    out = outcome.resolve(_run(home))

    assert out["blame"] == "consumer"
    assert "Looks like a task fault" in _text(out)
    assert "consumer-repo/tests/test_shim.py" in _text(out)
    assert _triage_buttons(out) == []


# ── unknown (infra) ────────────────────────────────────────────────────────

@pytest.mark.parametrize("finish", ["timeout", "cost_limit"])
def test_timeout_or_budget_blames_unknown_and_says_why(home: Path, finish: str) -> None:
    """An infra finish reason → ``unknown``; the sentence names that evidence
    and offers no action (never fix blind)."""
    _seed(home, nodes=(("research", "researcher", finish),))
    out = outcome.resolve(_run(home))

    assert out["blame"] == "unknown"
    text = _text(out)
    assert "Can't tell yet whether this is a mini-ork fault or a task fault" in text
    assert finish in text or (finish == "cost_limit" and "budget" in text)
    assert _triage_buttons(out) == []


# ── no blame on healthy / running / needs-you ──────────────────────────────

def test_healthy_run_has_no_blame(home: Path) -> None:
    """A published run carries no ``blame`` key and no blame callout."""
    _seed(home, status="published",
          nodes=(("build", "implementer", "done"),), log=MINI_ORK_LOG)
    out = outcome.resolve(_run(home))

    assert out["state"] == "done"
    assert "blame" not in out
    titles = [c.get("title") for c in out["callouts"]]
    assert "Whose fault is this" not in titles
    assert "fault" not in _text(out).lower()


def test_running_run_has_no_blame(home: Path) -> None:
    """A run still in flight carries no blame."""
    _seed(home, status="executing",
          nodes=(("build", "implementer", "done"),), log=MINI_ORK_LOG)
    out = outcome.resolve(_run(home))

    assert out["state"] == "running"
    assert "blame" not in out
    assert "fault" not in _text(out).lower()


def test_needs_you_run_has_no_blame(home: Path) -> None:
    """A failure waiting on the operator renders through the needs-you lane,
    which owns the one retry decision — no duplicate blame action."""
    run_dir = _seed(home, log=MINI_ORK_LOG)
    (run_dir / "retry-notify.json").write_text(json.dumps({"state": "pending"}))
    run = _run(home)
    run.card["task_state"] = {"state": "needs_you"}
    out = outcome.resolve(run)

    assert out["state"] == "needs_you"
    assert "blame" not in out
    assert _triage_buttons(out) == []


# ── fail-closed ────────────────────────────────────────────────────────────

def test_missing_log_still_resolves_unknown(home: Path) -> None:
    """No log on disk → ``unknown``, and ``resolve`` does not raise."""
    _seed(home)  # no log file
    out = outcome.resolve(_run(home))

    assert out["blame"] == "unknown"
    assert "Can't tell yet" in _text(out)
    assert _triage_buttons(out) == []


def test_unreadable_log_still_resolves_unknown(home: Path) -> None:
    """A directory where the node log would be is unreadable — absorbed."""
    _seed(home)
    (home / "runs" / RUN / "build.log").mkdir()
    out = outcome.resolve(_run(home))

    assert out["blame"] == "unknown"
    assert "Can't tell yet" in _text(out)


def test_attribute_crash_is_absorbed_to_unknown(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashing ``triage.blame.attribute`` degrades the card, never raises."""
    import mini_ork.triage.blame as blame_mod

    def _boom(*_a, **_k):
        raise RuntimeError("triage module broke")

    monkeypatch.setattr(blame_mod, "attribute", _boom)
    _seed(home, log=MINI_ORK_LOG)
    out = outcome.resolve(_run(home))

    assert out["blame"] == "unknown"
    assert "Can't tell yet" in _text(out)
