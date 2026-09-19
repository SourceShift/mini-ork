# U2 chapter-validation-10lens: real gating edges + parallel lens wave

## Goal

- Problem: `recipes/chapter-validation-10lens/workflow.yaml` declares `edges: []`, so the
  compiler produces zero control_parents and the executor falls back to serial
  declared-order dispatch. The 10 "parallel lens agents" promised by the recipe
  description actually run one-by-one, and nothing structurally guarantees the
  synthesizer runs after the lenses. Node-level `dispatch_mode: partitioned` is ignored
  by the readiness-wave batcher — only the literal string `parallel` batches.
- Fix, in `recipes/chapter-validation-10lens/workflow.yaml` only:
  1. Change each of the 10 lens nodes (`lens_01_structure` … `lens_10_synthesis_originality`)
     from `dispatch_mode: partitioned` to `dispatch_mode: parallel`, and delete the
     now-meaningless `partition_key: lens_id` lines.
  2. Replace `edges: []` with real `depends_on` edges forming the chain:
     `planner -> each lens` (10 edges), `each lens -> synthesizer` (10 edges),
     `synthesizer -> lens_outputs_complete` (1 edge),
     `lens_outputs_complete -> publisher` (1 edge). Edge shape used elsewhere in this
     repo: `{from: <src>, to: <dst>, edge_type: depends_on}`.
- Effect: `dependency_aware` dispatch turns on (control_parents non-empty); after the
  planner completes, all 10 lenses become ready in the same wave and batch together
  because their dispatch_mode is the literal `parallel`; synthesizer waits for all 10;
  verifier and publisher gate the tail. ~10x wall-clock reduction on the lens stage.
- Add a compiled-topology unit test `tests/unit/test_chapter_validation_10lens_topology.py`:
  load the workflow YAML with `yaml.safe_load`, compile with
  `mini_ork.workflow.compiler.compile_workflow`, then assert:
  - `control_parents` of every lens node == `{"planner"}`;
  - `control_parents["synthesizer"]` == the set of all 10 lens node names;
  - `control_parents["lens_outputs_complete"]` == `{"synthesizer"}`;
  - `control_parents["publisher"]` == `{"lens_outputs_complete"}`;
  - every lens node dict in the YAML has `dispatch_mode == "parallel"` and no
    `partition_key` key.
- Size: **S** (one YAML edit + one new test file).

## Files in scope

- `recipes/chapter-validation-10lens/workflow.yaml`
- `tests/unit/test_chapter_validation_10lens_topology.py` (new)

## Definition of Done

- The workflow YAML contains exactly 22 edges as specified, all `edge_type: depends_on`.
- All 10 lens nodes: `dispatch_mode: parallel`, no `partition_key`.
- Planner / synthesizer / verifier / publisher nodes untouched apart from edges.
- The new topology test passes and encodes all five assertions above.
- No other recipe or core module modified.

## Verification commands

- `python3 -m pytest tests/unit/test_chapter_validation_10lens_topology.py -q`
- `python3 -c "import yaml; yaml.safe_load(open('recipes/chapter-validation-10lens/workflow.yaml'))"`
- `python3 -m ruff check tests/unit/test_chapter_validation_10lens_topology.py --select F,E9`

## Done When

- All verification commands pass in the isolated worktree.
- Reviewer node reports pass; diff touches only the two in-scope files.
