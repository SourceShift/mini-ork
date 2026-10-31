"""`mini-ork specs` — ingest, list, lint, and ratify markdown specs.

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
* ``ratify <run_id> --list [--json]`` prints one line per unacknowledged
  entry in the run's ``ratification/*.json``. ``ratify <run_id> --spec
  <spec_id> (--all | --entry N) --note TEXT`` acknowledges selected entries
  with an operator audit trail. Acknowledging refuses inside a run: a human
  ratifies, never the agent that produced the diff.

Usage errors and missing paths exit 2.

    main(argv=None, *, root=None) -> int
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mini_ork.specdir.index import INDEX_FILENAME, SpecIndexError, build_index, read_index, write_index
from mini_ork.specdir.lint import Finding, SEVERITY, has_errors, scan_and_lint


def _codes_line() -> str:
    """Derive the Codes line from SEVERITY so the help cannot drift from the checks.

    It was a literal, and it fell behind the moment a code was added. The whole
    class of defect this help documents — two readers of one artifact disagreeing
    because one of them is a hardcoded copy — is what the derivation prevents.
    """
    errors = sorted(code for code, severity in SEVERITY.items() if severity == "error")
    warnings = sorted(code for code, severity in SEVERITY.items() if severity != "error")
    return f"Codes: {', '.join(errors)} (error); {', '.join(warnings)} (warning)."


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
  ratify <run_id> --list [--json] [--run-dir PATH]
                               List unacknowledged ratification entries as
                               spec_id<TAB>index<TAB>reason<TAB>excerpt.
  ratify <run_id> --spec <spec_id> (--all | --entry N [--entry N ...])
                  --note TEXT [--run-dir PATH]
                               Acknowledge selected entries with an audit
                               trail. Refuses inside a run.

__CODES__
Env: MO_SDD_SPEC_GLOB, MO_SDD_MIN_SPECS (default 1), MO_SDD_VAGUE_TERMS
(comma-separated), MO_SDD_SPEC_MAX_BYTES (default 262144).
""".replace("__CODES__", _codes_line())


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
    ratify = sub.add_parser("ratify", add_help=False)
    ratify.add_argument("run_id")
    ratify.add_argument("--list", action="store_true")
    ratify.add_argument("--json", action="store_true")
    ratify.add_argument("--spec")
    ratify.add_argument("--note")
    ratify.add_argument("--run-dir")
    selection = ratify.add_mutually_exclusive_group()
    selection.add_argument("--all", dest="all_entries", action="store_true")
    selection.add_argument("--entry", dest="entries", action="append", type=int)
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


def _ratify_run_dir(args) -> Path:
    """``--run-dir`` when given, else ``$MINI_ORK_HOME/runs/<run_id>``.

    Mirrors ``mini_ork.cli.approvals_cmd``: the default home is ``.mini-ork``
    relative to the operator's cwd, and ``MINI_ORK_HOME`` overrides it.
    """
    if args.run_dir:
        return Path(args.run_dir).expanduser()
    home = os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    return Path(home).expanduser() / "runs" / args.run_id


def _flatten_excerpt(value) -> str:
    """One TSV-safe cell: tabs/newlines become spaces, then cut at 120 chars.

    Exactly the kickoff's rule, and in that order: flatten first so the cut
    is measured against the single-line text the operator actually sees.
    """
    text = "" if value is None else (value if isinstance(value, str) else str(value))
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")[:120]


def _ratify_list(rat_dir: Path, as_json: bool) -> int:
    """Print every entry whose ``acknowledged`` is not ``True``. Read-only."""
    items: list[dict] = []
    for path in sorted(rat_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"specs: {path}: {exc}\n")
            return 2
        if not isinstance(record, dict) or not isinstance(record.get("ratification"), list):
            sys.stderr.write(
                f"specs: {path}: expected an object with a 'ratification' list\n")
            return 2
        spec_id = str(record.get("spec_id") or path.stem)
        for index, entry in enumerate(record["ratification"]):
            if isinstance(entry, dict) and entry.get("acknowledged") is True:
                continue
            reason = entry.get("reason", "") if isinstance(entry, dict) else ""
            excerpt = entry.get("source_excerpt", "") if isinstance(entry, dict) else ""
            items.append({
                "spec_id": spec_id,
                "index": index,
                "reason": "" if reason is None else str(reason),
                "source_excerpt": _flatten_excerpt(excerpt),
            })
    if as_json:
        if items:  # "print nothing when everything is acknowledged" holds here too
            sys.stdout.write(json.dumps(items, indent=2) + "\n")
        return 0
    for item in items:
        sys.stdout.write(
            f"{item['spec_id']}\t{item['index']}\t{item['reason']}\t{item['source_excerpt']}\n")
    return 0


