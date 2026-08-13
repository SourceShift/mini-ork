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
output. Define recipes in code with `RecipeBuilder` (see below) or point at
YAML — both are supported.

### Provider policy & auto-init

`RunRequest` can carry a `ProviderPolicy` (e.g. `ProviderPolicy.codex_only()`),
which mini-ork writes to `.mini-ork/config/agents.yaml` before the run.
`auto_init=True` bootstraps `.mini-ork/` on first use (matching `mini-ork init`);
set it to `False` when your app manages initialization itself.

```python
from mini_ork import MiniOrk, ProviderPolicy, RunRequest
result = MiniOrk().run(RunRequest(
    kickoff="kickoff.md", recipe="docs", mode="dry-run",
    provider_policy=ProviderPolicy.codex_only(), auto_init=True,
))
```

### Recursive delegation — `spawn`

A run can spawn child runs. `spawn` is parent-centric: you name the parent run,
and mini-ork enforces recursion limits before approving a child.

```python
from mini_ork import MiniOrk, SpawnRequest
child = MiniOrk().spawn(SpawnRequest(
    parent_run_id="run-root-123", kickoff="child-task.md",
    recipe="code-fix", allow_child_spawn=True, mode="dry-run",
))
print(child.spawn_id, child.child_workspace, child.spawn_status)
```

Limits (env vars, with defaults): `MINI_ORK_RECURSIVE_MAX_DEPTH` (2),
`MINI_ORK_RECURSIVE_MAX_CHILDREN` (4), `MINI_ORK_RECURSIVE_MAX_DESCENDANTS`
(16), `MINI_ORK_RECURSIVE_MAX_PARALLEL` (4). Children run under
`.mini-ork/runs/<parent>/children/<child>/worktree/` and share the parent's
state db; the parent owns merge and publish.

### Defining recipes in code — `RecipeBuilder`

Build a recipe as typed Python instead of hand-writing YAML, then register it:

```python
from mini_ork import ExtensionRegistry, NodeSpec, RecipeBuilder

registry = ExtensionRegistry()
recipe = (
    RecipeBuilder("invoice-audit", "invoice_audit", "Audit invoices")
    .keywords("invoice", "tax", "vat")
    .node(NodeSpec(name="planner", type="planner", model_lane="planner"))
    .node(NodeSpec(name="verifier", type="verifier", verifier_ref="verifiers/check.sh"))
    .edge("planner", "verifier", "verifies")
    .build()
)
registry.recipe(recipe)

@registry.verifier("invoice-total")
def verify_invoice_total(artifact_path, plan_path):
    return artifact_path.exists() and plan_path.exists()
```

`RecipeBuilder` targets the same `recipes/<name>/` format the CLI reads
(`workflow.yaml`, `task_class.yaml`, prompts, verifiers) — code and YAML are two
front-ends to one contract.

### Transparency — what every result carries

`RunResult` is a transparency contract, not just a return code: it exposes the
exact `command`, the full `output`, line-by-line `events` (for streaming
adapters), `plan_path`, the retained `.mini-ork` home (`retained_home`), and
whether bootstrap ran (`init_ran` / `init_output`). `SpawnResult` adds
`spawn_id`, `child_run_id`, and `child_workspace`. A caller never has to scrape
terminal output or guess where evidence was written.

## Prompt optimization — GEPA, bring-your-own-evaluator

GEPA (Genetic-Pareto reflective prompt evolution) mutates a prompt by reflecting
on an evaluator's *natural-language* feedback, keeping a Pareto front of
candidates — ~35× fewer rollouts than RL. mini-ork wraps the external `gepa`
framework (`pip install gepa`) two ways:

- **Recipe mode** optimizes a recipe's node prompts, scored on real mini-ork runs
  against the state db (`MiniOrkGEPAAdapter`).
- **External mode** optimizes *any* seed prompt against *any* critic — no recipe,
  no state db. The critic is a command (any language) that reads
  `{"candidate": {...}, "task": {...}}` JSON on **stdin** and prints
  `{"score": <0..1>, "feedback": "<text>"}` as the **last non-blank line of
  stdout** (log freely above it). The critic's feedback is forwarded *verbatim*
  into GEPA's reflection — that NL diagnosis is the signal GEPA turns into
  targeted mutations.

```bash
python -m mini_ork.gepa.run_gepa \
    --seed-file seed_prompt.json \        # {component: text} map, or raw text → {"prompt": text}
    --eval-cmd "node dist/eval-c4.js" \    # your critic (stdin JSON → stdout {score,feedback})
    --trainset fixtures/*.json \           # each file = a task object (or JSON array of them)
    --reflection-lm anthropic/opus \
    --budget 30 --out out/gepa-c4          # winning {component}.txt + best_candidate.json
```

The seam is three composable pieces in `mini_ork.gepa` — `backends` is pure
stdlib (importable without the framework), the adapter composes it into GEPA:

```python
from mini_ork.gepa import (
    SubprocessRunBackend,      # cross-process / cross-language critic
    CallbackRunBackend,        # in-process Python: eval_fn(candidate, task) -> (score, feedback)
    GEPARunBackendAdapter,     # composes any RunBackend into gepa's adapter (needs `pip install gepa`)
)
import gepa

backend = CallbackRunBackend(lambda candidate, task: (score_it(candidate, task), "why"))
adapter = GEPARunBackendAdapter(backend, components=["prompt"])
result = gepa.optimize(
    seed_candidate={"prompt": seed_text}, trainset=tasks, adapter=adapter,
    reflection_lm="anthropic/opus", max_metric_calls=30,
)
```

**Fail-loud (zero-fallback).** A non-zero exit, unparseable stdout, a missing
`score`, or a `score` outside `[0, 1]` raises `EvaluatorError` and halts the run.
A broken evaluator is never silently scored `0.0` — that would poison the Pareto
front with a fake gradient. Multi-objective search comes for free: because GEPA's
Pareto front is *instance-level* (each trainset task is its own objective),
supplying several fixtures optimizes the whole vector, not just the mean.

## Runnable example

```bash
python3 examples/sdk/hello_world.py
```

Exercises backends, a real memory roundtrip, routing, and the fail-fast dispatch
contract — all in-process, no configuration.
