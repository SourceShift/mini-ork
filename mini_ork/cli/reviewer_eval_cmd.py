"""``mini-ork reviewer-eval`` — score the framework-edit reviewer.

Five verbs:

    list        one line per eval case
    add-case    add a case from a run's diff or a landed commit
    run         dispatch the reviewer over the cases and append a results file
    status      compare the current reviewer fingerprint with the last result
    score FILE  print the summary row of a results file

The harness never calls a real lane unless ``run`` is asked to (and
``--dry-run`` dispatches nothing at all). Results are keyed by
:func:`mini_ork.review.reviewer_eval.fingerprint`, so a score is only compared
with a score from the same prompt + envelope + lane + model.

Kickoff: rev6-r4 ``reviewer-eval``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from mini_ork.review import reviewer_eval as rev

_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class _Refused(Exception):
    """A semantic refusal (rc 1), distinct from an argparse usage error (rc 2)."""


# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────


def _root() -> Path:
    return Path(os.environ.get("MINI_ORK_ROOT") or Path(__file__).resolve().parents[2])


def _home(root: Path) -> Path:
    return Path(os.environ.get("MINI_ORK_HOME") or (root / ".mini-ork"))


def _cases_dir(root: Path) -> Path:
    return root / "evals" / "reviewer" / "cases"


def _results_dir(root: Path) -> Path:
    return root / "evals" / "reviewer" / "results"


# ─────────────────────────────────────────────────────────────────────────────
# argparse
# ─────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-ork reviewer-eval",
        description="Score the framework-edit reviewer on incidents + clean changes.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<subcommand>")

    sub.add_parser("list", help="print one line per eval case")

    add = sub.add_parser("add-case", help="add an incident or clean case")
    add.add_argument("--id", required=True, help="case id ([a-z0-9][a-z0-9-]*)")
    add.add_argument("--kind", required=True, choices=("incident", "clean"))
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", help="run id whose framework-edit.diff is the case")
    source.add_argument("--commit", help="commit sha to use as the case diff")
    add.add_argument("--kickoff", help="kickoff.md path (defaults per source)")
    add.add_argument("--defect-file", action="append", default=[], metavar="F",
                     help="repo-relative path a finding must name (repeatable)")
    add.add_argument("--keyword", action="append", default=[], metavar="K",
                     help="substring a finding issue must contain (repeatable)")
    add.add_argument("--summary", default="", help="one-sentence defect summary")
    add.add_argument("--notes", default="", help="free-form notes")

    run = sub.add_parser("run", help="run the reviewer over cases")
    run.add_argument("--lane", help="override the recipe reviewer lane")
    run.add_argument("--case", action="append", dest="cases", default=[], metavar="ID",
                     help="run only this case (repeatable)")
    run.add_argument("--timeout", type=int, default=900, help="per-case timeout seconds")
    run.add_argument("--work-dir", help="parent for per-case working dirs")
    run.add_argument("--dry-run", action="store_true",
                     help="print the fingerprint and prompt sizes; dispatch nothing")

    status = sub.add_parser("status", help="compare current + last-eval fingerprints")
    status.add_argument("--lane", help="override the recipe reviewer lane")

    score = sub.add_parser("score", help="print a results file's summary row")
    score.add_argument("file", help="results .jsonl file")

    return parser


# ─────────────────────────────────────────────────────────────────────────────
# list
# ─────────────────────────────────────────────────────────────────────────────


def _cmd_list(root: Path) -> int:
    cases = rev.load_cases(_cases_dir(root))
    if not cases:
        sys.stdout.write("no reviewer eval cases\n")
        return 0
    for case in cases:
        summary = ""
        if isinstance(case.defect, dict):
            summary = str(case.defect.get("summary") or "")
        sys.stdout.write(f"{case.id}  {case.kind}  {case.expect}  {summary}\n")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# add-case
# ─────────────────────────────────────────────────────────────────────────────


def _kickoff_from_profile(run_dir: Path) -> str:
    profile = run_dir / "run_profile.json"
    if not profile.is_file():
        return ""
    try:
        payload = json.loads(profile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    value = payload.get("kickoff_path") if isinstance(payload, dict) else ""
    return str(value or "")


def _kickoff_from_db(home: Path, run_id: str) -> str:
    db = home / "state.db"
    if not db.is_file():
        return ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:
            row = con.execute(
                "SELECT kickoff_path FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    return str(row[0] or "") if row else ""


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise _Refused(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def _source_from_run(args, home: Path) -> tuple[str, str, dict]:
    run_id = args.run
    run_dir = home / "runs" / run_id
    diff_path = run_dir / "framework-edit.diff"
    if not diff_path.is_file():
        raise _Refused(f"no framework-edit.diff for run {run_id!r} at {diff_path}")
    diff = diff_path.read_text(encoding="utf-8")
    if not diff.strip():
        raise _Refused(f"framework-edit.diff for run {run_id!r} is empty")

    kickoff_path = args.kickoff or _kickoff_from_profile(run_dir) or _kickoff_from_db(home, run_id)
    if not kickoff_path:
        raise _Refused(f"no kickoff for run {run_id!r} (pass --kickoff)")
    if not Path(kickoff_path).is_file():
        raise _Refused(f"kickoff does not exist: {kickoff_path}")

    return diff, Path(kickoff_path).read_text(encoding="utf-8"), {
        "run": run_id, "commit": ""}


def _source_from_commit(args, root: Path) -> tuple[str, str, dict]:
    sha = args.commit
    diff = _git(root, "show", "--format=", "--no-color", sha)
    if not diff.strip():
        raise _Refused(f"commit {sha!r} has no diff")

    if args.kickoff:
        kickoff_path = Path(args.kickoff)
        if not kickoff_path.is_file():
            raise _Refused(f"kickoff does not exist: {kickoff_path}")
        kickoff = kickoff_path.read_text(encoding="utf-8")
    else:
        log = _git(root, "log", "-1", "--format=%s%n%n%b", sha)
        subject, _, body = log.partition("\n\n")
        kickoff = f"# {subject.strip()}\n\n{body}"

    return diff, kickoff, {"run": "", "commit": sha}


def _cmd_add_case(args, root: Path, home: Path) -> int:
    if not _CASE_ID_RE.match(args.id):
        raise _Refused(
            f"invalid case id {args.id!r}: must match {_CASE_ID_RE.pattern}")

    case_dir = _cases_dir(root) / args.id
    if case_dir.exists():
        raise _Refused(f"case {args.id!r} already exists at {case_dir}")

    if args.run:
        diff, kickoff, source = _source_from_run(args, home)
    else:
        diff, kickoff, source = _source_from_commit(args, root)

    if args.kind == "incident":
        if not args.summary.strip():
            raise _Refused("incident cases need --summary")
        if not args.defect_file and not args.keyword:
            raise _Refused(
                "incident cases need at least one --defect-file or --keyword")
        defect = {
            "files": list(args.defect_file),
            "summary": args.summary.strip(),
            "keywords": list(args.keyword),
        }
    else:
        defect = {"files": [], "summary": "", "keywords": []}

    payload = {
        "id": args.id,
        "kind": args.kind,
        "expect": "block" if args.kind == "incident" else "pass",
        "source": source,
        "defect": defect,
        "added_at": date.today().isoformat(),
        "notes": args.notes,
    }

    case_dir.mkdir(parents=True)
    (case_dir / "case.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (case_dir / "kickoff.md").write_text(kickoff, encoding="utf-8")
    (case_dir / "diff.patch").write_text(diff, encoding="utf-8")
    sys.stdout.write(f"added {args.id} ({args.kind})\n")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# run / status / score
# ─────────────────────────────────────────────────────────────────────────────


def _print_score(summary: dict) -> None:
    incidents = summary.get("incidents") or {}
    clean = summary.get("clean") or {}

    def _rate(value) -> str:
        return "n/a" if value is None else f"{value:.3f}"

    sys.stdout.write(
        f"incidents: n={incidents.get('n', 0)} "
        f"caught={incidents.get('caught', 0)} "
        f"caught_on_defect={incidents.get('caught_on_defect', 0)} "
        f"recall={_rate(incidents.get('recall'))} "
        f"wilson_lower={_rate(incidents.get('recall_wilson_lower'))}\n")
    sys.stdout.write(
        f"clean: n={clean.get('n', 0)} "
        f"false_blocks={clean.get('false_blocks', 0)} "
        f"false_block_rate={_rate(clean.get('false_block_rate'))}\n")
    sys.stdout.write(f"errors: {summary.get('errors', 0)}\n")
    sys.stdout.write(f"cost_usd: {float(summary.get('cost_usd') or 0.0):.4f}\n")


def _cmd_run(args, root: Path, home: Path) -> int:
    lane = args.lane or rev.reviewer_lane(root)
    fp = rev.fingerprint(root, lane, home=str(home))

    cases = rev.load_cases(_cases_dir(root))
    if args.cases:
        wanted = set(args.cases)
        known = {case.id for case in cases}
        missing = wanted - known
        if missing:
            raise _Refused(f"unknown case id(s): {', '.join(sorted(missing))}")
        cases = [case for case in cases if case.id in wanted]

    if not cases:
        sys.stdout.write("no reviewer eval cases\n")
        return 1

    if args.dry_run:
        sys.stdout.write(f"fingerprint {fp['id']}\n")
        for case in cases:
            prompt = rev.build_prompt(case, root=root, review_file="review.json")
            sys.stdout.write(f"{case.id}  prompt {len(prompt)} chars\n")
        return 0

    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(
        prefix="mini-ork-reviewer-eval-"))

    results = [
        rev.run_case(case, root=root, lane=lane, work_dir=work_dir,
                     timeout_s=args.timeout)
        for case in cases
    ]
    summary = rev.score(results)

    results_dir = _results_dir(root)
    results_dir.mkdir(parents=True, exist_ok=True)
    results_path = results_dir / rev.today_name(fp["id"])
    with results_path.open("a", encoding="utf-8") as fh:
        for result in results:
            row = asdict(result)
            row["fingerprint"] = fp["id"]
            fh.write(json.dumps(row) + "\n")
        fh.write(json.dumps({
            "summary": summary,
            "fingerprint": fp,
            "cases": [case.id for case in cases],
            "at": datetime.now().isoformat(),
        }) + "\n")

    _print_score(summary)
    every_case_errored = all(r.error or r.verdict == "error" for r in results)
    return 1 if every_case_errored else 0


def _cmd_status(args, root: Path, home: Path) -> int:
    cases_dir = _cases_dir(root)
    if not cases_dir.is_dir() or not rev.load_cases(cases_dir):
        sys.stdout.write("no reviewer eval cases\n")
        return 0

    lane = args.lane or rev.reviewer_lane(root)
    current = rev.fingerprint(root, lane, home=str(home))["id"]
    sys.stdout.write(f"current fingerprint: {current}\n")

    latest = rev.latest_result(_results_dir(root))
    last_id = ""
    last_date = ""
    if latest is not None:
        path, row = latest
        stored = row.get("fingerprint")
        if isinstance(stored, dict):
            last_id = str(stored.get("id") or "")
        elif stored:
            last_id = str(stored)
        last_date = path.name[:10]
        sys.stdout.write(f"latest result: {last_id or 'none'} ({last_date or 'unknown'})\n")
    else:
        sys.stdout.write("latest result: none\n")

    if latest is not None and last_id == current:
        return 0

    sys.stdout.write(
        "reviewer changed since its last eval — run: mini-ork reviewer-eval run\n")
    return 1


def _cmd_score(args) -> int:
    path = Path(args.file)
    rows: list = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError as exc:
        sys.stderr.write(f"cannot read {path}: {exc}\n")
        return 1

    for row in reversed(rows):
        if isinstance(row, dict) and isinstance(row.get("summary"), dict):
            _print_score(row["summary"])
            return 0

    sys.stderr.write(f"{path}: no summary row\n")
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:  # argparse: --help → 0, usage error → 2
        return int(exc.code or 0)

    if not args.command:
        parser.print_help()
        return 2

    root = _root()
    home = _home(root)

    try:
        if args.command == "list":
            return _cmd_list(root)
        if args.command == "add-case":
            return _cmd_add_case(args, root, home)
        if args.command == "run":
            return _cmd_run(args, root, home)
        if args.command == "status":
            return _cmd_status(args, root, home)
        if args.command == "score":
            return _cmd_score(args)
    except _Refused as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
