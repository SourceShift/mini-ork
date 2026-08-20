# Promote the per-engine dispatch registries into one `HarnessEngine` object per engine

## Problem (one sentence)

The three harness engines (`claude`, `codex`, `opencode`) are described by
**two parallel engine-keyed registries plus a byte-identical copy-pasted pair of
dispatch backends**, so a fourth engine must rediscover four scattered
conventions and the codex/opencode transport logic is duplicated verbatim —
promote the engine-keyed concerns into **one `HarnessEngine` object per engine**.

## Grounding (verified in the live tree, `mini_ork/dispatch/providers.py`)

Three engines exist: `EXECUTABLE_MODELS = frozenset({"codex", "opencode"})`
(providers.py:26); `engine_of(model)` (providers.py:1171) returns the model for
those two else `"claude"`.

`dispatch_model` runs three stages (providers.py:739-788):

1. **Resolve spec** — `spec = resolve_provider(model, …)` (line 741), which
   dispatches on the providers.yaml **`kind`** via
   `PROVIDER_KIND_BUILDERS[kind]` (providers.py:511-518). There are **6 kinds**
   (`anthropic-native`, `anthropic-compat`, `openai-compat`, `codex-native`,
   `opencode-native`, `executable`) mapping onto **3 engines**.
2. **Rewrite argv** — `builder = ENGINE_COMMAND_BUILDERS.get(engine_of(model))`
   (line 759); only `{"claude": _claude_command_builder}` is registered
   (providers.py:1200-1204). The claude builder injects node-scoped tool grants
   + E4 turn-resume (providers.py:1179-1197).
3. **Dispatch** — `backend = MODEL_DISPATCH_BACKENDS.get(model, _dispatch_standard)`
   (line 787); `{"codex": _dispatch_codex_via_wrapper, "opencode":
   _dispatch_opencode_via_wrapper}` (providers.py:1232-1237).

`_dispatch_codex_via_wrapper` (providers.py:1082-1117) and
`_dispatch_opencode_via_wrapper` (providers.py:1120-1156) are **byte-identical
except their docstrings and two inline comments** — same tempfile dance, same
`DispatchRequest` rebuild, same `_read_codex_sidecars` (providers.py:1062), same
`finally` cleanup.

## The deliverable (exact target shape)

Introduce a `HarnessEngine` abstraction that unifies the **two engine-keyed**
registries (stages 2 + 3) and the `Capabilities` asymmetries, collapsing the
duplicated wrappers. Concretely, in `mini_ork/dispatch/providers.py`:

### 1. A `Capabilities` dataclass

```python
@dataclass(frozen=True)
class Capabilities:
    tool_grants: bool = False      # accepts --allowedTools/--mcp-config (claude only)
    resume: bool = False           # accepts --resume <id>          (claude only)
    session_capture: bool = False  # surfaces session_id to DispatchResult (claude only)
    byo_endpoint: bool = False     # BYO OpenAI endpoint via -c model_providers (codex only)
```

### 2. A `HarnessEngine` base with three concrete engines

```python
class HarnessEngine:
    name: str
    def build_command(self, command, *, request, env) -> tuple[str, ...]:
        return command                      # default: identity (executables)
    def dispatch(self, request, spec) -> DispatchResult:
        return _dispatch_standard(request, spec)   # default backend
    def capabilities(self) -> Capabilities:
        return Capabilities()               # default: no claude-only powers
```

- `ClaudeEngine` — `build_command` = the existing `_claude_command_builder`
  body (tool grants + resume); `dispatch` = `_dispatch_standard`;
  `capabilities` = `Capabilities(tool_grants=True, resume=True,
  session_capture=True)`.
- `SidecarTelemetryEngine(HarnessEngine)` — **the collapse.** Constructed with
  the transport's base `command` builder (or takes the resolved `spec.command`
  as today) and a `byo` flag. Its `dispatch` is the single shared body of the
  two wrapper functions (tempfile `.tokens`/`.cost`, rebuild `DispatchRequest`
  preserving `cwd`+`workspace`, `dispatch(req, spec.command)`, on `ok` fold
  `_read_codex_sidecars`, `finally` unlink the two paths + the derived
  `.stream.jsonl`). `build_command` = identity.
- Instantiate two: `SidecarTelemetryEngine(name="codex",
  capabilities=Capabilities(byo_endpoint=True))` and
  `SidecarTelemetryEngine(name="opencode", capabilities=Capabilities())`.

### 3. One engine registry + shimmed OCP hooks

```python
ENGINES: dict[str, HarnessEngine] = {
    "claude": ClaudeEngine(), "codex": <codex engine>, "opencode": <opencode engine>,
}
```

`dispatch_model` (providers.py:759-788) becomes:

