# B6: a `mock` provider kind with rule files, for deterministic dispatch tests

## Goal

mini-ork's retry, fallback walk, lane fuse and timeout logic is tested only
with ad-hoc fakes (`dispatch_fn` stubs, `tests/fixtures/bin/mo-fake-agent`
through the `executable` kind). The next changes to dispatch (a typed failure
classifier, a first-output deadline, held-lane cooldowns) need failures to be
reproducible. Bifrost ships a Mocker plugin for this.

Add a `kind: mock` lane. Its rules file maps a request (prompt regex, lane,
attempt number) to a response or to a preset failure, with optional
probability and latency, all deterministic.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/dispatch/mock_provider.py` (new)

```
PRESETS: dict[str, tuple[int, str]]
def load_rules(path: Path) -> dict
def next_attempt(lane: str) -> int
def dispatch(request: DispatchRequest, spec: ProviderSpec) -> DispatchResult
```

1. **`PRESETS`.** Each value is `(rc, error text)`; the text is chosen so
   today's `llm_dispatch.classify_error` gives the right category:
   - `rate_limit: (1, "HTTP 429 rate limit exceeded")` → capacity
   - `auth: (1, "API Error: 401 Unauthorized")` → auth
   - `quota: (1, "429 You exceeded your current quota")` → quota
   - `server_error: (1, "HTTP 500 Internal Server Error")` → provider
   - `overloaded: (1, "503 overloaded")` → capacity
   - `bad_request: (1, "400 Bad Request: invalid request")` → request
   - `stream_cut: (1, "unexpected EOF: partial stream")` → stream
   - `timeout: (124, "timeout after {timeout}s (mock)")`
2. **`load_rules(path)`.** YAML with `rules:` (a list) and an optional
   `default:`. Each rule has:
   - optional `match: {prompt: <regex>, lane: <name>, attempt: <int>}`;
   - exactly one of `respond: {text: <str>}` or
     `error: {preset: <PRESETS key>, message: <str?>, rc: <int?>}`;
   - optional `probability` (0–1, default 1) and `latency_ms` (default 0).
   An invalid file (bad YAML, unknown preset, both or neither of
   respond/error, a bad regex) raises `ValueError` naming the file and the
   rule index.
3. **`next_attempt(lane)`.** A per-lane attempt counter, starting at 1, kept
   in `<MINI_ORK_RUN_DIR or tempfile.gettempdir()>/.mock-attempts.json`,
   updated under an `fcntl.flock` lock (read, increment, atomic write). The
   counter is per process tree and run, so "attempt 1 fails, attempt 2
   succeeds" works across the retry loop and the fallback walk.
   `MO_MOCK_RESET=1` in the env truncates it on the first call of a process.
4. **`dispatch(request, spec)`.**
   - **Setup.** `lane = spec.model`; `attempt = next_attempt(lane)`; the rules
     path comes from `spec.env["MO_MOCK_RULES"]`, set by the builder.
   - **Matching.** The first rule whose every given `match` key matches
     (`re.search` on the prompt, exact lane, exact attempt) and whose
     probability check passes wins. The probability check is
     `random.Random(f"{os.environ.get('MO_MOCK_SEED','0')}:{lane}:{attempt}:{i}").random() < probability`.
     With no match, the `default` applies; with no default, the result is an
     error: rc 1, `"mock: no rule matched (lane=<lane>, attempt=<n>)"`.
   - **Latency.** Sleep `latency_ms`. If `latency_ms / 1000 > request.timeout_s`,
     sleep `timeout_s` instead and return the `timeout` preset.
   - **Results.**
     - A response → `DispatchResult(ok=True, rc=0, text=<text>, error="", model=lane, usage=TokenUsage(), cost_usd=0.0, duration_ms=<elapsed>)`.
     - An error → `ok=False`, rc from the preset or rule, `text=""`,
       `error=<message or preset text>` (formatted with the timeout).
   - **Audit trail.** Append one line per dispatch to
     `<run dir or tmp>/.mock-calls.jsonl`:
     `{lane, attempt, rule (index or "default"), outcome, at}`.

### 2. `mini_ork/dispatch/providers.py`

1. **Builder** `_build_mock(name, entry, root, extra_env, model_id) -> ProviderSpec`.
   - `entry["rules"]` is required: a path, relative to `mini_ork_root(root)`
     unless absolute. Load it with `mock_provider.load_rules`, so a bad file
     surfaces as the lane-preflight reason.
   - Return `ProviderSpec(model=name, command=(), env={**extra_env, "MO_MOCK_RULES": str(path)}, kind="mock")`.
   - Register it in `PROVIDER_KIND_BUILDERS["mock"]`.
2. **Engine.** `ENGINES["mock"]` is a `HarnessEngine` whose dispatch backend
   is `mock_provider.dispatch` and whose capabilities are ALL true, the same
   pattern as `UhpEngine` (~2465-2490), so MCP/skills/agent-doc axes never
   reject a mock dispatch with rc 66.
3. **Local only.** Add `"mock"` to `_LOCAL_ONLY_LANE_KINDS` (~1411).
4. **Do not** register a backend by lane name. Kind-based engine selection
   (`spec.kind in ENGINES`, ~1544) routes it.

### 3. Docs

`docs/operator/mock-provider.md` (new): the providers.yaml entry, the rules
format, the presets and their classifier categories, the attempt counter,
`MO_MOCK_SEED` / `MO_MOCK_RESET`, and two examples:
- "attempt 1 rate-limited, attempt 2 answers";
- "lead lane 401, fallback lane answers".

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/mock_provider.py` (new)
- `mini_ork/dispatch/providers.py`
- `tests/unit/test_harness_engine.py` (only add `"mock"` to the expected `ENGINES` set at ~36-38)
- `tests/unit/test_mock_provider.py` (new)
- `docs/operator/mock-provider.md` (new)

