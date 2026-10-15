"""The run buddy — an optional observer that narrates a run stage by stage.

*What happened, what happens next, and what the operator can do about it*,
reported after each node and — inside a node that runs long — on an interval.
It is a hook, not a recipe node: the engine cannot inject an engine-owned node
into a user recipe, and doing so would change every run's graph. It is off
unless the ``run_buddy`` feature is enabled (``MO_BUDDY=1``; see
``mini_ork/features/registry.py``).

Two trigger points, one report shape:

* :func:`report_boundary` — always writes, at every node's end. This is the
  stage-by-stage narrative the reader sees after the fact.
* :class:`Watch` — a daemon thread started around a node, ticking every
  :func:`interval_seconds`. It writes **only when a signal crosses**
  (:func:`judge`); a healthy node produces nothing at all.

The node is a blocking in-process call (verifiers run through
``mini_ork.runtime.contract.run_check``, agent lanes through
``mini_ork.dispatch.core``), so the dispatcher is alive and parked for exactly
the window an in-stage check is worth having — hence the thread.

**Files only.** The watcher reads the run dir and writes
``<run_dir>/buddy/<epoch>.json``, never the database, so it cannot contend with
the run's own writers. Nothing here may raise into a run: every entry point
returns ``None`` on any internal error, the same discipline
``mini_ork.orchestration.runner_orchestrator.on_node_boundary`` holds itself to.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# ── env contract ─────────────────────────────────────────────────────────────
_ENV_FLAG = "MO_BUDDY"
_ENV_INTERVAL = "MO_BUDDY_INTERVAL_SECONDS"
_ENV_STALL = "MO_BUDDY_STALL_SECONDS"
_ENV_LONG_NODE = "MO_BUDDY_LONG_NODE_SECONDS"
_ENV_COST = "MO_BUDDY_COST_USD"

# Off unless explicitly enabled — unlike the run orchestrator (whose flag is
# default-ON with a budget brake), this one spends money on prose, so a typo or
# an unset variable must leave it off.
_FALSY = frozenset({"", "0", "false", "no", "off"})

_DEFAULT_INTERVAL = 180
_DEFAULT_STALL = 300
_DEFAULT_LONG_NODE = 900
_DEFAULT_COST_USD = 1.0

# Identical normalized lines in the tail before "the same error on repeat".
_REPEAT_LIMIT = 3
_TAIL_LINES = 200

DIRNAME = "buddy"
REPORT_VERSION = 1

# ── signal vocabulary ────────────────────────────────────────────────────────
STALLED = "stalled"
REPEATING_ERROR = "repeating_error"
DURATION_OVERRUN = "duration_overrun"
COST_OVERRUN = "cost_overrun"
# Not a sampled signal: the stage ended badly, which only a boundary can know.
FAILED = "failed"

# Signals that end in "resume this node" (see :func:`actions_for`).
_RETRY_HINTS = (STALLED, REPEATING_ERROR, DURATION_OVERRUN, FAILED)

# Log candidates in the order the node view looks for them
# (``ide_pages.node._overview_command_headline``).
_LOG_TEMPLATES = ("verifier_{node}.log", "evidence/{node}.log", "impl-{node}.log")


# ── flag / knobs ─────────────────────────────────────────────────────────────
def _truthy(raw: str) -> bool:
    return raw.strip().lower() not in _FALSY


def enabled() -> bool:
    """True only when ``MO_BUDDY`` is set to something explicitly on."""
    return _truthy(os.environ.get(_ENV_FLAG, ""))


def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def interval_seconds() -> int:
    return _env_int(_ENV_INTERVAL, _DEFAULT_INTERVAL)


def stall_seconds() -> int:
    return _env_int(_ENV_STALL, _DEFAULT_STALL)


def long_node_seconds() -> int:
    """How long a node may run before "long" alone is worth a word."""
    return _env_int(_ENV_LONG_NODE, _DEFAULT_LONG_NODE)


def cost_cap_usd() -> float:
    """The run's spend cap for this feature; falls back to the orchestrator's."""
    default = _env_float("MO_ORCH_BUDGET_USD", _DEFAULT_COST_USD)
    return _env_float(_ENV_COST, default)


# ── observation ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Observation:
    """One instant of a node's life, read from files only."""

    run_id: str
    node_id: str
    node_type: str
    started_at: float
    now: float
    idle_seconds: float
    spent_usd: float = 0.0
    repeats: int = 0
    repeat_line: str = ""
    last_line: str = ""

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.now - self.started_at)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def node_log(run_dir: Path, node_id: str) -> Path | None:
    """The node's own log file, when it has one."""
    for template in _LOG_TEMPLATES:
        candidate = run_dir / template.format(node=node_id)
        if candidate.is_file():
            return candidate
    return None


