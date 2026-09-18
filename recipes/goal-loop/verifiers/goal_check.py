#!/usr/bin/env python3
# verifiers/goal_check.py — re-evaluate every unit, emit panel-verdict.json.
#
# Verifier contract (kickoff U4a):
#   * Reads MO_GOAL_TARGET_CWD, MO_GOAL_UNITS_CMD, MO_GOAL_PREDICATE_CMD.
#   * Re-runs the units lister + predicate; classifies pass/fail per unit.
#   * Writes ${MINI_ORK_RUN_DIR}/panel-verdict.json with shape:
#       {"verdict": "pass"|"fail", "failing_units": [...], "total_units": N}
#   * Exits 0 in BOTH pass and fail cases — a failing wave is a VALID wave;
#     only missing / undecidable state exits non-zero.
#   * Zero units -> verdict=fail, reason="no_units" (also rc=0).
#
# Reads MINI_ORK_RUN_DIR + MINI_ORK_VERIFIER_EVIDENCE from the env exactly
# like recipes/chapter-validation-10lens/verifiers/lens_outputs_complete.py.

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.environ.get("MINI_ORK_VERIFIER_EVIDENCE") or os.path.join(
    RUN_DIR, "verifier-goal-check.log"
)
PANEL = os.path.join(RUN_DIR, "panel-verdict.json")

# ``recipes/`` is not a Python package (only ``recipes/<name>/lib/__init__.py``
# is a real module marker), so the goal_state helpers cannot be reached with a
# normal ``from recipes.goal_loop.lib.goal_state import ...`` import. Load the
# helper module by file path, mirroring the recipe_register loader pattern
# (``mini_ork/cli/recipe_register.py:67``). Idempotent per-process via
# ``sys.modules`` cache.
_HELPER_PATH = Path(__file__).resolve().parent.parent / "lib" / "goal_state.py"
_helper_spec = importlib.util.spec_from_file_location("goal_loop_goal_state", _HELPER_PATH)
if _helper_spec is None or _helper_spec.loader is None:
    raise ImportError(f"could not load goal_state helper from {_HELPER_PATH}")
_helper_module = importlib.util.module_from_spec(_helper_spec)
sys.modules.setdefault(_helper_spec.name, _helper_module)
_helper_spec.loader.exec_module(_helper_module)
evaluate_units = _helper_module.evaluate_units
list_units = _helper_module.list_units


def _evidence(msg: str) -> None:
    with open(EVIDENCE, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def _emit(payload: dict) -> None:
    # The executor grades a verifier_ref on its captured STDOUT, not on the
    # self-managed EVIDENCE log: _run_verifier_ref/verify.py redirect stdout
    # into the evidence file and treat an empty capture as a vacuous pass
    # (suppressed -> node fails). Print the verdict JSON so the capture is
    # non-empty. The payload deliberately carries NO "pass" key, so
    # _run_verifier_ref propagates this script's own exit code: 0 for a valid
    # wave (incl. verdict=fail) and non-zero only for undecidable state.
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _missing_env() -> list[str]:
    needed = ("MO_GOAL_TARGET_CWD", "MO_GOAL_UNITS_CMD", "MO_GOAL_PREDICATE_CMD")
    return [name for name in needed if not os.environ.get(name)]


def main() -> int:
    open(EVIDENCE, "w").close()
    missing = _missing_env()
    if missing:
        reason = f"missing required env: {', '.join(missing)}"
        _evidence(reason)
        _emit({"verdict": "error", "reason": reason})
        return 1

    target_cwd = os.environ["MO_GOAL_TARGET_CWD"]
    units_cmd = os.environ["MO_GOAL_UNITS_CMD"]
    predicate_cmd = os.environ["MO_GOAL_PREDICATE_CMD"]

    try:
        units = list_units(target_cwd, units_cmd)
    except RuntimeError as exc:
        reason = f"units lister failed: {exc}"
        _evidence(reason)
        _emit({"verdict": "error", "reason": reason})
        return 1

    states = evaluate_units(target_cwd, predicate_cmd, units)
    failing = sorted(
        unit_id for unit_id, state in states.items() if not state.get("pass", False)
    )
    total = len(states)
    # Zero units is a special fail state — the goal can't be satisfied if the
    # units lister returns nothing. The kickoff explicitly carves out
    # ``reason: no_units`` so the outer driver can distinguish "nothing to
    # check" from "everything passed".
    if total == 0:
        verdict = "fail"
        payload = {
            "verdict": verdict,
            "failing_units": [],
            "total_units": 0,
            "reason": "no_units",
        }
    else:
        verdict = "pass" if not failing else "fail"
        payload = {
            "verdict": verdict,
            "failing_units": failing,
            "total_units": total,
        }

    with open(PANEL, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")

    _evidence(
        f"verdict={verdict} total={total} failing={len(failing)} "
        f"units={list(states.keys())[:10]}{'…' if len(states) > 10 else ''}"
    )
    _emit(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())