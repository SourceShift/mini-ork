"""``mini-ork skills`` — operate the skill registry from the terminal.

The registry itself lives in ``dispatch/skill_registry``; this module is the
operator surface over it: see what a run can grant, inspect a skill's body,
catch registry entries a grant would silently skip, and promote a procedure
TraceOtter mined out of real trajectories into a grantable skill — the last
mile of the distillation loop (episodes → learned procedure → granted to
future runs).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from ..dispatch.skill_registry import (
    SKILL_FILE,
    describe_skill,
    invalid_skill_entries,
    load_skills,
    skill_source_dirs,
)


def _tier_of(directory: Path, env: dict[str, str] | None = None) -> str:
    """The registry tier a skill directory belongs to (run / operator / project).

    ``directory`` is a skill's directory (a CHILD of a registry source), so
    the match is by containment. The tier follows WHICH source contains it,
    never a positional index: ``skill_source_dirs`` drops sources whose env
    var is unset, so index 0 is only "run" when the run dir exists.
    """
    e = os.environ if env is None else env
    sources = {
        str(e.get("MINI_ORK_RUN_DIR") or ""): "run",
        str(e.get("MINI_ORK_HOME") or ""): "operator",
        str(e.get("MINI_ORK_ROOT") or ""): "project",
    }
    try:
        resolved = directory.resolve()
    except OSError:
        resolved = directory
    for base, tier in sources.items():
        if not base:
            continue
        try:
            if resolved.relative_to(Path(base).resolve()) is not None:
                return tier
        except (OSError, ValueError):
            continue
    return "project"


def _cmd_list(args: argparse.Namespace) -> int:
    env = os.environ
    skills = load_skills(env)
    if args.json:
        payload = [
            {"name": name, "source": _tier_of(directory), "path": str(directory),
             **describe_skill(directory)}
            for name, directory in sorted(skills.items())
        ]
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0
    if not skills:
        sys.stdout.write(
            "no skills in the registry — searched: "
            + ", ".join(str(d) for d in skill_source_dirs(env)) + "\n"
        )
        return 0
    for name, directory in sorted(skills.items()):
        meta = describe_skill(directory)
        desc = meta["description"]
        if len(desc) > 90:
            desc = desc[:87] + "..."
        sys.stdout.write(f"{name:<32} [{_tier_of(directory)}]  {desc}\n")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    skills = load_skills(os.environ)
    directory = skills.get(args.name)
    if directory is None:
        sys.stderr.write(f"no skill named {args.name!r} in the registry\n")
        return 1
    body = (directory / SKILL_FILE).read_text(encoding="utf-8", errors="replace")
    sys.stdout.write(body)
    if not body.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_validate(_: argparse.Namespace) -> int:
    env = os.environ
    problems = 0
    for entry in invalid_skill_entries(env):
        sys.stderr.write(f"not a skill (no {SKILL_FILE}): {entry}\n")
        problems += 1
    skills = load_skills(env)
    for name, directory in sorted(skills.items()):
        if not describe_skill(directory)["description"].strip():
            sys.stderr.write(
                f"{name}: no description in frontmatter — the model cannot "
                "decide when to use it\n"
            )
            problems += 1
    if problems:
        sys.stderr.write(f"{problems} problem(s); grants would skip or misuse these\n")
        return 1
    sys.stdout.write(f"registry ok — {len(skills)} skill(s)\n")
    return 0


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:64]


def _cmd_promote(args: argparse.Namespace) -> int:
    """TraceOtter-mined procedure → registry skill directory.

    The distillation loop is only closed when a mined skill becomes grantable:
    ``skills.json`` holds procedures with a confidence, and a grant needs a
    directory with a ``SKILL.md``. This writes one — frontmatter from the
    trigger, body from the procedure + verification steps, provenance kept in
    the body so a future reader can trace the skill to its episodes.
    """
    home = Path(os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork"))
    source = home / "traceotter" / "skills.json"
    if not source.is_file():
        sys.stderr.write(f"no TraceOtter skills to promote: {source} is missing\n")
        return 1
    try:
        mined = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"cannot read {source}: {exc}\n")
        return 1
    if not isinstance(mined, list):
        sys.stderr.write(f"{source} does not hold a skill list\n")
        return 1

    match = next(
        (
            s for s in mined
            if isinstance(s, dict)
            and args.id in (str(s.get("skillId") or ""), _slugify(str(s.get("title") or "")))
        ),
        None,
    )
    if match is None:
        sys.stderr.write(
            f"no mined skill matching {args.id!r} — "
            f"known: {', '.join(str(s.get('skillId')) for s in mined if isinstance(s, dict))}\n"
        )
        return 1

    name = _slugify(str(match.get("title") or match.get("skillId") or ""))
    if not name:
        sys.stderr.write("mined skill has neither title nor skillId; cannot name it\n")
        return 1
    if args.to == "root":
        base = Path(os.environ.get("MINI_ORK_ROOT") or os.getcwd())
        target = base / ".agents" / "skills" / name
    else:
        target = home / "config" / "skills" / name
    if target.exists() and not args.force:
        sys.stderr.write(f"{target} already exists; pass --force to overwrite\n")
        return 1

    procedure = match.get("procedure") or []
    verification = match.get("verification") or []
    episodes = match.get("sourceEpisodeIds") or []
    lines = [
        "---",
        f"name: {name}",
        f"description: {str(match.get('trigger') or match.get('title') or name)}",
        "---",
        "",
        f"# {match.get('title') or name}",
        "",
        "## Procedure",
        "",
    ]
    lines += [f"{i}. {step}" for i, step in enumerate(procedure, 1)] or ["(no steps mined)"]
    if verification:
        lines += ["", "## Verification", ""] + [f"- {v}" for v in verification]
    lines += [
        "",
        "## Provenance",
        "",
        f"- mined by TraceOtter, confidence {match.get('confidence', '?')}, "
        f"status {match.get('status', '?')}",
        f"- source episodes: {', '.join(str(e) for e in episodes) or '(none recorded)'}",
        "",
    ]
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / SKILL_FILE).write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"cannot write {target / SKILL_FILE}: {exc}\n")
        return 1
    sys.stdout.write(f"promoted {match.get('skillId')} -> {target / SKILL_FILE}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The argv contract, shared by ``main`` and any in-process caller."""
    parser = argparse.ArgumentParser(
        prog="mini-ork skills",
        description="Operate the skill registry: list, inspect, validate, promote.",
    )
    sub = parser.add_subparsers(dest="sub", required=True)

    p_list = sub.add_parser("list", help="every grantable skill, with its registry tier")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.set_defaults(fn=_cmd_list)

    p_show = sub.add_parser("show", help="print a skill's SKILL.md body")
    p_show.add_argument("name")
    p_show.set_defaults(fn=_cmd_show)

    p_validate = sub.add_parser("validate", help="fail on registry entries a grant would skip")
    p_validate.set_defaults(fn=_cmd_validate)

    p_promote = sub.add_parser(
        "promote", help="promote a TraceOtter-mined procedure into a grantable skill"
    )
    p_promote.add_argument("id", help="skillId (or slug of the title) from skills.json")
    p_promote.add_argument("--to", choices=("home", "root"), default="home",
                           help="operator registry (default) or the repo's .agents/skills")
    p_promote.add_argument("--force", action="store_true", help="overwrite an existing skill")
    p_promote.set_defaults(fn=_cmd_promote)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