```python
engine = ENGINES.get(engine_of(request.model), _DEFAULT_ENGINE)
command = engine.build_command(spec.command, request=request, env=effective_env)
if command != spec.command:
    spec = replace(spec, command=command)
… (env merge + _select_workspace unchanged) …
result = engine.dispatch(effective, spec)
```

Keep `register_engine_command_builder` and `register_dispatch_backend` (and
`ENGINE_COMMAND_BUILDERS`/`MODEL_DISPATCH_BACKENDS`) **as thin shims** that mutate
the corresponding `HarnessEngine` (wrap the passed callable so an external
consumer registering a builder/backend still works). External code imports these
names; do not break them.

## Hard invariants (a change that violates any of these is WRONG)

1. **`PROVIDER_KIND_BUILDERS` is NOT folded.** It is kind-keyed (6 kinds → 3
   engines) and is consumed by `resolve_provider` *before* `engine_of` is known.
   Leave it, `register_provider_kind`, and every `_build_*` spec builder exactly
   as-is; the `HarnessEngine` **consumes** a `ProviderSpec`, it does not build one.
2. **The A.3 ratchet holds structurally.** Executable engines must expose
   `capabilities().tool_grants is False` and a `build_command` that cannot inject
   claude flags — so claude argv can never leak into codex/opencode by
   construction. This replaces "executable engines have no
   `ENGINE_COMMAND_BUILDERS` entry."
3. **Behavior-preserving.** No ProviderSpec output changes for any kind; the
   codex/opencode sidecar protocol is unchanged (same env keys, same `.tokens`
   suffix requirement, same cleanup); claude tool-grant + resume injection is
   unchanged; `_stash_session_id` still runs after dispatch.
4. **The `openai-compat` BYO-codex path still works** — `capabilities().byo_endpoint`
   is descriptive only; do not reroute or gate dispatch on it.

## Files in scope (touch nothing else)

- `mini_ork/dispatch/providers.py` — add `Capabilities`, `HarnessEngine`,
  `ClaudeEngine`, `SidecarTelemetryEngine`, `ENGINES`; rewire `dispatch_model`;
  reduce `ENGINE_COMMAND_BUILDERS`/`MODEL_DISPATCH_BACKENDS`/their `register_*`
  to shims; delete the now-duplicate `_dispatch_opencode_via_wrapper` body by
  routing both engines through `SidecarTelemetryEngine`.
- `tests/unit/test_harness_engine.py` — NEW. Assert: (a) `ENGINES` has the three
  engines; (b) `ENGINES["codex"].capabilities().tool_grants is False` and same
  for opencode (ratchet); (c) `ENGINES["claude"].capabilities()` has
  tool_grants+resume+session_capture True; (d) `build_command` is identity for
  codex/opencode and injects for claude when `MO_TOOL_GRANTS_DISABLED != "1"`;
  (e) one `SidecarTelemetryEngine.dispatch` test that monkeypatches
  `dispatch`/`_read_codex_sidecars` and asserts usage/cost fold in on `ok`.

## Explicitly OUT of scope

- Do NOT touch `mini_ork/dispatch/codex_transport.py`,
  `opencode_transport.py`, `core.py`, `routing.py`, `llm_dispatch.py`, or
  `mini_ork/cli/execute.py`.
- Do NOT add `spawn`/`converse`/`observe`/`workspace` verbs — `spawn` and
  `workspace` are shared axes (`core.dispatch` / `request.workspace`), and
  `converse`/`observe` have zero engine consumers (see
  `internal-docs/research/2026-08-12-harness-engine-verb-enumeration.md`).
- Do NOT fold `PROVIDER_KIND_BUILDERS` (invariant 1).
- Do NOT edit `.mini-ork/config/agents.yaml` or any providers.yaml.

## Acceptance / verification

- `python3.11 -m pytest tests/unit/test_harness_engine.py -q` → all green.
- `python3.11 -m pytest tests/unit -k "dispatch or engine or provider or lane" -q`
  → no regressions (includes `test_engine_command_builder_py`, the A.3 ratchet).
- `python3.11 -c "from mini_ork.dispatch.providers import ENGINES, engine_of;
  print(sorted(ENGINES), ENGINES['codex'].capabilities().tool_grants)"`
  → prints `['claude', 'codex', 'opencode'] False`.

## Notes

mini-ork **self-edit** (edits its own dispatch core); the run sets
`MO_ALLOW_FRAMEWORK_CWD=1`. The change is a structure-preserving promotion:
every existing call site keeps working through shims, the spec-building path is
untouched, and the ratchet becomes an explicit capability instead of a
maintained registry-absence. Design rationale + the falsified roadmap verbs are
in `internal-docs/research/2026-08-12-harness-engine-verb-enumeration.md`.
