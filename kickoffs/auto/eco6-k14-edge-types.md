# ECOSYSTEM §6 · K14: five edge types that behave exactly like `depends_on` become `depends_on`

## Goal

`schemas/workflow.schema.json` allows 8 `edge_type` values and
`docs/ARCHITECTURE.md` §6 says "The executor enforces them". It enforces three:

| Edge type | Edges / recipes | Runtime behaviour |
|---|---|---|
| `depends_on` | 219 | ordering parent |
| `retries` | 13 / 6 | revise loop (`compiler.py` retry edges, `execute.py`) |
| `escalates_to` | 158 / 43 | repair edge **only** when the recipe declares a `recursion:` block; otherwise not an ordering parent and no runtime effect |
| `supplies_context_to` | 435 / 36 | same as `depends_on` (no context injection) |
| `verifies` | 60 / 44 | same as `depends_on` |
| `blocks` | 0 | same as `depends_on` |
| `human_decision_gate` | 2 / 2 | same as `depends_on` (human pauses are the intervention gate / `node_review`) |
| `verifies_user_choice` | 2 / 2 | same as `depends_on` |

The collapse is `mini_ork/workflow/compiler.py` (~403-406): any edge that is not
`escalates_to`, `retries` or `recursive: true` becomes an ordering parent;
artifact handoff happens through `from_output`/`to_input` on any edge type.
`mini_ork/types.py` `EdgeType` lists only six of the eight.

Rename the five decorative types to `depends_on` in every shipped recipe, keep
accepting the old names for one release (deprecated, with a `validate` warning)
so out-of-tree recipes keep compiling, and correct the docs.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. **Codemod, checked in and run.** `scripts/codemods/eco6_collapse_edge_types.py`:
   for every `recipes/*/workflow.yaml`, rewrite `edge_type: <old>` to
   `edge_type: depends_on` for `<old>` in `supplies_context_to`, `verifies`,
   `blocks`, `human_decision_gate`, `verifies_user_choice` — both inline flow and
   block forms, preserving every other byte (line/regex-level, not YAML
   load/dump). Run it; commit the script and the edits. Afterwards every recipe
   must compile (`mini_ork.workflow.compile_workflow`) and produce the **same**
   `control_parents`, `retry_edges`, `escalation_edges` and bindings as before
   the codemod (assert this in the test by compiling a `git show HEAD:<path>`
   copy against the new file).
2. **Schema**: keep all eight names in the `edge_type` enum (one-release
   compatibility) but change the description to: "`depends_on`, `retries` and
   `escalates_to` have runtime meaning; `supplies_context_to`, `verifies`,
   `blocks`, `human_decision_gate` and `verifies_user_choice` are deprecated
   aliases of `depends_on` (2026-10) and will be removed."
3. **Types**: `mini_ork/types.py` `EdgeType` lists all eight with a comment
   naming the five deprecated aliases.
4. **validate** (`mini_ork/cli/validate.py`, `_check_recipe`): one **warning**
   per recipe that uses a deprecated alias:
   `recipe <r> uses deprecated edge_type(s) <a, b> — they behave exactly like depends_on`
   with hint `rename them to depends_on (scripts/codemods/eco6_collapse_edge_types.py does it)`.
   Also one **warning** per recipe whose edges carry a plain-string
   `condition:` (ignored by the runtime): `recipe <r> has N string condition(s) the runtime ignores`
   with hint `remove them, or use an object predicate on a retries edge`.
5. **Docs**: rewrite `docs/ARCHITECTURE.md` §6 "Edge-Type Semantics": the
   executor gives meaning to `depends_on` (ordering + optional artifact ports),
   `retries` (revise loop, `max_rounds`) and `escalates_to` (repair edge only
   inside a `recursion:` block); list the five deprecated aliases. Remove "The
   executor enforces them".

## Files in scope (touch ONLY these)

- `scripts/codemods/eco6_collapse_edge_types.py` (new)
- `recipes/*/workflow.yaml` — only the codemod's edits
- `schemas/workflow.schema.json` (edge_type description only)
- `mini_ork/types.py` (`EdgeType` only)
- `mini_ork/cli/validate.py`
- `docs/ARCHITECTURE.md` (§6 only)
- `tests/unit/test_eco6_k14_edge_types.py` (new)

Do NOT modify `mini_ork/workflow/compiler.py` (it already treats the aliases as
`depends_on`) or any other file.

## Tests (`tests/unit/test_eco6_k14_edge_types.py`, hermetic)

- No shipped `recipes/*/workflow.yaml` edge uses a deprecated alias.
- **Behaviour is unchanged.** For every recipe the codemod changed (find them
  with `git diff --name-only HEAD -- recipes` at test-write time and list them
  in the test, or compare against fixture copies), compiling the old text and
  the new text yields equal `control_parents`, `retry_edges`,
  `escalation_edges` and binding tuples.
- **The codemod preserves bytes.** On a tmp file with an inline-form
  `verifies` edge, a block-form `supplies_context_to` edge, a comment, and a
  node whose description contains the word `verifies`, only the two
  `edge_type` values change.
- **Aliases still compile.** A tmp workflow with a `verifies` edge compiles and
  the edge is an ordering parent.
- **validate warns.** A tmp recipe with a `blocks` edge →
  `validate.main(["--recipe", …])` rc 0 (warnings only) and stderr contains
  `deprecated edge_type`; with `--strict` rc 1. A recipe with a string
  `condition: pass` → stderr contains `string condition`.
- Every shipped recipe still validates against the schema.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k14_edge_types.py tests/unit/test_eco6_k3_recipe_schema.py tests/unit/test_cli_validate_py.py tests/unit/test_recipe_graph_compile.py $(git grep -l -E "compile_workflow|workflow\.compiler" -- tests | tr '\n' ' ')`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k14_edge_types.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k14-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k14-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test unchanged.
- `ruff check --select F,E9 scripts/codemods/eco6_collapse_edge_types.py mini_ork/types.py mini_ork/cli/validate.py` is clean.
- `git diff --stat -- recipes/` shows only `workflow.yaml` files, and every
  changed line is an `edge_type` value.
