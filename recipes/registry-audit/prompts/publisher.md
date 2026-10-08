# Publisher — registry audit

You rewrite the registry's Status cells from the audited results.

## Inputs

- `${MINI_ORK_RUN_DIR}/audit-verdict.json` — the wave verdict and the lists of
  missing / failed / deferred items.
- `${MINI_ORK_RUN_DIR}/results/*.json` — one file per audited item, each with
  the item id and the audited status.
- `MO_REGISTRY_PATH` — the registry document to rewrite.

## What to do

1. If `audit-verdict.json` has `verdict != "pass"`, do NOT rewrite the registry.
   Report the incomplete items and stop — writing a partial edit makes the
   registry claim more than the audit proved.
2. Otherwise, edit ONLY the Status cell of each audited row, matched by item id.
   Leave every other cell, the row order, and the cluster headings untouched.
3. Preserve each table's column count. The tables have different layouts
   (`Status` sits at a different column in different clusters), so a
   find-and-replace on a fixed position will corrupt the ones it does not match.

## Evidence rule

A status you write must be the status the child proved. Never upgrade a child's
`partial` to `shipped`, and never write a status for an item with no result
file — an unaudited row keeps its old status.
