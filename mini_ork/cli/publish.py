"""``mini-ork publish`` — share a finished run on console.miniork.dev.

    mini-ork publish run-1795604701-80144
    mini-ork publish <run_id> --dry-run --out /tmp/run-bundle.json
    mini-ork publish <run_dir> --api-url http://127.0.0.1:7823/api/v1

The command builds a ``mini-ork.public-run/v1`` bundle from the run directory,
scrubs it (secrets, credentials, emails, home paths), re-scans the result, and
only then uploads it. A run that still matches a secret rule after scrubbing is
*not* uploaded — the command exits 4 and names the rule, never the value.

Exit codes:
    0  published (or a clean ``--dry-run``)
    2  usage error / run not found
    3  the bundle could not be built
    4  residual secret after redaction — upload refused
    5  the control plane rejected the upload (digest, size, guard, auth)

The API key is read from ``--api-key-env`` (default ``ATTESTIA_API_KEY``) or
stdin, never from an argument, so it does not land in shell history or ``ps``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mini_ork.publish.bundle import collect_bundle, discover_run_dir
from mini_ork.publish.client import DEFAULT_BASE, PublishError, publish, resolve_publish_url
from mini_ork.publish.redact import scan_for_secrets_obj

__all__ = ["main", "HELP_TEXT"]

HELP_TEXT = (
    "Usage: mini-ork publish <run_id|run_dir> [options]\n"
    "\n"
    "Publish a finished run as a public, redacted share page.\n"
    "\n"
    "  --home DIR             run home (default $MINI_ORK_HOME or .mini-ork)\n"
    "  --run-dir DIR          explicit run directory (same as a path argument)\n"
    "  --title TEXT           override the page title\n"
    "  --api-url URL          console base (default $MINI_ORK_PUBLISH_URL or\n"
    f"                         {DEFAULT_BASE})\n"
    "  --api-key-env VAR      env var holding the API key (default ATTESTIA_API_KEY)\n"
    "  --api-key-stdin        read the API key from stdin instead of the env\n"
    "  --slug SLUG            request a specific slug (server may refuse)\n"
    "  --no-transcripts       publish summaries only, not agent conversations\n"
    "  --max-agent-bytes N    per-agent transcript cap (default 2000000)\n"
    "  --dry-run              build + scrub, do not upload\n"
    "  --out PATH             write the (scrubbed) bundle JSON to PATH\n"
    "  --json                 machine-readable result on stdout\n"
    "  --help                 this message\n"
)


class _UsageError(Exception):
    pass


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("run", nargs="?")
    parser.add_argument("--home")
    parser.add_argument("--run-dir")
    parser.add_argument("--title")
    parser.add_argument("--api-url", default=os.environ.get("MINI_ORK_PUBLISH_URL", DEFAULT_BASE))
    parser.add_argument("--api-key-env", default="ATTESTIA_API_KEY")
    parser.add_argument("--api-key-stdin", action="store_true")
    parser.add_argument("--slug")
    parser.add_argument("--no-transcripts", action="store_true")
    parser.add_argument("--max-agent-bytes", type=int, default=2_000_000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--json", action="store_true")
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        raise _UsageError(f"unknown flag(s): {' '.join(unknown)}")
    if not (args.run or args.run_dir):
        raise _UsageError("a run id or --run-dir is required")
    return args


def _api_key(args: argparse.Namespace) -> str:
    if args.api_key_stdin:
        return sys.stdin.readline().strip()
    return os.environ.get(args.api_key_env, "").strip()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(HELP_TEXT)
        return 0
    try:
        args = _parse(argv)
    except _UsageError as err:
        sys.stderr.write(f"publish: {err}\n\n{HELP_TEXT}")
        return 2

    try:
        run_dir = discover_run_dir(args.run_dir or args.run, args.home)
    except FileNotFoundError as err:
        sys.stderr.write(f"publish: {err}\n")
        return 2

    try:
        bundle, transcripts, _report = collect_bundle(
            run_dir,
            include_transcripts=not args.no_transcripts,
            max_agent_bytes=max(args.max_agent_bytes, 0),
            title=args.title,
        )
    except Exception as err:  # a half-built bundle must never upload
        sys.stderr.write(f"publish: could not build bundle: {err}\n")
        return 3

    # The producer's own guarantee: after scrubbing, nothing may still match a
    # secret rule. Names only — the matched value is never printed.
    residual = scan_for_secrets_obj({"bundle": bundle, "transcripts": transcripts})
    if residual:
        sys.stderr.write(
            "publish: refused — residual secrets after redaction in: "
            + ", ".join(sorted(set(residual)))
            + "\n"
        )
        return 4

    if args.out:
        Path(args.out).expanduser().write_text(
            json.dumps({"bundle": bundle, "transcripts": transcripts},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    run = bundle["run"]
    summary = {
        "run_id": run["id"],
        "title": run["title"],
        "verdict": run["verdict"],
        "nodes": len(bundle["dag"]["nodes"]),
        "agents": len(bundle["agents"]),
        "transcript_bytes": sum(len(json.dumps(m)) for m in transcripts.values()),
        "redaction": bundle["redaction"],
    }

    if args.dry_run:
        result = {"dry_run": True, "bundle_path": args.out, **summary}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"dry run: {run['title']}")
            print(f"  verdict      {run['verdict']}")
            print(f"  nodes        {summary['nodes']}")
            print(f"  agents       {summary['agents']}")
            print(f"  redaction    {json.dumps(bundle['redaction']['counts'])}")
            if args.out:
                print(f"  bundle       {args.out}")
        return 0

    key = _api_key(args)
    try:
        response = publish(
            bundle, transcripts,
            api_url=args.api_url, api_key=key, slug=args.slug,
        )
    except PublishError as err:
        sys.stderr.write(f"publish: {err}\n")
        return 5

    url = response.get("url") or f"{args.api_url.rstrip('/')}/r/{response['slug']}"
    result = {
        **summary,
        "slug": response.get("slug"),
        "url": url,
        "sha256": response.get("sha256"),
        "endpoint": resolve_publish_url(args.api_url),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"published: {url}")
        print(f"  verdict      {run['verdict']}")
        print(f"  nodes        {summary['nodes']}")
        print(f"  agents       {summary['agents']}")
        print(f"  redaction    {json.dumps(bundle['redaction']['counts'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
