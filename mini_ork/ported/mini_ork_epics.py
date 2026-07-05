"""Python port of bin/mini-ork-epics — ingest/list/inspect epics + dependencies.

Strangler-fig parity port of the subcommand CLI: ingest (roadmap md → epics +
epic_dependencies + auto-block), split (per-epic kickoff files + kickoff_path),
list, ready (via ported epic_graph), show, priority. Verbatim markdown/dep
regexes and slug/dedupe/auto-block logic.

    main(argv=None, *, db=None, root=None) -> int
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

from . import epic_graph

_USAGE = """Usage: mini-ork epics <subcommand> [args]

  ingest <roadmap.md>          Parse roadmap markdown -> epics + deps
  split <roadmap.md>           Emit per-epic kickoff files under kickoffs/auto/
                               and set epics.kickoff_path for each.
  list [--status STATUS]       List epics (default: active, non-archived)
  ready                        List ready-to-dispatch epics
  show <epic_id>               Show one epic + its deps
  priority <epic_id> [VALUE]   Show or set an epic's base priority (integer,
                               higher = more important; default 0). The
                               scheduler computes effective priority at
                               dispatch time as the max of self + blocked
                               waiters (priority inheritance, Track B5).
"""

_EPIC_RE = re.compile(r"^##\s+(.+?)\s*(?:\(id:\s*([\w.-]+)\))?\s*$")
_DEP_RES = {
    "hard": re.compile(r"^\s*(?:-\s+)?(?:depends on|blocked by|after|requires)\s*:\s*(.+)$", re.I),
    "soft": re.compile(r"^\s*(?:-\s+)?(?:should follow|prefer after)\s*:\s*(.+)$", re.I),
    "informational": re.compile(r"^\s*(?:-\s+)?(?:related to|see also|context)\s*:\s*(.+)$", re.I),
}
_DEP_ANY = re.compile(
    r"^\s*(?:-\s+)?(?:depends on|blocked by|after|requires|should follow|prefer after|"
    r"related to|see also|context)\s*:.*$", re.I)


def _resolve_db(db):
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    return db or os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", title.lower().strip()).strip("-")
    return s or "epic"


def _parse_epics(text: str):
    epics, cur = [], None
    for raw in text.splitlines():
        m = _EPIC_RE.match(raw)
        if m:
            title = m.group(1).strip()
            eid = (m.group(2) or "").strip() or _slugify(title)
            cur = {"id": eid, "title": title, "body": []}
            epics.append(cur)
        elif cur is not None:
            cur["body"].append(raw)
    seen, deduped = set(), []
    for e in epics:
        if e["id"] in seen:
            e["id"] = f"{e['id']}-{len(deduped)}"
        seen.add(e["id"])
        deduped.append(e)
    return deduped


def ingest(roadmap: str, db: str) -> int:
    if not os.path.isfile(roadmap):
        sys.stderr.write(f"no such file: {roadmap}\n"); return 2
    epics = _parse_epics(Path(roadmap).read_text(encoding="utf-8", errors="replace"))
    if not epics:
        sys.stderr.write("ingest: no '## <title>' headings found\n"); return 1
    con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
    inserted = dep_count = 0
    for e in epics:
        if not con.execute("SELECT id FROM epics WHERE id=?", (e["id"],)).fetchone():
            con.execute("INSERT INTO epics(id, title, status) VALUES(?,?,'not started')",
                        (e["id"], e["title"][:200]))
            inserted += 1
        for line in e["body"]:
            for kind, rgx in _DEP_RES.items():
                m = rgx.match(line)
                if not m:
                    continue
                for raw_dep in re.split(r"[,\s]+", m.group(1)):
                    dep = raw_dep.strip().strip("`")
                    if not dep or dep == e["id"]:
                        continue
                    try:
                        con.execute("INSERT OR IGNORE INTO epic_dependencies "
                                    "(from_epic_id, to_epic_id, kind) VALUES(?,?,?)",
                                    (dep, e["id"], kind))
                        dep_count += 1
                    except sqlite3.Error:
                        pass
    con.execute("""UPDATE epics SET status='blocked'
        WHERE status='not started' AND id IN (
            SELECT DISTINCT to_epic_id FROM epic_dependencies
             WHERE kind='hard' AND resolved_at IS NULL)""")
    con.commit(); con.close()
    sys.stdout.write(f"ingest: {inserted} new epic(s), {dep_count} dep edge(s) processed\n")
    return 0


def _path_hints(body):
    p = re.findall(r"`([a-z_][\w./-]*\.(?:sh|py|sql|md|yaml|yml|json|ts|tsx|js|jsx))`", body, re.I)
    p += re.findall(r"`(bin/[\w-]+)`", body)
    p += re.findall(r"`(lib/[\w_-]+\.sh)`", body)
    p += re.findall(r"`(recipes/[\w-]+/?[\w./-]*)`", body)
    return list(dict.fromkeys(p))


def _synth_verify(paths):
    shells = [p for p in paths if p.endswith(".sh") or p.startswith("bin/")]
    pys = [p for p in paths if p.endswith(".py")]
    sqls = [p for p in paths if p.endswith(".sql")]
    cmds = []
    if shells:
        cmds.append("shellcheck " + " ".join(shells[:5]))
    if pys:
        cmds.append("python3 -m py_compile " + " ".join(pys[:5]))
    if sqls:
        cmds.append("# Apply: sqlite3 .mini-ork/state.db < " + sqls[0])
    if not cmds:
        cmds = ["bash -n bin/mini-ork-epics bin/mini-ork-scheduler",
                "bash tests/integration/test_autonomous_epic_pipeline.sh"]
    return cmds


def split(roadmap: str, db: str, root: str) -> int:
    if not os.path.isfile(roadmap):
        sys.stderr.write(f"no such file: {roadmap}\n"); return 2
    epics = _parse_epics(Path(roadmap).read_text(encoding="utf-8", errors="replace"))
    if not epics:
        sys.stderr.write("split: no '## <title>' headings found\n"); return 1
    out_dir = Path(root) / "kickoffs" / "auto"; out_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
    written = 0
    for e in epics:
        body_lines = [ln for ln in e["body"] if not _DEP_ANY.match(ln)]
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        body = "\n".join(body_lines).strip()
        hints = _path_hints(body)
        verify = _synth_verify(hints)
        parts = [f"# {e['title']}", "", "## Goal", "", body or "(no body extracted from roadmap)", ""]
        if hints:
            parts += ["## Scope Hint", "", *[f"- `{p}`" for p in hints[:10]], ""]
        parts += ["## Verification commands", "", *[f"- `{c}`" for c in verify], "",
                  "## Done When", "",
                  '- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"` with all verifiers passing.',
                  "- All verification commands pass in the isolated worktree.", "",
                  f"_Auto-generated by `mini-ork epics split` from {Path(roadmap).name}._", ""]
        (Path(root) / f"kickoffs/auto/{e['id']}.md").write_text("\n".join(parts), encoding="utf-8")
        con.execute("UPDATE epics SET kickoff_path=? WHERE id=?",
                    (f"kickoffs/auto/{e['id']}.md", e["id"]))
        written += 1
    con.commit(); con.close()
    sys.stdout.write(f"split: wrote {written} kickoff(s) under kickoffs/auto/ + updated kickoff_path\n")
    return 0


def _list(status: str, db: str) -> int:
    con = sqlite3.connect(db)
    where = ("WHERE status=? AND archived_at IS NULL" if status else "WHERE archived_at IS NULL")
    args = (status,) if status else ()
    rows = con.execute(f"SELECT id, status, priority, title FROM epics {where} ORDER BY created_at", args).fetchall()
    con.close()
    for eid, st, pri, title in rows:
        sys.stdout.write(f"{eid:<22} | {st:<15} | priority={pri:<5} | {(title or '')[:60]}\n")
    return 0


def _priority(epic_id: str, value, db: str) -> int:
    con = sqlite3.connect(db)
    if value is None:
        row = con.execute("SELECT id, priority FROM epics WHERE id=?", (epic_id,)).fetchone()
        con.close()
        if row:
            sys.stdout.write(f"{row[0]} | priority={row[1]}\n")
        return 0
    if not re.fullmatch(r"-?[0-9]+", str(value)):
        con.close(); sys.stderr.write(f"priority: VALUE must be an integer (got: {value})\n"); return 2
    con.execute("UPDATE epics SET priority=? WHERE id=?", (int(value), epic_id)); con.commit(); con.close()
    return _priority(epic_id, None, db)


def main(argv: list[str] | None = None, *, db: str | None = None, root: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    db = _resolve_db(db)
    sub = argv[0] if argv else "help"
    rest = argv[1:]
    if sub == "ingest":
        return ingest(rest[0], db) if rest else (sys.stderr.write("roadmap.md path required\n") or 2)
    if sub == "split":
        return split(rest[0], db, root) if rest else (sys.stderr.write("roadmap.md path required\n") or 2)
    if sub == "list":
        status = ""
        for i, a in enumerate(rest):
            if a == "--status" and i + 1 < len(rest):
                status = rest[i + 1]
        return _list(status, db)
    if sub == "ready":
        for line in (epic_graph.ready_now(db) or []) if hasattr(epic_graph, "ready_now") else []:
            sys.stdout.write(f"{line}\n")
        return 0
    if sub == "show":
        return _show(rest[0], db) if rest else (sys.stderr.write("epic_id required\n") or 2)
    if sub == "priority":
        if not rest:
            sys.stderr.write("epic_id required\n"); return 2
        return _priority(rest[0], rest[1] if len(rest) > 1 else None, db)
    if sub in ("help", "--help", "-h"):
        sys.stdout.write(_USAGE); return 0
    sys.stderr.write(f"Unknown subcommand: {sub}\n"); sys.stdout.write(_USAGE); return 2


def _show(epic_id: str, db: str) -> int:
    con = sqlite3.connect(db)
    e = con.execute("SELECT id, title, status, priority, created_at, updated_at FROM epics WHERE id=?",
                    (epic_id,)).fetchone()
    sys.stdout.write("=== epic ===\n")
    if e:
        for k, v in zip(("id", "title", "status", "priority", "created_at", "updated_at"), e):
            sys.stdout.write(f"{k} = {v}\n")
    incoming = con.execute("SELECT from_epic_id, kind, resolved_at FROM epic_dependencies "
                           "WHERE to_epic_id=? ORDER BY kind", (epic_id,)).fetchall()
    outgoing = con.execute("SELECT to_epic_id, kind, resolved_at FROM epic_dependencies "
                           "WHERE from_epic_id=? ORDER BY kind", (epic_id,)).fetchall()
    con.close()
    sys.stdout.write("\n=== deps (incoming — must be 'done' for this epic to run) ===\n")
    for fid, kind, res in incoming:
        sys.stdout.write(f"{fid:<22} | {kind:<15} | {'UNRESOLVED' if res is None else 'resolved'}\n")
    sys.stdout.write("\n=== deps (outgoing — this epic blocks these) ===\n")
    for tid, kind, res in outgoing:
        sys.stdout.write(f"{tid:<22} | {kind:<15} | {'UNRESOLVED' if res is None else 'resolved'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
