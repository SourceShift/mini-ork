# Epic: build a `db-migration-audit` recipe

We need a new mini-ork recipe that takes a database-migration plan
(SQL file + intended target schema) and produces an audit report covering:

- **Backward compatibility** — does the migration break any existing reader?
- **Lock impact** — at production row counts, would this migration take a
  table lock long enough to page someone?
- **Reversibility** — can we generate an automatic rollback script?
- **Data integrity** — for any data-rewriting migration, is the transform
  expressible as a query the operator can verify against a snapshot?

## Inputs the recipe will receive

- A `.sql` file (the migration)
- An optional schema snapshot (the current target table's `\d+`-style description)
- Table row-count context (operator-supplied)

## Success criteria

- An audit report `db-migration-audit.md` with 4 sections matching the
  axes above, each with a verdict ({pass, warn, fail}) and ≥1 file:line
  anchor into the SQL file
- A machine-readable `db-migration-audit-verdict.json` with the per-axis
  pass/warn/fail
- A proposed rollback `.sql` (best-effort — verifier flags when not
  generable)

## Heterogeneity expectation

This is a high-risk operation (production DB) so we want the strongest
heterogeneous coverage:
- A "DBA stance" researcher (concurrency + lock + io)
- A "developer stance" researcher (call sites + ORM mappings)
- A "compliance stance" researcher (audit trail + retention)
- A reviewer that produces the final verdict + the rollback script

Pick 3+ distinct model families across these stances.

## Verification command (HOW success is proved)

```bash
bash recipes/db-migration-audit/verifiers/recipe-validator.sh
```

The generated recipe's own `verifier_smith` populates this verifier with
structural + heterogeneity-floor checks. The recipe-creator's own
`recipe-validator.sh` is the meta-validator that fires before publish.
No external test suite required.

## Out of scope

- Executing the migration (read-only audit)
- Sampling production data (operator must provide row counts; we don't connect)
- ORM-specific patches (the implementer node can suggest but not write)

## Output

A complete `recipes/db-migration-audit/` directory under the canonical
repo path, committed by the publisher under `mini-ork@local` identity.
