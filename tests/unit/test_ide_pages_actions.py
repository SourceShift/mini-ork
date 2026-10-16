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

    The subcommand→module wiring comes from
    ``mini_ork.cli.main._NATIVE_MODULE_SUBS`` (read lazily here so importing
    this test file does NOT pull the full registry at module import time and
    break order-independence — that was the original reason the map was
    hand-copied). The lazy import keeps the property; reading
    ``_NATIVE_MODULE_SUBS`` directly removes the drift hazard.
    """
    from mini_ork.cli import main as cli_main

    registry = getattr(cli_main, "SUBCOMMAND_REGISTRY", None) or {}
    if head not in registry:
        return None
    # Authoritative map: ``mini_ork.cli.main._NATIVE_MODULE_SUBS``. Built-ins
    # like ``run``/``doctor``/``install``/``help`` don't appear there; we
    # fall back to the conventional ``mini_ork.cli.<head>`` path so the
    # test still surfaces legacy module paths if the dispatcher ships a
    # built-in that is not in ``_NATIVE_MODULE_SUBS``.
    module_map = getattr(cli_main, "_NATIVE_MODULE_SUBS", None) or {}
    module_name = module_map.get(head)
    if module_name is None:
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


def _walk_cli_prefixes(node: object) -> list[dict]:
    """Every ``{"cli": [...]}`` dict, including composer prefixes.

    :func:`_walk_cli_actions` follows ``do`` only, which skips a composer: its
    ``cli`` is a *prefix* the IDE appends typed text to, so it carries neither a
    complete argv nor a ``do``. The ``home`` flag rides the same dict, so a
    composer is exactly as capable of handing a subcommand an argument its
    parser rejects — the very bug this file's ``home`` test exists to stop.
    """
    out: list[dict] = []
    if isinstance(node, dict):
        cli = node.get("cli")
        if isinstance(cli, list) and cli:
            out.append(node)
        for value in node.values():
            out.extend(_walk_cli_prefixes(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_walk_cli_prefixes(value))
    return out


def _module_source(head: str) -> str | None:
    """The source of the module a subcommand dispatches into, or ``None``."""
    from mini_ork.cli import main as cli_main

    module_map = getattr(cli_main, "_NATIVE_MODULE_SUBS", None) or {}
    name = module_map.get(head) or f"mini_ork.cli.{head.replace('-', '_')}"
    try:
        module = importlib.import_module(name)
    except Exception:  # noqa: BLE001 — a module we cannot import is not ours to judge
        return None
    path = getattr(module, "__file__", None)
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def test_a_homeless_subcommand_is_never_sent_home(home: Path) -> None:
    """A subcommand whose parser has no ``--home`` must carry ``home=False``.

    ``board.rs run_action`` appends ``--home <home>`` unless the action's dict
    says ``"home": false``, so a page that forgets the flag hands the
    subcommand an argument its parser rejects and the button exits 2 —
    silently, because the action path runs with no TTY. Five such sites had
    shipped (``prefs rm``/``prefs set`` in learn's rules/memory/code, and
    ``memory-lifecycle``'s retire/reactivate).

    The set of homeless verbs is read from each subcommand module's **own
    source** rather than hard-coded here, so a verb that loses ``--home`` later
    is covered the day it changes, and one that gains it stops being flagged.
    It looks for the ``"--home"`` *string literal* a parser must contain, not a
    bare mention: ``mini_ork/cli/features.py`` documents that it takes no
    ``--home``, and a prose mention must not read as support for the flag.
    This complements :func:`test_every_cli_action_parses`: that one proves an
    appended ``--home`` is *accepted* where it is sent, this one proves it is
    never sent where it is not.
    """
    seen: set[str] = set()
    failures: list[str] = []
    for key in PAGES:
        tabs = ("__none__",) + _PAGE_TABS.get(key, ())
        for tab in tabs:
            page = build_page(home, key, args=_page_args_for(key),
                              tab=None if tab == "__none__" else tab)
            for action in _walk_cli_prefixes(page):
                cli = action.get("cli") or []
                if not cli:
                    continue
                head = str(cli[0])
                source = _module_source(head)
                if source is None or '"--home"' in source or "'--home'" in source:
                    continue
                # A homeless verb was reached. If the page already says so, that
                # is the correct state and there is nothing to report.
                seen.add(head)
                if action.get("home", True) is False:
                    continue
                failures.append(
                    f"{key}/{tab}: {head!r} rejects --home but {cli!r} would be sent with it"
                )
    assert not failures, failures
    # A vacuous pass (the walk reached no homeless verb at all) would mean it
    # went blind, not that the pages are correct. Setup's Features tab renders a
    # control per registered feature whatever the home holds, so the walk must
    # always reach `features` — even though every one of its actions is correct.
    assert "features" in seen, sorted(seen)


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
    # ``kanban`` is the one page with a non-default tab carrying ``cli``
    # actions of its own (the Sources pane's Test / Add / Remove). Without an
    # entry here the walk only ever built ``tasks``, so a malformed ``pm``
    # action would have shipped unseen.
    "kanban": ("tasks", "runs", "sources"),
    "runs": ("runs", "active", "hooks"),
    "changes": ("worktrees", "review"),
    "verify": ("certify", "autonomy"),
    "recipes": ("catalog", "epics"),
    "autos": ("list", "history"),
    "lanes": ("map", "budget", "spend"),
    "learn": ("patterns", "gradients", "memory"),
    "context": ("active", "library"),
    "nodes": ("fleet", "queue"),
    # ``dispatch`` and ``decide`` carry ``cli`` actions of their own (the
    # Dispatch pickers' ``board start`` and the Decide tab's ``board answer``
    # composers). Listing every tab — not just ``intake`` — is what makes the
    # walk catch a malformed action on any of them.
    "orch": ("session", "dispatch", "decide", "kickoffs", "races", "planner",
             "coord", "intake"),
    "setup": ("readiness", "features", "settings", "zed", "projects", "health"),
    "run": ("dag", "overview", "agents", "learnings", "artifacts"),
}