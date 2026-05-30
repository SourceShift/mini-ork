#!/usr/bin/env bash
# mini-ork demo — exercises the universal task loop end-to-end in a throwaway
# project, using dry-run mode (no LLM calls, no API keys required).
#
# What this proves:
#   1. `mini-ork init` bootstraps a clean .mini-ork/ directory with state.db
#      seeded across 13 migrations (72 tables).
#   2. `mini-ork run code-fix <kickoff>` walks classify → plan → execute → verify.
#   3. The task_runs row records: task_class=code-fix, recipe=code-fix, status
#      transitions across the loop.
#
# Usage:
#   bash examples/00-demo.sh            # uses default MINI_ORK_ROOT
#   MINI_ORK_DRY_RUN=0 ./examples/00-demo.sh   # real LLM calls (needs claude CLI)
set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN="${MINI_ORK_DRY_RUN:-1}"

# ── isolated demo project ─────────────────────────────────────────────────────
DEMO_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t ork-demo)
trap 'rm -rf "$DEMO_DIR"' EXIT
cd "$DEMO_DIR"
git init -q
export MINI_ORK_HOME="$DEMO_DIR/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

echo "==> mini-ork demo (dry-run=${MINI_ORK_DRY_RUN})"
echo "    project: $DEMO_DIR"
echo "    home:    $MINI_ORK_HOME"
echo ""

# ── 1. bootstrap ──────────────────────────────────────────────────────────────
echo "── 1. mini-ork init ────────────────────────────────────────────────"
mini-ork init >/dev/null
echo "  [OK] state.db tables: $(sqlite3 "$MINI_ORK_DB" .schema | grep -c 'CREATE TABLE')"
echo "  [OK] task_classes seeded: $(ls "$MINI_ORK_HOME/config/task_classes/" | wc -l | tr -d ' ')"
echo ""

# ── 2. doctor ─────────────────────────────────────────────────────────────────
echo "── 2. mini-ork doctor ──────────────────────────────────────────────"
mini-ork doctor | sed 's/^/  /'
echo ""

# ── 3. write synthetic kickoff ────────────────────────────────────────────────
echo "── 3. synthesize kickoff ───────────────────────────────────────────"
cat > kickoff.md <<'EOF'
# Fix off-by-one bug in tally.js

## Problem
`computeTotal()` in tally.js line 42 returns one less than expected.

## Definition of Done
- Bug fixed; npm test passes.

## Scope
- ONLY tally.js may be edited.
EOF
echo "  [OK] kickoff.md written ($(wc -l < kickoff.md | tr -d ' ') lines)"
echo ""

# ── 4. run the universal loop ─────────────────────────────────────────────────
echo "── 4. mini-ork run code-fix kickoff.md ─────────────────────────────"
mini-ork run code-fix "$DEMO_DIR/kickoff.md" 2>&1 | sed 's/^/  /' || true
echo ""

# ── 5. inspect task_runs ──────────────────────────────────────────────────────
echo "── 5. inspect task_runs ────────────────────────────────────────────"
sqlite3 -header "$MINI_ORK_DB" \
  "SELECT id, task_class, recipe, status, verdict FROM task_runs;" \
  | sed 's/^/  /'
echo ""

echo "==> demo complete. Set MINI_ORK_DRY_RUN=0 to invoke real LLM calls."
