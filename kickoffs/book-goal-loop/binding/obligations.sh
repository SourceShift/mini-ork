#!/usr/bin/env bash
# obligations-cmd for the goal-loop: declare the duties the TARGET's contract
# imposes that the loop's predicate cannot express.
#
# Contract (recipes/goal-loop/lib/goal_state.py::read_obligations): run via
# shell in MO_GOAL_TARGET_CWD; one `<name>|<declared>|<satisfied>|<detail>` row
# per line on stdout; rc!=0 is recorded as a sensor FAILURE (never as "no
# obligations"). Rows are emitted most-important-first: the driver names the
# FIRST unsatisfied row (loop_state.py::obligation_gap), so order is priority.
#
# This is the axis-gap sensor. The predicate decides on `committed_complete` +
# `rubric_status`; it has no way to express "this book's chapters are supposed
# to carry figures". Without a row here, a book whose every chapter is
# figure-less passes green and nothing says why that is wrong. Seeding the row
# does not teach the loop what a figure is — it declares an obligation the
# target already imposes, and lets the loop notice it is unmet.
#
# Connection comes from libpq env vars (PGHOST/PGPORT/PGUSER/PGDATABASE/
# PGPASSWORD) so no secret lives in this file; launch-armed.sh exports them.
set -euo pipefail

: "${BOOK_UUID:?BOOK_UUID must be set (target book to drive to completion)}"
# Trusted operator env, but keep the interpolation hygienic.
if [[ ! "${BOOK_UUID}" =~ ^[0-9a-fA-F-]{36}$ ]]; then
  echo "BOOK_UUID is not a uuid: ${BOOK_UUID}" >&2
  exit 2
fi

# Same two knobs chapter_quality.py measures with, so the obligation this file
# declares and the floor that check enforces can never disagree.
MIN_TOTAL_CHARS="${MO_GOAL_QUALITY_MIN_TOTAL_CHARS:-4000}"
CHARS_PER_WORD="${MO_GOAL_QUALITY_CHARS_PER_WORD:-6}"
for _n in "${MIN_TOTAL_CHARS}" "${CHARS_PER_WORD}"; do
  if [[ ! "${_n}" =~ ^[0-9]+$ ]]; then
    echo "non-numeric floor knob: ${_n}" >&2
    exit 2
  fi
done

# ROW 1 — figure_requirement.
#   declared  = chapters for the book.
#   satisfied = chapters holding at least one live figure image. A figure is a
#               block with node_type='viz_image', source_type='book_chapter',
#               source_id=<chapter_uuid> (sourceFigureHarvester.ts:262,1840).
#   Both counts come from the same CTE so declared can never drift from the
#   denominator the satisfied count is taken over.
#
# ROW 2 — chapter_length.
#   declared  = chapters for the book.
#   satisfied = chapters whose committed prose clears the floor their OWN plan
#               declared (bg_chapter_plan_spec.budgets.min_words, chars =
#               min_words x CHARS_PER_WORD). When no plan declares one — every
#               book adopted before the gate derived a floor — the operator
#               floor stands in, so the bar degrades exactly to what
#               chapter_quality.py already enforces rather than to nothing.
#   The declaration coverage rides in the detail column: "k/n chapters declare
#   one" is the fact that makes an all-satisfied row readable as "nothing is
#   thin against a bar" rather than "no bar was ever set". Deliberately NOT its
#   own row — an adopted book can never retroactively declare a floor, so a
#   declaration row would be a red the loop could not clear by fixing anything.
#
# ROW 3 — rubric_axis_coverage.
#   declared  = distinct rubric axes the target populates on the chapters'
#               receipts, joined the SAME way the predicate's heal path joins
#               (chapter_number + evaluated_content_hash, scope='chapter').
#   satisfied = 0, by construction: the predicate consumes `judge_geval_passed`
#               and none of `axis_scores`. This row states the unread surface
#               rather than measuring a moving quantity — it is honest that it
#               never moves, and it sorts last so row 1 stays the headline.
psql -h "${PGHOST:-REDACTED-INTERNAL-IP}" -p "${PGPORT:-5932}" \
     -U "${PGUSER:-researcher_user}" -d "${PGDATABASE:-researcher_db}" \
     -tA -F '|' -c "
WITH lc AS (
  SELECT chapter_number, chapter_uuid, content_hash
  FROM book_chapter_lifecycle
  WHERE book_uuid='${BOOK_UUID}'
),
fig AS (
  SELECT count(*) AS declared,
         count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM blocks b
           WHERE b.source_id = lc.chapter_uuid::text
             AND b.node_type = 'viz_image'
             AND b.source_type = 'book_chapter'
             AND b.is_deleted IS NOT TRUE
         )) AS satisfied
  FROM lc
),
ax AS (
  SELECT count(DISTINCT k) AS declared
  FROM lc
  JOIN book_rubric_results r
    ON r.chapter_number = lc.chapter_number
   AND r.evaluated_content_hash = lc.content_hash
   AND r.scope = 'chapter'
  CROSS JOIN LATERAL jsonb_object_keys(r.axis_scores) k
),
-- Prose committed per chapter, beside the floor the chapter's own newest spec
-- declares. Both subqueries are correlated on the current lc row, so `declared`
-- can never drift from the denominator `satisfied` is taken over.
lens AS (
  SELECT
    (SELECT coalesce(sum(coalesce(s.content_length, length(s.content_markdown), 0)), 0)
       FROM book_chapter_sections s
      WHERE s.book_uuid = '${BOOK_UUID}'
        AND s.chapter_number = lc.chapter_number
        AND s.is_latest) AS chars,
    (SELECT (sp.budgets->>'min_words')::int
       FROM bg_chapter_plan_spec sp
       JOIN books b ON b.id = sp.book_id
      WHERE b.document_uuid = '${BOOK_UUID}'
        AND sp.chapter_number = lc.chapter_number
      ORDER BY sp.created_at DESC, sp.plan_hash DESC LIMIT 1) AS min_words
  FROM lc
)
SELECT 'figure_requirement', fig.declared, fig.satisfied,
       'chapters whose live viz_image forest is non-empty'
FROM fig
UNION ALL
SELECT 'chapter_length', count(*),
       count(*) FILTER (
         WHERE chars >= coalesce(min_words * ${CHARS_PER_WORD}, ${MIN_TOTAL_CHARS})
       ),
       'chapters clearing their plan-declared floor ('
         || count(*) FILTER (WHERE min_words IS NOT NULL) || '/' || count(*)
         || ' chapters declare one)'
FROM lens
UNION ALL
SELECT 'rubric_axis_coverage', COALESCE(ax.declared, 0), 0,
       'rubric axes on the receipt; the predicate reads judge_geval_passed only'
FROM ax;"
