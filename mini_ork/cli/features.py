"""``mini-ork features`` — the controllable-feature catalogue, and its skill sync.

Reads :mod:`mini_ork.features.registry` (the single source of truth) and prints
it. Two write-side verbs keep a skill in step with the registry:

* ``features render-skill`` — rewrite the generated block inside
  ``skills/wizard/SKILL.md`` from the registry.
* ``features check-skill`` — exit non-zero when the committed block is stale.

``check-skill`` is what makes "a new feature is added to the skill
automatically" a *guarantee* rather than a habit: register a feature, forget to
re-render, and the gate goes red instead of the skill silently omitting it.

Two further verbs persist a choice for every future run in the project, into
``.mini-ork/config/features.yaml`` (see :mod:`mini_ork.features.store`):
``set <id> <value>`` and ``unset <id>``. They read the project from
``$MINI_ORK_HOME`` and take no ``--home`` — the IDE action path sets the
environment, not the argv (see ``board.rs`` ``run_action``).
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
  mini-ork features set <id> <value>             persist a default value for this project
                     [--accept-premium]          required to turn a premium feature on
  mini-ork features unset <id>                   drop a persisted default
  mini-ork features set-env <NAME> <value>       persist a raw setting (e.g. a cap)
  mini-ork features unset-env <NAME>             drop a raw setting

The catalogue is read from mini_ork.features.registry — the single source of
truth shared by this command, the wizard skill, and the /wizard thread command.
``set``/``unset`` write .mini-ork/config/features.yaml, loaded at startup below
the real environment. A value may be ``on``/``off`` a count, or a lane list.
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
                   choices=["list", "render-skill", "check-skill", "set", "unset",
                            "set-env", "unset-env"])
    p.add_argument("rest", nargs="*")
    p.add_argument("--json", action="store_true")
    p.add_argument("--recipe", default=None)
    p.add_argument("--path", default=DEFAULT_SKILL)
    p.add_argument("--accept-premium", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    return p


def build_parser() -> argparse.ArgumentParser:
    """The parser, under the name every other subcommand module exposes.

    Callers that enumerate subcommands (the IDE action walker looks up
    ``build_parser`` / ``_build_parser`` to check an emitted argv parses) find
    this one; the runtime entry point stays :func:`main`.
    """
    return _parser()


def _set_unset(args: argparse.Namespace) -> int:
    """Persist (or drop) one feature's project default. Returns an exit code."""
    from mini_ork.features import store

    if args.sub in ("set-env", "unset-env"):
        if len(args.rest) < 1:
            sys.stderr.write(f"usage: mini-ork features {args.sub} <NAME> [<value>]\n")
            return 2
        name = args.rest[0]
        try:
            if args.sub == "unset-env":
                if not store.unset_env(name):
                    sys.stderr.write(f"{name} is not set\n")
                    return 1
            else:
                if len(args.rest) < 2:
                    sys.stderr.write(f"usage: mini-ork features set-env {name} <value>\n")
                    return 2
                store.set_env(name, " ".join(args.rest[1:]))
        except store.StoreError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
        sys.stdout.write(f"{args.sub} {name} → {store.store_path()}\n")
        return 0

    if args.sub == "unset":
        if len(args.rest) != 1:
            sys.stderr.write("usage: mini-ork features unset <id>\n")
            return 2
        if not store.unset_feature(args.rest[0]):
            sys.stderr.write(f"{args.rest[0]} is not set\n")
            return 1
        sys.stdout.write(f"unset {args.rest[0]} → {store.store_path()}\n")
        return 0

    if len(args.rest) < 2:
        sys.stderr.write("usage: mini-ork features set <id> <value> [--accept-premium]\n")
        return 2
    feature_id, value = args.rest[0], " ".join(args.rest[1:])
    try:
        store.set_feature(feature_id, value, accept_premium=args.accept_premium)
    except store.StoreError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write(f"{feature_id} = {value} → {store.store_path()}\n")
    return 0


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

    if args.sub in ("set", "unset", "set-env", "unset-env"):
        return _set_unset(args)

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
