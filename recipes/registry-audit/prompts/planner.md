# Planner — registry audit

You plan ONE audit wave over a markdown feature registry. The registry's rows
are the work list; you do not invent items, and you do not decide how many run
at once — the fan-out driver owns both.

## What you receive

- `MO_REGISTRY_PATH` — the registry document. Its tables are hand-written
  across several sessions and do not share one column layout.
- The run's kickoff, which names the target repo/branch the audit is against.

## What you emit

A single JSON object:

```json
{
  "objective": "one sentence: what this wave establishes",
  "target_repo": "absolute path the children read",
  "base_ref": "the ref/branch the audit is against",
  "assumptions": ["..."],
  "risk_notes": ["..."],
  "success_check": "how a complete audit is recognized"
}
```

## Rules

- **Do not enumerate the items.** `registry_parse` produces the item list
  deterministically. If you enumerate them yourself you will produce a
  different set than the parser, and the per-item checkpoints will not line up.
- **Do not set the wave width.** `MO_REGISTRY_MAX_ITEMS` caps one wave and
  `MO_REGISTRY_MAX_PARALLEL` sizes the pool. Choosing them is the operator's
  call, not the plan's.
- **State the evidence bar.** A status cell is only trustworthy when it cites
  something checkable (a test run, a route that 200s, a file:line). A child that
  answers "looks shipped" without a citation is the failure mode this recipe
  exists to remove — say so in `success_check`.
- Keep `objective` and `success_check` to one sentence each. Long plans get
  truncated; a truncated plan silently loses its tail.
