"""Unit tests: per-recipe probe config and local placement in the probe scorer.

Kickoff rsi-gap-fe-probes. A recipe may ship ``probes/probe-config.yaml`` whose
``budget_usd`` / ``timeout_s`` / ``control_n`` / ``max_tasks`` become that
recipe's DEFAULTS; an explicit env var still wins, and a config that exists but
cannot be parsed fails closed. Probe arms always launch with ``--placement
local``. Everything here is hermetic: no arm is launched for real.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.learning import probe_scorer as ps

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBES = REPO_ROOT / "recipes" / "framework-edit" / "probes"

_ENV_KEYS = (
    "MO_APPLY_PROBE_BUDGET_USD",
    "MO_APPLY_PROBE_TIMEOUT_S",
    "MO_APPLY_PROBE_CONTROL_N",
    "MO_APPLY_PROBE_MAX_TASKS",
    "MINI_ORK_RUN_DIR",
)


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """No probe env vars, and the scorer resolves recipes from this checkout."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ps, "_ROOT", str(REPO_ROOT))


def _make_recipe(root: Path, name: str, *, gate: int = 0, anchors: int = 0,
                 config: str | None = None) -> Path:
    probes = root / "recipes" / name / "probes"
    probes.mkdir(parents=True)
    for i in range(1, gate + 1):
        (probes / f"probe-{i}.md").write_text(f"# gate probe {i}\n", encoding="utf-8")
    for j in range(1, anchors + 1):
        (probes / f"anchor-{j}.md").write_text(
            "---\nanchor: true\n---\n# anchor\n", encoding="utf-8")
    if config is not None:
        (probes / "probe-config.yaml").write_text(config, encoding="utf-8")
    return probes


# ── defaults ────────────────────────────────────────────────────────────────


