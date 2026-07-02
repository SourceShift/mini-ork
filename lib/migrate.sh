#!/usr/bin/env bash
# migrate.sh — versioned, checksummed, transactional DB migrations for mini-ork.
#
# Design: docs/_meta/migration-and-update-design.md. Source this file and call
# mo_migrate_apply / mo_migrate_status / mo_migrate_verify. Requires MINI_ORK_DB.
#
# What this fixes vs the old inline loop in db/init.sh:
#   - REAL sha256 checksums (not the 'runner-applied' placeholder), so an
#     already-applied migration that gets edited is detected as drift.
#   - TRANSACTIONAL apply: each migration + its schema_migrations record commit
#     together, or neither does — a failed migration leaves the DB unchanged
#     instead of half-migrated (which is what pushed people to wipe the DB).
#   - Legacy rows (checksum='runner-applied'/'view-v1'/'recovered-*') are
#     re-hashed to the real value in place, never re-run.

# sha256 of a file, portable across macOS (shasum) and Linux (sha256sum).
mo_migrate_checksum() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# Is this a legacy placeholder rather than a real checksum? A real checksum is
# exactly 64 hex chars (sha256). Everything the old runner wrote — 'v1',
# 'runner-applied', 'phase-a-1', per-migration version tags, empty — contains a
# non-hex char or the wrong length, so this classifies all of them without
# enumerating the (open-ended) set of placeholder strings.
_mo_migrate_is_legacy_checksum() {
  case "$1" in
    *[!0-9a-f]* | "") return 0 ;; # non-hex char or empty → legacy
  esac
  [ "${#1}" -eq 64 ] && return 1 || return 0 # 64 hex → real; otherwise legacy
}

# schema_migrations, backward-compatible with the original (filename, applied_at,
# checksum) table — adds the v2 columns only if missing.
mo_migrate_ensure_table() {
  local db="$1"
  sqlite3 "$db" "CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    checksum   TEXT
  );" 2>/dev/null || return 1
  local cols
  cols=$(sqlite3 "$db" "SELECT ',' || group_concat(name) || ',' FROM pragma_table_info('schema_migrations');" 2>/dev/null)
  case "$cols" in *,mini_ork_version,*) ;; *) sqlite3 "$db" "ALTER TABLE schema_migrations ADD COLUMN mini_ork_version TEXT;" 2>/dev/null || true ;; esac
}

_mo_migrate_version() {
  grep -oE '[0-9]+\.[0-9]+\.[0-9]+' "${MINI_ORK_ROOT:-.}/bin/mini-ork" 2>/dev/null | head -1
}

# Apply ONE migration file and record it, atomically. BEGIN + file + INSERT +
# COMMIT piped to `sqlite3 -bail`: any error stops before COMMIT and the open
# transaction is rolled back when the connection closes. The file is piped raw
# (never through an expanding heredoc) so SQL like json_extract('$.x') is safe.
_mo_migrate_apply_one() {
  local db="$1" file="$2" filename="$3" checksum="$4" ver="$5"
  local record
  # OR REPLACE, because ~half the shipped migrations self-record a placeholder
  # row (checksum='v1'); this overwrites it with the real sha256.
  record="INSERT OR REPLACE INTO schema_migrations(filename, applied_at, checksum, mini_ork_version) VALUES ('$filename', strftime('%Y-%m-%dT%H:%M:%fZ','now'), '$checksum', '$ver');"
  # A top-level `BEGIN;` means the migration already manages its own transaction
  # (many shipped ones do, and some define triggers with their own BEGIN…END;).
  # Run those as-is under -bail (a failure leaves the tx uncommitted → rolled
  # back on close) and record on success. Wrap only BARE migrations so they get
  # the same all-or-nothing guarantee.
  if grep -qiE '^[[:space:]]*begin([[:space:]]+transaction)?[[:space:]]*;' "$file"; then
    sqlite3 -bail "$db" < "$file" || return 1
    sqlite3 "$db" "$record"
  else
    { printf 'BEGIN;\n'; cat "$file"; printf '\n%s\nCOMMIT;\n' "$record"; } | sqlite3 -bail "$db"
  fi
}

