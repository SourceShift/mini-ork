#!/usr/bin/env bash
# units-cmd for the PERF goal-loop: emit one chapter_number per line for BOOK_UUID.
#
# Contract (recipes/goal-loop/lib/goal_state.py::list_units): run via shell in
# MO_GOAL_TARGET_CWD, one unit id per line, rc!=0 aborts the wave. Connection
# comes from libpq env vars (PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD) so no
# secret lives in this file; the launcher exports them.
set -euo pipefail

: "${BOOK_UUID:?BOOK_UUID must be set (target book to drive to completion)}"
# Trusted operator env, but keep the interpolation hygienic.
if [[ ! "${BOOK_UUID}" =~ ^[0-9a-fA-F-]{36}$ ]]; then
  echo "BOOK_UUID is not a uuid: ${BOOK_UUID}" >&2
  exit 2
fi

psql -h "${PGHOST:-100.74.239.22}" -p "${PGPORT:-5932}" \
     -U "${PGUSER:-researcher_user}" -d "${PGDATABASE:-researcher_db}" \
     -tA -c "SELECT chapter_number
             FROM book_chapter_lifecycle
             WHERE book_uuid='${BOOK_UUID}'
             ORDER BY chapter_number;"
