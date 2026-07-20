"""Canonical Python classify runtime — keyword task router.

Strangler-fig parity port. Scores each config/task_classes/*.yaml +
recipes/*/task_class.yaml against the kickoff (keyword word-boundary hits,
regex +2, class-name alias +3), picks the highest (lex-first on ties), falls
back to 'generic'; --task-class forces. Writes a task_runs row unless --dry-run.
The scoring block is a verbatim transcription of the bash's embedded python.

    main(argv=None, *, db=None, root=None) -> int
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from mini_ork import trace_store

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_USAGE = """Usage: mini-ork classify <kickoff.md> [--workflow-version <ver>] [--dry-run]

Classify a kickoff file into a task_class by matching against
config/task_classes/*.yaml pattern files.

Outputs:
  task_class=<name>  (stdout)
  runs table row     (DB, unless --dry-run)

Options:
  --workflow-version <ver>  Override default workflow version for this class
  --dry-run                 Print classification; do not write DB
  --help                    Show this help
"""


def _score(yaml_file: str, kickoff_text: str, candidate_class: str) -> int:
    data = (yaml.safe_load(open(yaml_file)) if yaml else {}) or {}
    keywords, regexes = [], []
    m = data.get("matches", [])
    if isinstance(m, dict):
        keywords += m.get("keywords", []) or []
        regexes += m.get("regex", []) or []
    elif isinstance(m, list):
        keywords += m
    keywords += data.get("keywords", []) or []

    score, seen = 0, set()
    for raw in keywords:
        kw = str(raw or "").strip()
        key = kw.lower()
        if not kw or key in seen:
            continue
        seen.add(key)
        if re.fullmatch(r"[A-Za-z0-9_ -]+", kw):
            pat = r"(?<![A-Za-z0-9_])" + re.escape(kw) + r"(?![A-Za-z0-9_])"
            matched = re.search(pat, kickoff_text, re.I) is not None
        else:
            matched = key in kickoff_text.lower()
        if matched:
            wc = max(1, len(re.findall(r"[A-Za-z0-9_]+", kw)))
            score += 1 + (1 if wc > 1 else 0)
    for raw in regexes:
        rx = str(raw or "").strip()
        if not rx:
            continue
        try:
            if re.search(rx, kickoff_text, re.I):
                score += 2
        except re.error:
            continue
    for alias in {candidate_class, candidate_class.replace("_", "-"), candidate_class.replace("_", " ")}:
        if not alias:
            continue
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(alias) + r"(?![A-Za-z0-9_])", kickoff_text, re.I):
            score += 3
            break
    return score


def _candidates(task_classes_dir: str, root: str, home: str) -> list[str]:
    files = []
    if os.path.isdir(task_classes_dir):
        files += sorted(str(p) for p in Path(task_classes_dir).glob("*.yaml"))
    recipes = os.path.join(home, "recipes")
    if not os.path.isdir(recipes):
        recipes = os.path.join(root, "recipes")
    if os.path.isdir(recipes):
        files += sorted(str(p) for p in Path(recipes).glob("*/task_class.yaml"))
    return files


def _candidate_class(yaml_file: str) -> str:
    if yaml_file.replace(os.sep, "/").endswith("task_class.yaml") and "/recipes/" in yaml_file.replace(os.sep, "/"):
        d = (yaml.safe_load(open(yaml_file)) if yaml else {}) or {}
        name = str(d.get("name") or os.path.basename(os.path.dirname(yaml_file))).strip()
        cc = name or os.path.basename(os.path.dirname(yaml_file))
    else:
        cc = os.path.basename(yaml_file)[:-5]
    return cc.replace("-", "_")


def _safe_trace_write(payload: dict, db: str) -> None:
    """Best-effort trace side-channel matching Bash's ``|| true`` contract."""
    try:
        trace_store.trace_write(payload, db=db)
    except Exception:
        pass


