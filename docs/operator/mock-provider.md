# Mock provider (`kind: mock`)

A mock lane answers from a **rules file** instead of a model. It is how retry,
fallback-walk, lane-fuse and timeout behavior is tested deterministically: the
same request against the same rules produces the same result, with no network
call and no subprocess. Use it in unit tests and in recipe smoke runs that need
a reproducible failure.

## providers.yaml entry

```yaml
providers:
  mockA:
    kind: mock
    rules: tests/fixtures/mock-rules.yaml   # required; absolute or repo-relative
    extra_env:                              # optional, merged into the lane env
      MO_MOCK_MARKER: "1"
```

`rules` is resolved against `mini_ork_root(root)` unless it is absolute. The
builder exports the resolved path to the lane environment as `MO_MOCK_RULES`,
which is what the mock dispatcher reads. The file is loaded during lane
preflight, so a malformed rules file makes the lane report unhealthy with the
file name and rule index — it never fails halfway through a run. A mock lane is
local-only: under `--placement remote` it stays on the current machine
(shipping a rule-file lane to a remote node buys nothing).

Select the lane like any other (`dispatch_model`, a recipe's `lane:`, or
`dispatch_with_fallback`). Engine selection is **by kind** (`spec.kind in
ENGINES`); there is no per-lane backend registration, so any lane name works.

## Rules file format

```yaml
rules:                       # evaluated in order; first match wins
  - match:                   # optional; every given key must match
      prompt: "retry"        # re.search() against the request prompt
      lane: mockA            # exact lane name
      attempt: 1             # exact attempt number (1-based per lane)
    respond:                 # a canned success
      text: "the answer"
    probability: 0.5         # optional, 0–1, default 1
    latency_ms: 250          # optional, default 0
  - error:                   # a preset failure (exactly one of respond/error)
      preset: rate_limit
      message: "custom text" # optional; overrides the preset text
      rc: 7                  # optional; overrides the preset exit code
default:                     # optional; applied when no rule matches
  respond:
    text: "fallback answer"
```

Exactly one of `respond` or `error` is required per rule (and for `default`).
`load_rules` raises `ValueError` naming the file and the rule index for bad
YAML, an unknown preset, both/neither of `respond`/`error`, or an invalid
`match.prompt` regex.

When nothing matches and there is no `default`, the dispatch fails with
`rc=1` and `mock: no rule matched (lane=<lane>, attempt=<n>)`.

The probability roll is
`random.Random(f"{MO_MOCK_SEED}:{lane}:{attempt}:{rule_index}").random() < probability`,
so it is reproducible for a given seed and varies across attempts and rules.

## Presets

Each preset is `(rc, error text)`; the text is chosen so
`llm_dispatch.classify_error` returns the category callers branch on.

| preset         | rc  | error text                            | classifier |
| -------------- | --- | ------------------------------------- | ---------- |
| `rate_limit`   | 1   | `HTTP 429 rate limit exceeded`        | capacity   |
| `auth`         | 1   | `API Error: 401 Unauthorized`         | auth       |
| `quota`        | 1   | `429 You exceeded your current quota` | quota      |
| `server_error` | 1   | `HTTP 500 Internal Server Error`      | provider   |
| `overloaded`   | 1   | `503 overloaded`                      | capacity   |
| `bad_request`  | 1   | `400 Bad Request: invalid request`    | request    |
| `stream_cut`   | 1   | `unexpected EOF: partial stream`      | stream     |
| `timeout`      | 124 | `timeout after {timeout}s (mock)`     | unknown    |

`timeout` has no classifier bucket today; it surfaces as `unknown` with rc 124.
A rule whose `latency_ms / 1000` exceeds the request's `timeout_s` sleeps only
`timeout_s` and returns this preset (the caller's deadline is burned, not
extended).

## Attempt counter

Attempts are per lane, start at 1, and persist for the run in
`<MINI_ORK_RUN_DIR or tempfile.gettempdir()>/.mock-attempts.json`. The file is
read, incremented and atomically replaced under an `fcntl.flock`, so the retry
loop and the fallback walk share one counter and concurrent processes in the
same run tree never hand out the same number.

- `MO_MOCK_RESET=1` truncates the counter on the first `next_attempt` call of
  the process (not every call), giving each test a clean attempt 1.
- `MO_MOCK_SEED` (default `"0"`) seeds the probability roll. Same seed +
  same lane/attempt/rule index → same outcome.

Every dispatch appends one line to `<run dir or tmp>/.mock-calls.jsonl`:
`{lane, attempt, rule, outcome, at}` (`rule` is the rule index or `"default"`).

## Examples

### Attempt 1 rate-limited, attempt 2 answers

```yaml
rules:
  - match: {attempt: 1}
    error: {preset: rate_limit}
  - respond: {text: "recovered on the second attempt"}
```

The first `dispatch_model` on the lane returns rc 1 with
`HTTP 429 rate limit exceeded`; the retry gets attempt 2 and the response.

### Lead lane 401, fallback lane answers

```yaml
# mockA.rules.yaml
rules:
  - error: {preset: auth}
```

```yaml
# mockB.rules.yaml
rules:
  - respond: {text: "answer from the fallback lane"}
```

```python
result = dispatch_with_fallback(request, ["mockA", "mockB"])
assert result.ok and result.model == "mockB"
```

The `auth` failure is non-retryable, so the walk moves straight to `mockB`.
