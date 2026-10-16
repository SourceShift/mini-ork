# Per-Node Sandboxes

Opt-in isolation: one cloud sandbox per **workflow node**, instead of one per run.

**N, or N+1?** `MO_NODE_ISOLATION=1` on an N-node run boots **N node sandboxes
plus one run-level sandbox — N+1, not N.** The extra one is not a leak. A
process that belongs to no single node (session provisioning before the first
node, and anything happening *between* nodes) has no node to be named after, so
it stays on the run-level sandbox rather than being stranded with nowhere to run
— `run_nodes.isolated_node` returns `""` for it, deliberately. Count by the
`mo.node` label: the N node sandboxes each carry their own, the run-level one
carries `mo.node=None`.

Set `MO_NODE_ISOLATION=1` to turn it on. Absent or unreadable, nothing changes —
the run keeps its single sandbox, byte-identical to before this feature existed.

## Context

`--placement remote` boots a cloud sandbox that hosts `mini-ork node-agent`, and
every dispatch in the run goes to it. That sandbox is keyed on the **run**:
`acquire_run_node` (`mini_ork/remote/providers/base.py:306`) takes a run-scoped
flock, writes `<run_dir>/.node-provider.json`, and returns the one endpoint that
every later dispatch re-attaches to.

That is the right shape for cost and for latency — a boot costs seconds and a
round trip, so paying it once per run rather than once per node is what makes a
multi-node recipe affordable. It is the wrong shape for **isolation**: a
reviewer node and the implementer it reviews share a machine, a process tree and
a filesystem, and the reviewer's container holds whatever the implementer's
container held when it exited. Secrets are also scoped per lane
(`mini_ork/remote/secrets_scope.py`), so two lanes' keys are present in the same
place at different times; a node that can read the sandbox's disk after its
sibling ran is not a node that was confined to its own lane's secrets.

`MO_NODE_ISOLATION=1` addresses exactly that: each node gets its own machine.

## What changes, and what does not

```
BEFORE (one sandbox per run)          AFTER MO_NODE_ISOLATION=1
                                      
  control plane (local)                 control plane (local)
  ├── plan, state.db, UI, git           ├── plan, state.db, UI, git
  └── sandbox "mo-<run>"                ├── sandbox "mo-<run>--implementer-1-3f2a1b"
      ├── implementer node              │   └── implementer node
      └── reviewer node                 └── sandbox "mo-<run>--reviewer-a91c0d22"
                                            └── reviewer node
```

Unchanged: the control plane. The plan, the run dir, `state.db`, the UI, git and
the publisher all stay local, and the run-level record
(`<run_dir>/.node-provider.json`) keeps its name and its meaning.

**The local worktree is the handoff between nodes.** A node's replica syncs down
into the local worktree at teardown (`RemoteWorkspace._sync_down`) and the next
node syncs up from it (`RemoteWorkspace._sync_up`). A *sequential* recipe
therefore needs no cloud filesystem to pass work from one node to the next — the
handoff crosses sandboxes through the control plane, which is already the only
writer to the worktree.

**Parallel fan-out is a different problem.** Two nodes writing the same path at
the same time still have nowhere to put the conflict; that is what
`docs/architecture/artifact-transport.md` (content-addressed artifact transport)
is for. Per-node isolation does not depend on it, and does not deliver it: turn
this on for a sequential recipe and fan-out remains the pre-existing limitation.

## The lever is the session key, not the resolution call

This is the part that is easy to get wrong, so it is worth stating plainly.

Every dispatch inside a run gets its workspace from `get_run_session`
(`mini_ork/runtime/workspace_session.py`), which caches **one** `Workspace` per
`(run_id, backend)` for the whole run and calls `up()` exactly once — deliberately,
because a remote boot is expensive and a 2-lane fallback chain would otherwise
burn two containers.