def _normalize(line: str) -> str:
    """A log line with its variable parts masked, so repeats compare equal."""
    stripped = re.sub(r"\d+", "#", line.strip())
    stripped = re.sub(r"0x[0-9a-fA-F]+", "#", stripped)
    return re.sub(r"\s+", " ", stripped)


def _tail(path: Path | None) -> list[str]:
    if path is None:
        return []
    lines = _read_text(path).splitlines()
    return [ln for ln in lines[-_TAIL_LINES:] if ln.strip()]


def _repeat_of(lines: list[str]) -> tuple[int, str]:
    """The most-repeated normalized line and its count (``0, ""`` when none)."""
    counts: dict[str, int] = {}
    for line in lines:
        key = _normalize(line)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return 0, ""
    top = max(counts, key=lambda key: counts[key])
    return counts[top], top


def _spent_usd(run_dir: Path) -> float:
    """The run's spend so far, from the ``.cost-spent`` sidecar."""
    try:
        return float(_read_text(run_dir / ".cost-spent").strip() or 0.0)
    except ValueError:
        return 0.0


def observe(
    run_dir: Path,
    *,
    run_id: str,
    node_id: str,
    node_type: str = "",
    started_at: float,
    now: float | None = None,
) -> Observation:
    """Sample everything the signals need. Never raises on a missing file."""
    now = time.time() if now is None else now
    log = node_log(run_dir, node_id)
    lines = _tail(log)
    idle = now - started_at
    if log is not None:
        try:
            idle = now - log.stat().st_mtime
        except OSError:
            pass
    repeats, repeat_line = _repeat_of(lines)
    if repeats < _REPEAT_LIMIT:
        repeats, repeat_line = 0, ""
    return Observation(
        run_id=run_id,
        node_id=node_id,
        node_type=node_type,
        started_at=started_at,
        now=now,
        idle_seconds=max(0.0, idle),
        spent_usd=_spent_usd(run_dir),
        repeats=repeats,
        repeat_line=repeat_line,
        last_line=lines[-1].strip() if lines else "",
    )


# ── judgement (pure) ─────────────────────────────────────────────────────────
def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def judge(
    obs: Observation,
    *,
    stall: int | None = None,
    long_node: int | None = None,
    cost_cap: float | None = None,
) -> list[dict[str, str]]:
    """The signals this observation crosses, most urgent first.

    Pure: same observation in, same signals out. An uninteresting node yields
    ``[]`` — the caller then writes nothing.
    """
    stall = stall_seconds() if stall is None else stall
    long_node = long_node_seconds() if long_node is None else long_node
    cost_cap = cost_cap_usd() if cost_cap is None else cost_cap

    signals: list[dict[str, str]] = []
    if obs.repeats >= _REPEAT_LIMIT:
        signals.append({
            "kind": REPEATING_ERROR,
            "detail": f"the same line {obs.repeats}x: {obs.repeat_line[:160]}",
        })
    if obs.idle_seconds >= stall:
        signals.append({
            "kind": STALLED,
            "detail": f"nothing new for {_clock(obs.idle_seconds)}",
        })
    # A node past the deadline budget, or simply far past what a node takes.
    overrun = obs.elapsed_seconds > long_node
    if overrun:
        signals.append({
            "kind": DURATION_OVERRUN,
            "detail": f"running for {_clock(obs.elapsed_seconds)}",
        })
    if obs.spent_usd > cost_cap:
        signals.append({
            "kind": COST_OVERRUN,
            "detail": f"the run has spent ${obs.spent_usd:.2f} of a ${cost_cap:.2f} cap",
        })
    return signals


def _looks_bad(line: str) -> bool:
    lowered = line.lower()
    return any(token in lowered for token in
               ("error", "fail", "traceback", "exception", "timeout", "refused", "not found"))


