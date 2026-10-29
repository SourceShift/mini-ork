"""``mini-ork kickoff`` — draft a kickoff file from a one-line goal.

``kickoff new <text>`` writes ``<home>/kickoffs/<slug>.md`` — the same folder
the Orchestrator's Kickoffs tab reads and ``board start`` launches from, so a
drafted kickoff is immediately visible and startable. The slug comes from
:func:`mini_ork.kickoff_lint.slug`, the canonical deriver, so a kickoff drafted
here and one drafted in a thread land on the same filename.

The file seeds front matter with the resolved default recipe and a ``## Success``
stub, so a fresh draft lints closer to clean than to red. It refuses to
overwrite an existing file and prints that path instead — the caller can then
open the kickoff rather than silently losing it.

Invocation contract (matches ``mini_ork.cli.main``'s ``_native_module_handler``):
the subprocess runs ``python -m mini_ork.cli.kickoff_cmd new <text> --home H``,
so ``--home`` rides the ``new`` subparser — the IDE appends it after the
positional. Exit codes: 0 written, 2 usage / refusal.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

#: The recipe seeded when neither ``--recipe`` nor ``MO_ACP_RECIPE`` names one.
#: Mirrors ``mini_ork.acp.agent.DEFAULT_RECIPE`` and the Orchestrator page's own
#: fallback, so a drafted kickoff and the page agree on what "default" means.
DEFAULT_RECIPE = "code-fix"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mini-ork kickoff",
        description="Draft and inspect kickoff files.",
    )
    sub = p.add_subparsers(dest="verb")
    new = sub.add_parser("new", help="draft a kickoff from a one-line goal")
    new.add_argument("text", metavar="TEXT", help="the goal, one line")
    new.add_argument("--recipe", default=None,
                     help="recipe to seed (default: MO_ACP_RECIPE, else code-fix)")
    # The IDE appends ``--home`` after the positional, so it belongs on the
    # subparser; a top-level flag would be rejected there.
    new.add_argument("--home", default=None, help="the mini-ork home to write under")
    return p


def _default_home() -> Path:
    return Path(os.environ.get("MINI_ORK_HOME", "").strip() or (Path.cwd() / ".mini-ork"))


def _template(title: str, body: str, recipe: str) -> str:
    parts = ["---", f"recipe: {recipe}", "---", f"# {title}", ""]
    if body:
        parts += [body, ""]
    parts += ["## Success", "", "- `<command that exits 0>`", ""]
    return "\n".join(parts)


def _cmd_new(args: argparse.Namespace) -> int:
    lines = [ln.strip() for ln in (args.text or "").splitlines() if ln.strip()]
    if not lines:
        sys.stderr.write("mini-ork kickoff new: the goal is empty\n")
        return 2
    title, body = lines[0], "\n".join(lines[1:])

    home = (Path(args.home) if args.home else _default_home()).expanduser().absolute()

    from mini_ork import kickoff_lint

    slug = kickoff_lint.slug(f"# {title}")
    folder = home / "kickoffs"
    path = folder / f"{slug}.md"
    if path.exists():
        sys.stderr.write(
            f"mini-ork kickoff new: {path} already exists — open it instead\n")
        return 2
    recipe = args.recipe or os.environ.get("MO_ACP_RECIPE") or DEFAULT_RECIPE
    folder.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(_template(title, body, recipe), encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"mini-ork kickoff new: could not write {path}: {exc}\n")
        return 2
    sys.stdout.write(f"{path}\n")
    return 0


def main(rest: list[str] | None = None, root: str | None = None) -> int:
    del root
    argv = list(sys.argv[1:] if rest is None else rest)
    args = build_parser().parse_args(argv)
    if args.verb == "new":
        return _cmd_new(args)
    sys.stderr.write("usage: mini-ork kickoff new <text> [--recipe R] [--home H]\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
