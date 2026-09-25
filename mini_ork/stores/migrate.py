"""Python port of lib/migrate.sh + db/init.sh — versioned, checksummed,
transactional DB migrations for mini-ork.

Strangler-fig parity port:

    checksum(path)                       -> sha256 hex of a file
    is_legacy_checksum(s)                -> True if s is a placeholder (not 64-hex)
    ensure_table(db)                     -> create/upgrade schema_migrations
    migrate_apply(dir, dry_run, db, root)-> apply pending *.sql (lex order)
    migrate_status(dir, db)              -> summary + pending/drifted lines
    migrate_verify(dir, db)              -> 0 if all applied checksums match, else 1
    init_db(db, root)                    -> full db/init.sh port (migrations + views)

Each apply + its schema_migrations record commit together (all-or-nothing), and
already-applied migrations with a legacy placeholder checksum are re-hashed in
place, never re-run — mirroring the bash byte-for-byte (applied_at timestamps
excepted, as bash uses strftime('now')).

Dot-commands: some shipped migrations contain sqlite3 CLI dot-commands
(`.read "|sh -c '…'"`, `.once`, `.shell`) which ``sqlite3 -bail`` interprets
but Python's executescript cannot. ``_exec_statements`` re-implements the
subset the shipped migrations use: `.read "|<cmd>"` runs the shell pipeline
(with MINI_ORK_DB / MINI_ORK_ROOT exported, exactly like db/init.sh) and
inlines its stdout as SQL, `.read <path>` inlines a file, `.once <path>`
redirects the next statement's rows to a file, and `.shell <cmd>` runs a
shell command. Any other dot-command fails the migration (rolled back),
matching `sqlite3 -bail` aborting on an unrecognised command.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


def checksum(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_sql(text: str) -> str:
    """Comment-insensitive, literal-preserving canonical form.

    Strips ``--`` line comments and ``/* ... */`` block comments ONLY outside
    single-quoted string literals (SQL ``''`` escape respected, so a semantic
    edit inside a string can never be hidden as a comment). Outside string
    literals, whitespace runs collapse to a single space between tokens;
    inside a single-quoted literal, every byte — including whitespace — flows
    through verbatim, so a payload like ``VALUES ('a  b')`` produces a
    different canonical form from ``VALUES ('a b')``.

    Dot-command lines (any line starting with ``.`` followed by a word char —
    ``.read``, ``.once``, ``.shell``, …) reach the canonical form by the
    same walk: their single-quoted payloads survive verbatim because the
    in-walk rule already preserves literal contents.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    # True iff we have just consumed an outside-string whitespace run and
    # have not yet emitted the separator that bridges it to the next
    # outside-string token. Reset on every non-whitespace outside-string
    # emit (the separator — if needed — is emitted right before the token).
    need_separator = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "'":
                # SQL '' escape: skip the second quote, stay in string.
                if i + 1 < n and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if c == "'":
            if need_separator:
                out.append(" ")
            out.append(c)
            in_string = True
            need_separator = False
            i += 1
            continue
        # -- line comment: skip to (and consume) the newline.
        if c == "-" and i + 1 < n and text[i + 1] == "-":
            j = text.find("\n", i)
            if j == -1:
                break
            i = j  # newline is whitespace; the next iteration skips it.
            need_separator = True
            continue
        # /* block comment */
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
            need_separator = True
            continue
        # Whitespace outside string: collapse the run to nothing; the next
        # outside-string token will insert a single space separator.
        if c.isspace():
            need_separator = True
            i += 1
            continue
        # Non-whitespace outside string: emit, with a single leading space
        # iff a whitespace run preceded us in the original text.
        if need_separator:
            out.append(" ")
        out.append(c)
        need_separator = False
        i += 1
    return "".join(out)


def canonical_checksum(path: str | os.PathLike) -> str:
    """sha256 hex of the comment-insensitive canonical form of a migration."""
    return hashlib.sha256(_canonical_sql(Path(path).read_text(encoding="utf-8"))
                          .encode("utf-8")).hexdigest()


