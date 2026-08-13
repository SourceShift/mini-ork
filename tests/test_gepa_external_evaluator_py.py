"""Tests for the GEPA external-evaluator seam (R1 RunBackend + R2 Subprocess +
R3 public entry).

The seam lets ANY external critic — a Node script, a shell command, an
in-process Python fn — drive GEPA prompt optimization with no mini-ork recipe
and no state.db. Two invariants are load-bearing and get the most coverage:

1. **Fail-loud (zero-fallback).** A broken evaluator (nonzero exit, unparseable
   stdout, missing/out-of-range score) must raise ``EvaluatorError`` — never be
   silently scored 0.0, which would poison the Pareto front with a fake gradient.
2. **The JSON contract survives a real subprocess round-trip.** ``candidate`` and
   ``task`` reach the command's stdin; the LAST non-blank stdout line is parsed as
   the result, so the command may log freely above it.

The backend tests are stdlib-only (no ``gepa`` framework). The adapter tests
``importorskip('gepa')`` because ``GEPARunBackendAdapter`` composes the external
framework's ``EvaluationBatch``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mini_ork.gepa.backends import (
    CallbackRunBackend,
    EvaluatorError,
    ExternalTrace,
    RunBackend,
    SubprocessRunBackend,
)


# ─── a hermetic cross-process critic ────────────────────────────────────────
#
# Reads {"candidate","task"} from stdin, logs noise to BOTH stdout and stderr
# (to prove _last_json_line skips it), then prints the result JSON as the LAST
# line. score = 1.0 iff the token "GOOD" is in the candidate prompt; feedback
# echoes the task so the round-trip is observable.
_ECHO_CRITIC = r"""
import json, sys
blob = json.loads(sys.stdin.read())
cand = blob["candidate"]
task = blob["task"]
print("critic: log line on stdout (must be ignored)")
print("critic: another log line", file=sys.stderr)
prompt = " ".join(str(v) for v in cand.values())
score = 1.0 if "GOOD" in prompt else 0.0
print(json.dumps({"score": score, "feedback": "task=%s" % json.dumps(task)}))
"""

# A critic that violates the contract in a way chosen by the FIRST CLI arg.
_BROKEN_CRITIC = r"""
import json, sys
mode = sys.argv[1]
_ = sys.stdin.read()
if mode == "exit":
    print("boom", file=sys.stderr); sys.exit(3)
if mode == "empty":
    sys.exit(0)                                  # no stdout at all
if mode == "notjson":
    print("this is not json")
elif mode == "noscore":
    print(json.dumps({"feedback": "forgot the score"}))
elif mode == "highscore":
    print(json.dumps({"score": 1.7}))
elif mode == "notdict":
    print(json.dumps([1, 2, 3]))
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ─── SubprocessRunBackend: happy path + fail-loud ───────────────────────────


def test_subprocess_backend_round_trip(tmp_path):
    critic = _write(tmp_path, "echo_critic.py", _ECHO_CRITIC)
    backend = SubprocessRunBackend([sys.executable, str(critic)])

    trace = backend.run({"prompt": "a GOOD self-contained answer"}, {"id": 7})
    assert trace.score == 1.0
    # task echoed back through stdin→stdout proves the JSON contract end-to-end
    assert '"id": 7' in trace.feedback
    assert backend.score(trace) == 1.0
    assert backend.feedback(trace, trace.score) == trace.feedback

    trace_bad = backend.run({"prompt": "a mediocre answer"}, {"id": 8})
    assert trace_bad.score == 0.0


def test_subprocess_backend_is_a_runbackend(tmp_path):
    critic = _write(tmp_path, "echo_critic.py", _ECHO_CRITIC)
    assert isinstance(SubprocessRunBackend([sys.executable, str(critic)]), RunBackend)


@pytest.mark.parametrize(
    "mode, needle",
    [
        ("exit", "exited 3"),
        ("empty", "no stdout"),
        ("notjson", "not JSON"),
        ("noscore", "missing 'score'"),
        ("highscore", "outside [0, 1]"),
        ("notdict", "not an object"),
    ],
)
def test_subprocess_backend_fails_loud(tmp_path, mode, needle):
    """Every contract violation raises EvaluatorError with a diagnostic — never
    a silent 0.0. A silent 0.0 would look like a real (bad) gradient to GEPA."""
    critic = _write(tmp_path, "broken_critic.py", _BROKEN_CRITIC)
    backend = SubprocessRunBackend([sys.executable, str(critic), mode])
    with pytest.raises(EvaluatorError) as exc:
        backend.run({"prompt": "x"}, {"id": 1})
    assert needle in str(exc.value)


def test_subprocess_backend_timeout(tmp_path):
    sleeper = _write(tmp_path, "sleeper.py", "import time, sys; sys.stdin.read(); time.sleep(5)")
    backend = SubprocessRunBackend([sys.executable, str(sleeper)], timeout_s=1)
    with pytest.raises(EvaluatorError) as exc:
        backend.run({"prompt": "x"}, {"id": 1})
    assert "timed out" in str(exc.value)


def test_subprocess_backend_empty_cmd_rejected():
    with pytest.raises(EvaluatorError):
        SubprocessRunBackend("")


def test_subprocess_backend_string_cmd_is_shlex_split_not_shelled(tmp_path):
    """A str eval_cmd is shlex.split — never run through a shell — so shell
    metacharacters are inert (no injection surface)."""
    critic = _write(tmp_path, "echo_critic.py", _ECHO_CRITIC)
    backend = SubprocessRunBackend(f"{sys.executable} {critic}")
    trace = backend.run({"prompt": "GOOD"}, {"id": 1})
    assert trace.score == 1.0


