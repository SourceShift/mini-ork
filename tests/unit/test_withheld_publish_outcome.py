"""A withheld publish is "needs you", never "failed at implementer".

Covers ``kickoffs/auto/withheld-publish-outcome.md``: the live run that passed
typecheck, the build test, the reviewer (round 2), the rubric and eval — then
the publisher abstained (``levels_decision: "abstain"``) because a level was
not PROVEN. The IDE told the user "Failed at implementer; the cause was not
classified" and blamed a node that never failed.

Three fixes are pinned here:

1. ``_failing_node`` blames a node only when its ``node_end`` *says* it failed
   (an explicit ``finish_reason`` other than ``done``/``skipped``/``abstain``,
   a negative ``verdict``, or an ``error``). A bare ``node_end`` is not a
   failure, and a later clean end clears an earlier failure of the same node.
2. A terminal-failed run whose level report says ``abstain`` — with no failing
   node — is ``needs_you`` ("Not published: <levels> unverified") everywhere:
   ``task_state``, ``run_mark`` and ``ide_pages.outcome``.
3. A ``landed.json`` marker turns a terminal-failed run into ``done`` — a
   delivered change must never keep looking failed.

The "no failing node" half of 2 is judged over the run's *real* lifecycle rows
(``run_events``), not the card's ``finish_reason``-only node projection, so the
common crash shape — ``kill_run`` / the reaper closing a dangling start with
``{verdict: "CRASH", interrupted: true}`` and no ``finish_reason`` — reaches
every surface: the card, the fleet row and the counts tile all say "failed".
``levels_unverified`` (the publisher's own abstain signal) is not a failure.

Plus ``retry_hint``: the withheld run is a *retryable verify*, so
``mini-ork recover <run> --from-node test`` no longer refuses it.

Hermetic: ``tmp_path`` homes, ``mig.init_db`` for the schema, no LLM, no
network, no live home.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp import fleet as fleet_mod  # noqa: E402
from mini_ork.acp.task_state import (  # noqa: E402
    MARKS,
    _failing_node,
    run_mark,
    task_state,
)
from mini_ork.ide_pages import outcome  # noqa: E402
from mini_ork.ide_pages import run as run_page  # noqa: E402
from mini_ork.recovery import retry_hint  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

RUN = "ide-orca-b2a-triage-20261008104922"
T0 = 1_791_000_000

# The live run's shape: the implementer's node_end carries NO finish_reason (the
# node that used to be blamed), the reviewer revised then passed, eval finished,
# and the publisher only ever started (it abstained, so it wrote no end).
LIVE_EVENTS: list[dict[str, Any]] = [
    {"event_type": "node_start", "payload_json": json.dumps({"node_id": "planner"})},
    {"event_type": "node_end", "payload_json": json.dumps({"node_id": "planner", "finish_reason": "done"})},
    {"event_type": "node_start", "payload_json": json.dumps({"node_id": "implementer"})},
    {"event_type": "node_end", "payload_json": json.dumps({"node_id": "implementer"})},
    {"event_type": "node_end", "payload_json": json.dumps({"node_id": "reviewer", "verdict": "needs_revision"})},
    {"event_type": "node_end", "payload_json": json.dumps({"node_id": "reviewer", "finish_reason": "done"})},
    {"event_type": "node_end", "payload_json": json.dumps({"node_id": "eval", "finish_reason": "done"})},
    {"event_type": "node_start", "payload_json": json.dumps({"node_id": "publisher"})},
]

# applies/executes/preserve proven, contract n/a (no behavioral verifier),
# target UNVERIFIED — the level the publisher abstains on.
LIVE_VERDICT: dict[str, Any] = {
    "verdict": "pass",
    "levels": {
        "applies": "PROVEN",
        "executes": "PROVEN",
        "target": "UNVERIFIED",
        "preserve": "PROVEN",
        "contract": "n/a",
    },
    "levels_reasons": {
        "target": "verifier_test.json: replay overlap 0/3 — the suite does not exercise the change",
    },
    "levels_required": ["applies", "executes", "target", "preserve"],
    "levels_ok": False,
    "levels_decision": "abstain",
}


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed_run(home: Path, *, status: str = "failed", recipe: str = "code-fix",
              run_id: str = RUN) -> Path:
    """A run dir + one ``task_runs`` row (+ the kickoff the card title reads)."""
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# Make the demo pass\n\nDetails.\n", encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version, trace_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, 0.5, T0, T0 + 120, T0 + 100, "code_fix",
         str(kickoff), "latest", "tr-demo-1"),
    )
    con.commit()
    con.close()
    return run_dir


def _write_verdict(run_dir: Path, payload: dict[str, Any]) -> None:
    (run_dir / "verdict.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_events(home: Path, run_id: str, events: list[dict[str, Any]]) -> None:
    con = sqlite3.connect(home / "state.db")
    for index, ev in enumerate(events):
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"ev-{run_id}-{index}", run_id, ev["event_type"], ev["payload_json"], T0 + index),
        )
    con.commit()
    con.close()


def _snapshot(*, status: str | None = None, events: list[dict] | None = None) -> dict:
    return {"status": status, "events": events or [], "llm_calls": []}


def _run(home: Path, run_id: str = RUN):
    run = run_page._load(home, run_id)
    assert run is not None
    return run


def _init_repo(tmp_path: Path, name: str = "landed-repo") -> Path:
    project = tmp_path / name
    project.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True,
                   capture_output=True, text=True)
    return project


# ── 1. _failing_node: only an explicit failure signal blames a node ──────────


def test_missing_finish_reason_is_not_failing() -> None:
    """The live bug: a bare ``node_end`` must not name a failing node."""
    events = [{"event_type": "node_end",
               "payload_json": json.dumps({"node_id": "implementer"})}]
    assert _failing_node(events) is None


def test_explicit_error_finish_reason_is_failing() -> None:
    events = [{"event_type": "node_end",
               "payload_json": json.dumps({"node_id": "implementer",
                                           "finish_reason": "error"})}]
    assert _failing_node(events) == ("implementer", "error")


def test_reviewer_needs_revision_then_pass_is_not_failing() -> None:
    """A later clean ``node_end`` of the same id clears the earlier failure."""
    events = [
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "reviewer", "verdict": "needs_revision"})},
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "reviewer", "finish_reason": "done"})},
    ]
    assert _failing_node(events) is None


def test_negative_verdict_and_error_field_are_failing() -> None:
    assert _failing_node([
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "reviewer", "verdict": "REQUEST_CHANGES"})},
    ]) == ("reviewer", "REQUEST_CHANGES")
    assert _failing_node([
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "implementer", "error": "boom"})},
    ]) == ("implementer", "error")


# ── 2. withheld publish → needs_you everywhere ──────────────────────────────


def test_task_state_withheld_is_needs_you(home: Path) -> None:
    run_dir = _seed_run(home)
    _write_verdict(run_dir, LIVE_VERDICT)
    ts = task_state(run_dir, _snapshot(status="failed", events=LIVE_EVENTS))
    assert ts.state == "needs_you"
    assert ts.detail == "Not published: target unverified — review and decide"


def test_run_mark_and_fleet_counts_agree_on_withheld(home: Path) -> None:
    """The cheap tile count and the precise list must not disagree."""
    run_dir = _seed_run(home)
    _write_verdict(run_dir, LIVE_VERDICT)

    assert run_mark("failed", run_dir) == MARKS["needs_you"]

    rows, counts = fleet_mod.fleet_rows(home)
    shown = [r for r in rows if r.run_id == RUN]
    assert len(shown) == 1
    assert shown[0].state == "needs_you"
    assert shown[0].mark == MARKS["needs_you"]
    assert counts["needs_you"] == 1
    assert counts["needs_you"] == sum(1 for r in rows if r.state == "needs_you")


def test_outcome_withheld_card_offers_certify_and_publish_again(home: Path) -> None:
    run_dir = _seed_run(home)
    _write_verdict(run_dir, LIVE_VERDICT)
    (run_dir / "review-reviewer.json").write_text(
        json.dumps({"verdict": "pass", "findings": []}), encoding="utf-8")
    (run_dir / "verifier_typecheck.json").write_text(
        json.dumps({"verifier": "typecheck", "pass": True}), encoding="utf-8")

    out = outcome.resolve(_run(home))
    assert out["state"] == "needs_you"
    assert out["tone"] == "orange" and out["icon"] == "?"
    assert out["text"] == "Not published — target unverified"
    assert "Every step passed (pass, checks 1/1)" in out["detail"]
    assert out["detail"].startswith("Every step passed")
    assert "replay overlap 0/3" in out["detail"]

    labels = [a["label"] for a in out["actions"]]
    assert labels == ["Certify this change", "Publish again"]
    certify = out["actions"][0]
    assert certify["kind"] == "primary"
    assert certify["do"]["page"] == "verify" and certify["do"]["tab"] == "certify"
    assert certify["do"]["args"]["run"] == RUN
    again = out["actions"][1]
    assert again["do"]["cli"] == ["board", "retry", RUN]
    assert "publishes only if the levels are now proven" in again["do"]["confirm"]
    # The level badges ride the counts as usual.
    assert "target UNVERIFIED" in {c["t"] for c in out["counts"]}


def test_refuted_level_uses_the_failed_rule(home: Path) -> None:
    """An explicit REFUTED level means the change was wrong — not "needs you"."""
    run_dir = _seed_run(home)
    payload = json.loads(json.dumps(LIVE_VERDICT))
    payload["levels"]["target"] = "REFUTED"
    _write_verdict(run_dir, payload)

    ts = task_state(run_dir, _snapshot(status="failed", events=LIVE_EVENTS))
    assert ts.state == "failed"

    out = outcome.resolve(_run(home))
    assert out["state"] == "failed" and out["tone"] == "red"
    assert out["text"] == "Not published — target refuted"


def test_genuinely_failed_run_keeps_the_node_and_retry(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No abstain verdict → the old behaviour: the node, its reason, a retry."""
    run_dir = _seed_run(home)
    events = [
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "implementer", "finish_reason": "timeout"})},
    ]
    _seed_events(home, RUN, events)
    ts = task_state(run_dir, _snapshot(status="failed", events=events))
    assert ts.state == "failed"
    assert ts.detail == "Failed at implementer (timeout)"

    monkeypatch.setattr(retry_hint, "load_or_compute",
                        lambda *a, **k: {"retryable": True, "strategy": "resume",
                                         "from_node": "implementer", "needs_change": None})
    out = outcome.resolve(_run(home))
    assert out["state"] == "failed"
    assert out["text"] == "Failed at implementer (timeout)"
    assert "Retry from implementer" in [a["label"] for a in out["actions"]]


