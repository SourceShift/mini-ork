# SDE-6a replan graph patches: re-plan the failing step from existing node templates

## Goal

SDE-5a's `mini_ork/recovery/graph_patch.py` accepts `replan` proposals (the
per-run orchestrator writes one for every harness / prompt / stage-order MUTATE)
but `materialize` and `apply` refuse them with `replan patches are not
applicable yet`. Make `replan` applicable: ask the run's planner lane for a short
replacement chain for the failing node, built only from node templates the
recipe already has, graft it into the workflow, and validate it with the
compiler. Then set `REPLAN_SUPPORTED = True` so the supervisor's R6 (SDE-5b)
starts applying them.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. `mini_ork/recovery/graph_patch.py`:
   - Module constant `REPLAN_SUPPORTED = True`.
   - `_replan_chain(home, run_id, patch, base_document, *, dispatch=None) -> list[dict]`:
     - Cache: `<run_dir>/graph-patches/NNN.replan.json` holds the accepted
       chain. If it exists, return it unchanged (materialize must be
       deterministic after the first call; the patch YAML itself stays
       immutable). It is written once with `O_CREAT|O_EXCL`.
     - Otherwise build one prompt containing: the base workflow's `nodes` and
       `edges` (YAML), the failing node (`patch["from_node"]`), the patch
       `reason`, and failure evidence: `repair.json` last attempt `reason` and
       `diagnosis` (if any), `diagnosis.json` (if any), and the last 60 lines of
       the newest `*.log` whose name contains the failing node's name. Ask for
       strict JSON `{"nodes": [...]}`: a chain of 1 to 3 nodes that together
       replace the failing node, each node using ONLY a `type`, `model_lane` and
       `prompt_ref` triple that already appears on some node of the base workflow
       (copy every other key of that template node), with a new unique `name`.
     - Dispatch once through `mini_ork.dispatch.providers.dispatch_model` with a
       `DispatchRequest` shaped exactly like
       `mini_ork/recovery/diagnoser_llm.py`'s call (scratch tempdir as cwd,
       timeout, max turns). The lane is `MO_REPLAN_LANE`, else the run's own
       planner lane from `<run_dir>/config/agents.yaml` `lanes.planner`, else
       `glm`. `dispatch` is an injectable `callable(request) -> result` for tests.
     - Validate the reply: a JSON object with `nodes`, 1–3 mappings, each name
       unique and absent from the base workflow, each (`type`, `model_lane`,
       `prompt_ref`) triple present on some base node. Any failure (dispatch
       error, unparseable reply, a violated rule) raises `GraphPatchError`
       naming the reason, and writes no cache file.
   - `materialize` for `kind == "replan"`: take the chain from `_replan_chain`,
     replace the failing node in `nodes` with the chain (in order), redirect
     every non-control edge whose `to` is the failing node to `chain[0]` and
     every edge whose `from` is the failing node to `chain[-1]`, keep control
     edges (`retries`, `escalates_to`) INTO the failing node pointing at
     `chain[-1]` (it now does the failing node's job), add `depends_on` edges
     `chain[i] -> chain[i+1]`, then compile exactly as for the other kinds.
   - `apply` for `kind == "replan"`: the recover `--from-node` is `chain[0]["name"]`
     (the failing node no longer exists in the patched workflow);
     `NNN.applied.json` and `repair.json` `revive_from_node` record that name.
   - Remove the `replan patches are not applicable yet` refusal.
2. `mini_ork/cli/graph_patch.py`: `validate` and `apply` work for `replan`
   unchanged; no new verbs.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/graph_patch.py`
- `tests/unit/test_graph_patch.py`
- `tests/unit/test_graph_patch_replan.py` (new)

Do NOT modify any other file. In `tests/unit/test_graph_patch.py`, change only the
tests that assert the old `not applicable yet` refusal so they assert the new
behaviour.

## Tests (`tests/unit/test_graph_patch_replan.py`, hermetic; inject `dispatch`, never call a real lane)

- a fake planner reply of a 2-node chain built from existing templates →
  materialized workflow compiles, the failing node is gone, incoming dataflow
  edges point at chain[0], outgoing edges leave chain[-1], a `retries` edge into
  the failing node now targets chain[-1], and `chain[0] -> chain[1]` exists;
- the chain is cached: a second `materialize` does not call `dispatch` again and
  yields an identical file;
- rejected replies, each → `GraphPatchError` and no `NNN.replan.json`: non-JSON;
  0 nodes; 4 nodes; a node whose (`type`, `model_lane`, `prompt_ref`) triple is
  not in the base workflow; a name that already exists; a dispatch that raises;
- `apply` of a replan patch passes `--from-node <chain[0].name>` and records it
  in `applied.json` and `repair.json`;
- `REPLAN_SUPPORTED is True`;
- lane resolution: `MO_REPLAN_LANE` wins; else `lanes.planner` from the run's
  `config/agents.yaml`; else `glm`.

## Verification commands

- `pytest -q tests/unit/test_graph_patch_replan.py tests/unit/test_graph_patch.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_graph_patch_replan.py tests/unit/test_graph_patch.py --junitxml="${MINI_ORK_RUN_DIR}/replan-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/replan-junit.xml`

## Success criteria

- The verification command passes.
- `ruff check --select F,E9 mini_ork/recovery/graph_patch.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
