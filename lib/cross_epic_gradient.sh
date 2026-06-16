#!/usr/bin/env bash
# cross_epic_gradient.sh — fan-out gradient transfer across task_classes.
#
# Default learning loops scope gradients per task_class (so HOT-1's lesson
# stays on HOT-1). When a SAME signal recurs in ≥N distinct task_classes with
# confidence ≥ THRESHOLD, the lesson is generalizable. We promote it to a
# pseudo task_class '__cross_class__'; the context_assembler then injects it
# into EVERY task's failure_modes.
#
# Public API:
#   cross_epic_gradient_promote [--min-classes N] [--min-confidence X]
#                               [--window EPOCH_OR_DURATION]
#
# Idempotence: deterministic gradient_id derived from sha256(signal[:200]) so
# repeated promotion just upserts.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

cross_epic_gradient_promote() {
  local min_classes=2
  local min_confidence=0.7
  local window="14d"
  while [ $# -gt 0 ]; do
    case "$1" in
      --min-classes)    min_classes="$2"; shift 2 ;;
      --min-confidence) min_confidence="$2"; shift 2 ;;
      --window)         window="$2"; shift 2 ;;
      *) shift ;;
    esac
  done

  python3 - "$STATE_DB" "$min_classes" "$min_confidence" "$window" <<'PY'
import re, sqlite3, sys, hashlib, time

db, min_classes, min_conf, window = sys.argv[1:5]
min_classes = int(min_classes)
min_conf = float(min_conf)

m = re.match(r"^(\d+)([dh])$", window.strip())
secs = (int(m.group(1)) * (86400 if m.group(2) == "d" else 3600)) if m else 14 * 86400
since = int(time.time()) - secs

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")

# Cluster cross-class lessons by TARGET (e.g. 'agent.reviewer.prompt',
# 'verifier.lens-exists', 'workflow.recipe.framework_edit'). Signal text is
# observation-specific per-incident prose — exact-match across task_classes
# yields ~0 hits — but a recurring TARGET across multiple classes IS the
# meaningful "this prompt/verifier/edge keeps misbehaving system-wide" signal.
# Two-pass: first find recurring targets, then pick highest-confidence exemplar.
recurring = con.execute(f"""
    SELECT target,
           GROUP_CONCAT(DISTINCT task_class) AS classes,
           COUNT(DISTINCT task_class)        AS n_classes
      FROM gradient_records
     WHERE created_at >= {since}
       AND task_class NOT IN ('', '__cross_class__')
       AND task_class IS NOT NULL
       AND confidence >= ?
       AND target IS NOT NULL AND target != ''
     GROUP BY target
    HAVING n_classes >= ?
""", (min_conf, min_classes)).fetchall()

rows = []
for target, classes, n_classes in recurring:
    exemplar = con.execute("""
        SELECT confidence, signal, suggested_change, evidence
          FROM gradient_records
         WHERE target = ?
           AND task_class NOT IN ('', '__cross_class__')
           AND task_class IS NOT NULL
         ORDER BY confidence DESC
         LIMIT 1
    """, (target,)).fetchone()
    if exemplar:
        top_conf, top_signal, top_change, top_evidence = exemplar
        rows.append((target, classes, n_classes, top_conf, top_signal, top_change, top_evidence))

promoted = 0
for target, classes, n_classes, top_conf, top_signal, top_change, top_evidence in rows:
    if not target:
        continue
    # Deterministic gradient_id derived from target so re-promotion upserts.
    gid = "gr-cx-" + hashlib.sha256(target.encode()).hexdigest()[:12]
    # Annotate the existing target with cross-class scope so the assembler
    # can render it differently in failure_modes pane.
    cx_target = f"cross_class:{target}"
    sig = top_signal or f"Target {target} flagged across {n_classes} task_classes: {classes}"
    suggested = top_change or f"Lesson fanned out from {n_classes} task_classes: {classes}"
    evidence = top_evidence or ""
    exists = con.execute(
        "SELECT 1 FROM gradient_records WHERE gradient_id=? LIMIT 1", (gid,)
    ).fetchone()
    if exists:
        con.execute("""
            UPDATE gradient_records
               SET confidence=?, suggested_change=?, signal=?,
                   evidence=?, target=?, task_class='__cross_class__'
             WHERE gradient_id=?
        """, (top_conf, suggested[:1000], sig[:500],
              evidence[:1000], cx_target, gid))
    else:
        con.execute("""
            INSERT INTO gradient_records
                (gradient_id, target, signal, suggested_change,
                 evidence, confidence, task_class, created_at)
            VALUES (?,?,?,?,?,?, '__cross_class__', ?)
        """, (gid, cx_target, sig[:500], suggested[:1000],
              evidence[:1000], top_conf, int(time.time())))
        promoted += 1

con.commit()
con.close()
print(promoted)
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "cross_epic_gradient.sh — source me and call cross_epic_gradient_promote"
fi
