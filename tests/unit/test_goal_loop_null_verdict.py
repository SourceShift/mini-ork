"""Unit tests for the G2 null-verdict cycle.

A unit is pass, confirmed-fail, or not-reproduced. A not-reproduced red is a
null verdict: the honest output is "nothing to fix", so the loop must stop
saying so rather than dispatch a child against a defect that is not there.

Hermetic: no network, no lane, no ``bin/mini-ork``. The flaky predicate is a
``pred.py`` in ``tmp_path`` that appends to a counter file and exits 1 the
first time, 0 thereafter — a real subprocess, not a test double.

The eight assertions below are the contract (kickoff ``## Tests``); they pin:

1. ``evaluate_units(..., confirm_runs=2)`` on a fails-then-passes predicate →
   ``pass is False``, ``reproduced is False``, ``attempts == 2``, ``reason``
   starts ``"flake: did not reproduce"``.
2. ``evaluate_units(..., confirm_runs=3)`` on an always-failing predicate →
   ``pass is False``, ``reproduced is True``, ``attempts == 3``, no ``flake:``.
3. default ``evaluate_units`` runs the predicate exactly once (counter proof);
   a passing predicate reports ``reproduced is True``.
4. ``_select_units`` skips ``reproduced=False``, selects ``reproduced=True``,
   never selects a passing unit.
5. legacy ``goal-state.json`` (no ``reproduced`` key) selects as today.
6. the null-verdict exclusion has NO starvation guard (returns ``[]`` when
   every red is non-reproducible) while the quarantine guard still falls back.
7. the driver stops ``nothing_to_fix`` only when every failing unit is
   ``reproduced=False``.
8. a wave verdict with no ``unit_reproduced`` key never stops ``nothing_to_fix``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

RECIPE_DIR = Path(__file__).resolve().parents[2] / "recipes" / "goal-loop"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


_GOAL_STATE = _load("goal_loop_gs_nullverdict", RECIPE_DIR / "lib" / "goal_state.py")
_TRANSFORMS = _load("goal_loop_transforms_nullverdict", RECIPE_DIR / "lib" / "transforms.py")
_DRIVE = _load("goal_loop_drive_nullverdict", RECIPE_DIR / "lib" / "drive.py")

evaluate_units = _GOAL_STATE.evaluate_units
select_units = _TRANSFORMS._select_units  # noqa: SLF001 — test seam
drive = _DRIVE.drive


# ── predicate fixtures ─────────────────────────────────────────────────────


def _write_predicate(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    """Write ``pred.py`` + ``count.txt``; return ``(pred, count)``.

    ``mode`` is ``"flaky"`` (fail once, then pass), ``"always-fail"``, or
    ``"always-pass"``. The predicate appends one ``x`` to ``count.txt`` per
    invocation, so ``len(count.read_text())`` is the exact run count.
    """
    count = tmp_path / "count.txt"
    count.write_text("", encoding="utf-8")
    pred = tmp_path / "pred.py"
    if mode == "flaky":
        body = (
            "import sys\n"
            f"p = {str(count)!r}\n"
            "open(p, 'a').write('x')\n"
            "n = len(open(p).read())\n"
            "print('flaky red' if n == 1 else 'clean')\n"
            "sys.exit(1 if n == 1 else 0)\n"
        )
    elif mode == "always-fail":
        body = (
            "import sys\n"
            f"p = {str(count)!r}\n"
            "open(p, 'a').write('x')\n"
            "print('always red')\n"
            "sys.exit(1)\n"
        )
    else:  # always-pass
        body = (
            "import sys\n"
            f"p = {str(count)!r}\n"
            "open(p, 'a').write('x')\n"
            "print('clean')\n"
            "sys.exit(0)\n"
        )
    pred.write_text(body, encoding="utf-8")
    return pred, count


def _eval(tmp_path: Path, mode: str, confirm_runs: int = 1):
    pred, count = _write_predicate(tmp_path, mode)
    cmd = f"{sys.executable} {pred}"
    states = evaluate_units(str(tmp_path), cmd, ["u1"], confirm_runs=confirm_runs)
    return states, count


def _run_driver(tmp_path: Path, unit_reproduced: dict | None):
    state_dir = tmp_path / "state"

    def run_wave(wave_no, quarantined):
        verdict = {
            "verdict": "fail",
            "failing_before": ["u1"],
            "failing_after": ["u1"],
            "cost_usd": 0.0,
            "run_id": f"r{wave_no}",
        }
        if unit_reproduced is not None:
            verdict["unit_reproduced"] = unit_reproduced
        return verdict

    return drive(
        goal_id="gnull", target_cwd=str(tmp_path), units_cmd="echo u1",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=1000.0,
        run_wave_fn=run_wave, cost_fn=lambda: 0.0, state_dir=state_dir,
    )


# ── 1. confirm_runs marks a fails-then-passes red not-reproduced ────────────


def test_confirm_runs_marks_a_flaky_red_not_reproduced(tmp_path):
    states, _ = _eval(tmp_path, "flaky", confirm_runs=2)
    s = states["u1"]
    assert s["pass"] is False
    assert s["reproduced"] is False
    assert s["attempts"] == 2
    assert s["reason"].startswith("flake: did not reproduce")


# ── 2. confirm_runs marks an always-failing red reproduced ─────────────────


def test_confirm_runs_marks_a_reproduced_red(tmp_path):
    states, _ = _eval(tmp_path, "always-fail", confirm_runs=3)
    s = states["u1"]
    assert s["pass"] is False
    assert s["reproduced"] is True
    assert s["attempts"] == 3
    assert "flake:" not in s["reason"]


# ── 3. default confirm_runs is a single run; passing is reproduced ─────────


def test_default_confirm_runs_is_single_run_and_reproduced(tmp_path):
    states, count = _eval(tmp_path, "always-fail")
    assert states["u1"]["pass"] is False
    assert states["u1"]["attempts"] == 1
    assert states["u1"]["reproduced"] is True
    assert len(count.read_text()) == 1  # counter proves the predicate ran once

    states2, _ = _eval(tmp_path, "always-pass")
    assert states2["u1"]["pass"] is True
    assert states2["u1"]["reproduced"] is True


# ── 4. _select_units skips null verdicts, never selects a pass ─────────────


def test_select_units_skips_null_verdicts():
    gs = {
        "1": {"pass": False, "reproduced": False},
        "2": {"pass": False, "reproduced": True},
        "3": {"pass": True, "reproduced": True},
    }
    assert select_units(gs, 3) == ["2"]


# ── 5. legacy state (no reproduced key) selects as today ───────────────────


def test_select_units_legacy_state_is_unchanged():
    legacy = {"1": {"pass": False}, "2": {"pass": False}, "3": {"pass": True}}
    reproduced = {
        "1": {"pass": False, "reproduced": True},
        "2": {"pass": False, "reproduced": True},
        "3": {"pass": True, "reproduced": True},
    }
    assert select_units(legacy, 3) == select_units(reproduced, 3)
    assert select_units(legacy, 3) == ["1", "2"]


# ── 6. null verdict has NO starvation guard; quarantine guard still works ──


def test_select_units_null_verdict_has_no_starvation_guard():
    gs = {
        "1": {"pass": False, "reproduced": False},
        "2": {"pass": False, "reproduced": False},
    }
    assert select_units(gs, 1) == []  # no fallback: empty is the honest verdict

    gs2 = {"1": {"pass": False, "reproduced": True}}
    assert select_units(gs2, 1, quarantined={"1"}) == ["1"]  # quarantine guard


# ── 7. driver stops nothing_to_fix only when every failing unit is null ────


def test_driver_stops_nothing_to_fix_when_all_null(tmp_path):
    verdict = _run_driver(tmp_path, {"u1": False})
    assert verdict["stop"] == "nothing_to_fix"


def test_driver_does_not_stop_nothing_to_fix_when_reproduced(tmp_path):
    verdict = _run_driver(tmp_path, {"u1": True})
    assert verdict["stop"] != "nothing_to_fix"


# ── 8. no unit_reproduced key → never stops nothing_to_fix ─────────────────


def test_driver_no_unit_reproduced_key_never_stops_nothing_to_fix(tmp_path):
    verdict = _run_driver(tmp_path, None)
    assert verdict["stop"] != "nothing_to_fix"
