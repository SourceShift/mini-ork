"""Non-interactive Agent-S driver script.

Standalone CLI that mirrors ``gui_agents.s3.cli_app.run_agent`` but without
``input()`` and without the brokered ``input("Query: ")`` loop. Used by
:mod:`mini_ork.gates.agent_s_smoke` as the command the gate spawns per attempt.

Contract:

* argv: ``[--task TEXT] [--provider …] [--model …] [--model-url …]``
  ``[--ground-provider …] [--ground-url …] [--ground-model …]``
  ``[--ground-api-key-env NAME] [--grounding-width N] [--grounding-height N]``
  ``[--shot-dir DIR] [--max-steps N] [--observe-only]``
* stdout: EXACTLY one JSON line ``{"success": bool, "outcome": str,
  "steps": int, "error": str|null}``
* stderr: human-readable progress (forwarded by the gate into
  ``verdict.json``'s ``log_tail``); does not participate in the verdict
* exit codes: 0 for ``DONE``; 1 for ``FAIL`` / ``MAX_STEPS`` / ``ERROR``
* The driver never imports ``mini_ork``; it is meant to run under the
  Agent-S interpreter, with ``gui_agents`` and ``pyautogui`` on sys.path.
  Tests inject fakes via ``sys.modules`` before calling ``main``.

Mirrors ``gui_agents/s3/cli_app.py`` (Agent-S 0.3.2): same objects, same loop
shape; the only deliberate differences are: (1) lazy imports of
``gui_agents`` / ``pyautogui`` / ``PIL`` so this file loads in a stub-only
test interpreter, (2) case-insensitive stripped equality with the bare
strings ``DONE`` / ``FAIL`` / ``WAIT`` / ``NEXT`` instead of cli_app's
substring test (which would treat any code containing "done" as terminal),
(3) ``--observe-only`` never ``exec``s an action, and (4) stdout is exactly
one JSON line on completion (Agent-S's own ``print`` calls during the loop
are redirected to stderr).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import sys
import time

# NB: gui_agents, pyautogui, and PIL are imported LAZILY inside main() so
# this module loads cleanly on interpreters where they are unavailable. Do
# not add a module-level "from mini_ork..." import here.


def scale_screen_dimensions(width: int, height: int, max_dim_size: int) -> tuple[int, int]:
    """Copy of ``gui_agents.s3.cli_app.scale_screen_dimensions`` (kept inline so
    this file has zero module-scope dependency on ``gui_agents``).

    Mirrors cli_app.py exactly: ``min(max_dim_size / width, max_dim_size /
    height, 1)``, truncated via ``int()``.
    """
    scale_factor = min(max_dim_size / width, max_dim_size / height, 1)
    safe_width = int(width * scale_factor)
    safe_height = int(height * scale_factor)
    return safe_width, safe_height


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drive Agent-S non-interactively.")
    parser.add_argument("--task", required=True, help="Natural-language instruction.")
    parser.add_argument("--provider", default="openai", help="Generation engine type.")
    parser.add_argument("--model", default="", help="Generation model id.")
    parser.add_argument("--model-url", default="", help="Generation base URL.")
    parser.add_argument(
        "--ground-provider",
        default="huggingface",
        help="Grounding engine type.",
    )
    parser.add_argument("--ground-url", default="", help="Grounding base URL.")
    parser.add_argument("--ground-model", default="", help="Grounding model id.")
    parser.add_argument(
        "--ground-api-key-env",
        default="",
        help="Name of the env var holding the grounding API key (NOT the key itself).",
    )
    parser.add_argument("--grounding-width", type=int, default=1920)
    parser.add_argument("--grounding-height", type=int, default=1080)
    parser.add_argument(
        "--shot-dir",
        default=".",
        help="Directory to write step_<n>.png screenshots into.",
    )
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument(
        "--observe-only",
        action="store_true",
        help="Plan actions but never execute them (default for safe smoke runs).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Drive one Agent-S attempt. See module docstring for the contract."""
    args = _parse_args(argv)
    captured_stdout = sys.stdout  # see G2 — preserve pytest capsys capture

    steps_taken = 0
    last_plan = ""
    success = False
    outcome = "ERROR"  # overwritten by the loop; "ERROR" is the pre-loop fallback
    error_msg: str | None = None

    try:
        # All work that may emit stdout goes under redirect_stdout so Agent-S's
        # own ``print`` calls land on stderr and do not break the
        # "exactly one JSON line on stdout" contract.
        with contextlib.redirect_stdout(sys.stderr):
            # Lazy imports — keep these INSIDE main so this module loads
            # under any interpreter (tests stub sys.modules; production
            # callers have the real packages installed). See G8.
            import io as _io  # noqa: WPS433  — deliberate lazy import
            from PIL import Image  # noqa: WPS433
            import pyautogui  # noqa: WPS433
            from gui_agents.s3.agents.grounding import OSWorldACI  # noqa: WPS433
            from gui_agents.s3.agents.agent_s import AgentS3  # noqa: WPS433

            platform_name = platform.system().lower()
            if platform_name not in ("darwin", "linux", "windows"):
                # OSWorldACI rejects unknown platforms; fail closed to linux.
                platform_name = "linux"
            screen_w, screen_h = pyautogui.size()
            scaled_w, scaled_h = scale_screen_dimensions(screen_w, screen_h, 2400)

            engine_params = {
                "engine_type": args.provider,
                "model": args.model,
                "base_url": args.model_url,
                "api_key": "",
            }
            engine_params_for_grounding = {
                "engine_type": args.ground_provider,
                "model": args.ground_model,
                "base_url": args.ground_url,
                "api_key": os.environ.get(args.ground_api_key_env or "AGENT_S_NO_KEY", ""),
                "grounding_width": args.grounding_width,
                "grounding_height": args.grounding_height,
            }

            grounding_agent = OSWorldACI(
                env=None,
                platform=platform_name,
                engine_params_for_generation=engine_params,
                engine_params_for_grounding=engine_params_for_grounding,
                width=screen_w,
                height=screen_h,
            )
            agent = AgentS3(
                engine_params,
                grounding_agent,
                platform=platform_name,
                max_trajectory_length=8,
                enable_reflection=True,
            )
            agent.reset()

            outcome = "MAX_STEPS"  # loop natural exit without break → MAX_STEPS
            for n in range(1, args.max_steps + 1):
                steps_taken = n  # count attempted steps (one per iteration)

                screenshot = pyautogui.screenshot()
                screenshot = screenshot.resize((scaled_w, scaled_h), Image.LANCZOS)
                buffered = _io.BytesIO()
                screenshot.save(buffered, format="PNG")
                obs = {"screenshot": buffered.getvalue()}

                # Persist the screenshot BEFORE calling predict. The kickoff
                # requires step_<n>.png files to exist in --shot-dir for each
                # iteration the driver actually entered, not just terminal
                # ones — so write here, not after the break.
                if args.shot_dir:
                    os.makedirs(args.shot_dir, exist_ok=True)
                    shot_path = os.path.join(args.shot_dir, f"step_{n}.png")
                    with open(shot_path, "wb") as fh:
                        fh.write(obs["screenshot"])

                info, code = agent.predict(instruction=args.task, observation=obs)
                if not code:
                    raise IndexError("predict returned an empty code list")
                # The agent's own plan is the only record of WHY it chose an
                # action (or DONE / FAIL); keep it on stderr and the tail of
                # the last one in the verdict so a FAIL is diagnosable.
                plan = ""
                if isinstance(info, dict):
                    plan = str(info.get("executor_plan") or info.get("plan") or "")
                if plan:
                    print(f"[step {n}] plan: {plan}", file=sys.stderr)
                    last_plan = plan

                # cli_app tests "done" in code[0].lower() — that substring
                # test would treat any action containing "done" as terminal.
                # The kickoff wants case-insensitive stripped equality against
                # the bare strings.
                code0 = code[0].strip().upper()
                if code0 == "DONE":
                    outcome = "DONE"
                    success = True
                    break
                if code0 == "FAIL":
                    outcome = "FAIL"
                    break
                if code0 == "WAIT":
                    time.sleep(5)
                    continue
                if code0 == "NEXT":
                    continue

                if args.observe_only:
                    print(
                        f"skipped action (observe-only): {code[0]}",
                        file=sys.stderr,
                    )
                    continue

                time.sleep(1.0)
                # Seed exec's globals with the modules action strings assume
                # are already imported (cli_app's module globals include
                # ``pyautogui`` and ``time``; some action strings — e.g.
                # grounding.py's wait-for-file-open path — call ``time.sleep``
                # without importing it).
                exec(code[0], {"pyautogui": pyautogui, "time": time})
                time.sleep(1.0)
    except SystemExit:
        # argparse usage errors and deliberate sys.exit calls — propagate.
        raise
    except Exception as exc:
        # Any error in setup, predict, or the loop → outcome ERROR, exit 1.
        success = False
        outcome = "ERROR"
        error_msg = str(exc)

    payload = {
        "success": success,
        "outcome": outcome,
        "steps": steps_taken,
        "error": error_msg,
        "reason": last_plan[-600:],
    }
    captured_stdout.write(json.dumps(payload) + "\n")
    captured_stdout.flush()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
