"""``board page <key>`` — every CLI action the IDE panels surface parses.

The panel review's P1 finding A1: build every page/tab on a temp home, walk
every ``cli`` action in the payload, and assert the matching subcommand's
argparse accepts the args (with ``--home`` appended when the action's
``home`` flag is not ``False``). The test NEVER calls a subcommand's
``main`` / handler. A page that does not build with ``ok: true`` fails the
test — the panel review's "no silent skips" requirement.
"""
from __future__ import annotations

import argparse
import importlib
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages import PAGES, build_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    del _out
    return h


def _seed_run(home: Path, run_id: str) -> None:
    """A run row + matching run dir so the ``run`` page builds.

    ``run._load`` -> ``fleet.run_card`` -> ``RunDetailRepository.fetch_task_run_row``
    refuses to return None only if ``task_runs`` has a row. The ``run`` page
    tabs (dag/overview/agents/learnings/artifacts) all read through that path.
    """
    con = sqlite3.connect(home / "state.db")
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "code-fix", "published", 0.0, now, now, "code_fix", "", "latest"),
    )
    con.commit()
    con.close()
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)


def _walk_cli_actions(node: dict | list | object) -> list[dict]:
    """Collect every ``{"cli": [...]}`` action anywhere under ``node``.

    The page payload is a tree of dicts; ``do`` fields nest under buttons,
    table rows, list items, chips, and section actions. Walking the whole
    tree keeps the test agnostic to where a page puts its actions.
    """
    out: list[dict] = []
    if isinstance(node, dict):
        do = node.get("do")
        if isinstance(do, dict) and isinstance(do.get("cli"), list):
            out.append(do)
        for value in node.values():
            out.extend(_walk_cli_actions(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_walk_cli_actions(value))
    return out


def _page_args_for(key: str) -> dict[str, str]:
    """The minimum ``args`` a page needs to build."""
    return {"run": "run-1791000000-aaaaaa"} if key == "run" else {}


def _parser_for_action(head: str) -> argparse.ArgumentParser | None:
    """The argparse for the subcommand the action dispatches into.

    ``SUBCOMMAND_REGISTRY`` is the source of truth for which subcommand names
    exist — the kickoff fix 8 closed the old ``SUBCOMMANDS`` skip path. But
    the registry wraps each native handler in a ``subprocess.run`` closure
    that doesn't carry ``build_parser``; the real parsers live on the
    per-subcommand modules (``mini_ork.cli.board_cmd.build_parser``,
    ``mini_ork.cli.automations_cmd._build_parser``, …). Look up the registry
    for the dispatch contract, then import the module and use ITS parser.
    """
    from mini_ork.cli import main as cli_main

    registry = getattr(cli_main, "SUBCOMMAND_REGISTRY", None) or {}
    if head not in registry:
        return None
    # Map a sub name to its parser factory. ``board`` uses the explicit
    # ``build_parser`` (factored out for this test); every other native
    # subcommand exposes ``_build_parser`` on its module. Built-ins like
    # ``run``/``doctor``/``install``/``help`` don't parse IDE action argv
    # (they don't appear as heads in any page action), so we accept a miss.
    module_name = _SUBCOMMAND_MODULE.get(head)
    if module_name is None:
        # Best-effort: try the conventional ``mini_ork.cli.<head>`` path.
        module_name = f"mini_ork.cli.{head.replace('-', '_')}"
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 — unknown module ⇒ no parser
        return None
    builder = getattr(module, "build_parser", None) or getattr(module, "_build_parser", None)
    if not callable(builder):
        return None
    parser = builder()
    if isinstance(parser, argparse.ArgumentParser):
        return parser
    return None


# Maps subcommand name → module path. Mirrors the wiring in
# ``mini_ork.cli.main._NATIVE_MODULE_SUBS``; kept inline so the test does
# not import the dispatcher (which would pull the full registry at import
# time and break order-independence).
_SUBCOMMAND_MODULE: dict[str, str] = {
    "board": "mini_ork.cli.board_cmd",
    "automations": "mini_ork.cli.automations_cmd",
    "nodes": "mini_ork.cli.nodes",
    "recipe-eval": "mini_ork.cli.recipe_eval",
    "sandbox-gc": "mini_ork.cli.sandbox_gc",
}


def test_every_page_builds_with_ok_true(home: Path) -> None:
    """A page that fails to build fails the test — no silent skips."""
    _seed_run(home, "run-1791000000-aaaaaa")
    failures: list[tuple[str, str]] = []
    for key in PAGES:
        tabs = ("__none__",) + _PAGE_TABS.get(key, ())
        for tab in tabs:
            page = build_page(home, key, args=_page_args_for(key),
                              tab=None if tab == "__none__" else tab)
            # No silent default: ``ok`` must be present and truthy.
            if page.get("ok") is not True:
                failures.append((f"{key}/{tab}", page.get("error") or "build failed"))
    assert not failures, failures


def test_every_cli_action_parses(home: Path) -> None:
    """Every ``cli`` action under every page must parse against its parser.

    The action's first element is the subcommand, the rest are args. Every
    subcommand is sourced from ``mini_ork.cli.main.SUBCOMMAND_REGISTRY`` (the
    OCP registry set up by ``register_subcommand``); ``board`` special-cases
    to ``board_cmd.build_parser()`` because its handler is a subprocess
    wrapper. No skip — a missing parser fails the test loud.
    """
    failures: list[str] = []
    for key in PAGES:
        tabs = ("__none__",) + _PAGE_TABS.get(key, ())
        for tab in tabs:
            page = build_page(home, key, args=_page_args_for(key),
                              tab=None if tab == "__none__" else tab)
            for action in _walk_cli_actions(page):
                cli = action.get("cli") or []
                if not cli:
                    continue
                head = str(cli[0])
                parser = _parser_for_action(head)
                if parser is None:
                    failures.append(f"{key}: unknown subcommand {head!r} in action {cli!r}")
                    continue
                argv = [str(a) for a in cli[1:]]
                if action.get("home", True):
                    argv += ["--home", str(home)]
                try:
                    parser.parse_known_args(argv)
                except SystemExit as exc:
                    failures.append(
                        f"{key}: parser rejected {argv!r} for {head!r} (exit {exc.code})"
                    )
    assert not failures, failures


def test_board_action_routes_through_board_cmd_parser(home: Path) -> None:
    """``board``-verb CLI actions pass through ``board_cmd.build_parser()``.

    The board parser has verbs (``show/run/merge/...``) and the IDE emits
    ``cli`` lists like ``["board", "stop", "<run_id>"]``. ``parse_known_args``
    (the IDE's "did the button dispatch?" check) must accept ``--home``.
    """
    for verb, run_id, extra in (("stop", "run-x", []), ("kill", "run-x", []),
                                ("resume", "run-x", [])):
        argv = [verb, run_id, "--home", str(home), *extra]
        # Use the real parser — never a hand-copied one.
        parser = board_cmd.build_parser()
        try:
            parser.parse_known_args(argv)
        except SystemExit as exc:
            pytest.fail(f"board parser rejected {argv!r}: exit {exc.code}")


# Tabs the test walks, mirroring each page's ``TABS`` tuple. ``__none__``
# covers the default tab; specific tab keys exercise the tab-specific
# builders.
_PAGE_TABS: dict[str, tuple[str, ...]] = {
    "runs": ("runs", "active", "hooks"),
    "changes": ("worktrees", "review"),
    "verify": ("certify", "autonomy"),
    "recipes": ("catalog", "epics"),
    "autos": ("list", "history"),
    "lanes": ("map", "budget", "spend"),
    "learn": ("patterns", "gradients", "memory"),
    "context": ("active", "library"),
    "nodes": ("fleet", "queue"),
    "setup": ("readiness", "zed", "projects", "health"),
    "run": ("dag", "overview", "agents", "learnings", "artifacts"),
}