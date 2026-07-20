# Operator Steering — supervisor-driven mid-run guidance

Mini-ork's recursive learning channels (prior runs, failure modes,
gradients) feed knowledge from PAST runs into the next planner. The
operator-steering channel does the same job for the CURRENT run — an
external supervisor (chat bot, dashboard, human operator) can inject
guidance between nodes, and the next node sees it in its context pack.

## When to use it

- Mid-run direction change when an observer (often a chat agent watching
  the event hook stream) spots an issue the worker won't catch — e.g.
  "the codebase has a hook blocking project-wide tsc, use
  `pnpm type-check:touched <files>` instead".
- Targeted reviewer guidance — "this PR's load-bearing risk is the
  migration, not the route; weight DB tests above unit tests".
- Recovery hints after a failed iter — "don't re-attempt the regex
  approach; try a parser instead".
- Cross-run lessons too specific to the current task to deserve a
  gradient promotion.

## API

### CLI

```bash
mini-ork inject \
  --run-id <run-id> \
  --role planner|implementer|reviewer|verifier|any \
  --message "<text>" \
  [--severity info|warn|critical]    # default: info
  [--source <free-form>]             # default: "operator-cli"
  [--confidence 0.0-1.0]             # default: 0.8
  [--ttl-secs <int>]                 # default: 3600 (1h)
```

Omit `--run-id` to land the message in the global queue — the next
planner of any run will pick it up. Use it for cross-run policy notes
(e.g. "for the next 3 hours, prefer Sonnet over Opus in this codebase").

### From your bridge / Monitor handler

After ingesting a `node_end` event from `MINI_ORK_ON_EVENT`, decide
whether you want to steer the next node and call inject:

```bash
# Watching the JSONL event stream
tail -F ~/.mini-ork-events.jsonl | while read -r evt; do
  case "$(jq -r .event <<<"$evt")" in
    node_end)
      node="$(jq -r '.payload.node_id' <<<"$evt")"
      run="$(jq -r '.run' <<<"$evt")"
      # Custom logic — read the run's transcript, decide whether to inject
      if grep -q "FAIL.*typecheck" "$RUNS_DIR/$run/execute.log"; then
        mini-ork inject \
          --run-id "$run" \
          --role implementer \
          --severity critical \
          --message "Project tsc is blocked here; use pnpm type-check:touched <files>"
      fi
      ;;
  esac
done
```

## What the worker agent sees

When the native execute runtime builds the prompt for an `implementer` /
`reviewer` / `researcher` node, `context_assembler` reads unconsumed
operator-steering rows targeting `<run_id, role>` (or the wildcard
`any`) and prepends a block to the prompt:

```
--- Operator steering (injected supervisor guidance) ---
2 message(s) targeted at this node. Treat as load-bearing:
- [CRITICAL] (from claude-supervisor) Use pnpm type-check:touched not project tsc
- [WARN] (from dashboard:ops) Reviewer must weight integration tests > unit tests
--- /operator steering ---
```

Rows are marked consumed when read, so the agent never sees the same
steering twice in one dispatch. Subsequent nodes get a fresh set.

## Severity ranking

Within a single fetch, rows are ordered:

1. **`critical`** — supervisor flags a load-bearing concern the worker MUST address
2. **`warn`** — strong guidance; worker SHOULD respect unless evidence contradicts
3. **`info`** — context / preference; worker MAY consider

Within the same severity, rows are ranked by `confidence` (0.0-1.0),
then by `created_at` (newest first). Up to 10 rows are surfaced per
node — beyond that, raise severity / confidence to bubble what matters.

## TTL + cleanup

Rows expire on `expires_at` (default 1 hour after emit). Expired rows
stay in the table as audit trail but are skipped by
`context_assembler`. Periodic cleanup is optional:

```sql
DELETE FROM operator_steering WHERE expires_at < strftime('%s','now')*1000 - 86400000;
```

## Composability

- **Pairs with `MINI_ORK_ON_EVENT` push hook** (see `docs/EVENT-HOOKS.md`)
  to close the supervisor loop: events arrive → handler decides →
  injects steering → next node consumes.
- **Pairs with `bug_reports` / `gradient_records`** — operator-steering
  is for CURRENT-run targeted guidance; bug reports + gradients carry
  CROSS-run durable lessons. The same observation may justify both:
  inject steering NOW so the in-flight run benefits, AND promote a
  gradient so the next planner of this task class learns the lesson
  durably.
- **Honors the recipe contract** — context_assembler runs the same
  consume-and-prepend path regardless of recipe. No recipe-side change
  required.

## Inspecting the queue

```bash
sqlite3 .mini-ork/state.db \
  "SELECT id, run_id, role_target, severity,
          substr(message,1,80), source, consumed_at IS NULL AS open
     FROM operator_steering
    ORDER BY created_at DESC LIMIT 20;"
```

## Composition with the four recursive-learning channels

| Channel | Cadence | Scope | Push or pull |
|---|---|---|---|
| Task-class memory (D1-D5) | every node | per task_class | pulled by next planner |
| Cross-class gradients (E7) | every reflect | recipe-wide | pulled by every node |
| Pattern emergence (D1 patch #5) | every reflect | task_class + status | pulled by promotion gate |
| Bug-report channel | every node + reflect | per agent role | pulled by next planner |
| **Operator steering (this PR)** | **on-demand** | **per run + role** | **pulled by next node** |

The first four channels are agent-emitted; operator steering is the
fifth and it's external-supervisor-emitted. Together they form a
bidirectional learning system: the agents learn from the supervisor,
and the supervisor's notes survive into cross-run gradients when the
same advice keeps landing.