The consequence: a per-node `NodeProvider.create` is **not sufficient**. The
first node resolves its sandbox, and every sibling reuses it through the cached
session without ever reaching the provider again. Hooking only the node
resolution would look correct in a unit test of the provider and deliver a
shared sandbox in a real run.

So the registry key gains the node:

```python
key = (run_id, backend, node_id)   # node_id is "" unless isolation is on
```

One key, and a node's workspace is genuinely its own: its own `up()`, its own
sandbox, its own secrets scope. Isolation-off keeps `""` and therefore the exact
historical key and behavior.

Both halves of the policy read the same helper — `run_nodes.isolated_node(env)`
— from both the session registry and `RemoteWorkspace.ensure_node`, so the layer
that *caches* the workspace and the layer that *resolves* the node can never
disagree about whether two nodes share a machine.

## Record layout

The run-level record is untouched. A node's record goes in its own directory:

```
<run_dir>/
  .node-provider.json                      # the run's sandbox (unchanged)
  .node-provider.lock
  .nodes/
    implementer_1/
      .node-provider.json                  # this node's sandbox
      .node-provider.lock
    reviewer_1/
      .node-provider.json
```

That is the whole trick: the lock and record helpers in `base.py` are already
**path**-parameterised, so handing them a per-node directory makes them per-node
with no new locking code to get wrong. Two writers can still race — several
dispatch processes of one run, and a `kill_run` teardown beside them — and the
same per-node flock serialises the same check-then-act pair.

`.nodes/<node>/` is the key, which means the node id is untrusted input to a
path join. `run_nodes.safe_node_id` maps it to one path segment with the rule the
run dir already spells a node with (`[^A-Za-z0-9._-] -> "_"`, as in
`dispatch/providers.py` and `acp/agent.py`), so a node's sandbox directory reads
like the `agent-<node>.stream.jsonl` beside it. It then appends a digest of the
original id **when the mapping changed something**, because the mapping is lossy:
`implementer:0` and `implementer_0` both become `implementer_0`, and two nodes
sharing a directory *is* two nodes sharing a sandbox. Plain ids — every id in
practice — come back unchanged.

## Naming and labels

The cloud idempotency key is `sandbox_node.sandbox_name(run_id, node_id)`:

| | name |
|---|---|
| no node (unchanged) | `mo-<run-slug>` |
| node | `mo-<slugged-seed>-<sha256(seed)[:8]>`, `seed = f"{run_id}--{node_id}"` |

The digest is unconditional on the per-node path for the same reason as above,
and it is the *name* that is the idempotency key: a lost run-dir record still
re-finds this node's sandbox by name, and two nodes can never converge on each
other's. `find_run_sandbox` and `spawn_sandbox` both call this one function, so
lookup and create cannot drift.

`run_labels` adds `mo.node` to a per-node sandbox only. The label value is
mapped through `node_label` (`[^A-Za-z0-9._-] -> "_"`, no digest — nothing keys
on a label, so collision is harmless where on a path or a name it is not),
because these labels go straight into the cloud's create call and an invalid
character fails the whole sandbox before it boots. GC matches on `mo.sandbox` /
`mo.provider`, so per-node sandboxes are swept by the existing query unchanged.

## Teardown

Three paths, all of which must free every node's sandbox — otherwise the run
leaks one machine per node until the 6 h TTL:

| path | how it frees them |
|---|---|
| run end (in-process) | `release_remote_session` → `release_all_nodes(run_dir, env)` |
| `kill_run` (out of process) | `sandbox_reaper.reap_by_marker` → the backend hook, then `release_all_nodes` |
| SIGKILL / crash | nothing — the TTL reaper is the net |

`release_all_nodes` walks `<run_dir>/.nodes/*/`, so it frees what the **filesystem**
says exists rather than what the current process's env flag says. A teardown in a
process that never saw `MO_NODE_ISOLATION` (a `kill_run` from the UI) therefore
still frees everything, and a run that never started a node frees nothing.

