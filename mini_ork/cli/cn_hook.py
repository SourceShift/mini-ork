"""Python bridge for the externally configured ContextNest shell hooks.

The hook files remain tiny Bash adapters because Claude Code invokes commands,
but all MiniOrk/CN behavior is implemented here without sourcing lib/*.sh.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from mini_ork import cn_client


def _prefetch(session: str, prompt: str, cwd: str, output: str) -> int:
    if not prompt or not cn_client.available():
        return 0
    atoms = cn_client.render_atoms_md(cn_client.retrieve(prompt[:1500], 8), 6)
    inbox = cn_client.render_inbox_md(cn_client.inbox(5), 5)
    features = cn_client.render_features_md(cn_client.features_recent("48h"), cwd, 8)
    if not (atoms or inbox or features):
        return 0
    body = [
        f"# ContextNest prefetch for session {session}",
        f"_Generated at {datetime.now(UTC).isoformat()} by mini_ork.cli.cn_hook._",
        "",
    ]
    body.extend(part.rstrip() + "\n" for part in (atoms, inbox, features) if part)
    try:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(body), encoding="utf-8")
    except OSError:
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mini_ork.cli.cn_hook")
    sub = parser.add_subparsers(dest="command", required=True)

    prefetch = sub.add_parser("prefetch")
    prefetch.add_argument("--session", required=True)
    prefetch.add_argument("--prompt", required=True)
    prefetch.add_argument("--cwd", default="")
    prefetch.add_argument("--output", required=True)

    post = sub.add_parser("post")
    post.add_argument("event")
    post.add_argument("session")
    post.add_argument("--cwd", default="")
    post.add_argument("--transcript", default="")

    outcome = sub.add_parser("outcome")
    outcome.add_argument("outcome")
    outcome.add_argument("atom_ids_csv")
    outcome.add_argument("--evidence", default="")
    outcome.add_argument("--session", default="")

    args = parser.parse_args(argv)
    if args.command == "prefetch":
        return _prefetch(args.session, args.prompt, args.cwd, args.output)
    if args.command == "post":
        return cn_client.hook_post(args.event, args.session, args.cwd, args.transcript)
    return cn_client.outcome_post(
        args.outcome, args.atom_ids_csv, args.evidence, args.session
    )


if __name__ == "__main__":
    raise SystemExit(main())
