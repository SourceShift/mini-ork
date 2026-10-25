"""Unit tests for ``mini_ork.plugins`` entry-point discovery.

Discovery is driven with fake entry points, so no distribution needs to be
installed and the global registries are never mutated: the tests use a
recording :class:`PluginContext` subclass instead of the real one.
"""
from __future__ import annotations

import types

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


def test_scan_requests_the_plugin_group(monkeypatch):
    seen = {}

    class _EPs(list):
        def select(self, *, group):
            seen["group"] = group
            return self

    monkeypatch.setattr(plugins_mod.importlib.metadata, "entry_points", lambda: _EPs())
    report = load_plugins()
    assert report.ok is True
    assert seen["group"] == ENTRY_POINT_GROUP == "mini_ork.plugins"