# ── 3. landed elsewhere → done everywhere ───────────────────────────────────


def test_landed_json_is_done_everywhere(home: Path, tmp_path: Path) -> None:
    run_dir = _seed_run(home)
    repo = _init_repo(tmp_path)
    (run_dir / "landed.json").write_text(json.dumps({
        "commit": "abcdef1234567890abcdef1234567890abcdef12",
        "repo": str(repo),
        "note": "landed through a later revision",
    }), encoding="utf-8")

    ts = task_state(run_dir, _snapshot(status="failed", events=LIVE_EVENTS))
    assert ts.state == "done"
    assert ts.detail == "Landed via abcdef123 — landed through a later revision"
    assert run_mark("failed", run_dir) == MARKS["done"]

    out = outcome.resolve(_run(home))
    assert out["state"] == "done" and out["tone"] == "green" and out["icon"] == "✓"
    assert out["text"] == "Landed via abcdef123"
    assert out["detail"] == "landed through a later revision"
    assert "Open commit" in [a["label"] for a in out["actions"]]


def test_landed_json_beats_the_withheld_rule(home: Path) -> None:
    """A landed change is done even when the level report also abstained."""
    run_dir = _seed_run(home)
    _write_verdict(run_dir, LIVE_VERDICT)
    (run_dir / "landed.json").write_text(
        json.dumps({"commit": "0123456789abcdef0123456789abcdef01234567"}),
        encoding="utf-8")
    ts = task_state(run_dir, _snapshot(status="failed", events=LIVE_EVENTS))
    assert ts.state == "done"
    assert ts.detail == "Landed via 012345678"
    assert run_mark("failed", run_dir) == MARKS["done"]


