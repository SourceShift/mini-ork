"""Unit tests for ``mini_ork.plugins`` entry-point discovery.

Discovery is driven with fake entry points, so no distribution needs to be
installed and the global registries are never mutated: the tests use a
recording :class:`PluginContext` subclass instead of the real one.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from mini_ork import plugins as plugins_mod
from mini_ork.plugins import (
    ENTRY_POINT_GROUP,
    PluginContext,
    PluginError,
    PluginFailure,
    enabled_plugins,
    load_entry_point,
    load_plugins,
)


class _Ctx(PluginContext):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def subcommand(self, name, handler, *, registry=None):  # noqa: D401
        self.calls.append(("subcommand", name))


class _EP:
    """Stand-in for ``importlib.metadata.EntryPoint``."""

    def __init__(self, name: str, target, value: str = "") -> None:
        self.name = name
        self._target = target
        self.value = value or f"fake:{name}"

    def load(self):
        if isinstance(self._target, BaseException):
            raise self._target
        return self._target


def test_callable_target_gets_context():
    ctx = _Ctx()
    seen = []

    def register(context):
        seen.append(context)
        context.subcommand("deploy", lambda rest, root: 0)

    loaded = load_entry_point(_EP("acme", register), context=ctx)

    assert seen == [ctx]
    assert ctx.calls == [("subcommand", "deploy")]
    assert loaded.name == "acme"


def test_module_target_uses_register_attribute():
    ctx = _Ctx()
    mod = types.ModuleType("acme_plugin")

    def register(context):
        context.subcommand("attest", lambda rest, root: 0)

    mod.register = register  # type: ignore[attr-defined]

    loaded = load_entry_point(_EP("acme", mod), context=ctx)

    assert ctx.calls == [("subcommand", "attest")]
    assert loaded.module


def test_module_without_register_is_rejected():
    mod = types.ModuleType("bad_plugin")
    with pytest.raises(PluginError, match="callable or a module with register"):
        load_entry_point(_EP("bad", mod), context=_Ctx())


def test_import_failure_is_wrapped():
    with pytest.raises(PluginError, match="import failed"):
        load_entry_point(_EP("broken", ImportError("no such module")), context=_Ctx())


def test_registration_failure_is_wrapped():
    def register(context):
        raise RuntimeError("boom")

    with pytest.raises(PluginError, match="registration failed"):
        load_entry_point(_EP("boom", register), context=_Ctx())


def test_load_plugins_collects_failures_without_aborting():
    ctx = _Ctx()

    def ok(context):
        context.subcommand("ok", lambda rest, root: 0)

    report = load_plugins(
        context=ctx,
        entry_points=[_EP("ok", ok), _EP("bad", ImportError("missing"))],
    )

    assert [p.name for p in report.loaded] == ["ok"]
    assert report.failed == [PluginFailure(name="bad", error="bad: import failed: missing")]
    assert report.ok is False
    assert report.to_dict()["loaded"][0]["name"] == "ok"


def test_load_plugins_strict_raises_on_first_failure():
    with pytest.raises(PluginError):
        load_plugins(strict=True, entry_points=[_EP("bad", ImportError("nope"))])


def test_load_plugins_is_a_noop_without_entry_points():
    report = load_plugins(entry_points=[])
    assert report.ok is True
    assert report.loaded == []
    assert report.failed == []


def test_load_plugins_records_enabled_plugins():
    def register(context):
        context.subcommand("visible", lambda rest, root: 0)

    report = load_plugins(context=_Ctx(), entry_points=[_EP("visible-plugin", register)])

    assert [p.name for p in report.loaded] == ["visible-plugin"]
    assert enabled_plugins()["visible-plugin"].name == "visible-plugin"
    assert enabled_plugins()["visible-plugin"].module


class _EPs(list):
    """Stand-in for ``EntryPoints``, which is filtered with ``.select``."""

    seen: dict = {}

    def select(self, *, group):
        type(self).seen["group"] = group
        return self


def test_scan_requests_the_plugin_group(monkeypatch):
    seen = {}
    monkeypatch.setattr(plugins_mod.importlib.metadata, "entry_points",
                        lambda: _EPs([_EP("noop", lambda ctx: None)]))
    monkeypatch.setattr(_EPs, "seen", seen)
    report = load_plugins()
    assert report.ok is True
    assert seen["group"] == ENTRY_POINT_GROUP == "mini_ork.plugins"


def _reset_bootstrap(monkeypatch) -> None:
    """Force the next ``bootstrap()`` to discover (it is once-per-process)."""
    monkeypatch.setattr(plugins_mod, "_BOOTSTRAPPED", False)
    monkeypatch.delenv(plugins_mod.BOOTSTRAP_ENV, raising=False)


def test_bootstrap_runs_discovery_once(monkeypatch):
    """The documented host seam must actually be wired.

    ``mini_ork/plugins.py`` says the host calls discovery at startup. While the
    CLI never did, ``MO_BUDGET_POLICY=acme`` could only fail closed in every
    real deployment, so the budget's secure tier was unreachable.
    """
    calls: list[str] = []

    def register(context):
        calls.append(context.__class__.__name__)

    monkeypatch.setattr(plugins_mod.importlib.metadata, "entry_points",
                        lambda: _EPs([_EP("acme", register)]))
    _reset_bootstrap(monkeypatch)

    report = plugins_mod.bootstrap()
    again = plugins_mod.bootstrap()  # idempotent: one scan per process

    assert calls == ["PluginContext"]
    assert [p.name for p in report.loaded] == ["acme"]
    assert again is None


def test_a_broken_plugin_warns_instead_of_killing_the_cli(monkeypatch, capsys):
    monkeypatch.setattr(plugins_mod.importlib.metadata, "entry_points",
                        lambda: _EPs([_EP("bad", ImportError("nope"))]))
    _reset_bootstrap(monkeypatch)

    report = plugins_mod.bootstrap()

    assert report is not None
    assert [f.name for f in report.failed] == ["bad"]
    assert "warning: plugin bad" in capsys.readouterr().err


def test_a_failing_scan_never_breaks_the_cli(monkeypatch, capsys):
    def boom():
        raise RuntimeError("metadata index is unreadable")

    monkeypatch.setattr(plugins_mod.importlib.metadata, "entry_points", boom)
    _reset_bootstrap(monkeypatch)

    assert plugins_mod.bootstrap() is None
    assert "plugin discovery failed" in capsys.readouterr().err


def test_bootstrap_can_be_opted_out(monkeypatch):
    scanned: list[bool] = []
    monkeypatch.setattr(plugins_mod.importlib.metadata, "entry_points",
                        lambda: scanned.append(True) or _EPs())
    _reset_bootstrap(monkeypatch)
    monkeypatch.setenv(plugins_mod.BOOTSTRAP_ENV, "1")

    assert plugins_mod.bootstrap() is None
    assert scanned == []


def _write_fake_dist(root) -> object:
    """A minimal installed distribution declaring one ``mini_ork.plugins`` entry."""
    dist = root / "acme-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: acme\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text(
        "[mini_ork.plugins]\nacme = acme_plugin:register\n")
    (root / "acme_plugin.py").write_text(
        "def register(context):\n"
        "    from mini_ork.cost.budget import Verdict, register_budget_policy\n"
        "    register_budget_policy('acme', lambda db, run_id, run_dir:"
        " Verdict(0.0, 9.0, False, 'acme quota'))\n")
    return root


def test_a_native_subcommand_child_process_sees_plugins(tmp_path):
    """``python -m mini_ork.cli.<sub>`` is a fresh interpreter.

    That is exactly what ``mini-ork resume`` execs, and the budget gate's own
    refusal tells the operator to run it. Discovery in ``main()`` alone would
    never reach that child, so a plugin-provided policy could never un-park a
    run. Bootstrap runs at ``mini_ork.cli`` import, which every child does.
    """
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[2]
    _write_fake_dist(tmp_path)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(repo)])
    env.pop(plugins_mod.BOOTSTRAP_ENV, None)
    code = ("import mini_ork.cli; from mini_ork.cost import budget;"
            " print(sorted(budget.BUDGET_POLICIES))")

    out = subprocess.run([sys.executable, "-c", code], cwd=str(repo), env=env,
                         capture_output=True, text=True, timeout=120)

    assert out.returncode == 0, out.stderr
    assert "acme" in out.stdout
