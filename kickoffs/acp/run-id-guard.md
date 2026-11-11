# `/run` refuses a run id instead of launching a second run named after it

## Goal

`_prompt_thread` (`mini_ork/acp/agent.py:1903`) splits a `/run <text>` turn on
the prefix and hands `<text>` to `_prompt_thread_direct`, which **mints a new
run id and launches a fresh run** using that text as the kickoff
(`agent.py:1939-1963`). Nothing checks what the text is. So a user who types

```
/run run-1794331944-51032
```

to continue or re-chew an existing run gets a brand-new run whose entire
kickoff is the string `run-1794331944-51032` — a new worktree, a new
dispatcher, a new lease, and a run that cannot succeed because its
instructions are an identifier. `/race` has the identical hole through
`_strip_slash_race` (`agent.py:1928`) and `_prompt_thread_race`.

The failure is silent: the agent reports a launched run, and nothing in the
UI distinguishes "I started your run" from "I started a run that will
misread its own prompt". The fix is a guard, not a feature: detect a payload
that *is* a run id and refuse with a message that says what the user
probably meant.

## Scope Hint

- `mini_ork/acp/agent.py` — a module-level pattern next to `mint_run_id()`
  (line 192), a private predicate, and the two call sites in
  `_prompt_thread` (the `_strip_slash_run` branch at ~1903 and the
  `_strip_slash_race` branch at ~1908).
- `tests/unit/test_acp_agent_py.py` — the file already drives the direct
  path in-thread (see the tests around line 5847); extend it rather than
  adding a new module.

## Expected Edit

1. **The shape.** `mint_run_id()` (`agent.py:192`) produces
   `f"run-{int(time.time())}-{secrets.token_hex(3)}"` — `run-<epoch>-<6 hex>`.
   Add a module-level compiled pattern beside it:

   ```python
   #: The shape ``mint_run_id`` mints. Used to tell "a run id" from "a kickoff
   #: that happens to contain a run id": only a payload that is a *single*
   #: token of this shape is treated as an id.
   _RUN_ID_PAYLOAD = re.compile(r"^run-\d{6,}-[0-9a-f]{4,}$")
   ```

   `re` is already imported in this module; if it is not, import it. Do not
   import `mini_ork.ide_pages.cn_session`'s `_RUN_ID_RE` — that module is
   page-facing and its pattern is looser (`[A-Za-z0-9]{4,}`); the guard must
   match what this module *mints*, or it will refuse somebody's prose.

2. **The predicate.** One private helper:

   ```python
   def _run_id_payload(text: str) -> str | None:
       """The run id ``text`` names, or None when it is ordinary kickoff text.

       A run id is a single token of the minted shape. Prose that merely
       contains an id (``fix run-1794-abc``) is a kickoff and is left alone.
       """
   ```

   Strip the text, reject anything containing whitespace, then match.

3. **The guard.** In `_prompt_thread`, on both slash branches, before the
   dispatch call:

   ```python
   if slash_text is not None:
       named = _run_id_payload(slash_text)
       if named is not None:
           await self._emit_run_id_refusal(session_id, named, home, slash="run")
           return PromptResponse(stop_reason="refusal")
       ...
   ```

   and the same for `race_text` with `slash="race"`. Put the emitter on the
   class (it needs `self._emit`), not at module level.

4. **The message.** One `AgentMessageChunk` / `TextContentBlock` through
   `self._emit` — the pattern used at `agent.py:2098` — chosen by whether the
   id exists under `<home>/runs/<id>`:

   - exists:
     ```
     `/run run-1794331944-51032` names an existing run, not a kickoff — a
     new run would have been started with that id as its prompt. Open run
     run-1794331944-51032 from the board to read or steer it, or give
     `/run` a kickoff sentence.
     ```
   - does not exist:
     ```
     `/run run-1794-abc123` looks like a run id, not a kickoff, and no run
     with that id exists in this home. Check the id, or give `/run` a
     kickoff sentence.
     ```
   Say `/race` instead of `/run` when `slash="race"`. Keep the message to
   those facts; no advice about other commands.

5. **Nothing else happens on refusal.** No `mint_run_id()`, no worktree
   create/adopt, no `_launch`, no `_launches[...]` entry, no `_run_base_titles`
   write. Assert this in the test by stubbing the launcher and the
   workspace helpers with raising spies.

## Requirements

- One diff, two files. Do not touch `.mini-ork/`, `db/migrations/`,
  `config/risk-tiers.yaml`, or any frozen path.
- A payload that is ordinary prose launches exactly as it does today, even
  when it contains a run id in the middle or at the end.
- Only the *whole* trimmed payload being one minted-shape token triggers the
  refusal — no substring matching, no fuzzy "starts with run-".
- The refusal returns `PromptResponse(stop_reason="refusal")`, the same
  shape the other guarded paths use (`agent.py:1791`, `1842`, `1963`).
- Exactly one message chunk is emitted per refusal; the message names the id
  and the slash that triggered it.
- No new dependency, no new config, no change to `mint_run_id()`.
- Comments and docstrings in the surrounding style: lowercase, declarative,
  explaining *why*.

## Done When

- AC1 `/run run-1794331944-51032` (an id present under `<home>/runs/`) emits
  one message naming that run and returns `stop_reason="refusal"`; the
  launcher and the worktree helpers are never called.
- AC2 `/run run-1794-abc123def0` (no such run) returns
  `stop_reason="refusal"` with the "not found" message, and likewise never
  reaches the launcher.
- AC3 `/race <run id>` behaves the same, with `/race` in its message.
- AC4 `/run fix the flaky test run-1794331944-51032` and `/run
  run-1794331944-51032 please` still launch (a run id inside prose is not a
  run id payload).
- AC5 `python3.11 -m pytest tests/unit/test_acp_agent_py.py -q` passes and
  `make lint` is clean.

## Files in scope

- `mini_ork/acp/agent.py`
- `tests/unit/test_acp_agent_py.py`

## Out of scope

- Attaching to, folding into, or steering the named run automatically. The
  guard refuses and explains; choosing a run stays the user's action.
- Any change to the board, the thread UI, or a new ACP command.
- `/run` with a path, a branch name, or any other id-shaped argument.
- Changing `mint_run_id()`'s format or the run-id patterns in
  `mini_ork/ide_pages/`.

## Verification command

```bash
python3.11 -m pytest tests/unit/test_acp_agent_py.py -q && make lint
```