# ── 4. retry_hint: a withheld publish is a retryable verify ─────────────────


def test_retry_hint_withheld_is_retryable_verify(home: Path) -> None:
    run_dir = _seed_run(home, recipe="code-fix")
    _write_verdict(run_dir, LIVE_VERDICT)
    _seed_events(home, RUN, [
        {"event_type": "node_start", "payload_json": json.dumps({"node_id": "implementer"})},
        {"event_type": "node_end", "payload_json": json.dumps({"node_id": "implementer"})},
        {"event_type": "node_end", "payload_json": json.dumps({"node_id": "reviewer", "finish_reason": "done"})},
    ])

    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["retryable"] is True
    assert hint["strategy"] == "verify"
    # The `test` verifier proves `target` — the level the publisher withholds on
    # (NOT `typecheck`, the first verifier in topo order).
    assert hint["from_node"] == "test"
    # Nothing must change before a re-verify: a needs_change here would be
    # refused by `recover` / `board retry` without --ack-change.
    assert hint["needs_change"] is None
    # The command re-enters at that same verifier, not the topo-first one.
    assert hint["command"] == f"mini-ork recover {RUN} --strategy verify --from-node test"
    assert any("PROVEN" in n for n in hint["notes"])


def test_retry_hint_declines_when_a_node_really_failed(home: Path) -> None:
    """An abstain verdict plus a failed node is NOT a withheld publish."""
    run_dir = _seed_run(home, recipe="code-fix")
    _write_verdict(run_dir, LIVE_VERDICT)
    _seed_events(home, RUN, [
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "implementer", "finish_reason": "timeout"})},
    ])
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["strategy"] != "verify"


