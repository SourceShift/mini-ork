"""``mini-ork features`` — the controllable-feature catalogue, and its skill sync.

Reads :mod:`mini_ork.features.registry` (the single source of truth) and prints
it. Two write-side verbs keep a skill in step with the registry:

* ``features render-skill`` — rewrite the generated block inside
  ``skills/wizard/SKILL.md`` from the registry.
* ``features check-skill`` — exit non-zero when the committed block is stale.

``check-skill`` is what makes "a new feature is added to the skill
automatically" a *guarantee* rather than a habit: register a feature, forget to
re-render, and the gate goes red instead of the skill silently omitting it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mini_ork.features import catalogue_json, catalogue_markdown

#: Sentinels delimiting the generated block inside a skill file. Everything
#: between them is owned by this module; everything outside is hand-written.
BEGIN = "<!-- BEGIN GENERATED:features -->"
END = "<!-- END GENERATED:features -->"

#: The skill this command defaults to syncing.
DEFAULT_SKILL = "skills/wizard/SKILL.md"

_USAGE = """\
mini-ork features — user-controllable run features and their cost.

Usage:
  mini-ork features [--json] [--recipe <r>]      print the catalogue
  mini-ork features render-skill [--path <f>]    rewrite the skill's generated block
  mini-ork features check-skill  [--path <f>]    exit 1 if that block is stale

The catalogue is read from mini_ork.features.registry — the single source of
truth shared by this command, the wizard skill, and the /wizard thread command.
"""


def _block(recipe: str | None) -> str:
    body = catalogue_markdown(recipe).rstrip()
    return f"{BEGIN}\n{body}\n{END}"


def _replace_block(text: str, block: str) -> str:
    """Swap the generated block in ``text``; error (ValueError) if absent."""
    start = text.find(BEGIN)
    stop = text.find(END)
    if start == -1 or stop == -1 or stop < start:
        raise ValueError(f"no {BEGIN} … {END} block in the skill file")
    return text[:start] + block + text[stop + len(END):]


def _skill_path(args: argparse.Namespace, root: str | None) -> Path:
    path = Path(args.path)
    if not path.is_absolute():
        base = Path(root) if root else Path.cwd()
        path = base / path
    return path


def _render_stale(path: Path, block: str) -> bool:
    """True when ``path``'s committed block differs from ``block``."""
    try:
        return _replace_block(path.read_text(encoding="utf-8"), block) != path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return True


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mini-ork features", add_help=False, usage=argparse.SUPPRESS)
    p.add_argument("sub", nargs="?", default="list",
                   choices=["list", "render-skill", "check-skill"])
    p.add_argument("--json", action="store_true")
    p.add_argument("--recipe", default=None)
    p.add_argument("--path", default=DEFAULT_SKILL)
    p.add_argument("-h", "--help", action="store_true")
    return p


def main(argv=None, *, root=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help",):
        sys.stdout.write(_USAGE)
        return 0 if argv else 2
    args = _parser().parse_args(argv)
    if args.help:
        sys.stdout.write(_USAGE)
        return 0

    if args.sub == "list":
        if args.json:
            sys.stdout.write(json.dumps(catalogue_json(args.recipe), indent=2) + "\n")
        else:
            sys.stdout.write(catalogue_markdown(args.recipe))
        return 0

    block = _block(args.recipe)
    path = _skill_path(args, root)

    if args.sub == "render-skill":
        try:
            path.write_text(_replace_block(path.read_text(encoding="utf-8"), block), encoding="utf-8")
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"render-skill failed: {exc}\n")
            return 1
        sys.stdout.write(f"rendered feature block into {path}\n")
        return 0

    # check-skill
    if _render_stale(path, block):
        sys.stderr.write(
            f"feature block stale in {path} — run: mini-ork features render-skill\n"
        )
        return 1
    sys.stdout.write(f"feature block up to date in {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