Do NOT modify `mini_ork/dispatch/llm_dispatch.py`, `mini_ork/dispatch/core.py`,
or any path in `config/kernel-frozen.yaml`. Every other existing test must
pass unchanged.

## Tests (`tests/unit/test_mock_provider.py`, hermetic, no network)

Follow `tests/unit/test_uhp_dispatch.py:505-605`: point `MINI_ORK_HOME` at a
nonexistent dir so the repo's real providers.yaml cannot win, and write a
registry to `MINI_ORK_PROVIDERS`. Set `MINI_ORK_RUN_DIR` to a tmp dir.

1. Each preset's text → `llm_dispatch.classify_error(text, rc)` returns the
   category listed in §1.1 (timeout: assert whatever the classifier returns
   today, and record it in a comment).
2. A rule with `match.prompt` and a default → the right text for a matching
   and a non-matching prompt.
3. `match.attempt: 1` with preset `rate_limit`, then a respond rule →
   `providers.dispatch_model` twice on the same lane gives a failure, then
   success; `.mock-attempts.json` shows 2.
4. **Fallback walk.** `dispatch_with_fallback` over `[mockA, mockB]`, where
   mockA's rules give `auth` and mockB's give a response → `ok`, text from B,
   `model == "mockB"`.
5. **Latency over the timeout.** `latency_ms: 5000` with `timeout_s=0.2` →
   rc 124 within about 1 s.
6. **Probability is deterministic.** The same `MO_MOCK_SEED` gives the same
   outcome sequence over 5 calls; a different seed changes it.
7. **A bad rules file** (unknown preset) → `providers.lane_health("mockA")`
   is not ok, and its reason names the file.
8. `.mock-calls.jsonl` gets one line per dispatch.
9. `ENGINES["mock"]` accepts a request carrying MCP / skills / agent-doc env
   (no rc 66).

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_mock_provider.py tests/unit/test_harness_engine.py tests/unit/test_uhp_dispatch.py tests/unit/test_provider_registry_py.py tests/unit/test_providers_registry.py tests/unit/test_dispatch_fallback_py.py tests/unit/test_lane_preflight_gate_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_mock_provider.py --junitxml="${MINI_ORK_RUN_DIR}/bif-b6-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-b6-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged
  apart from the one-word ENGINES set addition.
- `ruff check --select F,E9 mini_ork/dispatch/mock_provider.py mini_ork/dispatch/providers.py tests/unit/test_mock_provider.py` is clean.
- A mock lane never makes a network call and never spawns a subprocess.