# ─── CallbackRunBackend: coercion of the three return shapes ─────────────────


def test_callback_backend_accepts_trace_tuple_and_mapping():
    b_trace = CallbackRunBackend(lambda c, t: ExternalTrace(score=0.5, feedback="fb"))
    assert b_trace.run({"prompt": "x"}, None).score == 0.5

    b_tuple = CallbackRunBackend(lambda c, t: (0.25, "pair feedback"))
    tr = b_tuple.run({"prompt": "x"}, None)
    assert tr.score == 0.25 and tr.feedback == "pair feedback"

    b_map = CallbackRunBackend(
        lambda c, t: {"score": 0.9, "feedback": "map", "subscores": {"a": 0.1}}
    )
    tr = b_map.run({"prompt": "x"}, None)
    assert tr.score == 0.9 and tr.subscores == {"a": 0.1}


def test_callback_backend_fails_loud_on_garbage():
    b = CallbackRunBackend(lambda c, t: "not a valid result")
    with pytest.raises(EvaluatorError):
        b.run({"prompt": "x"}, None)

    b_nan = CallbackRunBackend(lambda c, t: (float("nan"), "fb"))
    with pytest.raises(EvaluatorError):
        b_nan.run({"prompt": "x"}, None)


# ─── run_gepa pure helpers ──────────────────────────────────────────────────


def test_seed_from_file_json_map_and_raw():
    from mini_ork.gepa.run_gepa import _seed_from_file

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        j = d / "seed.json"
        j.write_text('{"planner": "p", "reviewer": "r"}', encoding="utf-8")
        assert _seed_from_file(j) == {"planner": "p", "reviewer": "r"}

        raw = d / "seed.txt"
        raw.write_text("just some prompt text", encoding="utf-8")
        assert _seed_from_file(raw) == {"prompt": "just some prompt text"}

        # a JSON file that isn't a {str: str} map is treated as raw seed text
        arr = d / "arr.json"
        arr.write_text("[1, 2, 3]", encoding="utf-8")
        assert _seed_from_file(arr) == {"prompt": "[1, 2, 3]"}


def test_tasks_from_trainset_object_array_and_nonjson(tmp_path):
    from mini_ork.gepa.run_gepa import _tasks_from_trainset

    (tmp_path / "one.json").write_text('{"id": 1}', encoding="utf-8")
    (tmp_path / "many.json").write_text('[{"id": 2}, {"id": 3}]', encoding="utf-8")
    (tmp_path / "raw.md").write_text("# heading\nbody", encoding="utf-8")

    tasks = _tasks_from_trainset([str(tmp_path / "*.json"), str(tmp_path / "*.md")])
    ids = sorted(t.get("id") for t in tasks if isinstance(t, dict) and "id" in t)
    assert ids == [1, 2, 3]
    raw = [t for t in tasks if isinstance(t, dict) and t.get("path", "").endswith(".md")]
    assert len(raw) == 1 and "# heading" in raw[0]["text"]


def test_tasks_from_trainset_empty_raises(tmp_path):
    from mini_ork.gepa.run_gepa import _tasks_from_trainset

    with pytest.raises(SystemExit):
        _tasks_from_trainset([str(tmp_path / "nonexistent-*.json")])


# ─── GEPARunBackendAdapter: needs the external gepa framework ────────────────


def test_generic_adapter_evaluate_and_reflect():
    pytest.importorskip("gepa")
    from mini_ork.gepa.generic_adapter import GEPARunBackendAdapter

    # a deterministic in-process critic
    def critic(candidate, task):
        prompt = candidate["prompt"]
        want = task["want"]
        score = 1.0 if want in prompt else 0.0
        fb = f"looked for {want!r}: {'found' if score else 'MISSING'}"
        return ExternalTrace(score=score, feedback=fb, subscores={"exact": score})

    adapter = GEPARunBackendAdapter(CallbackRunBackend(critic), components=["prompt"])
    batch = [{"want": "alpha"}, {"want": "omega"}]
    candidate = {"prompt": "the answer mentions alpha only"}

    eval_batch = adapter.evaluate(batch, candidate, capture_traces=True)
    assert eval_batch.scores == [1.0, 0.0]
    assert eval_batch.trajectories is not None and len(eval_batch.trajectories) == 2

    reflective = adapter.make_reflective_dataset(candidate, eval_batch, ["prompt"])
    assert set(reflective) == {"prompt"}
    assert len(reflective["prompt"]) == 2
    # the critic's NL diagnosis is forwarded VERBATIM (GEPA's highest-signal input)
    fb_texts = [rec["Feedback"] for rec in reflective["prompt"]]
    assert any("MISSING" in t for t in fb_texts)
    assert any("found" in t for t in fb_texts)
    # subscores are preserved for richer reflection
    assert reflective["prompt"][0]["Generated Outputs"]["subscores"] == {"exact": 1.0}


def test_generic_adapter_capture_traces_off_yields_no_trajectories():
    pytest.importorskip("gepa")
    from mini_ork.gepa.generic_adapter import GEPARunBackendAdapter

    adapter = GEPARunBackendAdapter(
        CallbackRunBackend(lambda c, t: (0.5, "fb")), components=["prompt"]
    )
    eval_batch = adapter.evaluate([{"x": 1}], {"prompt": "p"}, capture_traces=False)
    assert eval_batch.scores == [0.5]
    assert eval_batch.trajectories is None
