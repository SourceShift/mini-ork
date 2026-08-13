"""Run backends for GEPA prompt optimization — the bring-your-own-evaluator seam.

A ``RunBackend`` is the thing GEPA's adapter calls to turn a candidate prompt
into a graded outcome. It has three responsibilities, deliberately split so a
caller can supply any one of them:

    run(candidate, task) -> ExternalTrace   # produce an outcome for one example
    score(trace)         -> float           # the scalar GEPA optimizes (0..1)
    feedback(trace, score) -> str           # the NL diagnosis GEPA reflects on

The recipe/DB-backed path (``mini_ork.gepa.miniork_adapter.MiniOrkGEPAAdapter``)
is one specialization; this module adds two GENERIC backends so an *external*
prompt can be optimized against an *external* critic that has nothing to do with
mini-ork recipes or the state.db:

- ``CallbackRunBackend`` — in-process Python. Give it a callable returning
  ``(score, feedback)`` (or an :class:`ExternalTrace`). For tests and Python
  callers.
- ``SubprocessRunBackend`` — cross-process / cross-language. For each candidate
  it writes ``{"candidate": {...}, "task": {...}}`` as JSON to a command's stdin
  and reads ``{"score": <0..1>, "feedback": "<text>"}`` back from the LAST
  non-blank line of stdout. mini-ork stays ignorant of the caller's language and
  domain; the caller implements the tiny eval command.

Fail-loud contract (zero-fallback): a non-zero exit, unparseable stdout, a
missing ``score``, or a ``score`` outside ``[0, 1]`` raises
:class:`EvaluatorError`. A broken evaluator must never be silently scored 0.0 —
that would corrupt the Pareto front with a fake gradient.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


class EvaluatorError(RuntimeError):
    """The external/callback evaluator failed or returned a malformed result.

    Raised loudly so a broken evaluator halts the run instead of poisoning the
    search with a silent 0.0.
    """


@dataclass
class ExternalTrace:
    """One graded outcome. ``subscores`` is an optional per-objective vector the
    evaluator may return (e.g. per-violation-class); GEPA optimizes the scalar
    ``score`` but the vector is preserved for richer feedback and future
    multi-objective callers."""

    score: float
    feedback: str = ""
    subscores: dict[str, float] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class RunBackend(Protocol):
    """What a GEPA adapter composes to grade a candidate on one example."""

    def run(self, candidate: dict[str, str], task: Any) -> ExternalTrace: ...

    def score(self, trace: ExternalTrace) -> float: ...

    def feedback(self, trace: ExternalTrace, score: float) -> str: ...


def _coerce_trace(result: Any) -> ExternalTrace:
    """Accept an ExternalTrace, a ``(score, feedback)`` pair, or a mapping."""
    if isinstance(result, ExternalTrace):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        return ExternalTrace(score=_valid_score(result[0]), feedback=str(result[1] or ""))
    if isinstance(result, dict):
        return _trace_from_mapping(result)
    raise EvaluatorError(
        f"evaluator returned {type(result).__name__}; expected ExternalTrace, "
        "(score, feedback), or a mapping with a 'score' key"
    )


def _trace_from_mapping(out: dict[str, Any]) -> ExternalTrace:
    if "score" not in out:
        raise EvaluatorError(f"evaluator result missing 'score' key: keys={sorted(out)}")
    subscores = out.get("subscores")
    if subscores is not None and not isinstance(subscores, dict):
        raise EvaluatorError(f"'subscores' must be an object, got {type(subscores).__name__}")
    return ExternalTrace(
        score=_valid_score(out["score"]),
        feedback=str(out.get("feedback", "") or ""),
        subscores={k: float(v) for k, v in subscores.items()} if subscores else None,
        raw=out,
    )


def _valid_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise EvaluatorError(f"score is not a number: {value!r}") from exc
    if score != score:  # NaN
        raise EvaluatorError("score is NaN")
    if not (0.0 <= score <= 1.0):
        raise EvaluatorError(f"score {score} outside [0, 1]")
    return score


class CallbackRunBackend:
    """In-process backend. ``eval_fn(candidate, task)`` returns an
    :class:`ExternalTrace`, a ``(score, feedback)`` pair, or a mapping.
    """

    def __init__(self, eval_fn: Callable[[dict[str, str], Any], Any]):
        self._eval_fn = eval_fn

    def run(self, candidate: dict[str, str], task: Any) -> ExternalTrace:
        return _coerce_trace(self._eval_fn(candidate, task))

    def score(self, trace: ExternalTrace) -> float:
        return trace.score

    def feedback(self, trace: ExternalTrace, score: float) -> str:
        return trace.feedback


class SubprocessRunBackend:
    """Cross-process / cross-language backend.

    ``eval_cmd`` may be a shell-style string (split with :func:`shlex.split`,
    NEVER run through a shell — no injection surface) or an argv list. For each
    candidate the command receives ``{"candidate": ..., "task": ...}`` JSON on
    stdin and must print ``{"score": <0..1>, "feedback": "<text>"}`` as the LAST
    non-blank line of stdout. Logs may go to stderr (or earlier stdout lines).
    """

    def __init__(
        self,
        eval_cmd: str | list[str],
        *,
        timeout_s: int = 1800,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ):
        self.argv = shlex.split(eval_cmd) if isinstance(eval_cmd, str) else list(eval_cmd)
        if not self.argv:
            raise EvaluatorError("eval_cmd is empty")
        self.timeout_s = timeout_s
        self.cwd = cwd
        self.env = env

    def run(self, candidate: dict[str, str], task: Any) -> ExternalTrace:
        payload = json.dumps({"candidate": candidate, "task": task})
        try:
            proc = subprocess.run(
                self.argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                cwd=self.cwd,
                env=self.env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EvaluatorError(f"eval_cmd timed out after {self.timeout_s}s: {self.argv}") from exc
        except (OSError, ValueError) as exc:
            raise EvaluatorError(f"eval_cmd failed to launch ({self.argv}): {exc}") from exc
        if proc.returncode != 0:
            raise EvaluatorError(
                f"eval_cmd exited {proc.returncode}: {self.argv}\n"
                f"stderr: {proc.stderr[-2000:].strip()}"
            )
        line = _last_json_line(proc.stdout)
        if line is None:
            raise EvaluatorError(
                f"eval_cmd produced no stdout to parse: {self.argv}\n"
                f"stderr: {proc.stderr[-1000:].strip()}"
            )
        try:
            out = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluatorError(
                f"eval_cmd last stdout line is not JSON ({exc}): {line[:500]!r}"
            ) from exc
        if not isinstance(out, dict):
            raise EvaluatorError(f"eval_cmd JSON is not an object: {type(out).__name__}")
        return _trace_from_mapping(out)

    def score(self, trace: ExternalTrace) -> float:
        return trace.score

    def feedback(self, trace: ExternalTrace, score: float) -> str:
        return trace.feedback


def _last_json_line(stdout: str) -> str | None:
    """The result contract is: the evaluator's JSON is the last non-blank line
    of stdout. This lets the evaluator log freely above it."""
    for raw in reversed(stdout.splitlines()):
        stripped = raw.strip()
        if stripped:
            return stripped
    return None