Session markers follow the same split. The run-level marker keeps its exact name
and path (`.workspace-session.json`); a node's is
`.workspace-session.<safe-node>.json`. Both `run_mirror.DEFAULT_EXCLUDES` and the
reaper learn the per-node form, so a node's marker is neither mirrored to the
cloud nor left behind. All readers go through two helpers —
`workspace_session.session_marker_paths` / `read_session_marker` — so the naming
rule has exactly one owner.

## Cost and when to use it

Isolation multiplies the boot cost by the node count: a 6-node sequential recipe
boots 6 sandboxes instead of 1, plus the one run-level sandbox that stays for
work happening between nodes (see *Verified* — N+1, not N). Sandboxes are freed
as the run proceeds where the path allows, and the TTL (default 360 min) bounds
a run that dies, but the floor is still N boots.

Turn it on when the security property is worth the boots — untrusted or
third-party node code, a run whose nodes handle different secrets, or a shared
deployment where one node's compromise should not reach its siblings. Leave it
off for a trusted single-tenant run, where one sandbox per run is cheaper and
gives the same result.

## Verified

`tests/unit/test_per_node_sandboxes.py` covers the pieces and, more importantly,
the two properties that are silent when broken: the registry keys per node (two
nodes → two `up()` calls) and the names stay injective where the slugging is
lossy. The unit tier is not the whole story for a cloud feature — a live
Daytona smoke is the acceptance test, and the two things it can catch that no
unit test can are whether the cloud accepts the generated label values and
whether `attach` to a restarted sandbox really re-finds it by name.

### Live evidence

Three tiers, all run against real Daytona (2026-10-16):

**Provider smoke** — `scripts/smoke_sandbox_node.py --per-node`, **13/13 PASS in
32.1 s** (two sandboxes created in 25.3 s). It proves what the unit tier cannot:
the cloud accepts `mo.node=implementer_1` / `mo.node=reviewer_1` verbatim, two
nodes get two distinct handles, a re-acquire attaches instead of minting a
third, one record lands per node on disk, and `release_all_nodes` frees both —
checked by polling, because Daytona's delete is fire-and-forget.

**Full run** — `mini-ork run code-fix <kickoff> --placement remote --env daytona`
with `MO_NODE_ISOLATION=1` (run `run-1792137016-90593`). `verdict: pass`, both
verifiers green, replay adequacy ADEQUATE. The target repo's seeded bug
(`add()` returning `a - b`) was fixed and committed as `8a9d870`. Five nodes
dispatched, **five distinct sandboxes**, each carrying its own label:

```
mo-run-1792137016-90593                          mo.node=None      ← see below
mo-run-1792137016-90593--implementer-37e4eb0c    mo.node=implementer
mo-run-1792137016-90593--typecheck-082b55d1      mo.node=typecheck
mo-run-1792137016-90593--test-60d51db3           mo.node=test
mo-run-1792137016-90593--reviewer-32b71c62       mo.node=reviewer
mo-run-1792137016-90593--eval-c38532ef           mo.node=eval
```

**The run-level sandbox still boots under isolation** — by design, not an
oversight. `isolated_node` returns `""` for a process with no node to name
(`run_nodes.isolated_node`), and that process belongs on the run's sandbox
rather than being stranded: `_provision_remote_session` runs *before* the first
node, so it has no node identity. The property the feature exists for is
unaffected — no two **nodes** share a machine — but the count is N+1 boots, not
N. The run-level sandbox in the run above was touched only by control-plane
work; no node ever synced into it.

One caveat found while verifying: `Provider.list_sandboxes` is the provider-wide
TTL sweep, so the smoke's "exactly the two" checks must scope to the smoke's own
`mo.run` before counting. Counted raw, a run live elsewhere reads as a leak —
which is how the smoke above was run, next to a six-sandbox run, and still
passed.
