# Framework edit — wire `opencode` as a live mini-ork dispatch engine

## Goal

Add `opencode` as a first-class dispatch engine alongside `claude` and `codex`,
by mirroring the existing **codex-native sidecar** pattern. After this change,
a provider of `kind: opencode-native` resolves to a self-contained transport
subprocess that shells out to the `opencode` CLI, parses its JSON output into
mini-ork's `{text, usage, cost}` result contract, and is dispatchable as a lane.

This is the concrete *second, differently-shaped* executable engine that makes
the deferred `HarnessEngine` Protocol non-speculative (two real adapters instead
of one). Scope here is ONLY the engine wiring — do NOT design the Protocol.

## Files in scope

Touch ONLY these files. Do not modify anything else (the scope gate enforces this):

- `mini_ork/dispatch/opencode_transport.py`  — NEW sidecar (mirror `codex_transport.py`)
- `mini_ork/dispatch/providers.py`            — MODIFY (register the new engine + kind)
- `config/providers.yaml`                     — MODIFY (add the `opencode` provider block)
- `tests/unit/test_opencode_engine.py`        — NEW unit test

Do NOT modify `.mini-ork/config/**` (run-local routing is wired by hand after merge).

## Background — the template to copy

The `codex` engine is the exact structural precedent. Read these first:

- `mini_ork/dispatch/codex_transport.py` — the sidecar template. It is invoked as
  `python -m mini_ork.dispatch.codex_transport --print --output-format text`,
  reads the prompt on **stdin** (E2BIG-proof), accepts-and-ignores claude-dialect
  args (`--print`, `--output-format`, `text`), honors the **cwd guard**
  (`MO_ALLOW_FRAMEWORK_CWD`) and env hardening (`GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS`/`SSH_ASKPASS=/bin/false`), then parses the CLI's streamed events
  into `text` + `usage` + `cost`.
- `mini_ork/dispatch/providers.py`, specifically:
  - `EXECUTABLE_MODELS = frozenset({"codex"})` — add `"opencode"`.
  - `engine_of(model)` — returns the model when executable, else `"claude"`.
  - `ENGINE_COMMAND_BUILDERS = {"claude": _claude_command_builder}` — executable
    engines deliberately have NO builder (the A.3 **ratchet**: a claude-only argv
    cannot leak into an executable engine by construction). Do NOT add an
    `opencode` builder here — keep the ratchet.
  - `_codex_transport_command()` + `_build_codex_native()` — the command-factory
    and the `kind` builder to mirror.
  - `PROVIDER_KIND_BUILDERS` / `register_provider_kind(kind, builder)` — how kinds
    register.
  - `MODEL_DISPATCH_BACKENDS = {"codex": _dispatch_codex_via_wrapper}` — mirror the
    codex entry for `opencode` only if codex needs a dedicated backend; if the
    `ProviderSpec.command` is self-contained (it is), prefer `_dispatch_standard`
    and add no special backend. Match whatever codex actually does.

## `opencode` CLI facts (verified: v1.18.4 at `/opt/homebrew/bin/opencode`)

- Headless invocation: `opencode run [message..] --format json -m <provider>/<model> --dir <cwd>`
- `--format` choices are `default | json`; use `json`.
- `--auto` auto-approves permissions (required for non-interactive dispatch).
- Auth is ambient at `~/.local/share/opencode/auth.json` (like codex — no key in env).
- Model lever: read `MO_OPENCODE_MODEL` from env and pass it via `-m` when set;
  otherwise omit `-m` and let opencode use its configured default.

**Re-confirm the exact flags before coding** by running `opencode run --help`
(you have Bash). In particular verify whether `opencode run` reads the prompt on
**stdin**; if it does, use stdin (mirror codex). If it does NOT, pass the prompt
as the `message` positional arg. Parse the `--format json` payload for the
assistant text and any token/cost fields it exposes; if a field is absent, emit
`0`/empty rather than fabricating it.

## Requirements

1. `opencode_transport.py` must be a self-contained module runnable as
   `python -m mini_ork.dispatch.opencode_transport --print --output-format text`,
   accepting-and-ignoring those claude-dialect args exactly as codex does.
2. Honor the cwd guard: refuse a cwd inside the mini-ork framework tree unless
   `MO_ALLOW_FRAMEWORK_CWD=1` (reuse/mirror codex's guard helpers; do not weaken).
3. Apply the same env hardening codex applies before spawning the CLI.
4. `_build_opencode_native` returns a `ProviderSpec` whose `command` is the
   transport invocation, mirroring `_build_codex_native`.
5. Register `kind: opencode-native` via the same mechanism codex uses.
6. Add `"opencode"` to `EXECUTABLE_MODELS` so `engine_of("opencode") == "opencode"`
   and the ratchet holds (no `ENGINE_COMMAND_BUILDERS["opencode"]`).
7. `config/providers.yaml`: add an `opencode` provider block with
   `kind: opencode-native` (mirror the `codex` block's shape).
8. Preserve every existing public symbol and all `register_*` OCP hooks; add,
   don't rename.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` exists and applies cleanly with
  `git apply --check`.
- The diff creates `mini_ork/dispatch/opencode_transport.py` and adds the
  `providers.py` wiring (kind builder + `EXECUTABLE_MODELS` entry).
- `tests/unit/test_opencode_engine.py` exists and asserts, at minimum:
  - `"opencode" in EXECUTABLE_MODELS` and `engine_of("opencode") == "opencode"`;
  - `"opencode" not in ENGINE_COMMAND_BUILDERS` (the ratchet);
  - `resolve_provider` for an `opencode-native` entry returns a `ProviderSpec`
    whose `command` invokes `mini_ork.dispatch.opencode_transport`;
  - the transport builds an `opencode run … --format json` argv (mock the
    subprocess; assert `--auto` and `--dir <cwd>` are present, and `-m` is passed
    iff `MO_OPENCODE_MODEL` is set).
- `verdict.json` reports `{files_changed > 0, tests_pass: true, static_pass: true,
  pass: true}` written by the verifier nodes (do NOT pre-write it).

## Do NOT

- Do NOT add an `opencode` entry to `ENGINE_COMMAND_BUILDERS` (breaks the ratchet).
- Do NOT design or introduce a `HarnessEngine` Protocol / abstract base class.
- Do NOT modify `.mini-ork/config/**`, or any file outside the scope list.
- Do NOT hand-wave token/cost parsing — emit real values from the JSON or `0`.
