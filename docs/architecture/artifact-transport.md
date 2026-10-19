# Artifact Transport for Cloud Runs

## Context

MiniOrk's remote backend already moves a node's file writes between machines:
`mini_ork/remote/tree_sync.py` snapshots a tree, ships it as a git bundle,
materialises the bundle on the remote, and `apply_delta`
(`mini_ork/remote/tree_sync.py:267`) carries the node's edits back as a binary
git diff guarded by `SyncConflictError` (`mini_ork/remote/tree_sync.py:53`).
`MO_SHARED_DRIVE_ROOT` (`mini_ork/runtime/backends/remote.py:1758`) is the only
existing knob that hints at cross-machine state.

The current model assumes **one shared disk**. A recipe today declares a
`path` requirement ("write the report to `docs/research/<dir>/`"); when a cloud
run spawns each agent node on a separate isolated machine, that path is
unsatisfiable because no two nodes share a filesystem. Evidence: the survey
run `run-1791749486-16681` (recipe `audit-judge-panel-3l`) required
`docs/research/20261012-langgraph-parity-survey/`; the run died and that
directory was never created — reports stranded in the run dir, a rollback
node ran, and nothing published.

This document is the design the ordinary build pipeline can execute. P1
delivers the **index** half (a queryable `state.db` of per-node completion
records keyed by `run_id` + step). This document delivers the **transport**
half: a content-addressed artifact record so bytes are addressable from any
machine, and per-item slots so parallel writers do not collide on one tree.
The path contract described in [artifact-graph.md](./artifact-graph.md) stays
valid for single-tree runs; this design layers on top of it.

## Why the path contract fails under node isolation

The current artifact contract is a *path* contract: a producer publishes by
writing declared `outputs[].path` files on a filesystem the consumer can read.
Under node isolation, that contract breaks for four reasons:

1. **Per-node path requirement.** A path is a statement that "the artifact
   shall live at this filesystem location". When a recipe's kickoff specifies
   `docs/research/<dir>/<file>.md` and parses that scope in
   `mini_ork/context_v2.py:parse_contract`, every node must respect the same
   path. Two parallel nodes writing into the same path cannot both succeed —
   the second write clobbers or races the first.
2. **Whole-tree single-writer `apply_delta`.**
   `mini_ork/remote/tree_sync.py:267` is `apply_delta(local_repo, base, new)`:
   it applies the remote node's edits (`base` -> `new`) to the **local
   worktree only** and raises `SyncConflictError`
   (`mini_ork/remote/tree_sync.py:53`) the moment two edits to the same worktree
   diverge from base. The function is whole-tree and single-writer; it has no
   notion of per-item slots.
3. **`SyncConflictError` is binary, not mergeable.** When N parallel nodes each
   commit one report to the same tree, `base` is the snapshot they all share
   and `new` is the union of all N edits — every node's view of `new` differs,
   so N-1 of them conflict. The exception names the diverging paths but does
   not auto-merge.
4. **`MO_SHARED_DRIVE_ROOT` is the only shared-disk knob.**
   `mini_ork/runtime/backends/remote.py:1758` reads `MO_SHARED_DRIVE_ROOT` and
   falls back to `os.getcwd()` if unset; nothing else exposes a shared
   filesystem. There is no notion of a remote-reachable blob store.

The taxonomy is therefore: the path contract assumes one shared disk; the
transport assumes one shared *transport*; under node isolation neither holds.

## The model: index vs transport vs materialize

Three roles, cleanly separated:

- **Index.** P1's `state.db` (the `p1-2` spec clause F2: per-node completion
  records holding `node_id, status, artifact paths + sha256, rc, timestamps`,
  queryable by `run_id` and step). It records *where an artifact is named* and
  *what its hash is*. It does **not** move bytes; it is the manifest half.
