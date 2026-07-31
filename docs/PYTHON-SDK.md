# mini-ork Python SDK

mini-ork is usable two ways, and they compose:

1. **Primitives** — importable, in-process building blocks. No YAML, no
   subprocess, no provider credentials to *construct* them. This is what most
   applications embedding mini-ork actually want.
2. **Orchestrator** — the full `classify → plan → execute → verify` lifecycle,
   driven from Python via the `MiniOrk` client (it shells out to the `mini-ork`
   CLI behind a stable `--json` result contract).

## Install

```bash
pip install -e '.[full]'   # from a checkout; installs the CLI + runtime too
python3 -c "import mini_ork; print(sorted(dir(mini_ork)))"
```

## Primitives

```python
from mini_ork import (
    Crucible, RuntimeSpec, ExecOutcome, available_backends,  # verification
    dispatch_model, DispatchRequest, DispatchResult,          # model dispatch
    memory,                                                   # verified memory
    router, preferred_lane, recompute_advantages,             # bandit routing
)
```

Heavy modules load lazily (PEP 562), so `import mini_ork` stays cheap — you only
pay for the dispatch/runtime/memory machinery when you reference it.

### Execution-anchored verification — `Crucible`

The verdict on a change is what the code *did when it ran*, not a model's
opinion. `run_test` returns an `ExecOutcome` whose `status` is the primary
signal (`passed` / `failed` are the only ones that say anything about the
patch; `test_defect` / `error` / `apply_fail` / `no_run` are facts about the
harness).

```python
cru = Crucible(RuntimeSpec(image="python:3.11-slim", backend="auto"))
outcome = cru.run_test(test_src="def test_x():\n    assert add(2, 2) == 4\n", patch=my_patch)
if outcome.informative:          # passed or failed — real evidence
    ship = outcome.passed
elif outcome.test_is_broken:     # repair the probe, do not blame the patch
    ...
```

`available_backends()` reports where execution can happen (`docker`,
`subprocess`, `prime`, `modal`, `docker-cli`).

### Heterogeneous dispatch — `dispatch_model`

One call, any provider lane. An unroutable lane or a missing key comes back as a
**structured `ok=False`** result — never a raise, stall, or repo corruption.

```python
res = dispatch_model(DispatchRequest(model="codex", prompt="…"))
if res.ok:
    text, cost = res.text, res.cost_usd
else:
    handle(res.error, res.rc)
```

### Verified-outcome memory — `memory`

Scoped semantic memory backed by SQLite; the stdlib `HashEmbedder` means no new
dependency and no network for a basic roundtrip.

```python
from mini_ork import memory
memory.add("ingest lane retry budget is 3", scope="proj", infer=False)
hits = memory.search("how many ingest retries?", scope="proj", top_k=3)
```

### Cost-free bandit routing — `router`

`preferred_lane(task_class, node_type=…)` returns the lane that has been winning
for that role, learned from verified outcomes at zero extra model calls. It
reads a warmed state db (`mini-ork init` + a few runs); with no priors it
returns the default.

## Orchestrator

```python
from mini_ork import MiniOrk, RunRequest
mo = MiniOrk()
result = mo.run(RunRequest(kickoff="kickoff.md", recipe="code-fix", mode="live"))
print(result.run_id, result.verdict, result.ok)
```

`RunResult` is populated from the CLI's machine-readable `--json` contract
(`mini_ork_result={…}`), so `run_id` / `task_class` / `plan_path` / `verdict` /
`returncode` are parsed from one structured line rather than scraped from human
output. Define recipes in code with `RecipeBuilder` (see `mini_ork.extensions`)
or point at YAML — both are supported.

## Runnable example

```bash
python3 examples/sdk/hello_world.py
```

Exercises backends, a real memory roundtrip, routing, and the fail-fast dispatch
contract — all in-process, no configuration.