# ── 5. a node that really failed outranks a stale abstain report ────────────


def test_stale_abstain_plus_a_failed_node_is_failed_not_withheld(home: Path) -> None:
    """A recover re-run that died mid-node is NOT a withheld publish.

    The earlier attempt's ``abstain`` verdict.json is still in the run dir, but
    the implementer's node_end now says ``timeout``. The kickoff's gate ("no
    failing node") must win — otherwise a genuine failure is masked and
    "Publish again" is offered for a run that actually died.
    """
    run_dir = _seed_run(home)
    _write_verdict(run_dir, LIVE_VERDICT)  # the stale abstain report
    events = [
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "implementer", "finish_reason": "timeout"})},
    ]
    _seed_events(home, RUN, events)

    ts = task_state(run_dir, _snapshot(status="failed", events=events))
    assert ts.state == "failed"
    assert ts.detail == "Failed at implementer (timeout)"

    out = outcome.resolve(_run(home))
    assert out["state"] == "failed"
    assert out["text"] == "Failed at implementer (timeout)"
    assert "Publish again" not in [a["label"] for a in out["actions"]]


def test_stale_abstain_plus_a_crashed_node_is_failed_not_withheld(home: Path) -> None:
    """The reaper's crash shape: a verdict-only ``node_end``, no finish_reason.

    ``kill_run`` / the run reaper close a dangling start with
    ``{verdict: "CRASH", interrupted: true}`` and no ``finish_reason``
    (``web/control.py:_close_dangling_node_events``). That is the common real
    crash shape, and it must reach the card: the old gate rebuilt events from
    ``run.nodes`` (``finish_reason`` only) and missed it, so the card said
    "needs you" with a "Publish again" button the retry hint refuses.
    """
    run_dir = _seed_run(home)
    _write_verdict(run_dir, LIVE_VERDICT)  # the stale abstain report
    events = [
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "test", "verdict": "CRASH",
                                     "interrupted": True})},
    ]
    _seed_events(home, RUN, events)

    ts = task_state(run_dir, _snapshot(status="failed", events=events))
    assert ts.state == "failed"
    assert ts.detail == "Failed at test (CRASH)"

    # The tile must agree with the row: ``run_mark`` confirms the withheld gate
    # against the lifecycle, so the crash verdict is seen and the mark is failed.
    assert run_mark("failed", run_dir) == MARKS["failed"]
    _rows, counts = fleet_mod.fleet_rows(home)
    assert counts["needs_you"] == 0
    assert counts["failed"] == 1

    out = outcome.resolve(_run(home))
    assert out["state"] == "failed" and out["tone"] == "red"
    assert out["text"] == "Failed at test (CRASH)"
    assert "Publish again" not in [a["label"] for a in out["actions"]]