- **Transport.** This design's contribution: a content-addressed artifact
  record `{run_id, artifact_id, sha256, media_type, blob_ref}` where
  `blob_ref` is a location any machine can resolve (the shared drive root, a
  future object store, or the run's durable store). Bytes live in the
  transport; the index points at them by `artifact_id`.
- **Materialize.** On the publishing machine (or any consumer), the consumer
  reads the index by `run_id` + `artifact_id`, resolves `blob_ref`, fetches the
  bytes, verifies `sha256`, and writes them to the consumer's own filesystem
  path. The `path` in a node's declared `outputs` becomes a *materialisation
  target* on the publishing machine only, never a per-node requirement.

The path contract in [artifact-graph.md](./artifact-graph.md) is unchanged:
single-tree runs still materialise to the declared path. Cloud runs gain the
transport layer above the index; the path contract is the materialise step.

## The LangGraph mapping

LangGraph solves exactly this problem, and the mapping motivates the design:

| LangGraph concept       | MiniOrk equivalent                                 |
|-------------------------|----------------------------------------------------|
| channel + reducer       | per-item artifact slot (one slot per fan-out item) |
| checkpointer            | P1 `state.db` (index)                              |
| `thread_id`             | `run_id`                                           |
| `checkpoint_id`         | step index (P1's `p1-2` clause F2)                 |
| `Send` / map-reduce fan | `mini_ork/orchestration/item_fanout.py` slot assignment |
| in-flight worker state  | the transport — bytes in the store, not the disk   |

The store, not the disk, is the transport. LangGraph nodes never share a
filesystem; they share a **store** addressed by `thread_id` + `checkpoint_id`.
MiniOrk adopts the same shape: nodes share an *artifact store* addressed by
`run_id` + `artifact_id`; the disk is the materialise destination, not the
transport medium.

## Artifact record schema delta (D2)

The P1 `p1-2` completion record carries:

```json
{
  "node_id": "<node-id>",
  "status": "ok|error",
  "artifact_paths": ["<rel-path>", "..."],
  "sha256":   "<hex-digest-for-each-path>",
  "rc": <int>,
  "timestamps": { "started": "...", "finished": "..." }
}
```

This design extends the per-artifact entry to:

```json
{
  "run_id":      "<run-uuid>",
  "artifact_id": "<run_id>:<node_id>:<output-name>:<sha256[:16]>",
  "sha256":      "<hex-digest>",
  "media_type":  "text/markdown",
  "blob_ref":    "artifacts/<run_id>/<artifact_id>"
}
```

(`ArtifactRef` in `mini_ork/workflow/artifacts.py:24` is the in-process
carrier for the same five fields; the index row above is its persisted form.
The `artifact_id` suffix `<sha256[:16]>` is already how
`publish_node_outputs` constructs the runtime id
(`mini_ork/workflow/artifacts.py:113-120`).)

The schema delta to `schemas/artifact_contract.schema.json` is **additive on
the object variant** of `outputs[]` entries (the bare-string variant is
untouched). Concretely, against the current object shape
(`schemas/artifact_contract.schema.json:57-63`):

```diff
   {
     "type": "object",
     "required": ["path"],
     "additionalProperties": false,
     "properties": {
       "path":      { "type": "string", "minLength": 1 },
       "mime":      { "type": "string" },
       "description": { "type": "string" },
-      "required":  { "type": "boolean" }
+      "required":  { "type": "boolean" },
+      "blob_ref":  { "type": "string", "description": "Transport location resolvable from any machine (shared drive root, object store, or run-local durable store)." },
+      "sha256":    { "type": "string", "pattern": "^[0-9a-f]{64}$", "description": "Content hash; matches the bytes resolved from blob_ref." },
+      "slot":      { "type": "string", "description": "Per-item slot name for parallel fan-out writers; unique within a run." }
     }
   }
```

**Back-compat rule.** A bare path-string entry (e.g. `"outputs": ["x.md"]`) and
a path-only object entry (e.g. `{"path": "x.md"}`) remain valid for single-tree
runs. The new fields (`blob_ref`, `sha256`, `slot`) are *optional*; the
validator does not require them, and existing recipes that declare no
transport fields continue to publish via the legacy path contract. The
`publish_node_outputs` path (`mini_ork/workflow/artifacts.py:102`) still
computes `sha256` and `artifact_id` automatically — a node that does not
declare `blob_ref`/`slot` falls through to the legacy materialise step on the
publishing machine.

## Fan-out artifact slots (D3)

`tree_sync.apply_delta` (`mini_ork/remote/tree_sync.py:267`) is whole-tree
and single-writer; per-item slots layer above it. Each fan-out item writes to
its own slot; the aggregator reads N slots, not one directory.

YAML shape, demonstrated on the 3-lens `audit-judge-panel-3l` family:

```yaml
- name: lens_writer_a
  type: implementer
  outputs:
    - name: lens_report
      kind: markdown
      slot: "audit-judge-panel-3l/lens-a/<run_id>"
    - name: lens_summary
      kind: json
      slot: "audit-judge-panel-3l/lens-a/<run_id>/summary"
- name: lens_writer_b
  type: implementer
  outputs:
    - name: lens_report
      kind: markdown
      slot: "audit-judge-panel-3l/lens-b/<run_id>"
- name: lens_writer_c
  type: implementer
  outputs:
    - name: lens_report
      kind: markdown
      slot: "audit-judge-panel-3l/lens-c/<run_id>"
- name: panel_synthesizer
  type: implementer
  inputs:
    - from_output: lens_writer_a/lens_report
    - from_output: lens_writer_b/lens_report
    - from_output: lens_writer_c/lens_report
  outputs:
    - { name: panel_report, kind: markdown }
```

Touch-points:

- **`mini_ork/orchestration/item_fanout.py`** — slot assignment per fanned
  item. The fan-out dispatcher already builds per-item `ItemOutcome` records
  (`mini_ork/orchestration/item_fanout.py:89-126`); slot assignment extends
  `ItemOutcome.to_dict` to carry the slot name alongside `item_id`, so the
  publishing step can address each item's bytes by `(run_id, slot)`.
- **`mini_ork/workflow/artifacts.py`** — record construction. The existing
  `publish_node_outputs` (`mini_ork/workflow/artifacts.py:102`) already
  computes `sha256` and `artifact_id`; this change adds `blob_ref` resolution
  from the slot and writes the index row alongside the on-disk bytes when a
  slot is declared.
- **`mini_ork/gates/artifact_contract.py`** — validation of slot-bearing
  outputs. The gate accepts the new optional fields and rejects two nodes that
  declare the same `(run_id, slot)` so the aggregator's read path is
  unambiguous.

**Aggregator read path.** The panel_synthesizer reads inputs by index, not by
shared filesystem:

1. Query the P1 `state.db` for rows where `run_id = <run>` and
   `artifact_id in {lens_writer_a/lens_report, lens_writer_b/lens_report,
   lens_writer_c/lens_report}`.
2. For each row, resolve `blob_ref` (e.g. `artifacts/<run_id>/<artifact_id>`)
   from the run's durable store; fetch the bytes.
3. Verify `sha256` matches the row.
4. Materialise into the consumer's own filesystem path (the
   `consumer_input` destination) and proceed.

**Tree_sync is untouched.** This design layers above `apply_delta`
(`mini_ork/remote/tree_sync.py:267`); the git-bundle mechanics
(`snapshot` / `initial_bundle` / `materialize` / `incremental_bundle`) are not
modified. A slot-bearing node still uses `apply_delta` to carry bytes back;
the difference is that two parallel nodes now carry bytes to *different
slot keys* in the durable store rather than to the same worktree path. No
git-bundle change is implied.

## Verification plan (D4)

Two runnable probes that distinguish the current model from the proposed one.

**Probe 1: parallel writers do not collide.**

What it proves: two nodes with distinct `slot` values resolve to independent
artifact records and the P1 index returns both, where today's single-tree
model raises `SyncConflictError` (`mini_ork/remote/tree_sync.py:53`).

```bash
python3 - <<'PY'
import json, sqlite3, subprocess, tempfile, pathlib, hashlib

# 1. materialise two parallel "nodes" with distinct slots
with tempfile.TemporaryDirectory() as run:
    p = pathlib.Path(run)
    db = p / "state.db"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE artifacts(
        run_id TEXT, artifact_id TEXT, sha256 TEXT,
        media_type TEXT, blob_ref TEXT, slot TEXT
      );""")
    for slot in ("audit-judge-panel-3l/lens-a/r", "audit-judge-panel-3l/lens-b/r"):
        body = f"hello from {slot}".encode()
        digest = hashlib.sha256(body).hexdigest()
        con.execute(
          "INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
          ("r", f"r:writer:{slot.split('/')[-2]}:{digest[:16]}",
           digest, "text/markdown", f"artifacts/r/{slot}", slot))
    con.commit()
    rows = con.execute(
      "SELECT slot, sha256 FROM artifacts WHERE run_id=? ORDER BY slot",
      ("r",)).fetchall()
    assert len(rows) == 2 and rows[0][0] != rows[1][0], rows
    print("PASS: 2 distinct slots resolve without conflict")
PY
```

Expected under the current model: the second parallel node would raise
`SyncConflictError` because both nodes try to write into the same path; the
probe cannot construct that outcome with sqlite alone. Expected under this
design: two rows, distinct slots, distinct `sha256`s, no conflict.

**Probe 2: bytes are resolvable from a second machine by `run_id` +
`artifact_id` via `blob_ref`.**

What it proves: a consumer holding only the P1 index row can fetch the bytes
without sharing a filesystem with the producer.

```bash
python3 - <<'PY'
import sqlite3, hashlib, pathlib, tempfile

with tempfile.TemporaryDirectory() as run:
    store = pathlib.Path(run) / "store"
    store.mkdir()
    body = b"# lens-a report\nverdict: pass"
    blob = store / "artifacts" / "r" / "lens-a"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()

    db = pathlib.Path(run) / "state.db"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE artifacts(
        run_id TEXT, artifact_id TEXT, sha256 TEXT,
        media_type TEXT, blob_ref TEXT, slot TEXT
      );""")
    con.execute(
      "INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
      ("r", f"r:writer:lens_report:{digest[:16]}", digest,
       "text/markdown", "artifacts/r/lens-a", "audit-judge-panel-3l/lens-a/r"))
    con.commit()

    # Consumer reads ONLY by (run_id, artifact_id); resolves blob_ref;
    # verifies sha256; no shared filesystem with producer.
    row = con.execute(
      "SELECT blob_ref, sha256 FROM artifacts "
      "WHERE run_id=? AND artifact_id=?",
      ("r", f"r:writer:lens_report:{digest[:16]}")).fetchone()
    fetched = (store / row[0]).read_bytes()
    assert hashlib.sha256(fetched).hexdigest() == row[1]
    print("PASS: artifact resolved from a second machine by (run_id, artifact_id)")
PY
```

Expected under the current model: the consumer needs the producer's path to
resolve the bytes; if that path lives on a different machine the fetch
fails. Expected under this design: the consumer reads only the index row,
resolves `blob_ref` from the durable store, verifies `sha256`, succeeds.