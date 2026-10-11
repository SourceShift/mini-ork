# Repair the hermeticity of `test_learning_static_lane_parity`

## Goal

`tests/unit/test_mini_ork_execute_py.py::test_learning_static_lane_parity` fails
on any developer machine whose repo has a reachable `.mini-ork/` home. Its own
comment claims *"Hermetic: no agents.yaml in reach, so every role is unmapped"*,
but it only deletes `MINI_ORK_HOME` / `MINI_ORK_ROOT`. `mini_ork.dispatch.llm_dispatch.resolve_lane_family`
then still resolves a mapping (verified locally: `resolve_lane_family("reviewer")
-> "glm"`), so the "unmapped" premise is false, `learning_static_lane` correctly
returns the role, and the assertion `("reviewer","reviewer") == "opus_lens"` fails
with `assert 'reviewer' == 'opus_lens'`.

It passes in CI only because CI has no `.mini-ork` home. This is LOCAL TEST DEBT
(an environment-dependent fixture), not a source defect — the source contract is
deliberate (`5ab9e299 fix(routing): explicit lane knobs outrank the agents.yaml
role mapping`).

## Acceptance

- AC1 — make the test actually hermetic: point `MINI_ORK_HOME` at an EMPTY
  directory (e.g. a `tmp_path` subdir) instead of merely deleting it, so
  `resolve_lane_family` gets a clean miss and every role is genuinely unmapped —
  exactly what the docstring already promises. Keep `MINI_ORK_ROOT` deleted.
- AC2 — do NOT change any expected value in the `cases` dict, do NOT add a skip,
  and do NOT weaken/delete an assertion. The contract under test is unchanged;
  only the fixture is repaired. (Verified independently: with an empty
  `MINI_ORK_HOME`, `learning_static_lane("reviewer","reviewer") -> "opus_lens"`
  and `("researcher","researcher") -> "kimi_lens"` — the existing expectations.)
- AC3 — do NOT edit `mini_ork/dispatch/routing.py`, `mini_ork/dispatch/llm_dispatch.py`,
  `mini_ork/cli/execute.py`, or any other source file. Test file only.
- AC4 — the sibling `test_learning_static_lane_honours_agents_yaml_role` in the
  same file must still pass, and it must keep working by writing its own
  `agents.yaml` under its own `tmp_path` home (it already sets `MINI_ORK_HOME`;
  leave its behaviour intact).

## Files in scope

- `tests/unit/test_mini_ork_execute_py.py`

## Out of scope

- Any production module. Any other test file. Adding skips/xfails.

## Verification command

`python3 -m pytest tests/unit/test_mini_ork_execute_py.py -q`
