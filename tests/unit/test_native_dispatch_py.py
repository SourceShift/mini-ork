"""Integration tests: every subcommand dispatches natively (bash-removal WS1).

The bash trampoline (`_bash_entrypoint_handler` → bin/mini-ork-* →
runtime-select.sh → python -m) is gone; all subcommands resolve to a
native module handler or an in-process handler.
"""
import subprocess
import sys

from mini_ork.cli.main import (
    SUBCOMMAND_REGISTRY,
    _NATIVE_MODULE_SUBS,
    _NATIVE_SUBS,
    main,
)


def test_no_exec_subs_no_trampoline():
    """The _EXEC_SUBS/_bash_entrypoint_handler era is over."""
    import mini_ork.cli.main as m
    assert not hasattr(m, "_EXEC_SUBS")
    assert not hasattr(m, "_bash_entrypoint_handler")
    assert not hasattr(m, "_bin")


def test_all_former_exec_subs_registered_natively():
    expected = {
        "improve", "eval", "promote", "init", "update", "spawn", "scheduler",
        "epics", "bugs", "inject", "review", "traceotter", "metrics",
        "rollback", "resume", "recover", "serve",
    }
    assert expected == set(_NATIVE_MODULE_SUBS)
    for sub in expected:
        assert sub in SUBCOMMAND_REGISTRY, f"{sub} not registered"


def test_native_module_mapping_matches_runtime_select():
    """The mapping mirrors lib/runtime-select.sh's delegation table."""
    assert _NATIVE_MODULE_SUBS["scheduler"] == "mini_ork.scheduler"
    assert _NATIVE_MODULE_SUBS["review"] == "mini_ork.pre_push_review"
    assert _NATIVE_MODULE_SUBS["recover"] == "mini_ork.recovery.planner"
    for sub, module in _NATIVE_MODULE_SUBS.items():
        assert module.startswith("mini_ork.")


def test_native_dispatch_uses_python_m(monkeypatch):
    """A native sub dispatches [sys.executable, -m, module] — no bin/."""
    seen = {}

    class _P:
        returncode = 0

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = main(["improve", "--help"], root="/tmp/root")
    assert rc == 0
    assert seen["cmd"][:3] == [sys.executable, "-m", "mini_ork.cli.improve"]
    assert all("bin/mini-ork-" not in str(part) for part in seen["cmd"])


def test_native_subs_still_registered():
    for sub in _NATIVE_SUBS | {"recipe-eval", "execute", "run", "doctor",
                               "version", "help", "install", "providers"}:
        assert sub in SUBCOMMAND_REGISTRY, f"{sub} not registered"


def test_unknown_subcommand_contract(capsys):
    rc = main(["definitely-not-a-sub"], root="/tmp/root")
    assert rc == 2
    assert "Unknown subcommand: definitely-not-a-sub" in capsys.readouterr().err
