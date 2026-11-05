"""Skills — the registry runs can be granted, browsed and read from the IDE.

Rows come from :func:`mini_ork.dispatch.skill_registry.load_skills`, and the
tier from ``mini_ork.cli.skills._tier_of`` — the same two calls ``mini-ork
skills list`` makes, so the page cannot disagree with the CLI about what a run
can be granted or where it came from.

Reading a skill is ``?name=`` on this page: the body renders as markdown below
the table, so the operator never has to leave the tab to see the instructions a
run would follow. Installing is the one write on this page: a TraceOtter-mined
procedure is promoted into the project registry (``.agents/skills``), which is
the directory Zed reads too. Sharing a bundle has no host here and is not
offered.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TABS = [("registry", "Registry")]


def _env(home: Path) -> dict[str, str]:
    """The registry lookup's environment, pinned to the project this page is for.

    The page renders for one home; trusting the process's own ``MINI_ORK_*``
    would list another project's skills whenever the IDE or a test has them
    set. ``MINI_ORK_RUN_DIR`` is dropped for the same reason: a run-scoped
    registry is not the window's registry.
    """
    env = dict(os.environ)
    env["MINI_ORK_HOME"] = str(home)
    env["MINI_ORK_ROOT"] = str(home.parent)
    env.pop("MINI_ORK_RUN_DIR", None)
    return env


def _skills(home: Path, errors: dict[str, str]) -> list[dict[str, Any]]:
    from mini_ork.cli.skills import _tier_of
    from mini_ork.dispatch.skill_registry import (
        describe_skill,
        invalid_skill_entries,
        load_skills,
        skill_source_dirs,
    )

    env = _env(home)
    skills = load_skills(env)
    rows = []
    for name, directory in sorted(skills.items()):
        meta = describe_skill(directory)
        tier = _tier_of(directory, env)
        rows.append({
            "cells": [
                S.cell(name, "text", b=True),
                S.cell(tier, "green" if tier == "project" else "yellow"),
                S.cell(meta["description"] or "—", "sub"),
                S.mono(str(directory)),
            ],
            # A row opens the skill's body: the read is the page's own arg, so
            # it lands in this tab rather than opening another one.
            "do": S.page_link("skills", name=name),
            "acts": [S.btn("Reveal", S.reveal(str(directory)), "ghost")],
        })
    table = S.table(
        "Registry",
        [S.col(fr=1.2), S.col(90), S.col(fr=2.2), S.col(fr=1.6)],
        ["skill", "tier", "description", "directory"],
        rows,
        full=True,
        note="First registry in precedence order wins a name. "
             "Operator: <home>/config/skills · Project: <root>/.agents/skills.",
    )
    sources = S.lst(
        "Sources",
        [S.dot(str(source), "searched in this order", mc="sub")
         for source in skill_source_dirs(env)],
        full=True,
    )
    invalid = invalid_skill_entries(env)
    if invalid:
        errors["Registry"] = (
            f"{len(invalid)} director{'y' if len(invalid) == 1 else 'ies'} "
            "without a SKILL.md (grants skip them) — run skills validate"
        )
    return [table, sources]


def _read(home: Path, name: str) -> list[dict[str, Any]]:
    from mini_ork.dispatch.skill_registry import SKILL_FILE, load_skills

    directory = load_skills(_env(home)).get(name)
    if directory is None:
        return [S.lst("Read", [S.bad(
            f"No skill named {name!r}", "the registry changed since the page was read — reload")],
            full=True)]
    path = directory / SKILL_FILE
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [S.lst("Read", [S.bad(f"Cannot read {path}", str(exc))], full=True)]
    return [S.markdown(f"{name} · {SKILL_FILE}", body, path=str(path), full=True)]


def _mined(home: Path) -> list[dict[str, Any]]:
    """TraceOtter-mined procedures that are not in the registry yet.

    ``skills promote`` writes one of these into the project registry. The
    confidence and status are shown as mined, never interpreted: promotion is
    the operator's call, and the page only offers the same command the CLI has.
    """
    path = home / "traceotter" / "skills.json"
    try:
        mined = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(mined, list):
        return []
    rows = []
    for skill in mined:
        if not isinstance(skill, dict):
            continue
        skill_id = str(skill.get("skillId") or "").strip()
        if not skill_id:
            continue
        confidence = skill.get("confidence")
        rows.append({
            "cells": [
                S.cell(skill.get("title") or skill_id, "text", b=True),
                S.mono(skill_id),
                S.cell("—" if confidence is None else f"{float(confidence):.2f}", "sub", mono=True),
                S.cell(str(skill.get("status") or "—"), "sub"),
            ],
            "acts": [S.btn(
                "Promote",
                S.cli("skills", "promote", skill_id, "--to", "root", home=False,
                      confirm=f"Promote {skill_id} into the project registry?"),
                "ghost",
            )],
        })
    if not rows:
        return []
    return [S.table(
        "Mined, not installed",
        [S.col(fr=1.6), S.col(fr=1.4), S.col(90), S.col(80)],
        ["procedure", "id", "confidence", "status"],
        rows,
        full=True,
        note="Mined by TraceOtter from real runs. Promote writes the procedure into "
             "<root>/.agents/skills, where grants and Zed both read it.",
    )]


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else TABS[0][0]
    errors: dict[str, str] = {}
    sections = S.guarded(errors, "Registry", lambda: _skills(home, errors))
    name = str(args.get("name") or "").strip()
    if name:
        sections += S.guarded(errors, "Read", lambda: _read(home, name))
    sections += S.guarded(errors, "Mined procedures", lambda: _mined(home))
    actions = [
        S.btn("Validate", S.cli("skills", "validate", home=False), "ghost"),
        S.btn("Open operator skills", S.reveal(str(home / "config" / "skills")), "ghost"),
    ]
    return S.page(
        "skills",
        "Skills",
        "What runs can be granted: browse the registry, read a skill's own "
        "instructions, and install a mined procedure.",
        actions=actions,
        tabs=TABS,
        tab=tab,
        args=args,
        sections=sections,
        errors=errors,
    )
