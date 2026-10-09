"""Advisory Agent-S computer-use GUI smoke gate.

mini-ork's verification stack verifies code artifacts — diffs, tests, relations,
differential suites. When the deliverable is a human-visible state (the Zed-fork
IDE, a dashboard, a rendered document), verification today is a human eyeballing
a screenshot. `Agent-S <https://github.com/simular-ai/Agent-S>`_ (S3, >72.6% on
OSWorld) is a computer-use agent: it takes a natural-language task, reads the
screen, and drives real apps by clicking and typing. This module wires it in as
an **advisory evidence-producing gate** that verifies observables without ever
seeing the patch — it cannot be talked into passing a bad diff.

The gate is registered as the ``agent_s_gui`` gate type through the existing
OCP extension path (``gate_registry.register_gate_evaluator``); it edits no
registry file and is invocable only through the gate registry:

    from mini_ork.gates.agent_s_smoke import register
    register()          # gate_registry.GATE_EVALUATORS["agent_s_gui"] = evaluate

Unlike ``abstain_gate`` (which registers via a module-level import side effect
bootstrapped by ``gate_bootstrap``), this module has **no** import-time side
effect: importing it leaves ``GATE_EVALUATORS`` unchanged. An ``agent_s_gui``
row is therefore evaluatable only after the registering call site imports this
module and calls ``register()``; before that, ``gate_evaluate`` on such a row
defers (a missing evaluator is the registry's fail-safe). This change cannot
edit ``gate_bootstrap`` (the two new files are its whole scope), so the
boot-import wiring is deliberately a separate follow-up, not claimed here.

It also exposes the legacy executable contract via
``python -m mini_ork.gates.agent_s_smoke <agent_s_task.yaml>`` with exit codes
0 = pass, 1 = fail, 2 = defer (matching ``gate_registry``'s rc contract).

Task spec (``.mini-ork/runs/<run_id>/agent_s_task.yaml``)
---------------------------------------------------------

A run that wants GUI verification writes this file (YAML)::

    app: /path/to/target.app          # launched fresh by the evaluator
    app_args: ["/path/to/project"]    # optional
    launch_cmd: null                  # optional override; if set, `app` is ignored
    steps: >                          # natural language, what to do
      Open the Threads board, click a working run, open its run graph.
    expect: >                         # natural language, what must be observable
      The run graph shows nodes with lane marks.
    attempts: 3                       # k-of-n; pass = strict majority
    timeout_s: 300                    # per-attempt cap
    max_usd: 2.0                      # advisory cost cap

Missing optional keys are tolerated. A missing ``steps``/``expect`` or the
absence of both ``app`` and ``launch_cmd`` is a config error → DEFER (a malformed
task spec never fails the run). An absent spec → DEFER (the gate never fires
unless a run asks for it).

Safety rule (env gating)
------------------------

The agent executes real input events on the operator's GUI session, so it MUST
NOT run unless ALL of these hold:

* ``MO_AGENT_S_ALLOW=1`` — explicit operator opt-in, per invocation;
* a main-model provider key present — ``AGENT_S_PROVIDER`` (default
  ``openrouter``) picks the key var: ``OPENROUTER_API_KEY`` / ``OPENAI_API_KEY``
  / ``ANTHROPIC_API_KEY`` / ``AGENT_S_BASE_URL`` (vllm-style
  openai-compatible endpoint; ``AGENT_S_MODEL`` selects the model, default
  ``openrouter/openai/gpt-4o-mini``);
* a grounding endpoint present — ``AGENT_S_GROUND_URL``.

Any missing → DEFER with a one-line reason in ``verdict.json``. The gate never
FAILS a run because the GUI verifier could not run.

Grounding runs UI-TARS-1.5-7B via Hugging Face Inference Providers: set
``AGENT_S_GROUND_URL`` to the HF endpoint and ``HUGGINGFACE_API_KEY`` to an HF
token with Inference Providers access (the Agent-S README's
``--ground_provider huggingface`` path). The grounding coordinates are pinned to
the 1.5-7B default (1920x1080, per the README). A self-hosted grounding endpoint
does not need the key; only ``AGENT_S_GROUND_URL`` is gated on.

Cost guard: each attempt's cost is the reported usage when Agent-S's output
carries one, else a flat ``MO_AGENT_S_USD_PER_ATTEMPT`` estimate (default 0.5).
No further attempt is launched once ``max_usd`` would be exceeded; the result
state stays what the completed attempts support.

Verdict semantics
-----------------

This gate is ADVISORY. It returns the honest ``fail`` to the registry when a
majority of attempts did not pass (never downgrades fail → defer), and carries
``"advisory": true`` in ``verdict.json`` + this docstring so merge-time
consumers can treat it as evidence rather than a hard gate. Registry consumers
decide weighting; this module only reports.

Evidence is written to ``.mini-ork/runs/<run_id>/artifacts/agent_s/``:
``verdict.json`` (``pass`` bool|null, ``state``, ``attempts``, ``reason``,
``evidence``, ``advisory``) plus any screenshots copied from the scratch state
dir. Nothing is written outside the run dir.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

import yaml

from mini_ork.context import RunContext
from mini_ork.gates import gate_registry

__all__ = ["GATE_TYPE", "register", "evaluate", "main"]

#: Gate type registered via the OCP extension path (no edit to ``gate_registry``).
GATE_TYPE = "agent_s_gui"

# ── Env contract ─────────────────────────────────────────────────────────────

ALLOW_ENV = "MO_AGENT_S_ALLOW"
PROVIDER_ENV = "AGENT_S_PROVIDER"
MODEL_ENV = "AGENT_S_MODEL"
GROUND_URL_ENV = "AGENT_S_GROUND_URL"
USD_PER_ATTEMPT_ENV = "MO_AGENT_S_USD_PER_ATTEMPT"

DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = "openrouter/openai/gpt-4o-mini"
DEFAULT_GROUND_PROVIDER = "huggingface"
DEFAULT_GROUND_MODEL = "ui-tars-1.5-7b"
#: UI-TARS-1.5-7B grounding coordinates (Agent-S README: 1920x1080 for 1.5-7B,
#: 1000x1000 for 72B). The wrapper pins the 1.5-7B default.
DEFAULT_GROUNDING_WIDTH = 1920
DEFAULT_GROUNDING_HEIGHT = 1080
DEFAULT_USD_PER_ATTEMPT = 0.5

#: ``AGENT_S_PROVIDER`` → the env var holding that provider's key (vllm's "key"
#: is its openai-compatible base URL, which doubles as the credential gate).
_PROVIDER_KEY_VARS = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "vllm": "AGENT_S_BASE_URL",
}

# ── Task-spec defaults ───────────────────────────────────────────────────────

TASK_SPEC_NAME = "agent_s_task.yaml"
ARTIFACT_DIR_NAME = "agent_s"

DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT_S = 300
DEFAULT_MAX_USD = 2.0

_SCREENSHOT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif")

#: Conservative free-text success/failure markers. A failure marker anywhere
#: overrides a success marker; ambiguous output is not-passed. "completed" alone
#: is NOT trusted as success because Agent-S's own CLI shows a "Task Completed"
#: dialog for both the ``done`` and ``fail`` terminal codes.
_SUCCESS_MARKER = re.compile(r"\b(success|succeeded|done|pass(?:ed)?)\b")
_FAILURE_MARKER = re.compile(r"\b(fail(?:ed|ure)?|error|timeout)\b")


def register() -> None:
    """Register the ``agent_s_gui`` gate type's evaluator with the registry.

    Idempotent (a second call replaces the evaluator with the same function).
    This is the OCP extension path: pairing a ``gate_register(db, "agent_s_gui",
    ...)`` call with ``register()`` makes the new type registrable and
    evaluatable without editing ``gate_registry``.
    """
    gate_registry.register_gate_evaluator(GATE_TYPE, evaluate)


# ── Gate evaluator (gate_registry.GateEvaluator contract) ────────────────────


def evaluate(
    condition: str,
    context_json: str,
    db_path: str,
    mini_ork_root: str | None,
) -> str:
    """Locate the run's task spec, gate on env, run attempts, return a verdict.

    Signature matches ``gate_registry.GateEvaluator``:
    ``(condition, context_json, db_path, mini_ork_root) -> pass|fail|defer``.
    The ``condition`` carries no spec path (the spec lives at a fixed path in
    the run dir); ``db_path`` is unused because evidence goes to the run dir.
    """
    del condition, db_path, mini_ork_root
    try:
        ctx = json.loads(context_json) if context_json else {}
    except Exception:
        return "defer"
    if not isinstance(ctx, dict):
        return "defer"

    run_id = str(ctx.get("run_id") or os.environ.get("MINI_ORK_RUN_ID", "") or "")
    run_dir = _resolve_run_dir(ctx, run_id)
    if not run_dir:
        return "defer"

    spec_path = os.path.join(run_dir, TASK_SPEC_NAME)
    if not os.path.isfile(spec_path):
        return _finish(run_dir, "defer", [], "agent_s task spec absent", [])["state"]

    spec = _load_task_spec(spec_path)
    if spec is None:
        return _finish(run_dir, "defer", [], "agent_s task spec malformed", [])["state"]

    ok, reason = _check_env_gating()
    if not ok:
        return _finish(run_dir, "defer", [], reason, [])["state"]

    return _run_attempts(spec, run_id, run_dir)["state"]


def _resolve_run_dir(ctx: dict, run_id: str) -> str:
    """Run dir from ``context_json``, else ``MINI_ORK_RUN_DIR``, else home+run_id."""
    run_dir = str(ctx.get("run_dir") or "").strip()
    if run_dir:
        return run_dir
    env_dir = RunContext.from_env().run_dir
    if env_dir:
        return env_dir
    home = os.environ.get("MINI_ORK_HOME", "")
    if run_id and home:
        return os.path.join(home, "runs", run_id)
    return ""


def _load_task_spec(path: str) -> dict[str, Any] | None:
    """Parse + validate the YAML task spec; ``None`` on any config error."""
    try:
        with open(path, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)
    except Exception:
        return None
    if not isinstance(spec, dict):
        return None

    steps = str(spec.get("steps") or "").strip()
    expect = str(spec.get("expect") or "").strip()
    app = str(spec.get("app") or "").strip()
    launch_cmd = spec.get("launch_cmd")
    if not steps or not expect:
        return None
    if not app and not launch_cmd:
        return None

    return {
        "steps": steps,
        "expect": expect,
        "app": app or None,
        "app_args": [str(a) for a in (spec.get("app_args") or [])],
        "launch_cmd": launch_cmd,
        "attempts": _as_int(spec.get("attempts"), DEFAULT_ATTEMPTS, minimum=1),
        "timeout_s": _as_int(spec.get("timeout_s"), DEFAULT_TIMEOUT_S, minimum=1),
        "max_usd": _as_float(spec.get("max_usd"), DEFAULT_MAX_USD, minimum=0.0),
    }


# ── Env gating ───────────────────────────────────────────────────────────────


def _check_env_gating() -> tuple[bool, str]:
    """Return ``(ok, reason)`` for the hard safety rule (c:0)."""
    if os.environ.get(ALLOW_ENV, "0") != "1":
        return False, f"{ALLOW_ENV} is not 1"
    provider = (os.environ.get(PROVIDER_ENV, "") or DEFAULT_PROVIDER).strip()
    if provider not in _PROVIDER_KEY_VARS:
        return False, f"{PROVIDER_ENV}={provider!r} is not a supported provider"
    key_var = _PROVIDER_KEY_VARS[provider]
    if not os.environ.get(key_var):
        return False, f"provider key {key_var} is unset"
    if not os.environ.get(GROUND_URL_ENV):
        return False, f"{GROUND_URL_ENV} is unset"
    return True, ""


def _per_attempt_usd() -> float:
    raw = os.environ.get(USD_PER_ATTEMPT_ENV, "")
    try:
        value = float(raw)
        if value >= 0.0:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_USD_PER_ATTEMPT


# ── Launch isolation (macOS, best-effort) ────────────────────────────────────


def _app_name(app: str | None) -> str:
    base = os.path.basename(app or "")
    if base.endswith(".app"):
        base = base[:-4]
    return base or (app or "")


def _quit_app(app_name: str) -> None:
    """Quit a running instance of the target app (best-effort; never raises)."""
    if not app_name:
        return
    try:
        subprocess.run(
            ["osascript", "-e", f'quit app "{app_name}"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        pass


def _launch_target(spec: dict[str, Any], scratch: str) -> subprocess.Popen:
    """Launch the target app fresh with a scratch HOME/TMPDIR; raises on failure."""
    env = {**os.environ, "HOME": scratch, "TMPDIR": scratch}
    launch_cmd = spec.get("launch_cmd")
    if launch_cmd:
        if isinstance(launch_cmd, (list, tuple)):
            return subprocess.Popen([str(a) for a in launch_cmd], env=env)
        return subprocess.Popen(str(launch_cmd), shell=True, env=env)
    cmd = ["open", "-na", str(spec["app"])]
    if spec.get("app_args"):
        cmd += ["--args", *spec["app_args"]]
    return subprocess.Popen(cmd, env=env)


# ── Attempt loop ─────────────────────────────────────────────────────────────


def _run_attempts(spec: dict[str, Any], run_id: str, run_dir: str) -> dict[str, Any]:
    """Run up to ``attempts`` Agent-S invocations and derive a strict-majority verdict.

    Launch failure → DEFER (never fail); zero attempts actually run → DEFER.
    """
    del run_id
    app_name = _app_name(spec.get("app"))
    _quit_app(app_name)  # best-effort pre-launch quit

    scratch = tempfile.mkdtemp(prefix="agent_s_smoke_")
    try:
        proc = _launch_target(spec, scratch)
    except Exception:
        shutil.rmtree(scratch, ignore_errors=True)
        return _finish(run_dir, "defer", [], "failed to launch target app", [])

    artifacts = _artifacts_dir(run_dir)
    results: list[dict[str, Any]] = []
    evidence: list[str] = []
    spent = 0.0
    per_attempt_usd = _per_attempt_usd()
    max_usd = spec["max_usd"]
    try:
        for index in range(spec["attempts"]):
            if max_usd is not None and spent + per_attempt_usd > max_usd:
                break  # cost guard: no further attempts once the cap is exceeded
            attempt, cost, shots = _run_one_attempt(
                spec, index, spec["timeout_s"], scratch, artifacts
            )
            results.append(attempt)
            evidence.extend(shots)
            spent += cost
    finally:
        _quit_app(app_name)  # best-effort post-attempt quit
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        shutil.rmtree(scratch, ignore_errors=True)

    state = _consensus(results)
    reason = (
        f"{sum(1 for r in results if r['passed'])} of {len(results)} attempts passed"
        if results
        else "no attempts ran"
    )
    return _finish(run_dir, state, results, reason, evidence)


def _run_one_attempt(
    spec: dict[str, Any],
    index: int,
    timeout_s: int,
    scratch: str,
    artifacts: str,
) -> tuple[dict[str, Any], float, list[str]]:
    """One Agent-S invocation; returns (attempt_doc, cost, screenshots)."""
    cmd = _agent_s_command(spec)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env={**os.environ, "HOME": scratch, "TMPDIR": scratch},
        )
        rc: int | None = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        passed = rc == 0 and _parse_attempt_passed(stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        rc = None
        stdout = _coerce_text(exc.stdout)
        stderr = _coerce_text(exc.stderr)
        passed = False  # a timeout is an unmeasured attempt, never a pass
    except Exception:
        rc = None
        stdout = ""
        stderr = ""
        passed = False

    cost = _reported_usd(stdout, stderr)
    if cost is None:
        cost = _per_attempt_usd()

    return (
        {
            "rc": rc,
            "duration_s": round(time.monotonic() - start, 3),
            "passed": passed,
            "log_tail": _log_tail(stdout, stderr),
        },
        cost,
        _capture_evidence(scratch, artifacts, index),
    )


def _agent_s_command(spec: dict[str, Any]) -> list[str]:
    """Build the Agent-S CLI argv from the env contract + the task spec.

    Follows the Agent-S README's documented CLI flags (``--provider``/
    ``--model``/``--ground_provider``/``--ground_url``/``--ground_model``/
    ``--grounding_width``/``--grounding_height``). The README documents no
    ``--task`` flag (its only task mechanism is the SDK ``instruction`` string),
    so the task is passed via ``--task`` from the kickoff sketch; it is NOT
    pinned by verified evidence, and the post-merge live smoke must confirm the
    real CLI shape. Unit tests never invoke the real CLI.
    """
    provider = (os.environ.get(PROVIDER_ENV, "") or DEFAULT_PROVIDER).strip()
    model = (os.environ.get(MODEL_ENV, "") or DEFAULT_MODEL).strip()
    ground_url = (os.environ.get(GROUND_URL_ENV, "") or "").strip()
    return [
        "agent_s",
        "--provider", provider,
        "--model", model,
        "--ground_provider", DEFAULT_GROUND_PROVIDER,
        "--ground_url", ground_url,
        "--ground_model", DEFAULT_GROUND_MODEL,
        "--grounding_width", str(DEFAULT_GROUNDING_WIDTH),
        "--grounding_height", str(DEFAULT_GROUNDING_HEIGHT),
        "--task", _compose_task(spec),
    ]


def _compose_task(spec: dict[str, Any]) -> str:
    return f"{spec['steps']}\nExpected outcome: {spec['expect']}"


def _consensus(results: list[dict[str, Any]]) -> str:
    """Strict majority of attempts; zero attempts → defer (never a free pass)."""
    if not results:
        return "defer"
    passed = sum(1 for r in results if r.get("passed"))
    return "pass" if passed > len(results) / 2 else "fail"


# ── Success / cost parsing (conservative) ────────────────────────────────────


def _parse_attempt_passed(stdout: str, stderr: str) -> bool:
    """Did one attempt succeed? Structured report first, else strict markers.

    A structured JSON report's explicit success field is authoritative when
    present. Otherwise the free text must carry a success marker AND no failure
    marker — ambiguous output is not-passed (conservative, per the kickoff).
    """
    report = _maybe_json_report(stdout or "")
    if report is not None:
        verdict = _success_from_report(report)
        if verdict is not None:
            return verdict
    text = ((stdout or "") + "\n" + (stderr or "")).lower()
    if _FAILURE_MARKER.search(text):
        return False
    return bool(_SUCCESS_MARKER.search(text))


def _maybe_json_report(stdout: str) -> dict | None:
    try:
        data = json.loads(stdout)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _success_from_report(report: dict) -> bool | None:
    for key in ("success", "passed", "ok"):
        if key in report:
            value = report[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
    for key in ("status", "verdict", "result", "outcome"):
        if key in report:
            value = str(report[key]).strip().lower()
            if value in ("success", "succeeded", "done", "completed", "pass", "passed", "ok"):
                return True
            if value in ("fail", "failed", "failure", "error", "timeout"):
                return False
    return None


def _reported_usd(stdout: str, stderr: str) -> float | None:
    """Agent-S's reported usage when its output carries one, else ``None``."""
    del stderr
    report = _maybe_json_report(stdout or "")
    if not report:
        return None
    for key in ("cost_usd", "cost", "total_cost", "usage_cost"):
        value = report.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    usage = report.get("usage")
    if isinstance(usage, dict):
        for key in ("cost", "cost_usd", "total_cost"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    return None


# ── Evidence capture ─────────────────────────────────────────────────────────


def _artifacts_dir(run_dir: str) -> str:
    return os.path.join(run_dir, "artifacts", ARTIFACT_DIR_NAME)


def _capture_evidence(scratch: str, artifacts: str, index: int) -> list[str]:
    """Copy any screenshots Agent-S left in the scratch dir into ``artifacts``."""
    if not artifacts or not os.path.isdir(scratch):
        return []
    os.makedirs(artifacts, exist_ok=True)
    copied: list[str] = []
    try:
        for name in sorted(os.listdir(scratch)):
            if name.lower().endswith(_SCREENSHOT_EXTENSIONS):
                src = os.path.join(scratch, name)
                dst = os.path.join(artifacts, f"attempt_{index}_{name}")
                shutil.copyfile(src, dst)
                copied.append(dst)
    except OSError:
        pass
    return copied


def _finish(
    run_dir: str,
    state: str,
    attempts: list[dict[str, Any]],
    reason: str,
    evidence: list[str],
) -> dict[str, Any]:
    """Write ``verdict.json`` (advisory) and return the structured result."""
    doc = {
        "pass": {"pass": True, "fail": False, "defer": None}[state],
        "state": state,
        "attempts": attempts,
        "reason": reason,
        "evidence": evidence,
        "advisory": True,
    }
    _write_verdict(run_dir, doc)
    return {"state": state, "doc": doc}


def _write_verdict(run_dir: str, doc: dict[str, Any]) -> str | None:
    if not run_dir:
        return None
    try:
        out_dir = _artifacts_dir(run_dir)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "verdict.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return path
    except OSError:
        return None


# ── Small helpers ────────────────────────────────────────────────────────────


def _as_int(value: Any, default: int, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _as_float(value: Any, default: float, minimum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _coerce_text(blob: Any) -> str:
    if blob is None:
        return ""
    if isinstance(blob, bytes):
        return blob.decode("utf-8", "replace")
    return str(blob)


def _log_tail(stdout: str, stderr: str) -> str:
    combined = " ".join(
        line.strip() for line in ((stdout or "") + "\n" + (stderr or "")).splitlines()
        if line.strip()
    )
    return combined[-500:]


# ── Executable contract (python -m mini_ork.gates.agent_s_smoke <task.yaml>) ──

_RC = {"pass": 0, "fail": 1, "defer": 2}


def main(argv: list[str] | None = None) -> int:
    """Map the gate verdict onto the executable contract: 0=pass, 1=fail, 2=defer."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            f"usage: python -m mini_ork.gates.agent_s_smoke <{TASK_SPEC_NAME}>",
            file=sys.stderr,
        )
        return 2
    task_path = args[0]
    run_dir = os.path.dirname(os.path.abspath(task_path))
    run_id = os.path.basename(run_dir)
    verdict = evaluate("", json.dumps({"run_id": run_id, "run_dir": run_dir}), "", None)
    return _RC.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
