"""Unit tests for the non-interactive Agent-S driver.

``mini_ork.gates.agent_s_driver`` mirrors ``gui_agents/s3/cli_app.run_agent``
without the brokered ``input()`` loop. These tests never launch a real GUI,
import the real ``gui_agents`` / ``pyautogui``, or hit a network: stubs are
injected into ``sys.modules`` so the fakes take precedence over the optional
real packages, and ``time.sleep`` is monkeypatched to a no-op so MAX_STEPS /
WAIT / observe-only-pre / observe-only-post sleeps cost nothing.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

#: Lazy import so this test file is collected even if importing the driver
#: itself raises (it does import ``os`` and friends eagerly — but never
#: ``mini_ork`` / ``gui_agents`` / ``pyautogui`` / ``PIL``).
import importlib.util  # noqa: E402

_DRIVER_FILE = REPO / "mini_ork" / "gates" / "agent_s_driver.py"
spec = importlib.util.spec_from_file_location("agent_s_driver", _DRIVER_FILE)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

from mini_ork.gates import agent_s_smoke as ags  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────


class _FakeAgent:
    """Stand-in for ``gui_agents.s3.agents.agent_s.AgentS3``.

    ``predict_codes[i]`` is returned by the ``i+1``th call. After exhaustion
    the last value is reused so the driver eventually hits ``max_steps``.
    Calls to ``reset`` and ``predict`` are recorded for assertion.
    """

    def __init__(self, predict_codes):
        self._codes = [list(c) for c in predict_codes]
        self.reset_calls = 0
        self.predict_calls: list[tuple[str, dict]] = []

    def reset(self):
        self.reset_calls += 1

    def predict(self, instruction, observation):
        self.predict_calls.append((instruction, dict(observation)))
        idx = min(len(self.predict_calls) - 1, len(self._codes) - 1)
        code = self._codes[idx] if self._codes else []
        return ({}, code)


def _install_fake_modules(monkeypatch, predict_codes):
    """Inject ``gui_agents`` / ``pyautogui`` / ``PIL`` stubs into ``sys.modules``.

    Pillow is NOT a mini-ork dependency: CI installs only ``.[yaml,acp]`` plus
    pytest, fastapi, uvicorn, httpx — none of which pull PIL. The fake below
    covers the surface the driver touches (``Image.new``,
    ``screenshot.resize((w, h), Image.LANCZOS)``, ``screenshot.save(buf,
    format="PNG")``) so the tests collect and run even when Pillow is
    uninstalled.

    Returns ``(fake_pg, fake_agent, click_calls, constructed, exec_calls)`` so
    tests can inspect recorded ``pyautogui.click`` calls, the engine
    construction history, and any (forbidden) calls to ``builtins.exec``.
    """
    # ── Fake PIL ──────────────────────────────────────────────────────────
    class _FakeImage:
        def __init__(self, width: int, height: int):
            self.width = width
            self.height = height

        def copy(self):
            return _FakeImage(self.width, self.height)

        def resize(self, size, resample=None):
            del resample  # noqa: WPS420 — the driver never inspects this
            return _FakeImage(int(size[0]), int(size[1]))

        def save(self, buf, format=None):
            del format  # noqa: WPS420 — the driver doesn't introspect format
            # Write deterministic bytes so any future assertion on obs size
            # has something to read.
            buf.write(b"FAKE_PNG_" + str(self.width).encode() + b"x" + str(self.height).encode())

    class _FakeImageModule:
        LANCZOS = 1  # arbitrary sentinel; driver doesn't read its value

        @staticmethod
        def new(mode, size, color=None):
            del mode, color  # noqa: WPS420 — driver doesn't introspect mode/color
            return _FakeImage(int(size[0]), int(size[1]))

    fake_pil = types.ModuleType("PIL")
    fake_image_mod = types.ModuleType("PIL.Image")
    fake_image_mod.new = _FakeImageModule.new
    fake_image_mod.LANCZOS = _FakeImageModule.LANCZOS
    fake_pil.Image = fake_image_mod
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_image_mod)

    # ── Fake pyautogui ─────────────────────────────────────────────────────
    fake_pg = types.ModuleType("pyautogui")
    click_calls: list[tuple] = []
    fake_pg.click = lambda *args, **kwargs: click_calls.append((args, kwargs))

    def _size():
        return (1920, 1080)

    fake_pg.size = _size

    real_screenshot = fake_image_mod.new("RGB", (1920, 1080), color=(255, 255, 255))

    def _screenshot():
        return real_screenshot.copy()

    fake_pg.screenshot = _screenshot

    # ── Fake Agent-S / OSWorldACI ──────────────────────────────────────────
    fake_agent = _FakeAgent(predict_codes)
    constructed: dict[str, list] = {"engines_generation": [], "grounding": []}

    class _FakeOSWorldACI:
        def __init__(self, **kwargs):
            constructed["grounding"].append(kwargs)

    agent_s_mod = types.ModuleType("gui_agents.s3.agents.agent_s")

    def _agent_s_ctor(engine_params, grounding_agent, **kwargs):
        constructed["engines_generation"].append(engine_params)
        return fake_agent

    agent_s_mod.AgentS3 = _agent_s_ctor

    grounding_mod = types.ModuleType("gui_agents.s3.agents.grounding")
    grounding_mod.OSWorldACI = _FakeOSWorldACI

    agents_pkg = types.ModuleType("gui_agents.s3.agents")
    s3_pkg = types.ModuleType("gui_agents.s3")
    agents_pkg.grounding = grounding_mod  # legacy import alias
    agents_pkg.agent_s = agent_s_mod
    s3_pkg.agents = agents_pkg
    gui_agents_pkg = types.ModuleType("gui_agents")
    gui_agents_pkg.s3 = s3_pkg

    monkeypatch.setitem(sys.modules, "gui_agents", gui_agents_pkg)
    monkeypatch.setitem(sys.modules, "gui_agents.s3", s3_pkg)
    monkeypatch.setitem(sys.modules, "gui_agents.s3.agents", agents_pkg)
    monkeypatch.setitem(
        sys.modules, "gui_agents.s3.agents.grounding", grounding_mod
    )
    monkeypatch.setitem(sys.modules, "gui_agents.s3.agents.agent_s", agent_s_mod)
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pg)

    # ── exec() recorder ───────────────────────────────────────────────────
    # Observe-only and WAIT/NEXT branches must never reach ``exec``. Patch it
    # in ``builtins`` so any call the driver (or an action string) makes is
    # recorded — tests assert the call list is empty for the non-exec paths.
    exec_calls: list[tuple[str, dict]] = []
    real_exec = builtins.exec

    def _exec(source, globals=None, locals=None, /, *args, **kwargs):  # noqa: A002
        exec_calls.append((str(source), dict(globals or {})))
        return real_exec(source, globals, locals, *args, **kwargs)

    monkeypatch.setattr(builtins, "exec", _exec)

    return fake_pg, fake_agent, click_calls, constructed, exec_calls


def _capture_stdout(monkeypatch):
    """Replace ``sys.stdout`` with a ``StringIO`` and return it."""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    return buf


def _parse_only_line(stdout_buf: io.StringIO) -> dict:
    payload = stdout_buf.getvalue()
    lines = [ln for ln in payload.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line, got: {payload!r}"
    return json.loads(lines[0])


# ─────────────────────────────────────────────────────────────────────────────
# DoD 1: observe-only skips exec, emits DONE with steps=2, writes step_*.png
# ─────────────────────────────────────────────────────────────────────────────


def test_main_observe_only_does_not_exec_click(tmp_path, monkeypatch, capsys):
    """`click(1,2)` is never exec'd; stdout is exactly one JSON line DONE/2."""
    shot_dir = tmp_path / "shots"
    _pg, _agent, click_calls, _cons, exec_calls = _install_fake_modules(
        monkeypatch, predict_codes=[["click(1,2)"], ["DONE"]]
    )
    monkeypatch.setattr(driver.time, "sleep", lambda *_a, **_kw: None)

    rc = driver.main(
        [
            "--task", "open the board",
            "--observe-only",
            "--max-steps", "5",
            "--shot-dir", str(shot_dir),
            "--ground-url", "https://ground.example",
        ]
    )
    out = capsys.readouterr()
    payload = json.loads(out.out.strip().splitlines()[-1])

    assert rc == 0  # DONE → exit 0
    assert len(out.out.strip().splitlines()) == 1
    assert payload["success"] is True
    assert payload["outcome"] == "DONE"
    assert payload["steps"] == 2
    assert payload["error"] is None

    # Screenshots written to --shot-dir for each iteration entered.
    assert (shot_dir / "step_1.png").exists()
    assert (shot_dir / "step_2.png").exists()

    # No real `click(1,2)` was exec'd: the action string reached the observe-only
    # branch only, where the driver prints a skip note and `continue`s. The
    # exec-recorder proves the call never happened; without it a future change
    # that adds `click` to the exec globals (e.g. seeding `pyautogui.click`)
    # would silently turn this into a live-fire test.
    assert click_calls == [], "pyautogui.click must not be called under --observe-only"
    assert exec_calls == [], (
        f"exec() must not be called under --observe-only; got: {exec_calls!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# DoD 2: --observe-only absent → action IS executed via exec()
# ─────────────────────────────────────────────────────────────────────────────


def test_main_without_observe_only_executes_action(tmp_path, monkeypatch, capsys):
    """No `--observe-only` → fake `pyautogui.click(1, 2)` is recorded."""
    shot_dir = tmp_path / "shots"
    _pg, _agent, click_calls, _cons, _exec_calls = _install_fake_modules(
        monkeypatch, predict_codes=[["pyautogui.click(1, 2)"], ["DONE"]]
    )
    monkeypatch.setattr(driver.time, "sleep", lambda *_a, **_kw: None)

    rc = driver.main(
        [
            "--task", "press the button",
            "--max-steps", "5",
            "--shot-dir", str(shot_dir),
        ]
    )
    out = capsys.readouterr()
    payload = json.loads(out.out.strip().splitlines()[-1])

    assert rc == 0
    assert payload["success"] is True
    assert payload["outcome"] == "DONE"
    # click was recorded with the literal args the action string passed.
    assert click_calls, "pyautogui.click was not invoked"
    args, kwargs = click_calls[0]
    assert args == (1, 2)
    assert kwargs == {}


# ─────────────────────────────────────────────────────────────────────────────
# DoD 3: FAIL / MAX_STEPS / ERROR outcomes
# ─────────────────────────────────────────────────────────────────────────────


def test_main_fail_returns_failure_and_exit_1(tmp_path, monkeypatch, capsys):
    """`["FAIL"]` → success:false, exit 1, exactly one JSON line."""
    _pg, _agent, click_calls, _cons, _exec_calls = _install_fake_modules(
        monkeypatch, predict_codes=[["FAIL"]]
    )
    monkeypatch.setattr(driver.time, "sleep", lambda *_a, **_kw: None)

    rc = driver.main(
        [
            "--task", "open",
            "--observe-only",
            "--max-steps", "3",
            "--shot-dir", str(tmp_path),
        ]
    )
    out = capsys.readouterr()
    payload = json.loads(out.out.strip().splitlines()[-1])

    assert rc == 1
    assert len(out.out.strip().splitlines()) == 1
    assert payload["success"] is False
    assert payload["outcome"] == "FAIL"
    assert payload["steps"] == 1
    assert payload["error"] is None
    assert click_calls == []


def test_main_max_steps_returns_max_steps_outcome(
    tmp_path, monkeypatch, capsys
):
    """Loop runs to --max-steps without terminal → MAX_STEPS, exit 1.

    Each iteration returns a non-terminal action. With --observe-only the
    action branch is skipped (no exec, no 1.0s sleep), the loop just
    iterates, and after ``--max-steps`` iterations the loop exits naturally
    with ``outcome == "MAX_STEPS"``.
    """
    _pg, _agent, click_calls, _cons, _exec_calls = _install_fake_modules(
        monkeypatch, predict_codes=[["noop()"]]  # unrecognised action; observe-only skips
    )
    monkeypatch.setattr(driver.time, "sleep", lambda *_a, **_kw: None)

    rc = driver.main(
        [
            "--task", "look around",
            "--observe-only",
            "--max-steps", "2",
            "--shot-dir", str(tmp_path),
        ]
    )
    out = capsys.readouterr()
    payload = json.loads(out.out.strip().splitlines()[-1])

    assert rc == 1
    assert payload["success"] is False
    assert payload["outcome"] == "MAX_STEPS"
    assert payload["steps"] == 2
    assert payload["error"] is None


def test_main_predict_exception_returns_error_outcome(
    tmp_path, monkeypatch, capsys
):
    """Exception inside predict → outcome ERROR, exit 1, exactly one JSON line."""

    class _RaisingAgent(_FakeAgent):
        def predict(self, instruction, observation):
            self.predict_calls.append((instruction, dict(observation)))
            raise RuntimeError("predict kaboom")

    _pg, _agent_stub, click_calls, _cons, _exec_calls = _install_fake_modules(
        monkeypatch, predict_codes=[["ignored"]]
    )
    # Swap the fake agent with one that raises; agent_s_mod.AgentS3 is the
    # factory we patched, so re-stub it directly.
    raising = _RaisingAgent(["ignored"])
    agent_s_mod = sys.modules["gui_agents.s3.agents.agent_s"]
    agent_s_mod.AgentS3 = lambda *a, **kw: raising  # noqa: E731

    monkeypatch.setattr(driver.time, "sleep", lambda *_a, **_kw: None)

    rc = driver.main(
        [
            "--task", "t",
            "--observe-only",
            "--max-steps", "3",
            "--shot-dir", str(tmp_path),
        ]
    )
    out = capsys.readouterr()
    payload = json.loads(out.out.strip().splitlines()[-1])

    assert rc == 1
    assert len(out.out.strip().splitlines()) == 1
    assert payload["success"] is False
    assert payload["outcome"] == "ERROR"
    assert "predict kaboom" in (payload["error"] or "")


# ─────────────────────────────────────────────────────────────────────────────
# DoD 4: `_agent_s_command` shape (argv starts with python + driver, flags
# adjacent to values, observe-only by default, drops for interact: true,
# legacy `openrouter` alias → `open_router`).
# ─────────────────────────────────────────────────────────────────────────────


def _expect_flag_value(argv, flag):
    """Return the value adjacent to ``--flag`` in ``argv`` or raise."""
    for i, item in enumerate(argv):
        if item == flag:
            return argv[i + 1]
    raise AssertionError(f"flag {flag!r} not adjacent in argv: {argv}")


def _hermetic_env(monkeypatch):
    """Strip every env var the driver / gate resolves, plus PATH bits."""
    for var in (
        ags.PROVIDER_ENV,
        ags.MODEL_ENV,
        ags.MODEL_URL_ENV,
        ags.GROUND_PROVIDER_ENV,
        ags.GROUND_MODEL_ENV,
        ags.GROUND_URL_ENV,
        ags.GROUND_API_KEY_ENV_VAR_ENV,
        ags.PYTHON_ENV,
        "OPENROUTER_API_KEY",
        "HUGGINGFACE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    # Force `_resolve_python` to fall through to `sys.executable` (no venv
    # `agent_s` binary on the unit-test PATH).
    monkeypatch.setattr(ags.shutil, "which", lambda _name: None)


def test_agent_s_command_default_env_shape(monkeypatch):
    """Default env: python path, driver path, --provider open_router, --observe-only, --model-url https://openrouter.ai/api/v1."""
    _hermetic_env(monkeypatch)

    spec = {"steps": "open", "expect": "shown", "interact": False}
    argv = ags._agent_s_command(spec, "/tmp/scratch")

    # argv[0] = python; argv[1] = driver script path (must exist).
    assert argv[0] == sys.executable
    assert argv[1] == ags.DRIVER_PATH
    assert os.path.isfile(argv[1])

    # Defaults applied to adjacent flags.
    assert _expect_flag_value(argv, "--provider") == "open_router"
    assert _expect_flag_value(argv, "--model") == "openai/gpt-4o-mini"
    assert (
        _expect_flag_value(argv, "--model-url")
        == "https://openrouter.ai/api/v1"
    )
    assert _expect_flag_value(argv, "--ground-provider") == "huggingface"
    assert _expect_flag_value(argv, "--ground-model") == "ui-tars-1.5-7b"
    # --observe-only when spec is non-interactive.
    assert "--observe-only" in argv
    # No --interact flag (it's a spec key, not argv).
    assert "--interact" not in argv


def test_agent_s_command_ground_provider_open_router(monkeypatch):
    """`AGENT_S_GROUND_PROVIDER=open_router` propagates and sets the OpenRouter key var."""
    _hermetic_env(monkeypatch)
    monkeypatch.setenv(ags.GROUND_PROVIDER_ENV, "open_router")
    monkeypatch.setenv(ags.GROUND_MODEL_ENV, "bytedance/ui-tars-1.5-7b")
    monkeypatch.setenv(ags.GROUND_API_KEY_ENV_VAR_ENV, "OPENROUTER_API_KEY")

    spec = {"steps": "x", "expect": "y"}
    argv = ags._agent_s_command(spec, "/tmp/s")

    assert _expect_flag_value(argv, "--ground-provider") == "open_router"
    assert _expect_flag_value(argv, "--ground-model") == "bytedance/ui-tars-1.5-7b"
    assert _expect_flag_value(argv, "--ground-api-key-env") == "OPENROUTER_API_KEY"


def test_agent_s_command_interact_true_drops_observe_only(monkeypatch):
    """`interact: true` in the spec drops `--observe-only`."""
    _hermetic_env(monkeypatch)

    spec = {"steps": "x", "expect": "y", "interact": True}
    argv = ags._agent_s_command(spec, "/tmp/s")

    assert "--observe-only" not in argv


def test_agent_s_command_legacy_openrouter_alias(monkeypatch):
    """Legacy `openrouter` env value normalises to `open_router` on argv."""
    _hermetic_env(monkeypatch)
    monkeypatch.setenv(ags.PROVIDER_ENV, "openrouter")

    spec = {"steps": "x", "expect": "y"}
    argv = ags._agent_s_command(spec, "/tmp/s")

    assert _expect_flag_value(argv, "--provider") == "open_router"


def test_agent_s_command_python_resolution_precedence(monkeypatch):
    """`AGENT_S_PYTHON` precedes the sibling-of-agent_s fallback."""
    _hermetic_env(monkeypatch)
    monkeypatch.setattr(ags.shutil, "which", lambda _name: "/V/bin/agent_s")
    monkeypatch.setenv(ags.PYTHON_ENV, "/custom/venv/bin/python")

    spec = {"steps": "x", "expect": "y"}
    argv = ags._agent_s_command(spec, "/tmp/s")

    assert argv[0] == "/custom/venv/bin/python"


# ─────────────────────────────────────────────────────────────────────────────
# import hygiene — driver must not import mini_ork / gui_agents / pyautogui / PIL
# at module top
# ─────────────────────────────────────────────────────────────────────────────


def test_driver_has_no_module_level_mini_ork_or_agent_s_imports():
    """Lint: any future regression that re-adds ``import mini_ork`` /
    ``import gui_agents.*`` at module top is caught here. The driver is meant
    to run under the Agent-S interpreter; on a default test Python it must
    fail closed without import errors.
    """
    import ast

    tree = ast.parse(_DRIVER_FILE.read_text())
    top_imports = [
        n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    bad_modules: list[str] = []
    for node in top_imports:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in {
                    "mini_ork",
                    "gui_agents",
                    "pyautogui",
                    "PIL",
                }:
                    bad_modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in {
                "mini_ork",
                "gui_agents",
                "pyautogui",
                "PIL",
            }:
                bad_modules.append(node.module)
    assert not bad_modules, f"driver must not import these at module top: {bad_modules}"


# ─────────────────────────────────────────────────────────────────────────────
# argv adjacency: spec values land on the adjacent flag
# ─────────────────────────────────────────────────────────────────────────────


def test_agent_s_command_max_steps_from_spec(monkeypatch):
    """Spec `max_steps: 3` → adjacent `--max-steps 3`."""
    _hermetic_env(monkeypatch)
    spec = {"steps": "x", "expect": "y", "max_steps": 3}
    argv = ags._agent_s_command(spec, "/tmp/s")
    assert _expect_flag_value(argv, "--max-steps") == "3"
    assert _expect_flag_value(argv, "--shot-dir") == "/tmp/s"