# ── the report ───────────────────────────────────────────────────────────────
def actions_for(run_id: str, node_id: str, signals: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The buttons this report offers, as argv + label (the UI renders them).

    Every action is a CLI invocation with a ``confirm``: the operator sees what
    will run before it runs. No shell string is ever built here — the argv list
    is what ``spec.cli`` turns into a button.
    """
    kinds = {s.get("kind") for s in signals}
    actions: list[dict[str, Any]] = []
    if kinds & set(_RETRY_HINTS):
        actions.append({
            "label": f"Resume from {node_id}",
            "argv": ["recover", run_id, "--from-node", node_id],
            "confirm": f"Resume {run_id} from {node_id}?",
        })
    actions.append({
        "label": "Open the run page",
        "argv": [],
        "page": {"key": "run", "args": {"run": run_id}},
    })
    return actions


def _summary(obs: Observation, signals: list[dict[str, str]]) -> str:
    """The one-line "what happened" — deterministic, no model needed."""
    if not signals:
        return f"{obs.node_id} finished its stage."
    head = signals[0]
    if head["kind"] == FAILED:
        return f"{obs.node_id} failed — {head['detail']}"
    if head["kind"] == REPEATING_ERROR:
        return f"{obs.node_id} keeps hitting the same failure — {head['detail']}"
    if head["kind"] == STALLED:
        return f"{obs.node_id} has gone quiet — {head['detail']}"
    if head["kind"] == DURATION_OVERRUN:
        return f"{obs.node_id} is taking a long time — {head['detail']}"
    if head["kind"] == COST_OVERRUN:
        return f"The run is over its cost cap — {head['detail']}"
    return f"{obs.node_id}: {head['detail']}"


def _next_step(obs: Observation, signals: list[dict[str, str]]) -> str:
    """The one-line "what happens / what to do next"."""
    kinds = {s.get("kind") for s in signals}
    if REPEATING_ERROR in kinds:
        return ("A plain retry will hit the same thing. Fix the cause, then resume from "
                f"{obs.node_id}.")
    if STALLED in kinds:
        return (f"Either it is waiting on something that will not arrive, or the dispatcher is "
                f"gone. Resuming from {obs.node_id} is safe when the run is dead.")
    if DURATION_OVERRUN in kinds:
        return (f"The run's deadline will kill it soon if it does not finish. Check whether "
                f"{obs.node_id} is doing more than the stage needs.")
    if COST_OVERRUN in kinds:
        return "Stop and look at the spend before letting the run continue."
    if FAILED in kinds:
        return f"Fix the cause, then resume from {obs.node_id} — or roll the run back."
    if not signals and obs.last_line and _looks_bad(obs.last_line):
        return f"Last line reads like trouble: {obs.last_line[:160]}"
    return "Nothing to do — the next node is starting."


def narrate(obs: Observation, signals: list[dict[str, str]], *, model: str = "") -> str:
    """An optional model-written summary; ``""`` when there is no provider.

    The report is useful without this — :func:`_summary` and :func:`_next_step`
    always produce the findings. This is the secure tier's extra: prose that
    reads the situation, capped by :func:`cost_cap_usd` and recorded in
    ``llm_calls`` like any other call. Any failure (no lane, no key, bad output)
    degrades to the template, so the feature never depends on a model.
    """
    if not signals:
        return ""
    try:
        from mini_ork.dispatch.providers import DispatchRequest, dispatch_model
        from mini_ork.dispatch.telemetry import persist_call

        prompt = (
            "You are watching one stage of an automated engineering run. Reply with two short "
            "sentences: what is happening, and what the operator should do next. No preamble.\n\n"
            f"node: {obs.node_id} ({obs.node_type})\n"
            f"elapsed: {_clock(obs.elapsed_seconds)}, idle: {_clock(obs.idle_seconds)}\n"
            f"signals: {json.dumps(signals)}\n"
            f"last line: {obs.last_line[:300]}\n"
        )
        result = dispatch_model(DispatchRequest(model=model or os.environ.get("MO_BUDDY_LANE", ""),
                                                prompt=prompt))
        if not result.ok:
            return ""
        db = os.environ.get("MINI_ORK_DB", "")
        if db:
            try:
                from mini_ork.dispatch.providers import provider_for_model

                persist_call(db, result, provider=provider_for_model(result.model),
                             feature_name="run_buddy", run_id=obs.run_id)
            except Exception:  # noqa: BLE001 — telemetry must never break a report
                pass
        return result.text.strip()[:600]
    except Exception:  # noqa: BLE001 — a missing provider is not an error here
        return ""


def _report(obs: Observation, signals: list[dict[str, str]], *, phase: str,
            when: float | None = None, narrative: str = "") -> dict[str, Any]:
    return {
        "v": REPORT_VERSION,
        "at": int(when if when is not None else obs.now),
        "run_id": obs.run_id,
        "node_id": obs.node_id,
        "node_type": obs.node_type,
        "phase": phase,
        "elapsed_s": int(obs.elapsed_seconds),
        "signals": [s.get("kind", "") for s in signals],
        "summary": _summary(obs, signals),
        "detail": "; ".join(s.get("detail", "") for s in signals),
        "next": _next_step(obs, signals),
        "narrative": narrative,
        "actions": actions_for(obs.run_id, obs.node_id, signals),
    }


def write_report(run_dir: Path, report: dict[str, Any]) -> Path | None:
    """Write one report to ``<run_dir>/buddy/<at>.json``; ``None`` on failure.

    Append-only: the numbered history is what makes the stage-by-stage story
    readable later. ``latest.json`` is a convenience pointer for the readers.
    """
    directory = Path(run_dir) / DIRNAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        body = json.dumps(report, indent=2)
        stamp = int(report.get("at") or time.time())
        target = directory / f"{stamp}.json"
        # Same-second reports (a boundary landing on an interval tick) must not
        # silently overwrite each other.
        if target.exists():
            target = directory / f"{stamp}-{os.getpid()}.json"
        target.write_text(body, encoding="utf-8")
        (directory / "latest.json").write_text(body, encoding="utf-8")
        return target
    except OSError:
        return None


def read_reports(run_dir: Path) -> list[dict[str, Any]]:
    """Every report for the run, oldest first. Unreadable files are skipped."""
    directory = Path(run_dir) / DIRNAME
    try:
        paths = sorted(p for p in directory.glob("*.json") if p.name != "latest.json")
    except OSError:
        return []
    reports: list[dict[str, Any]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("v") == REPORT_VERSION:
            reports.append(data)
    return reports


def latest_report(run_dir: Path) -> dict[str, Any] | None:
    """The most recent report, or ``None`` when there is none."""
    reports = read_reports(run_dir)
    return reports[-1] if reports else None


# ── triggers ─────────────────────────────────────────────────────────────────
def assess(
    home: Path,
    *,
    run_id: str,
    node_id: str,
    node_type: str = "",
    started_at: float,
    phase: str = "in-stage",
    now: float | None = None,
    only_when_worried: bool = True,
    failed: bool = False,
) -> dict[str, Any] | None:
    """Sample, judge, and (when interesting, or at a boundary) write.

    ``only_when_worried`` is the in-stage rule: a healthy node writes nothing.
    The boundary call passes ``False`` so every stage gets its line in the
    story. ``failed`` adds the one signal only a boundary can know — that the
    stage ended badly — so a fast failure with no sampled signal still reads as
    a failure rather than as "finished its stage".

    Returns the report it wrote, or ``None`` when there was nothing to say.
    """
    if not enabled():
        return None
    try:
        run_dir = Path(home) / "runs" / run_id
        obs = observe(run_dir, run_id=run_id, node_id=node_id, node_type=node_type,
                      started_at=started_at, now=now)
        signals = judge(obs)
        if failed and not any(s.get("kind") in _RETRY_HINTS for s in signals):
            signals.insert(0, {"kind": FAILED,
                               "detail": obs.last_line[:160] or "the node did not complete"})
        if only_when_worried and not signals:
            return None
        report = _report(obs, signals, phase=phase, narrative=narrate(obs, signals))
        write_report(run_dir, report)
        return report
    except Exception:  # noqa: BLE001 — the buddy must never fail a run
        return None


def report_boundary(
    home: Path,
    *,
    run_id: str,
    node_id: str,
    node_type: str = "",
    started_at: float,
    failed: bool = False,
    now: float | None = None,
) -> dict[str, Any] | None:
    """The stage-boundary report: always written, at every node's end."""
    return assess(home, run_id=run_id, node_id=node_id, node_type=node_type,
                  started_at=started_at, phase="stage", now=now,
                  only_when_worried=False, failed=failed)


class Watch:
    """The in-stage ticker: one daemon thread per running node.

    Ticks on :func:`interval_seconds`; each tick is :func:`assess` with
    ``watch_only=True``, so a healthy node writes nothing. Owns no state the run
    depends on — dropping it, or a failed tick, changes nothing for the run.
    """

    def __init__(
        self,
        home: Path,
        *,
        run_id: str,
        node_id: str,
        node_type: str = "",
        started_at: float | None = None,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self.home = Path(home)
        self.run_id = run_id
        self.node_id = node_id
        self.node_type = node_type
        self.started_at = time.time() if started_at is None else started_at
        # ``sleep`` returns False when the wait was cut short (the stop event).
        self._sleep = sleep or self._event_sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _event_sleep(self, seconds: float) -> bool:
        return not self._stop.wait(seconds)

    def tick(self, *, now: float | None = None) -> dict[str, Any] | None:
        """One check. Public so tests drive it directly, with no thread."""
        return assess(self.home, run_id=self.run_id, node_id=self.node_id,
                      node_type=self.node_type, started_at=self.started_at,
                      phase="in-stage", now=now, only_when_worried=True)

    def _loop(self) -> None:
        interval = interval_seconds()
        while self._sleep(interval):
            self.tick()

    def start(self) -> None:
        """Begin ticking. A no-op when the feature is off."""
        if not enabled():
            return
        try:
            self._thread = threading.Thread(target=self._loop, name=f"run-buddy-{self.node_id}",
                                            daemon=True)
            self._thread.start()
        except RuntimeError:  # pragma: no cover - can only fail with no threads left
            self._thread = None

    def stop(self) -> None:
        """Stop ticking and wait briefly for the thread to notice."""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