def test_publisher_levels_unverified_finish_is_not_a_failure(home: Path) -> None:
    """The publisher's own abstain signal must never read as a failed node.

    ``publisher.py`` returns ``(0, "levels_unverified")`` on an abstain, which
    ``dispatch_node`` emits as that node's ``finish_reason``. Blaming the
    publisher there would make every withheld surface decline.
    """
    run_dir = _seed_run(home)
    _write_verdict(run_dir, LIVE_VERDICT)
    events = [
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "publisher",
                                     "finish_reason": "levels_unverified"})},
    ]
    _seed_events(home, RUN, events)

    ts = task_state(run_dir, _snapshot(status="failed", events=events))
    assert ts.state == "needs_you"
    assert ts.detail == "Not published: target unverified — review and decide"

    out = outcome.resolve(_run(home))
    assert out["state"] == "needs_you"
    assert "Publish again" in [a["label"] for a in out["actions"]]


# ── 6. the failed rule never guesses a node ────────────────────────────────


def test_failed_rule_never_guesses_a_node(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No failing node → the text is "Failed", never "Failed at <guess>".

    ``retry_hint`` case 5 fills ``from_node`` from the last ``impl-*.log`` — a
    best-effort guess. The kickoff's live message ("Failed at implementer; the
    cause was not classified") came from exactly that; the text must be a bare
    "Failed" with the hint's summary in the detail.
    """
    run_dir = _seed_run(home)
    events = [
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "implementer"})},  # bare
    ]
    _seed_events(home, RUN, events)
    (run_dir / "impl-implementer.log").write_text("working…\n", encoding="utf-8")

    ts = task_state(run_dir, _snapshot(status="failed", events=events))
    assert ts.detail == "Failed"

    monkeypatch.setattr(retry_hint, "load_or_compute", lambda *a, **k: {
        "retryable": False, "strategy": "none",
        "from_node": "implementer", "failed_node": "implementer",
        "needs_change": {"kind": "unknown",
                         "summary": "Failed at implementer; the cause was not classified"},
    })
    out = outcome.resolve(_run(home))
    assert out["state"] == "failed"
    assert out["text"] == "Failed"
    assert "the cause was not classified" in out["detail"]


# ── 7. the hint cache never serves a pre-fix classification ────────────────


def test_load_or_compute_recomputes_a_stale_version_hint(home: Path) -> None:
    """A ``retry-hint.json`` at an older ``HINT_VERSION`` is not authoritative.

    The live hole: ``recover`` wrote a strategy-none hint AFTER the run's
    abstain verdict.json, and the mtime-only cache check served it forever.
    """
    run_dir = _seed_run(home, recipe="code-fix")
    _write_verdict(run_dir, LIVE_VERDICT)
    _seed_events(home, RUN, [
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "implementer"})},
        {"event_type": "node_end",
         "payload_json": json.dumps({"node_id": "reviewer", "finish_reason": "done"})},
    ])
    stale = {
        "version": retry_hint.HINT_VERSION - 1,
        "run_id": RUN, "failed_node": "implementer", "retryable": False,
        "strategy": "none", "from_node": "implementer",
        "needs_change": {"kind": "unknown",
                         "summary": "Failed at implementer; the cause was not classified"},
        "notes": [], "command": "", "computed_at": "2026-10-08T00:00:00Z",
        "status": "failed",
    }
    cache = run_dir / retry_hint.CACHE_FILENAME
    time.sleep(0.01)
    cache.write_text(json.dumps(stale), encoding="utf-8")
    # The cache file is strictly the newest artefact in the run dir, so ONLY the
    # version check stands between the caller and the stale classification —
    # the mtime gate alone would have served it.
    assert cache.stat().st_mtime_ns > (run_dir / "verdict.json").stat().st_mtime_ns

    hint = retry_hint.load_or_compute(home, RUN, write=False)
    assert hint is not None
    assert hint["strategy"] == "verify", hint


def test_load_or_compute_still_serves_a_current_version_hint(home: Path) -> None:
    """The version check must not defeat the cache for an up-to-date hint."""
    run_dir = _seed_run(home, recipe="code-fix")
    _write_verdict(run_dir, LIVE_VERDICT)
    cached = {
        "version": retry_hint.HINT_VERSION,
        "run_id": RUN, "failed_node": None, "retryable": False,
        "strategy": "none", "from_node": None, "needs_change": None,
        "notes": [], "command": "", "computed_at": "2026-10-08T00:00:00Z",
        "status": "failed",
    }
    cache = run_dir / retry_hint.CACHE_FILENAME
    cache.write_text(json.dumps(cached), encoding="utf-8")

    hint = retry_hint.load_or_compute(home, RUN, write=False)
    assert hint is not None and hint["strategy"] == "none"
