"""Unit tests for the orchestrator's interval cadence and its state file.

The observer is pure: fixed clock, injected sampler/judge, a tmp run dir. The
process, the DB and the steering write are the CLI shell's job and are tested
there. What matters here is that the policy — when to speak, when to nudge, and
when to stop — is deterministic and fails closed.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from mini_ork.orchestration import runner_orchestrator as ro

NOW = 1_800_000_000.0


def _event(kind: str, node_id: str = "impl", created_at: float = NOW - 10) -> dict:
    return {
        "event_type": kind,
        "created_at": created_at,
        "payload_json": json.dumps({"node_id": node_id, "node_type": "implementer"}),
    }


def _inputs(*, status: str | None = "executing", events: list | None = None,
            probe: str = "alive", pid_present: bool = True,
            stop_requested: bool = False) -> ro.WatchInputs:
    return ro.WatchInputs(
        status=status,
        events=tuple(events if events is not None else [_event("node_start")]),
        probe=probe,
        pid_present=pid_present,
        stop_requested=stop_requested,
    )


class _Sample:
    """Stands in for ``watch.sample``; mutable so a test can age a node."""

    def __init__(self, *, idle=10.0, elapsed=60.0, spent=0.0, repeats=0,
                 repeat_line="", node_id="impl"):
        self.idle = idle
        self.elapsed = elapsed
        self.spent = spent
        self.repeats = repeats
        self.repeat_line = repeat_line
        self.node_id = node_id

    def __call__(self, run_dir, *, run_id, node_id, node_type, started_at, now):
        return ro_observation(
            node_id=self.node_id, run_id=run_id, started_at=now - self.elapsed,
            now=now, idle=self.idle, spent=self.spent, repeats=self.repeats,
            repeat_line=self.repeat_line,
        )


def ro_observation(**kwargs):
    # Imported lazily so this module stays importable without the package
    # __init__ (which pulls harness/dispatch) at test collection time.
    from mini_ork.acp_orchestrator.watch import Observation

    return Observation(
        run_id=kwargs["run_id"],
        node_id=kwargs["node_id"],
        node_type="implementer",
        started_at=kwargs["started_at"],
        now=kwargs["now"],
        idle_seconds=kwargs["idle"],
        spent_usd=kwargs["spent"],
        repeats=kwargs["repeats"],
        repeat_line=kwargs["repeat_line"],
    )


def _no_signals(_obs):
    return []


def _stalled(_obs):
    return [{"kind": "stalled", "detail": "nothing new for 12m00s"}]


def _observe(tmp_path, inputs=None, state=None, config=None, sampler=None,
             judge=None, **kwargs):
    return ro.observe(
        now=NOW,
        run_id="run-1",
        run_dir=tmp_path,
        inputs=inputs or _inputs(),
        state=state if state is not None else ro.WatchState(),
        config=config,
        sampler=sampler or _Sample(),
        judge_fn=judge or _no_signals,
        **kwargs,
    )


# ── config ───────────────────────────────────────────────────────────────────
def test_config_defaults(monkeypatch):
    for name in ("MO_ORCH_WATCH_INTERVAL_S", "MO_ORCH_WATCH_AUTONOMY",
                 "MO_ORCH_WATCH_MAX_STEERS", "MO_ORCH_WATCH_MAX_HOURS"):
        monkeypatch.delenv(name, raising=False)
    cfg = ro.WatchConfig.from_env()
    assert cfg.interval_s == 20.0
    assert cfg.autonomy == "steer"
    assert cfg.max_steers == 2
    assert cfg.max_hours == 12.0


def test_config_unknown_autonomy_fails_closed_to_notify(monkeypatch):
    monkeypatch.setenv("MO_ORCH_WATCH_AUTONOMY", "STEER-EVERYTHING")
    assert ro.WatchConfig.from_env().autonomy == "notify"


def test_config_bad_numbers_fall_back(monkeypatch):
    monkeypatch.setenv("MO_ORCH_WATCH_INTERVAL_S", "soon")
    monkeypatch.setenv("MO_ORCH_WATCH_MAX_STEERS", "-4")
    monkeypatch.setenv("MO_ORCH_WATCH_MAX_HOURS", "0")
    cfg = ro.WatchConfig.from_env()
    assert cfg.interval_s == 20.0
    assert cfg.max_steers == 0
    assert cfg.max_hours == 12.0


def test_watch_flag(monkeypatch):
    monkeypatch.delenv("MO_ORCH_WATCH", raising=False)
    assert ro.watch_enabled() is True
    for value in ("0", "false", "NO", "off"):
        monkeypatch.setenv("MO_ORCH_WATCH", value)
        assert ro.watch_enabled() is False, value
    monkeypatch.setenv("MO_ORCH_WATCH", "")
    assert ro.watch_enabled() is True


# ── state file ───────────────────────────────────────────────────────────────
def test_state_round_trip(tmp_path):
    state = ro.WatchState(pid=123, started_at=NOW, last_tick=NOW, tick_count=4,
                          steers=1, signaled={"impl:stalled": NOW},
                          steered={"impl:stalled": NOW},
                          steer_attempts={"impl:stalled": 1},
                          pending={"key": "impl:stalled"})
    ro.save_watch_state(tmp_path, state)
    loaded = ro.load_watch_state(tmp_path)
    assert loaded.pid == 123
    assert loaded.tick_count == 4
    assert loaded.signaled == {"impl:stalled": NOW}
    assert loaded.pending == {"key": "impl:stalled"}


def test_state_corrupt_file_reads_as_fresh(tmp_path):
    ro.watch_state_path(tmp_path).write_text("{not json", encoding="utf-8")
    state = ro.load_watch_state(tmp_path)
    assert state.tick_count == 0
    assert state.ended_reason is None


def test_state_file_is_owner_only(tmp_path):
    ro.save_watch_state(tmp_path, ro.WatchState(tick_count=1))
    mode = os.stat(ro.watch_state_path(tmp_path)).st_mode & 0o777
    assert mode == 0o600


def test_watch_active_requires_fresh_live_pid(tmp_path):
    state = ro.WatchState(pid=os.getpid(), last_tick=time.time(), tick_count=3)
    ro.save_watch_state(tmp_path, state)
    assert ro.watch_active(tmp_path) is True
    assert ro.watch_active(tmp_path, now=time.time() + 3600) is False

    state.ended_reason = "terminal:published"
    ro.save_watch_state(tmp_path, state)
    assert ro.watch_active(tmp_path) is False


def test_watch_active_false_without_a_tick_or_live_pid(tmp_path):
    assert ro.watch_active(tmp_path) is False
    ro.save_watch_state(tmp_path, ro.WatchState(pid=0, last_tick=time.time(),
                                                tick_count=3))
    assert ro.watch_active(tmp_path) is False


# ── observation policy ───────────────────────────────────────────────────────
def test_first_tick_announces_once(tmp_path):
    state = ro.WatchState()
    first = _observe(tmp_path, state=state)
    kinds = [kind for kind, _ in first.actions]
    assert kinds == ["orchestrator.watching"]
    assert "no signal yet" in first.actions[0][1]["reason"]
    second = _observe(tmp_path, state=state)
    assert [kind for kind, _ in second.actions] == []


def test_healthy_run_stays_silent(tmp_path):
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1))
    assert decision.actions == ()
    assert decision.steer is None


def test_signal_is_emitted_once_per_node_and_kind(tmp_path):
    state = ro.WatchState(tick_count=1)
    first = _observe(tmp_path, state=state, judge=_stalled)
    assert [kind for kind, _ in first.actions] == ["orchestrator.signal"]
    payload = first.actions[0][1]
    assert payload["kind"] == "stalled"
    assert payload["node_id"] == "impl"
    assert "nothing new" in payload["reason"]

    again = _observe(tmp_path, state=state, judge=_stalled)
    assert again.actions == ()


def test_steer_on_stall_when_live_and_alive(tmp_path):
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1), judge=_stalled)
    assert decision.steer is not None
    assert decision.steer.key == "impl:stalled"
    assert ro.guard_steer(decision.steer.message)
    assert "Orchestrator note for impl" in decision.steer.message


def test_notify_never_steers(tmp_path):
    config = ro.WatchConfig(autonomy="notify")
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1),
                        config=config, judge=_stalled)
    assert decision.steer is None
    assert decision.actions


def test_duration_and_cost_are_notify_only(tmp_path):
    def judge(_obs):
        return [
            {"kind": "duration_overrun", "detail": "running for 20m00s"},
            {"kind": "cost_overrun", "detail": "spent $2.00"},
        ]

    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1), judge=judge)
    assert len(decision.actions) == 2
    assert decision.steer is None


def test_no_steer_when_stop_requested(tmp_path):
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1),
                        inputs=_inputs(stop_requested=True), judge=_stalled)
    assert decision.steer is None


@pytest.mark.parametrize("status", ["classifying", "failed", "published", None])
def test_no_steer_outside_a_live_status(tmp_path, status):
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1),
                        inputs=_inputs(status=status), judge=_stalled)
    assert decision.steer is None


def test_no_steer_over_budget(tmp_path):
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1), judge=_stalled,
                        budget_fn=lambda spent: False)
    assert decision.steer is None
    assert decision.actions  # the signal is still reported


def test_steer_cap_is_respected(tmp_path):
    config = ro.WatchConfig(max_steers=1)
    state = ro.WatchState(tick_count=1, steers=1)
    decision = _observe(tmp_path, state=state, config=config, judge=_stalled)
    assert decision.steer is None


def test_steer_attempts_cap_and_pending_retry(tmp_path):
    state = ro.WatchState(tick_count=1)
    first = _observe(tmp_path, state=state, judge=_stalled)
    assert first.steer is not None
    assert state.steer_attempts["impl:stalled"] == 1

    # A failed write leaves ``pending`` set; the next tick offers it again
    # without waiting for a new signal.
    retry = _observe(tmp_path, state=state, judge=_no_signals)
    assert retry.steer is not None
    assert retry.steer.key == "impl:stalled"
    assert state.steer_attempts["impl:stalled"] == 2

    state.pending = None
    state.steer_attempts["impl:stalled"] = ro._MAX_STEER_ATTEMPTS
    state.signaled.pop("impl:stalled", None)
    assert _observe(tmp_path, state=state, judge=_stalled).steer is None


def test_message_falls_back_when_detail_trips_the_guard(tmp_path):
    def judge(_obs):
        return [{"kind": "stalled", "detail": "schema mismatch on exit_code"}]

    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1), judge=judge)
    assert decision.steer is not None
    assert "schema" not in decision.steer.message
    assert "exit_code" not in decision.steer.message
    assert ro.guard_steer(decision.steer.message)


# ── exit policy ──────────────────────────────────────────────────────────────
def test_disabled_flag_exits(tmp_path):
    decision = _observe(tmp_path, state=ro.WatchState(),
                        enabled_fn=lambda: False)
    assert decision.exit_reason == "disabled"
    assert decision.actions[-1][0] == "orchestrator.ended"
    assert decision.actions[-1][1]["exit"] == "disabled"


def test_autonomy_off_exits(tmp_path):
    decision = _observe(tmp_path, state=ro.WatchState(),
                        config=ro.WatchConfig(autonomy="off"))
    assert decision.exit_reason == "off"


def test_max_lifetime_exits(tmp_path):
    state = ro.WatchState(started_at=NOW - 13 * 3600)
    decision = _observe(tmp_path, state=state)
    assert decision.exit_reason == "max-lifetime"


@pytest.mark.parametrize("status", ["published", "failed", "rolled_back", "not_needed"])
def test_terminal_status_ends_the_watch(tmp_path, status):
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1),
                        inputs=_inputs(status=status))
    assert decision.exit_reason == f"terminal:{status}"
    assert decision.actions[-1][1]["status"] == status


def test_terminal_wins_over_a_dead_probe(tmp_path):
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1),
                        inputs=_inputs(status="published", probe="dead"))
    assert decision.exit_reason == "terminal:published"


def test_dead_dispatcher_ends_with_detail(tmp_path):
    inputs = ro.WatchInputs(status="executing", events=(), probe="dead",
                            probe_detail="pid 42 gone")
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1), inputs=inputs)
    assert decision.exit_reason == "dispatcher-dead"
    assert decision.actions[-1][1]["reason"] == "pid 42 gone"


def test_finished_run_ends(tmp_path):
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1),
                        inputs=_inputs(probe="finished"))
    assert decision.exit_reason == "finished"


@pytest.mark.parametrize("probe", ["remote", "paused"])
def test_remote_and_paused_idle_without_steering(tmp_path, probe):
    state = ro.WatchState(tick_count=1)
    decision = _observe(tmp_path, state=state, inputs=_inputs(probe=probe),
                        judge=_stalled)
    assert decision.exit_reason is None
    assert decision.actions == ()
    assert decision.steer is None
    assert state.missing_pid_ticks == 0


def test_pid_must_be_absent_for_two_ticks(tmp_path):
    state = ro.WatchState(tick_count=1)
    first = _observe(tmp_path, state=state, inputs=_inputs(pid_present=False))
    assert first.exit_reason is None
    assert state.missing_pid_ticks == 1
    second = _observe(tmp_path, state=state, inputs=_inputs(pid_present=False))
    assert second.exit_reason == "owner-record-gone"


def test_pid_present_resets_the_counter(tmp_path):
    state = ro.WatchState(tick_count=1, missing_pid_ticks=1)
    decision = _observe(tmp_path, state=state, inputs=_inputs(pid_present=True))
    assert decision.exit_reason is None
    assert state.missing_pid_ticks == 0


def test_no_live_node_is_quiet(tmp_path):
    decision = _observe(tmp_path, state=ro.WatchState(tick_count=1),
                        inputs=_inputs(events=[_event("node_start"),
                                               _event("node_end")]),
                        judge=_stalled)
    assert decision.actions == ()
    assert decision.steer is None


# ── wiring guarantees ────────────────────────────────────────────────────────
def test_observe_has_no_db_or_boundary_coupling():
    import inspect

    params = inspect.signature(ro.observe).parameters
    assert "db" not in params
    source = inspect.getsource(ro.observe)
    assert "on_node_boundary" not in source
    assert "mo_run_emit" not in source