def main(argv: list[str] | None = None, *, db: str | None = None, root: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    kickoff = ""
    force = ""
    wf_version = ""
    dry_run = 1 if os.environ.get("MINI_ORK_DRY_RUN") == "1" else 0

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(_USAGE); return 0
        elif a == "--dry-run":
            dry_run = 1; i += 1
        elif a == "--task-class":
            force = argv[i + 1]; i += 2
        elif a == "--workflow-version":
            wf_version = argv[i + 1]; i += 2
        elif a.startswith("-"):
            sys.stderr.write(f"Unknown flag: {a}. Try --help\n"); return 2
        else:
            if not kickoff:
                kickoff = a; i += 1
            else:
                sys.stderr.write(f"Unexpected argument: {a}\n"); return 2
    if not kickoff:
        sys.stdout.write(_USAGE); return 2
    if not os.path.isfile(kickoff):
        sys.stderr.write(f"kickoff not found: {kickoff}\n"); return 2

    max_bytes = int(os.environ.get("MO_MAX_KICKOFF_BYTES", "1048576"))
    if os.path.getsize(kickoff) > max_bytes:
        sys.stderr.write(f"classify: kickoff exceeds MO_MAX_KICKOFF_BYTES\n"); return 2

    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = db or os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    tcd = os.path.join(home, "config", "task_classes")
    if not os.path.isdir(tcd):
        tcd = os.path.join(root, "config", "task_classes")

    trace_id = f"tr-classify-{int(time.time())}-{os.getpid()}"
    if dry_run == 0:
        _safe_trace_write({
            "trace_id": trace_id,
            "task_class": "__classify__",
            "status": "running",
            "workflow_version_id": "classify-start",
        }, db)

    kickoff_text = open(kickoff, encoding="utf-8", errors="replace").read()
    task_class, best = "generic", 0
    if force:
        task_class, best = force, -1
    else:
        for yf in _candidates(tcd, root, home):
            cc = _candidate_class(yf)
            hits = _score(yf, kickoff_text, cc)
            if hits > best:
                best, task_class = hits, cc

    if wf_version:
        resolved_wf = wf_version
    else:
        cyaml = os.path.join(tcd, f"{task_class}.yaml")
        resolved_wf = "latest"
        if os.path.isfile(cyaml) and yaml:
            d = yaml.safe_load(open(cyaml)) or {}
            resolved_wf = str(d.get("default_workflow_version") or "latest")

    sys.stdout.write(f"task_class={task_class}\nworkflow_version={resolved_wf}\nkickoff={kickoff}\n")
    if dry_run == 1:
        sys.stdout.write(f"[dry-run] would write task_class={task_class} to DB run row\n")
        return 0

    run_id = None
    if os.path.isfile(db):
        import sqlite3
        run_id = os.environ.get("MINI_ORK_RUN_ID") or f"run-{int(time.time())}-{os.getpid()}"
        recipe = os.environ.get("MINI_ORK_RECIPE") or None
        now = int(time.time())
        con = sqlite3.connect(db); con.execute("PRAGMA journal_mode=WAL")
        try:
            con.execute("""
                INSERT INTO task_runs (id, task_class, recipe, workflow_version, kickoff_path,
                    status, trace_id, created_at, updated_at)
                VALUES (?,?,?,?,?, 'classified', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET task_class=excluded.task_class, recipe=excluded.recipe,
                    workflow_version=excluded.workflow_version, kickoff_path=excluded.kickoff_path,
                    status='classified',
                    trace_id=COALESCE(task_runs.trace_id, excluded.trace_id),
                    updated_at=excluded.updated_at
            """, (run_id, task_class, recipe, resolved_wf, kickoff,
                  trace_id, now, now))
            con.commit()
            sys.stdout.write(f"run_id={run_id}\n")
        except sqlite3.OperationalError as e:
            sys.stderr.write(f"[warn] task_runs table not yet created ({e}); DB write skipped\n")
        finally:
            con.close()
    _safe_trace_write({
        "trace_id": trace_id,
        "run_id": run_id,
        "task_class": task_class,
        "status": "success",
    }, db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
