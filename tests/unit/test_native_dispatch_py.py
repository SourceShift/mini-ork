"""Integration tests: every subcommand dispatches natively (bash-removal WS1).

The bash trampoline (`_bash_entrypoint_handler` → bin/mini-ork-* →
runtime-select.sh → python -m) is gone; all subcommands resolve to a
native module handler or an in-process handler.
"""
import os
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
        "rollback", "resume", "recover", "serve", "bug-collector", "conductor",
        "coord", "concord", "lifetime", "self-improve", "topology", "usage-report", "watchdog",
        "acp", "calibrate", "collapse-check", "collapse-precursor", "gate-fuzz",
        "memory-lifecycle",
        "prefs",
        "lessons",
        "reap",
        "harness-contrast", "harness-edit", "harness-audit", "hack-probe",
        "oversight", "metric-anchor", "active-eval", "certify", "node-agent",
        "nodes", "mcp", "vault", "mcp-context", "zed", "specs", "automations", "board",
        "triage",
        "repair",
        "features",
        "skills",
        "threads",
        "intake",
        "cn",
        "approvals",
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


def test_native_module_subs_are_runnable_via_python_m():
    """Every native module sub must guard ``main()`` behind ``__main__``.

    ``_native_module_handler`` dispatches as ``python -m <module>``, so a module
    without an ``if __name__ == "__main__":`` block imports and exits 0 having
    printed nothing — a silent no-op that looks like success. ``triage`` shipped
    exactly that way; this guards the whole class.
    """
    import importlib.util
    from pathlib import Path

    for sub, module in _NATIVE_MODULE_SUBS.items():
        spec = importlib.util.find_spec(module)
        assert spec and spec.origin, f"{sub}: cannot locate {module}"
        src = Path(spec.origin).read_text(encoding="utf-8")
        assert '__name__ == "__main__"' in src or "__name__ == '__main__'" in src, (
            f"{sub} ({module}) has no `if __name__ == \"__main__\":` entrypoint — "
            f"`python -m {module}` would import and exit silently"
        )


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


def test_default_root_is_the_engine_root(monkeypatch, tmp_path):
    """The default ``root`` is the directory that contains ``mini_ork``.

    ``_native_module_handler`` hands ``root`` to ``_module_env`` as the child's
    PYTHONPATH, so a root one level too deep — ``mini_ork/`` itself — makes every
    spawned subcommand die inside site init, where ``mini_ork/types.py`` shadows
    the stdlib ``types`` module and the interpreter aborts before ``main`` runs.
    ``bin/mini-ork`` reaches the same directory via ``parents[1]``; this is the
    ``python -m mini_ork.cli.main`` path, where nothing sets MINI_ORK_ROOT.
    """
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / ".mini-ork"))
    seen = {}

    class _P:
        returncode = 0

    def fake_run(cmd, **kw):
        seen["env"] = kw["env"]
        return _P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert main(["improve", "--help"]) == 0
    engine_root = seen["env"]["PYTHONPATH"]
    assert os.path.isdir(os.path.join(engine_root, "mini_ork")), (
        f"default root {engine_root!r} does not contain the mini_ork package — "
        "a subcommand spawned with it as PYTHONPATH cannot import the engine"
    )
    assert seen["env"]["PYTHONSAFEPATH"] == "1"


def test_native_subs_still_registered():
    for sub in _NATIVE_SUBS | {"recipe-eval", "execute", "run", "doctor",
                               "version", "help", "install", "providers"}:
        assert sub in SUBCOMMAND_REGISTRY, f"{sub} not registered"


def test_unknown_subcommand_contract(capsys):
    rc = main(["definitely-not-a-sub"], root="/tmp/root")
    assert rc == 2
    assert "Unknown subcommand: definitely-not-a-sub" in capsys.readouterr().err
