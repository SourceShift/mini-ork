# P2 — Artifact transport for cloud runs (isolated agent nodes)

## Context

mini-ork's remote backend already runs a node on another machine and carries
its file writes back. Verified surfaces:

- `mini_ork/remote/tree_sync.py` — `snapshot` → `initial_bundle`
  (full/branch/squashed ladder under `MO_REMOTE_BUNDLE_MAX_MB`) →
  `materialize`; back out via `incremental_bundle` + `apply_delta`
  (`tree_sync.py:267`), which applies the remote node's edits to the local
  worktree as a **binary git diff**, guarding divergence with
  `SyncConflictError` (`tree_sync.py:53`).
- `mini_ork/runtime/backends/remote.py` — `RemoteWorkspace` dispatches to a
  node-agent over HTTP; `MO_SHARED_DRIVE_ROOT` is already a knob
  (`remote.py:1758`).
- `mini_ork/context_v2.py:parse_contract` reads a kickoff's declared scope.

**The failure this epic fixes.** A kickoff that says "write the report to
`docs/research/<dir>/`" is a *path* requirement on **every** node. When a cloud
run spawns each agent node on a **separate isolated machine**, no two nodes
share a disk, so a path requirement is unsatisfiable. Evidence: the survey run
`run-1791749486-16681` (recipe `audit-judge-panel-3l`) required
`docs/research/20261012-langgraph-parity-survey/`; it died (pid gone) and that
directory **was never created** — reports stranded in the run dir, a `rollback`
node ran, nothing published.

**The LangGraph mechanism.** LangGraph does not give nodes a shared filesystem.
Nodes return partial **state**; a channel/reducer merges it into the thread's
state; a **checkpointer** persists that state per superstep, addressed by
`thread_id`/`checkpoint_id`, in a backend every worker can reach. `Send`/
map-reduce fans work to parallel workers whose results merge into the same
checkpointed state. The transport is the *store*, not the disk.

**The P1 half is in flight.** Run `run-1791750091-88029` (recipe
`spec-driven-delivery`, live) builds `mini_ork/checkpoints/` + a state.db
migration whose `p1-2` spec clause F2 mandates per-node completion records —
`node_id, status, artifact paths + sha256, rc, timestamps` — queryable by
`run_id` and step (`run_id` = thread, step = checkpoint id). That is the LangGraph
**index**. It stores **paths, not content**, and its plan never mentions cloud or
remote — so it is the manifest half, not the transport half. **Do not
re-implement P1; build on it.**

## Goal

Design the artifact-transport model that makes a mini-ork run correct when its
agent nodes run on isolated machines, and deliver it as a spec the ordinary
build pipeline can execute:

1. **Artifact record schema extension.** Replace a local `path` as the artifact
   identity with a content-addressed record —
   `{run_id, artifact_id, sha256, media_type, blob_ref}` — where `blob_ref` is a
   location any machine can resolve (shared drive root, object store, or the
   run's durable store). `sha256` is already computed by mini-ork; the `path`
   field in the P1 record becomes a *materialization target on the publishing
   machine only*, never a per-node requirement.
2. **Fan-out artifact slots.** `tree_sync.apply_delta` is **whole-tree and
   single-writer**: N parallel nodes each writing one report to the same tree
   would diverge from base → `SyncConflictError`. Give each fan-out item its own
   **artifact slot** (the Send/map-reduce analogue) so parallel writers never
   collide on one tree, and the aggregator reads N slots, not one directory.

## Non-goals

- Do not implement, change, or re-scope P1 (condition edges, checkpointer,
  resume CLI, retry policy). Depend on it.
- Do not change tree_sync's git-bundle mechanics; only add per-item slots above
  it.
- No new runtime dependency. Stdlib + PyYAML + jsonschema only. A zero-dep
  alternative (reuse `sha256` + `blob_ref` into an existing store) must be
  preferred and stated.
- Do not touch `docs/research/` (owned by other runs).

## Deliverables

- **D1** — `docs/architecture/artifact-transport.md`: the model (index vs
  transport vs materialize), the failure taxonomy of today's path contract, and
  the LangGraph mapping. Every claim about this repo cites `file:line`.
- **D2** — the **schema delta**: exact fields added to the artifact record
  (P1 `p1-2` shape) and to `schemas/artifact_contract.schema.json` (`outputs`
  entries gain `blob_ref`/`sha256`/`slot`), with a migration/back-compat note.
- **D3** — the **fan-out slot change**: the yaml/CLI shape (a node declares
  `outputs: [{name, kind, slot}]`; the aggregator reads slots by `run_id`), the
  `mini_ork/orchestration/item_fanout.py` touch-point, and how a slot is
  resolved on the publishing machine.
- **D4** — a verification plan: the probe commands that would prove (a) two
  parallel nodes with distinct slots do not conflict, (b) a node's bytes are
  resolvable from a second machine by `run_id` + `artifact_id`.

## Files in scope (candidates — confirm)

- `mini_ork/orchestration/item_fanout.py`
- `mini_ork/workflow/artifacts.py`
- `mini_ork/gates/artifact_contract.py`
- `mini_ork/remote/tree_sync.py`
- `mini_ork/runtime/backends/remote.py`
- `mini_ork/checkpoints/` (from P1 — read, do not edit its semantics)
- `schemas/artifact_contract.schema.json`
- `docs/architecture/artifact-transport.md` (new)
- `docs/architecture/artifact-graph.md`

## Out of scope

Any change to P1's checkpointer/resume semantics; any network/object-store code
(a design may *name* the store, it must not add an SDK); the IDE UI.

## Acceptance

- **AC1**: `docs/architecture/artifact-transport.md` exists and answers, with
  `file:line` citations, why a path contract fails under node isolation and how
  `{run_id, artifact_id, sha256, blob_ref}` + per-item slots fix it.
- **AC2**: the schema delta is stated as a concrete diff against the P1 artifact
  record and `schemas/artifact_contract.schema.json`, with a back-compat rule
  for existing recipes (a path-only artifact stays valid).
- **AC3**: the fan-out slot change names its touch-point files and shows the
  yaml shape for a 3-lens fan-out plus the aggregator's read path.
- **AC4**: the verification plan lists ≥ 2 probe commands, each runnable, that
  would distinguish the current model from the proposed one.

## Verification command

`bash -c 'test -f docs/architecture/artifact-transport.md && grep -q blob_ref docs/architecture/artifact-transport.md && grep -q artifact_id docs/architecture/artifact-transport.md && grep -q slot docs/architecture/artifact-transport.md'`
