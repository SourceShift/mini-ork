"""`mini-ork specs` — ingest, list, and lint a directory of markdown specs.

Thin CLI over :mod:`mini_ork.specdir` (deterministic, no model calls), the
first step of spec-driven-delivery (docs/plans/2026-10-03-spec-driven-delivery.md):

* ``ingest <dir>`` scans + lints the directory and writes a validated
  ``spec-index.json`` (default ``<dir>/spec-index.json``). The absolute index
  path goes to stdout; findings go to stderr. Exit 1 on any error-severity
  finding (the index is still written unless DUP_ID/DEP_CYCLE make it
  unbuildable) or when fewer than ``MO_SDD_MIN_SPECS`` (default 1) specs are
  found.
* ``list <spec-index.json>`` prints ``spec_id<TAB>status<TAB>title<TAB>source_path``.
* ``lint <dir>`` prints findings (``--json``: a JSON array of
  ``{spec_id, code, severity, message}`` and nothing else on stdout). Exit 1 on
  any error-severity finding.

Usage errors and missing paths exit 2.

    main(argv=None, *, root=None) -> int
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mini_ork.specdir.index import INDEX_FILENAME, SpecIndexError, build_index, read_index, write_index
from mini_ork.specdir.lint import Finding, has_errors, scan_and_lint

_USAGE = """Usage: mini-ork specs <subcommand> [args]

  ingest <dir> [--out PATH] [--recursive]
                               Scan <dir> for spec files (MO_SDD_SPEC_GLOB,
                               default *.md), lint them, and write
                               spec-index.json (default <dir>/spec-index.json).
                               Prints the index path; exits 1 on any
                               error-severity lint finding.
  list <spec-index.json>       One line per spec: id, status, title, path.
  lint <dir> [--recursive] [--json]
                               Lint every spec; --json prints a JSON array of
                               {spec_id, code, severity, message}. Exits 1 on
                               any error-severity finding.

Codes: NO_ACCEPTANCE, DUP_ID, DEP_CYCLE (error); NO_VERIFY_CMD,
VAGUE_CRITERIA, OVERSIZE, MISSING_SECTIONS (warning).
Env: MO_SDD_SPEC_GLOB, MO_SDD_MIN_SPECS (default 1), MO_SDD_VAGUE_TERMS
(comma-separated), MO_SDD_SPEC_MAX_BYTES (default 262144).
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"specs: {message}\n{_USAGE}")
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="mini-ork specs", add_help=False, usage=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="sub", required=True, parser_class=_Parser)
    ingest = sub.add_parser("ingest", add_help=False)
    ingest.add_argument("dir")
    ingest.add_argument("--out")
    ingest.add_argument("--recursive", action="store_true")
    lst = sub.add_parser("list", add_help=False)
    lst.add_argument("index")
    lint = sub.add_parser("lint", add_help=False)
    lint.add_argument("dir")
    lint.add_argument("--recursive", action="store_true")
    lint.add_argument("--json", action="store_true")
    return p


def _min_specs() -> int:
    try:
        return max(0, int(os.environ.get("MO_SDD_MIN_SPECS", "").strip()))
    except ValueError:
        return 1


def _write_findings(findings: list[Finding], stream) -> None:
    for f in findings:
        stream.write(f"{f.severity:<7}  {f.code:<16}  {f.spec_id}  {f.message}\n")
    errors = sum(1 for f in findings if f.severity == "error")
    stream.write(f"{len(findings)} finding(s): {errors} error(s), {len(findings) - errors} warning(s)\n")


def _ingest(args) -> int:
    spec_dir = Path(args.dir).expanduser().resolve()
    if not spec_dir.is_dir():
        sys.stderr.write(f"specs ingest: not a directory: {spec_dir}\n")
        return 2
    try:
        entries, findings = scan_and_lint(spec_dir, recursive=args.recursive)
    except OSError as exc:
        sys.stderr.write(f"specs ingest: {exc}\n")
        return 2
    need = _min_specs()
    if len(entries) < need:
        sys.stderr.write(f"specs ingest: found {len(entries)} spec(s) in {spec_dir}, "
                         f"need at least {need} (MO_SDD_MIN_SPECS)\n")
        return 1
    out = Path(args.out).expanduser().resolve() if args.out else spec_dir / INDEX_FILENAME
    try:
        written = write_index(build_index(entries, spec_dir), out)
    except SpecIndexError as exc:
        _write_findings(findings, sys.stderr)
        sys.stderr.write(f"specs ingest: index not written: {exc}\n")
        return 1
    sys.stdout.write(f"{written}\n")
    sys.stderr.write(f"specs ingest: {len(entries)} spec(s) indexed -> {written}\n")
    _write_findings(findings, sys.stderr)
    return 1 if has_errors(findings) else 0


def _list(args) -> int:
    path = Path(args.index).expanduser()
    if not path.is_file():
        sys.stderr.write(f"specs list: no such file: {path}\n")
        return 2
    try:
        index = read_index(path)
    except SpecIndexError as exc:
        sys.stderr.write(f"specs list: {exc}\n")
        return 1
    for spec_id, entry in sorted(index["specs"].items()):
        title = " ".join(entry["title"].split())  # keep the TSV columns intact
        sys.stdout.write(f"{spec_id}\t{entry['status']}\t{title}\t{entry['source_path']}\n")
    return 0


def _lint(args) -> int:
    spec_dir = Path(args.dir).expanduser().resolve()
    if not spec_dir.is_dir():
        sys.stderr.write(f"specs lint: not a directory: {spec_dir}\n")
        return 2
    try:
        _, findings = scan_and_lint(spec_dir, recursive=args.recursive)
    except OSError as exc:
        sys.stderr.write(f"specs lint: {exc}\n")
        return 2
    if args.json:
        sys.stdout.write(json.dumps([f.to_dict() for f in findings], indent=2) + "\n")
    else:
        _write_findings(findings, sys.stdout)
    return 1 if has_errors(findings) else 0


def main(argv=None, *, root=None) -> int:
    del root  # engine root is irrelevant: specs paths are user-supplied
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "help" or "--help" in argv or "-h" in argv:
        sys.stdout.write(_USAGE)
        return 0 if argv else 2
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    handler = {"ingest": _ingest, "list": _list, "lint": _lint}[args.sub]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