def is_legacy_checksum(s: str) -> bool:
    """True when s is NOT a real sha256 (non-hex char, empty, or wrong length)."""
    if not s or re.search(r"[^0-9a-f]", s):
        return True
    return len(s) != 64


def _checksum_clean(stored: str, raw_sum: str, canon_sum: str) -> bool:
    """True when ``stored`` matches the file under EITHER checksum scheme.

    The migration guard compares two versions of an applied file; this helper
    is the single source of truth used by ``migrate_apply``,
    ``migrate_verify``, and ``migrate_status`` so the three stay in lockstep.
    A stored row counts as clean when it equals the canonical (new-scheme)
    checksum OR the raw (legacy) sha256 — legacy placeholders are handled by
    the separate ``is_legacy_checksum`` rehash branch.
    """
    return stored == canon_sum or stored == raw_sum


def _is_out_of_order(filename: str, applied_names: set[str]) -> bool:
    """True when ``filename`` sorts before an already-applied entry in the ledger.

    Single source of truth for the 'pending set is a suffix of the sorted
    list' invariant checked by ``migrate_apply`` (and reusable by
    ``migrate_status`` / ``migrate_verify``) so the three cannot drift apart.
    Plain string comparison matches the lex walk order, so view files
    (``v_*.sql``, which always sort after numeric prefixes) participate
    correctly without numeric-prefix parsing or an applied_at comparison.
    """
    return any(name > filename for name in applied_names)


