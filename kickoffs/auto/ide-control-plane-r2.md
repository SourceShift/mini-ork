# Thread control plane + image attachments — revision 2 (Opus review of run ide-control-plane-20261007092628)

The WIP commit b069825e holds the first cut. Its picker removal, harness tools, prompt.md
rewrite, `prompt_capabilities` and orch.py defaults passed review — keep them. Fix only the list below.

## Files in scope

- `mini_ork/acp/agent.py`
- `tests/unit/test_acp_agent_py.py`

## Fixes (exact)

1. **Blocker — keep the slash-command rewrite** (`agent.py:~1565`). `_build_prompt_payload(prompt, …)`
   rebuilds the text from the raw blocks and throws away `rewrite` (`/recipe new`,
   `/recipe edit`, `/automation new`, `/kickoff` handlers). Split the helper: one function returns
   only the attachment lines (images saved + `Attached image: <abs path>`, embedded resources
   inlined, resource links `Attached: <uri>`) from the NON-text blocks; the payload is
   `text` (already the rewrite when one was stashed) + `"\n\n"` + those lines when there are any.
   These 4 tests, which pass on dc660934 and fail on b069825e, must pass again:
   `test_no_draft_means_no_buttons_even_after_recipe_new`,
   `test_recipe_edit_of_a_spec_recipe_goes_to_the_orchestrator`,
   `test_automation_new_reaches_the_orchestrator_with_the_bridge`,
   `test_a_proposal_renders_as_a_card_with_name_recipe_when_next_and_kickoff`.
2. **Every prompt goes to the orchestrator** (`agent.py:~1571`). Delete the
   `if mode == _MODE_DIRECT:` branch; a stored or legacy `mode=direct` (config option,
   persisted thread config, `MO_ACP_DEFAULT_MODE=direct`) no longer changes routing. Keep
   accepting and storing the value so old clients don't error. Direct runs stay reachable only via
   `/run` (and `/race`).
3. **`/run` keeps attachments**: the kickoff text passed to `_prompt_thread_direct` from `/run`
   gets the same attachment lines appended.
4. **Image files never overwrite** (`agent.py:~280`, `~304`). Number images from the files
   already in `<home>/attachments/<session>/` (next free integer), so turn 2's image does not
   replace turn 1's `0.png`.

## Tests (each must fail on b069825e)

- End-to-end through `agent.prompt()` with a fake `orchestrator_turn` capturing its input: a text
  block + an image block → the orchestrator receives the text plus
  `Attached image: <home>/attachments/<sid>/0.png`, and the file holds the decoded bytes.
- `/recipe new audit migrations` + an image block → the orchestrator text is the rewrite (contains
  `create a new recipe`) plus the attachment line.
- Thread with `mode=direct` set via `set_config_option` → a plain prompt reaches the orchestrator,
  not `_prompt_thread_direct`.
- Two prompts with one image each in the same session → `0.png` and `1.png` both exist with
  their own bytes.
- `/run fix the thing` + an image → the direct-run kickoff contains the attachment line.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_agent_py.py tests/unit/test_ide_pages_orch.py tests/unit/test_acp_orchestrator.py`
  — paste the summary line. Known env failures that also fail on dc660934 and are NOT in scope:
  `test_authenticate_succeeds_when_readiness_passes` and the `test_race_*` tests; list them
  separately and confirm they fail identically on dc660934.
- `ruff check mini_ork/acp/agent.py tests/unit/test_acp_agent_py.py` is clean.
- Diff touches only files in scope.
