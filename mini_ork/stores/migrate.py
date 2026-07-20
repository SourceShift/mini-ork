"""Python port of lib/migrate.sh — versioned, checksummed, transactional DB
migrations for mini-ork.

Strangler-fig parity port:

    checksum(path)                       -> sha256 hex of a file
    is_legacy_checksum(s)                -> True if s is a placeholder (not 64-hex)
    ensure_table(db)                     -> create/upgrade schema_migrations
    migrate_apply(dir, dry_run, db, root)-> apply pending *.sql (lex order)
    migrate_status(dir, db)              -> summary + pending/drifted lines
    migrate_verify(dir, db)              -> 0 if all applied checksums match, else 1

Each apply + its schema_migrations record commit together (all-or-nothing), and
already-applied migrations with a legacy placeholder checksum are re-hashed in
place, never re-run — mirroring the bash byte-for-byte (applied_at timestamps
excepted, as bash uses strftime('now')).
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path


def checksum(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_legacy_checksum(s: str) -> bool:
    """True when s is NOT a real sha256 (non-hex char, empty, or wrong length)."""
    if not s or re.search(r"[^0-9a-f]", s):
        return True
    return len(s) != 64


def _db(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if not env:
        raise RuntimeError("MINI_ORK_DB required")
    return env


def ensure_table(db: str) -> None:
    con = sqlite3.connect(db)
    con.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            checksum   TEXT
        )
    """)
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


def _apply_one(db: str, file: str, filename: str, checksum_hex: str, ver: str) -> bool:
    sql = Path(file).read_text()
    record = _RECORD.format(fn=filename, sum=checksum_hex, ver=ver)
    con = sqlite3.connect(db)
    con.isolation_level = None  # manual transaction control, mirroring sqlite3 -bail
    try:
        if _BEGIN_RE.search(sql):
            # migration manages its own transaction; run as-is then record
            con.executescript(sql)
            con.execute(record)
        else:
            con.executescript("BEGIN;\n" + sql + "\n" + record + "\nCOMMIT;")
        con.close()
        return True
    except sqlite3.Error:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        con.close()
        return False


def migrate_apply(migrations_dir: str, dry_run: bool = False, db: str | None = None,
                  root: str | None = None) -> tuple[int, list[str]]:
    """Apply pending *.sql in lex order. Returns (rc, output_lines).

    rc is 0 on success, 1 on a failed migration or disallowed checksum drift.
    output_lines are the ``  [apply] ...`` style stdout lines (stderr [FAIL]/
    [warn] omitted from the list but reflected in rc), matching bash order.
    """
    db = _db(db)
    out: list[str] = []
    if not os.path.isdir(migrations_dir):
        return 1, out
    ensure_table(db)
    ver = _version(root)
    con = sqlite3.connect(db)
    for f in sorted(Path(migrations_dir).glob("*.sql")):
        filename = f.name
        sum_hex = checksum(f)
        row = con.execute(
            "SELECT COALESCE(checksum,'') FROM schema_migrations WHERE filename=?",
            (filename,)).fetchone()
        applied = row is not None
        if applied:
            applied_sum = row[0]
            if applied_sum == sum_hex:
                continue
            elif is_legacy_checksum(applied_sum):
                if not dry_run:
                    con.execute(
                        "UPDATE schema_migrations SET checksum=?, "
                        "mini_ork_version=COALESCE(mini_ork_version,?) WHERE filename=?",
                        (sum_hex, ver, filename))
                    con.commit()
                out.append(f"  [rehash]  {filename} (legacy checksum → real sha256)")
            elif os.environ.get("MO_MIGRATE_ALLOW_DRIFT", "0") == "1":
                pass  # [warn] to stderr in bash
            else:
                con.close()
                return 1, out
            continue
        if dry_run:
            out.append(f"  [pending] {filename}")
            continue
        out.append(f"  [apply]   {filename}")
        if _apply_one(db, str(f), filename, sum_hex, ver):
            out.append(f"  [ok]      {filename}")
        else:
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
            if row[0] != checksum(f) and not is_legacy_checksum(row[0]):
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
        if row[0] != checksum(f) and not is_legacy_checksum(row[0]):
            rc = 1
    con.close()
    return rc