# Apply every pending *.sql in $1 (dir) in lex order. $2=1 → dry-run (list only).
# Returns non-zero on a failed migration or on unallowed checksum drift.
mo_migrate_apply() {
  local db="${MINI_ORK_DB:?MINI_ORK_DB required}"
  local dir="${1:?migrations dir required}"
  local dry_run="${2:-0}"
  [ -d "$dir" ] || { echo "[migrate] no such dir: $dir" >&2; return 1; }
  mo_migrate_ensure_table "$db" || { echo "[migrate] cannot init schema_migrations" >&2; return 1; }
  local ver f filename sum applied applied_sum
  ver=$(_mo_migrate_version)
  for f in $(ls "$dir"/*.sql 2>/dev/null | sort); do
    filename=$(basename "$f")
    sum=$(mo_migrate_checksum "$f")
    applied=$(sqlite3 "$db" "SELECT COUNT(*) FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo 0)
    if [ "$applied" = "1" ]; then
      applied_sum=$(sqlite3 "$db" "SELECT COALESCE(checksum,'') FROM schema_migrations WHERE filename='$filename';" 2>/dev/null)
      if [ "$applied_sum" = "$sum" ]; then
        continue
      elif _mo_migrate_is_legacy_checksum "$applied_sum"; then
        [ "$dry_run" = "1" ] || sqlite3 "$db" "UPDATE schema_migrations SET checksum='$sum', mini_ork_version=COALESCE(mini_ork_version,'$ver') WHERE filename='$filename';" 2>/dev/null
        echo "  [rehash]  $filename (legacy checksum → real sha256)"
      elif [ "${MO_MIGRATE_ALLOW_DRIFT:-0}" = "1" ]; then
        echo "  [warn]    $filename checksum drift (allowed by MO_MIGRATE_ALLOW_DRIFT)" >&2
      else
        echo "  [FAIL]    $filename was edited after being applied (checksum drift)." >&2
        echo "            Add a NEW migration instead of editing a shipped one, or set MO_MIGRATE_ALLOW_DRIFT=1." >&2
        return 1
      fi
      continue
    fi
    if [ "$dry_run" = "1" ]; then
      echo "  [pending] $filename"
      continue
    fi
    echo "  [apply]   $filename"
    if _mo_migrate_apply_one "$db" "$f" "$filename" "$sum" "$ver"; then
      echo "  [ok]      $filename"
    else
      echo "  [FAIL]    $filename — rolled back, DB unchanged" >&2
      return 1
    fi
  done
}

# Print a one-line summary + any pending/drifted files.
mo_migrate_status() {
  local db="${MINI_ORK_DB:?}" dir="${1:?}"
  local total pending=0 drifted=0 f filename sum a asum
  total=$(ls "$dir"/*.sql 2>/dev/null | wc -l | tr -d ' ')
  for f in $(ls "$dir"/*.sql 2>/dev/null | sort); do
    filename=$(basename "$f"); sum=$(mo_migrate_checksum "$f")
    a=$(sqlite3 "$db" "SELECT COUNT(*) FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo 0)
    if [ "$a" = "0" ]; then
      pending=$((pending + 1)); echo "  pending: $filename"
    else
      asum=$(sqlite3 "$db" "SELECT COALESCE(checksum,'') FROM schema_migrations WHERE filename='$filename';" 2>/dev/null)
      if [ "$asum" != "$sum" ] && ! _mo_migrate_is_legacy_checksum "$asum"; then
        drifted=$((drifted + 1)); echo "  DRIFTED: $filename"
      fi
    fi
  done
  echo "migrations: $((total - pending)) applied, $pending pending, $drifted drifted (of $total)"
}

# Verify every applied migration's checksum still matches its file. Non-zero on drift.
mo_migrate_verify() {
  local db="${MINI_ORK_DB:?}" dir="${1:?}"
  local rc=0 f filename sum a asum
  for f in $(ls "$dir"/*.sql 2>/dev/null | sort); do
    filename=$(basename "$f"); sum=$(mo_migrate_checksum "$f")
    a=$(sqlite3 "$db" "SELECT COUNT(*) FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo 0)
    [ "$a" = "1" ] || continue
    asum=$(sqlite3 "$db" "SELECT COALESCE(checksum,'') FROM schema_migrations WHERE filename='$filename';" 2>/dev/null)
    if [ "$asum" != "$sum" ] && ! _mo_migrate_is_legacy_checksum "$asum"; then
      echo "  DRIFT: $filename (applied=${asum:0:12} file=${sum:0:12})" >&2; rc=1
    fi
  done
  [ "$rc" = "0" ] && echo "migrate --verify: all applied checksums match" || echo "migrate --verify: drift detected" >&2
  return "$rc"
}