def _db(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if not env:
        raise RuntimeError("MINI_ORK_DB required")
    return env


def ensure_table(db: str) -> None:
    con = sqlite3.connect(db)
    # NOTE: the whitespace in this literal is load-bearing — sqlite stores the
    # CREATE statement text verbatim and the A/B schema-parity gate diffs the
    # `.schema` dump byte-for-byte against lib/migrate.sh's mo_migrate_ensure_table.
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (\n"
        "    filename   TEXT PRIMARY KEY,\n"
        "    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),\n"
        "    checksum   TEXT\n"
        "  )"
    )
    cols = [r[1] for r in con.execute("PRAGMA table_info('schema_migrations')").fetchall()]
    if "mini_ork_version" not in cols:
        con.execute("ALTER TABLE schema_migrations ADD COLUMN mini_ork_version TEXT")
    con.commit()
    con.close()


def _version(root: str | None) -> str:
    root = root or os.environ.get("MINI_ORK_ROOT", ".")
    try:
        with open(os.path.join(root, "bin", "mini-ork"), errors="ignore") as f:
            m = re.search(r"[0-9]+\.[0-9]+\.[0-9]+", f.read())
            return m.group(0) if m else ""
    except OSError:
        return ""


_BEGIN_RE = re.compile(r"^[ \t]*begin([ \t]+transaction)?[ \t]*;", re.IGNORECASE | re.MULTILINE)
_RECORD = ("INSERT OR REPLACE INTO schema_migrations"
           "(filename, applied_at, checksum, mini_ork_version) "
           "VALUES ('{fn}', strftime('%Y-%m-%dT%H:%M:%fZ','now'), '{sum}', '{ver}');")

_DOT_RE = re.compile(r"^\s*\.(\w+)\s*(.*)$")
_MAX_READ_DEPTH = 8


def _resolve_backslashes(s: str) -> str:
    """Port of sqlite3 shell.c resolve_backslashes(): C-style escapes.

    `\\n`→newline, `\\t`→tab, `\\r`→CR, `\\b`, `\\f`, `\\a`, `\\v`, `\\\\`→`\\`,
    `\\ooo` octal (1-3 digits), and any other `\\X` → X (so `\\\"` → `"`).
    """
    simple = {"a": "\a", "b": "\b", "f": "\f", "n": "\n",
              "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n in simple:
                out.append(simple[n])
                i += 2
            elif n.isdigit() and n < "8":
                j = i + 1
                oct_digits = ""
                while j < len(s) and len(oct_digits) < 3 and "0" <= s[j] <= "7":
                    oct_digits += s[j]
                    j += 1
                out.append(chr(int(oct_digits, 8)))
                i = j
            else:
                out.append(n)
                i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _unquote(arg: str) -> str:
    """sqlite3 CLI strips one layer of double quotes around dot-command args
    and resolves backslash escapes inside them (shell.c resolve_backslashes).
    Shipped migrations rely on this: `.read "|sh -c 'db=\\\"${MINI_ORK_DB:?}\\\"…'"`.
    """
    if len(arg) >= 2 and arg.startswith('"') and arg.endswith('"'):
        return _resolve_backslashes(arg[1:-1])
    return arg


def _dot_env(db: str, root: str | None) -> dict:
    """Env for `.read "|sh -c …"` / `.shell` subprocesses.

    db/init.sh exports MINI_ORK_DB (always) and MINI_ORK_ROOT (defaulted to the
    repo root) before applying migrations, because the shipped dot-command
    pipelines do `${MINI_ORK_DB:?}`. Mirror that exactly.
    """
    env = os.environ.copy()
    env["MINI_ORK_DB"] = db
    if root is not None:
        env["MINI_ORK_ROOT"] = root
    elif "MINI_ORK_ROOT" not in env:
        env["MINI_ORK_ROOT"] = "."
    return env


def _exec_statements(con: sqlite3.Connection, text: str, env: dict,
                     once_target: str | None = None, depth: int = 0) -> str | None:
    """Execute a sqlite3-CLI-style script statement-by-statement.

    Plain SQL runs through ``con`` (raising on the first error, mirroring
    ``sqlite3 -bail``). Lines starting with ``.`` at a statement boundary are
    interpreted as sqlite3 CLI dot-commands:

      .read "|<cmd>"  run <cmd> via sh -c; its stdout is executed as SQL
      .read <path>    execute the file's contents as SQL
      .once <path>    write the next statement's result rows (list mode,
                      ``|``-separated) to <path>, truncating it first
      .shell <cmd>    run <cmd> via sh -c (result ignored, like the CLI)

    Anything else raises, failing the migration — same as ``-bail`` aborting
    on an unknown command. Returns the unconsumed once_target, if any.
    """
    if depth > _MAX_READ_DEPTH:
        raise sqlite3.Error(".read recursion too deep")
    buf = ""

    def _buf_has_sql() -> bool:
        # `--` comment lines and blank lines do NOT start a statement in the
        # sqlite3 CLI — a dot-command line following them is still at a
        # statement boundary (e.g. 0039's header comments before `.once`).
        return any(s and not s.startswith("--")
                   for s in (ln.strip() for ln in buf.splitlines()))

    for raw in text.splitlines(keepends=True):
        if not _buf_has_sql() and raw.strip().startswith("."):
            buf = ""  # drop accumulated leading comments
            m = _DOT_RE.match(raw.strip())
            if not m:
                raise sqlite3.Error(f"unparseable dot-command: {raw.strip()!r}")
            cmd, arg = m.group(1).lower(), _unquote(m.group(2).strip())
            if cmd == "read":
                if arg.startswith("|"):
                    proc = subprocess.run(
                        ["sh", "-c", arg[1:]], env=env,
                        capture_output=True, text=True)
                    if proc.returncode != 0:
                        raise sqlite3.Error(
                            f".read pipeline exited {proc.returncode}: {arg[1:]}"
                            f" — stderr: {proc.stderr.strip()[:200]}")
                    once_target = _exec_statements(
                        con, proc.stdout, env, once_target, depth + 1)
                else:
                    try:
                        content = Path(arg).read_text()
                    except OSError as exc:
                        raise sqlite3.Error(f".read cannot open {arg}: {exc}") from exc
                    once_target = _exec_statements(
                        con, content, env, once_target, depth + 1)
            elif cmd == "once":
                Path(arg).write_text("")  # truncate, like sqlite3 .once
                once_target = arg
            elif cmd == "shell":
                subprocess.run(["sh", "-c", arg], env=env, capture_output=True)
            else:
                raise sqlite3.Error(f"unsupported sqlite3 dot-command: .{cmd}")
            continue
        buf += raw
        if sqlite3.complete_statement(buf):
            stmt, buf = buf, ""
            cur = con.execute(stmt)
            if once_target is not None:
                with open(once_target, "a", encoding="utf-8") as fh:
                    for row in cur.fetchall():
                        fh.write("|".join("" if v is None else str(v) for v in row) + "\n")
                once_target = None
            else:
                cur.fetchall()  # discard, like the CLI writing to stdout
    if buf.strip():
        raise sqlite3.Error(f"incomplete SQL at end of script: {buf.strip()[:80]!r}")
    return once_target


def _apply_one(db: str, file: str, filename: str, checksum_hex: str, ver: str,
               root: str | None = None) -> bool:
    sql = Path(file).read_text()
    record = _RECORD.format(fn=filename, sum=checksum_hex, ver=ver)
    env = _dot_env(db, root)
    con = sqlite3.connect(db)
    con.isolation_level = None  # manual transaction control, mirroring sqlite3 -bail
    try:
        if _BEGIN_RE.search(sql):
            # migration manages its own transaction; run as-is then record
            _exec_statements(con, sql, env)
            con.execute(record)
        else:
            _exec_statements(con, "BEGIN;\n" + sql + "\n" + record + "\nCOMMIT;\n", env)
        con.close()
        return True
    except (sqlite3.Error, OSError, subprocess.SubprocessError):
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        con.close()
        return False


def migrate_apply(migrations_dir: str, dry_run: bool = False, db: str | None = None,
                  root: str | None = None, err_out: list[str] | None = None
                  ) -> tuple[int, list[str]]:
    """Apply pending *.sql in lex order. Returns (rc, output_lines).

    rc is 0 on success, 1 on a failed migration or disallowed checksum drift.
    output_lines are the ``  [apply] ...`` style stdout lines, matching bash
    order. When ``err_out`` (a list) is given, the bash stderr lines
    (``[FAIL]``/``[warn]``) are appended to it in order; otherwise they are
    dropped (rc still reflects them).
    """
    db = _db(db)
    out: list[str] = []
    if not os.path.isdir(migrations_dir):
        if err_out is not None:
            err_out.append(f"[migrate] no such dir: {migrations_dir}")
        return 1, out
    ensure_table(db)
    ver = _version(root)
    con = sqlite3.connect(db)
    applied_names = {row[0] for row in con.execute(
        "SELECT filename FROM schema_migrations")}
    for f in sorted(Path(migrations_dir).glob("*.sql")):
        filename = f.name
        sum_hex = checksum(f)
        canon_sum = canonical_checksum(f)
        row = con.execute(
            "SELECT COALESCE(checksum,'') FROM schema_migrations WHERE filename=?",
            (filename,)).fetchone()
        applied = row is not None
        if applied:
            applied_sum = row[0]
            if _checksum_clean(applied_sum, sum_hex, canon_sum):
                # Clean — but if the stored row still uses the raw scheme,
                # re-encode it to canonical so future comment-only rewords
                # don't have to clear the same bar twice.
                if applied_sum != canon_sum and not dry_run:
                    con.execute(
                        "UPDATE schema_migrations SET checksum=?, "
                        "mini_ork_version=COALESCE(mini_ork_version,?) WHERE filename=?",
                        (canon_sum, ver, filename))
                    con.commit()
                continue
            if is_legacy_checksum(applied_sum):
                if not dry_run:
                    con.execute(
                        "UPDATE schema_migrations SET checksum=?, "
                        "mini_ork_version=COALESCE(mini_ork_version,?) WHERE filename=?",
                        (canon_sum, ver, filename))
                    con.commit()
                out.append(f"  [rehash]  {filename} (legacy checksum → canonical sha256)")
            elif os.environ.get("MO_MIGRATE_ALLOW_DRIFT", "0") == "1":
                if not dry_run:
                    # One-time re-baseline: rewrite the drifted row to the
                    # canonical checksum so the next run no longer needs
                    # MO_MIGRATE_ALLOW_DRIFT.
                    con.execute(
                        "UPDATE schema_migrations SET checksum=?, "
                        "mini_ork_version=COALESCE(mini_ork_version,?) WHERE filename=?",
                        (canon_sum, ver, filename))
                    con.commit()
                if err_out is not None:
                    err_out.append(f"  [warn]    {filename} checksum drift"
                                   " (allowed by MO_MIGRATE_ALLOW_DRIFT; checksum"
                                   " re-recorded to canonical)")
            else:
                if err_out is not None:
                    err_out.append(f"  [FAIL]    {filename} was edited after being"
                                   " applied (checksum drift).")
                    err_out.append("            Add a NEW migration instead of editing"
                                   " a shipped one, or set MO_MIGRATE_ALLOW_DRIFT=1.")
                con.close()
                return 1, out
            continue
        if _is_out_of_order(filename, applied_names) and err_out is not None:
            err_out.append(
                f"  [warn]    {filename} sorts before already-applied migrations"
                " - applying out of order")
        if dry_run:
            out.append(f"  [pending] {filename}")
            continue
        out.append(f"  [apply]   {filename}")
        if _apply_one(db, str(f), filename, canon_sum, ver, root=root):
            out.append(f"  [ok]      {filename}")
        else:
            if err_out is not None:
                err_out.append(f"  [FAIL]    {filename} — rolled back, DB unchanged")
            con.close()
            return 1, out
    con.close()
    return 0, out


def migrate_status(migrations_dir: str, db: str | None = None) -> tuple[int, int, int, int]:
    """Return (applied, pending, drifted, total)."""
    db = _db(db)
    files = sorted(Path(migrations_dir).glob("*.sql"))
    total = len(files)
    pending = drifted = 0
    con = sqlite3.connect(db)
    for f in files:
        row = con.execute(
            "SELECT COALESCE(checksum,'') FROM schema_migrations WHERE filename=?",
            (f.name,)).fetchone()
        if row is None:
            pending += 1
        else:
            applied_sum = row[0]
            if is_legacy_checksum(applied_sum):
                continue
            sum_hex = checksum(f)
            canon_sum = canonical_checksum(f)
            if not _checksum_clean(applied_sum, sum_hex, canon_sum):
                drifted += 1
    con.close()
    return total - pending, pending, drifted, total


def migrate_verify(migrations_dir: str, db: str | None = None) -> int:
    """Return 0 if every applied migration's checksum still matches, else 1."""
    db = _db(db)
    rc = 0
    con = sqlite3.connect(db)
    for f in sorted(Path(migrations_dir).glob("*.sql")):
        row = con.execute(
            "SELECT COALESCE(checksum,'') FROM schema_migrations WHERE filename=?",
            (f.name,)).fetchone()
        if row is None:
            continue
        applied_sum = row[0]
        if is_legacy_checksum(applied_sum):
            continue
        sum_hex = checksum(f)
        canon_sum = canonical_checksum(f)
        if not _checksum_clean(applied_sum, sum_hex, canon_sum):
            rc = 1
    con.close()
    return rc


def _ensure_column(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Port of db/init.sh's ensure_column: idempotent guarded ADD COLUMN."""
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    if not exists:
        return
    col_count = con.execute(
        f"SELECT COUNT(*) FROM pragma_table_info('{table}') WHERE name=?",
        (column,)).fetchone()[0]
    if col_count == 0:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(db: str | None = None, root: str | None = None) -> tuple[int, str, str]:
    """Python port of db/init.sh — apply migrations + views to a state.db.

    Returns (rc, stdout_text, stderr_text) byte-mirroring the bash script:
    the ``[mini-ork init] DB: …`` header, the per-migration ``[apply]`` /
    ``[ok]`` / ``[rehash]`` lines, and the ``[mini-ork init] Done. Tables: N``
    trailer on stdout; the symlink / migration-failure / table-count errors on
    stderr. rc is 0 on success, 1 on any failure (matching ``set -euo
    pipefail`` + explicit ``exit 1`` paths).
    """
    db = _db(db)
    root = root or os.environ.get("MINI_ORK_ROOT") or "."
    out: list[str] = []
    err: list[str] = []

    def _result(rc: int) -> tuple[int, str, str]:
        return (rc,
                ("\n".join(out) + "\n") if out else "",
                ("\n".join(err) + "\n") if err else "")

    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    out.append(f"[mini-ork init] DB: {db}")

    if os.path.islink(db):
        err.append(f"[mini-ork init] ERROR: state DB path is a symlink;"
                   f" refusing to initialize: {db}")
        return _result(1)

    # WAL + sync + busy_timeout (persistent journal mode; bash ignores errors).
    try:
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=5000;")
        # Guarded real-column repair (review_id=32 fix-1).
        _ensure_column(con, "execution_traces", "process_reward", "REAL DEFAULT NULL")
        _ensure_column(con, "agent_performance_memory", "relative_advantage",
                       "REAL NOT NULL DEFAULT 0.0")
        con.commit()
        con.close()
    except sqlite3.Error:
        pass

    ensure_table(db)

    # Legacy recovery: a partial/manual apply can leave llm_calls.session_id
    # present without 0018 marked. Mark it so the runner won't re-ADD the column.
    con = sqlite3.connect(db)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()]
        if "session_id" in cols:
            con.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_session"
                        " ON llm_calls(session_id) WHERE session_id IS NOT NULL;")
            con.execute("INSERT OR IGNORE INTO schema_migrations(filename, applied_at,"
                        " checksum) VALUES ('0018_llm_calls_session_id.sql',"
                        " strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                        " 'recovered-existing-session-id');")
            con.commit()
    finally:
        con.close()

    migrations_dir = os.path.join(root, "db", "migrations")
    views_dir = os.path.join(root, "db", "views")

    err_lines: list[str] = []
    rc, lines = migrate_apply(migrations_dir, db=db, root=root, err_out=err_lines)
    out.extend(lines)
    err.extend(err_lines)
    if rc != 0:
        return _result(1)

    if os.path.isdir(views_dir):
        err_lines = []
        rc, lines = migrate_apply(views_dir, db=db, root=root, err_out=err_lines)
        out.extend(lines)
        err.extend(err_lines)
        if rc != 0:
            return _result(1)
    else:
        out.append(f"  [info] No views dir found at {views_dir} — skipping")

    # Validate: at least 20 CREATE TABLE statements in the final schema
    # (bash: sqlite3 "$DB" ".schema" | grep -c "CREATE TABLE").
    con = sqlite3.connect(db)
    table_count = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    con.close()
    if table_count < 20:
        err.append(f"[mini-ork init] ERROR: expected >= 20 tables, found"
                   f" {table_count}. Aborting.")
        return _result(1)
    if table_count < 45:
        out.append(f"[mini-ork init] WARNING: expected >= 45 tables after full apply,"
                   f" found {table_count}. Redesign migrations (0009–0012) may not"
                   f" have been applied yet.")
    out.append(f"[mini-ork init] Done. Tables: {table_count}")
    return _result(0)


def main(argv: list[str] | None = None) -> int:
    """Compatibility CLI behind db/init.sh and native callers."""
    parser = argparse.ArgumentParser(prog="mini_ork.stores.migrate")
    parser.add_argument("--db", default=os.environ.get("MINI_ORK_DB"))
    parser.add_argument("--root", default=os.environ.get("MINI_ORK_ROOT"))
    args = parser.parse_args(argv)
    rc, stdout, stderr = init_db(db=args.db, root=args.root)
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