def _write_record_atomic(path: Path, record: dict) -> None:
    """Write ``record`` via a same-dir temp file + ``os.replace``.

    Same directory so the replace is a rename (never a cross-device copy),
    ``indent=2`` and no ``sort_keys`` so key and entry order survive.
    """
    tmp = tempfile.NamedTemporaryFile(
        "w", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
        delete=False, encoding="utf-8",
    )
    try:
        with tmp:
            json.dump(record, tmp, indent=2)
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _ratify(args) -> int:
    run_dir = _ratify_run_dir(args)
    rat_dir = run_dir / "ratification"
    if args.list:
        if not rat_dir.is_dir():
            sys.stderr.write(f"specs: no ratification records in {run_dir}\n")
            return 2
        return _ratify_list(rat_dir, args.json)

    # Acknowledge. Refuse first: the agent that produced the diff must never
    # be the one ratifying it. --list above stays available inside a run.
    from mini_ork.gates import node_review
    from mini_ork.gates.merge_approval import ApprovalError

    try:
        node_review._refuse_if_in_run(os.environ)  # noqa: SLF001 — the shared in-run contract
    except ApprovalError as exc:
        sys.stderr.write(f"specs: {exc}\n")
        return 2

    if not rat_dir.is_dir():
        sys.stderr.write(f"specs: no ratification records in {run_dir}\n")
        return 2

    spec_id = (args.spec or "").strip()
    if not spec_id:
        sys.stderr.write("specs: ratify requires --spec <spec_id> to acknowledge entries\n")
        return 2
    if args.note is None or not args.note.strip():
        sys.stderr.write("specs: ratify requires a non-blank --note\n")
        return 2
    if not args.all_entries and not args.entries:
        sys.stderr.write("specs: ratify requires --all or --entry N\n")
        return 2

    record_path = rat_dir / f"{spec_id}.json"
    if not record_path.is_file():
        sys.stderr.write(f"specs: no ratification record for {spec_id!r} in {rat_dir}\n")
        return 2
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"specs: {record_path}: {exc}\n")
        return 2
    if not isinstance(record, dict):
        sys.stderr.write(f"specs: {record_path}: expected a JSON object\n")
        return 2
    if record.get("spec_id") != spec_id:
        sys.stderr.write(
            f"specs: {record_path}: spec_id {record.get('spec_id')!r} "
            f"does not match {spec_id!r}\n")
        return 2
    entries = record.get("ratification")
    if not isinstance(entries, list):
        sys.stderr.write(f"specs: {record_path}: expected a 'ratification' list\n")
        return 2

    if args.all_entries:
        selected = list(range(len(entries)))
    else:
        selected = list(dict.fromkeys(args.entries))

    # Validate EVERY selected index and entry before any write, so a bad
    # --entry leaves the file byte-for-byte unchanged.
    for index in selected:
        if index < 0 or index >= len(entries):
            sys.stderr.write(
                f"specs: --entry {index} out of range for {spec_id} "
                f"({len(entries)} entries)\n")
            return 2
        if not isinstance(entries[index], dict):
            sys.stderr.write(f"specs: entry {index} in {spec_id} is not an object\n")
            return 2

    try:
        acknowledged_by = getpass.getuser()
    except (OSError, KeyError):
        acknowledged_by = "unknown"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ratified = 0
    for index in selected:
        entry = entries[index]
        if entry.get("acknowledged") is True:
            continue  # already-acknowledged entries are left untouched
        entry["acknowledged"] = True
        entry["acknowledged_by"] = acknowledged_by
        entry["acknowledged_at"] = stamp
        entry["acknowledged_note"] = args.note
        ratified += 1

    if ratified:
        _write_record_atomic(record_path, record)

    remaining = sum(
        1 for entry in entries
        if not (isinstance(entry, dict) and entry.get("acknowledged") is True)
    )
    noun = "entry" if ratified == 1 else "entries"
    sys.stdout.write(
        f"ratified {ratified} {noun} in {spec_id} ({remaining} still unacknowledged)\n")
    return 0


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
    handler = {"ingest": _ingest, "list": _list, "lint": _lint, "ratify": _ratify}[args.sub]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