def test_no_config_keeps_the_old_global_defaults(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    _make_recipe(root, "plainrecipe", gate=3)
    monkeypatch.setattr(ps, "_ROOT", str(root))

    assert ps._probe_setting("plainrecipe", "MO_APPLY_PROBE_BUDGET_USD",
                             "budget_usd", 2.0) == 2.0
    assert ps._probe_setting("plainrecipe", "MO_APPLY_PROBE_TIMEOUT_S",
                             "timeout_s", 600.0) == 600.0
    assert ps._probe_setting("plainrecipe", "MO_APPLY_PROBE_CONTROL_N",
                             "control_n", 3, as_int=True) == 3
    assert ps._probe_setting("plainrecipe", "MO_APPLY_PROBE_MAX_TASKS",
                             "max_tasks", 2, as_int=True) == 2
    gate = [p for p, anchor in ps._load_probes("plainrecipe") if not anchor]
    assert len(gate) == 2, "max_tasks must default to 2 with no probe-config.yaml"


def test_framework_edit_config_supplies_the_defaults(monkeypatch) -> None:
    monkeypatch.setattr(ps, "_ROOT", str(REPO_ROOT))

    assert ps._probe_setting("framework-edit", "MO_APPLY_PROBE_BUDGET_USD",
                             "budget_usd", 2.0) == 60.0
    assert ps._probe_setting("framework-edit", "MO_APPLY_PROBE_TIMEOUT_S",
                             "timeout_s", 600.0) == 3600.0
    assert ps._probe_setting("framework-edit", "MO_APPLY_PROBE_CONTROL_N",
                             "control_n", 3, as_int=True) == 2
    assert ps._probe_setting("framework_edit", "MO_APPLY_PROBE_MAX_TASKS",
                             "max_tasks", 2, as_int=True) == 2


def test_env_beats_the_config_value(monkeypatch) -> None:
    monkeypatch.setattr(ps, "_ROOT", str(REPO_ROOT))
    monkeypatch.setenv("MO_APPLY_PROBE_BUDGET_USD", "5")

    assert ps._probe_setting("framework-edit", "MO_APPLY_PROBE_BUDGET_USD",
                             "budget_usd", 2.0) == 5.0


def test_unparseable_config_raises_and_names_the_file(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    _make_recipe(root, "framework-edit", gate=1,
                 config="budget_usd: [unclosed\n  bad yaml here:\n")
    monkeypatch.setattr(ps, "_ROOT", str(root))

    with pytest.raises(ValueError) as exc:
        ps._load_probes("framework-edit")
    assert "probe-config.yaml" in str(exc.value)


def test_non_numeric_config_raises_and_names_the_file(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    _make_recipe(root, "framework-edit", gate=1, config="timeout_s: soon\n")
    monkeypatch.setattr(ps, "_ROOT", str(root))

    with pytest.raises(ValueError) as exc:
        ps._probe_setting("framework-edit", "MO_APPLY_PROBE_TIMEOUT_S",
                          "timeout_s", 600.0)
    assert "probe-config.yaml" in str(exc.value)


# ── the shipped framework-edit probe set ───────────────────────────────────


def test_load_probes_returns_two_gates_plus_anchor() -> None:
    probes = ps._load_probes("framework_edit")
    names = [os.path.basename(path) for path, _ in probes]
    assert names == ["probe-1.md", "probe-2.md", "probe-3.md"]
    flags = {os.path.basename(path): anchor for path, anchor in probes}
    assert flags["probe-1.md"] is False
    assert flags["probe-2.md"] is False
    assert flags["probe-3.md"] is True


# ── launch placement ───────────────────────────────────────────────────────


class _FakeProc:
    pid = 424242
    returncode = 0

    def poll(self):
        return 0

    def communicate(self, timeout=None):
        return "", ""


def test_launch_run_places_arms_locally(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        seen.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(ps.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ps, "_await_arm_run", lambda *a, **k: ("exited", None))
    monkeypatch.setattr(ps, "_run_id_from_stdout", lambda out: "run-1-1")
    monkeypatch.setattr(ps, "_run_cost", lambda run_id, db=None: 0.0)

    ps._launch_run("framework-edit", "probes/probe-1.md")

    assert seen, "subprocess.Popen was never invoked"
    argv = seen[0]
    i = argv.index("run")
    assert argv[i + 1:i + 3] == ["--placement", "local"]
    assert argv[i + 3] == "framework-edit"
    assert "probes/probe-1.md" in argv[i + 4:]


# ── every fixture is solvable and not already solved ───────────────────────

_FIXES = {
    "probe-1": {
        "test": "test_ledger.py",
        "edits": [(
            "ledger.py",
            "total += amount\n        else:",
            "total -= amount\n        else:",
        )],
    },
    "probe-2": {
        "test": "test_route.py",
        "edits": [
            ("units.py",
             '{"m": 1.0, "cm": 0.01}',
             '{"m": 1.0, "cm": 0.01, "km": 1000.0}'),
            ("route.py",
             "return sum(to_meters(value, unit) for value, unit in legs)",
             "return round(sum(to_meters(value, unit) for value, unit in legs), 3)"),
        ],
    },
    "probe-3": {
        "test": "test_greet.py",
        "edits": [(
            "greet.py",
            'return f"Hello, {name}"',
            'return f"Hello, {name}!"',
        )],
    },
}


def _run_fixture(cwd: Path, test_file: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test_file],
        cwd=str(cwd), capture_output=True, text=True, timeout=300,
    )


@pytest.mark.parametrize("stem", sorted(_FIXES))
def test_fixture_fails_shipped_and_passes_once_fixed(stem, tmp_path) -> None:
    spec = _FIXES[stem]
    src = PROBES / "fixtures" / stem
    assert src.is_dir(), f"missing fixture directory {src}"
    cwd = tmp_path / stem
    shutil.copytree(src, cwd)

    shipped = _run_fixture(cwd, spec["test"])
    assert shipped.returncode != 0, (
        f"{stem}: the shipped fixture must FAIL its frozen test:\n"
        f"{shipped.stdout[-800:]}")

    for name, old, new in spec["edits"]:
        path = cwd / name
        before = path.read_text(encoding="utf-8")
        assert old in before, f"{stem}: fix no longer applies to {name}"
        path.write_text(before.replace(old, new), encoding="utf-8")

    fixed = _run_fixture(cwd, spec["test"])
    assert fixed.returncode == 0, (
        f"{stem}: applying the documented fix must PASS:\n{fixed.stdout[-800:]}")
