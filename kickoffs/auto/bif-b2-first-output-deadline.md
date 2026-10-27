# B2: a first-output deadline on every lane but the last, so a hung lead lane cannot eat the node's budget

## Goal

A node's whole timeout (`MO_NODE_TIMEOUT_S`, 1500 s by default) is handed to
EVERY lane in its fallback chain:
- `mo_llm_dispatch` (`mini_ork/dispatch/llm_dispatch.py` ~724-748) never
  passes `per_attempt_timeout_s` to `dispatch_with_fallback`
  (`mini_ork/dispatch/providers.py` ~2547);
- `core.spawn_local` (`mini_ork/dispatch/core.py` ~342-490) blocks on
  `proc.wait(timeout=…)`.

Live: run `sde-3b1-diagnoser-20261015-142335` spent 3000 s on its implementer:
`deepseek_flash` timed out at 1500 s, then `opus` timed out at 1500 s.

A wall-clock cut would also kill legitimately long implementer sessions. The
safe signal is **no model output at all**. Claude-CLI lanes (anthropic-native
and anthropic-compat: deepseek_flash, glm, minimax, opus) stream JSON events
on stdout (`stream-json --verbose --include-partial-messages`,
`providers.py` ~692-708). `spawn_local` already drains stdout line by line in
`_drain_stream` (~493-528). A lane that produced no model event within N
seconds is hung, so give the next lane its turn.

The LAST lane in the chain never gets a first-output deadline.
Codex/opencode lanes redirect their output to a file, so they never get one
either.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/dispatch/models.py`

`DispatchRequest` gains `first_output_s: float | None = None` (last field,
default `None`, so every existing constructor keeps working).

### 2. `mini_ork/dispatch/core.py`

1. **`is_model_event(line: str) -> bool`**: true when the line parses as a
   JSON object whose `type` is `"assistant"`, `"stream_event"`, `"user"` or
   `"result"`. False for `system` (CLI init) and for anything unparseable.
2. **`spawn_local`** takes `first_output_s: float | None = None`.
   - **Watching.** When set (> 0), the stdout `_drain_stream` thread sets a
     `threading.Event` on the first line for which `is_model_event` is true.
     The `proc.wait(timeout=timeout)` call becomes a loop of
     `proc.wait(timeout=min(1.0, remaining))`.
   - **Cutoff.** When `first_output_s` seconds have passed since spawn and the
     event is not set, terminate the process group (`_terminate_process_group`,
     with the same reap handling as the timeout path) and return
     `124, "", f"no model output within {first_output_s:.0f}s (first-output deadline; spawned→kill {elapsed:.0f}s)"`.
   - **Overall timeout.** The existing behaviour and message are unchanged.
   - **Unset.** With `first_output_s` unset, behaviour is byte-for-byte as
     today.
3. **`core.dispatch`** passes `request.first_output_s` into `spawn_local`. It
   is ignored on the workspace/remote spawn path (`_spawn_in_workspace`).

### 3. `mini_ork/dispatch/providers.py`

1. **`dispatch_with_fallback`** takes `first_output_s: float | None = None`.
   For lane index `i`:
   - set it on the per-lane request only when `i < len(lanes) - 1` AND the
     lane's resolved spec streams JSON (`_wants_stream_json(spec)`; resolve
     the spec the way `dispatch_model` does, and treat a resolution error as
     "no deadline");
   - otherwise use `None`.
   The existing `per_attempt_timeout_s` handling is unchanged.
2. **`dispatch_model`** carries `request.first_output_s` through to the
   engine's dispatch unchanged.

### 4. `mini_ork/dispatch/llm_dispatch.py` — `mo_llm_dispatch`

- Read `MO_DISPATCH_FIRST_OUTPUT_S` (default `"300"`; `"0"` or empty disables;
  a non-number fails closed as disabled, with one stderr warning) and pass it
  as `first_output_s=` to `dispatch_with_fallback`.
- When it is `MO_DISPATCH_ATTEMPT_TIMEOUT_S` that is set and positive, also
  pass `per_attempt_timeout_s=` (today only `python -m mini_ork.dispatch`
  reads it).
- A single-lane dispatch (the `dispatch_model` branch) never gets a
  first-output deadline: it is the last lane.

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/models.py`
- `mini_ork/dispatch/core.py`
- `mini_ork/dispatch/providers.py`
- `mini_ork/dispatch/llm_dispatch.py`
- `tests/unit/test_first_output_deadline.py` (new)

Do NOT modify any transport (`codex_transport.py`, `opencode_transport.py`,
`openai_chat_transport.py`), `docs/`, or any path in
`config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (`tests/unit/test_first_output_deadline.py`, hermetic, no LLM)

Use tmp executable scripts as the spawned process, calling
`core.spawn_local` directly as `tests/test_dispatch_process_contract.py` does.
Use the `mock` provider kind or fake specs for the providers-level cases.

1. **Silent process.** A script that sleeps 30 s and prints nothing →
   `spawn_local(..., timeout=60, first_output_s=1)` returns rc 124 within
   about 3 s; the error starts with `no model output within 1s`; the process
   group is gone.
2. **Init event only.** A script that prints `{"type":"system","subtype":"init"}`
   then sleeps → still cut off at the deadline (an init event is not model
   output).
3. **Model output in time.** A script that prints
   `{"type":"stream_event","event":{}}` within 0.2 s, then sleeps 2 s and
   exits 0 → rc 0 (no cutoff), even with `first_output_s=1`.
4. **Unset deadline.** `first_output_s=None` with a silent 2 s script →
   rc 0, as today.
5. **`dispatch_with_fallback` over three lanes** (record the requests with a
   monkeypatched `dispatch_model`) → `first_output_s` is set on lanes 1 and 2
   and `None` on lane 3. A lane whose spec does not stream JSON gets `None`.
6. **`mo_llm_dispatch` with `MO_DISPATCH_FIRST_OUTPUT_S=0`** → no deadline is
   passed; with it unset → 300 is passed; with it `abc` → disabled plus one
   warning.
7. `is_model_event` table: `assistant`, `stream_event`, `user`, `result` →
   True; `system`, plain text, `{}` → False.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_first_output_deadline.py tests/test_dispatch_process_contract.py tests/unit/test_dispatch_timeout_wallclock.py tests/unit/test_dispatch_fallback_py.py tests/unit/test_dispatch_live_stream_py.py tests/unit/test_fallback_attempt_spend.py tests/unit/test_llm_dispatch_py.py tests/unit/test_mock_provider.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_first_output_deadline.py --junitxml="${MINI_ORK_RUN_DIR}/bif-b2-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-b2-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/dispatch/models.py mini_ork/dispatch/core.py mini_ork/dispatch/providers.py mini_ork/dispatch/llm_dispatch.py tests/unit/test_first_output_deadline.py` is clean.
- A lane that streams model output is never cut by the first-output
  deadline, and the last lane of a chain never gets one.
