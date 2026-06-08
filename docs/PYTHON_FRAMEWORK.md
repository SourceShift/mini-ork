# mini-ork Python Framework

The Python framework is the public integration layer for mini-ork. It is not a
test runner and it is not a rewrite of the whole runtime in one step. It gives
Python applications a stable, typed API for running, extending, inspecting, and
embedding mini-ork while the existing CLI/Bash runtime remains the execution
engine underneath.

## Architecture Influences

The design follows current OSS agent framework patterns:

- **LangGraph**: workflows are explicit stateful graphs, and persistence is
  part of the runtime contract rather than an afterthought.
- **CrewAI**: separate the process controller from the autonomous worker team.
  In mini-ork terms, the Python facade controls the run while recipes own the
  agent/lens workflow.
- **AutoGen**: teams expose run lifecycle and portable state. mini-ork mirrors
  this through `RunRequest`, `RunResult`, `RunEvent`, and retained `.mini-ork`
  artifacts.
- **Pydantic AI / pydantic-graph**: use typed Python objects as the integration
  boundary. mini-ork starts with standard dataclasses to avoid forcing a new
  dependency, with room to add Pydantic validation later.

| Project | Useful architecture pattern | mini-ork adaptation |
| --- | --- | --- |
| LangGraph | Nodes, edges, state, and checkpoints are first-class runtime concepts. | `WorkflowSpec` models nodes/edges; `RunResult.retained_home` points to persisted run state. |
| CrewAI | Flows provide controlled orchestration while Crews provide autonomous agent work units. | `MiniOrk` controls the run lifecycle; recipes define planner/reviewer/verifier/lens teams. |
| AutoGen | Agent teams expose run/stream lifecycle and serializable run state. | `RunRequest`, `RunEvent`, and `RunResult` make lifecycle and evidence inspectable from Python. |
| Pydantic AI / pydantic-graph | Typed Python boundaries make agent applications easier to validate and extend. | Public dataclasses define the stable API without forcing a dependency on application code. |

## Public API

```python
from pathlib import Path

from mini_ork import MiniOrk, ProviderPolicy, RunRequest

result = MiniOrk().run(
    RunRequest(
        kickoff=Path("kickoff.md"),
        recipe="docs",
        mode="dry-run",
        provider_policy=ProviderPolicy.codex_only(),
        auto_init=True,
    )
)

print(result.ok)
print(result.command)
print(result.task_class)
print(result.plan_path)
print(result.retained_home)
print(result.init_ran)
```

Every run returns the exact command, working directory, combined output,
parsed run id, task class, plan path, verdict, and retained `.mini-ork` home.
This is the transparency contract: Python callers should never have to scrape
terminal output blindly or guess where evidence was written.

By default, `RunRequest(auto_init=True)` bootstraps `.mini-ork/` on the first
run if the project is not initialized. That can update `.gitignore`, matching
the CLI `mini-ork init` behavior. Set `auto_init=False` when an embedding
application wants to manage initialization itself.

## Recursive Delegation API

```python
from pathlib import Path

from mini_ork import MiniOrk, SpawnRequest

client = MiniOrk()

child = client.spawn(
    SpawnRequest(
        parent_run_id="run-root-123",
        kickoff=Path("child-task.md"),
        recipe="code-fix",
        child_run_id="run-child-001",
        allow_child_spawn=True,
        mode="dry-run",
    )
)

print(child.ok)
print(child.spawn_id)
print(child.child_workspace)
print(child.spawn_status)
```

`SpawnRequest` is intentionally parent-centric. The caller must name the parent
run, and mini-ork applies recursive limits before a child is approved:

- `MINI_ORK_RECURSIVE_MAX_DEPTH` default `2`
- `MINI_ORK_RECURSIVE_MAX_CHILDREN` default `4`
- `MINI_ORK_RECURSIVE_MAX_DESCENDANTS` default `16`
- `MINI_ORK_RECURSIVE_MAX_PARALLEL` default `4`

Children run under `.mini-ork/runs/<parent>/children/<child>/worktree/` and
share the parent's state database for lineage/event records. The parent remains
responsible for merge and publish decisions.

## Extension API

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

The registry is intentionally in-memory for the first framework layer. The
next migration step is a materializer that writes `RecipeSpec` into a
`recipes/<name>/` directory with `workflow.yaml`, `task_class.yaml`, prompts,
and verifiers.

## Transparency Model

Python integrations should expose:

- `RunResult.command`: exact subprocess command.
- `RunResult.output`: full combined output.
- `RunResult.events`: line-by-line event objects for streaming adapters.
- `RunResult.plan_path`: concrete plan JSON path when one was emitted.
- `RunResult.retained_home`: `.mini-ork` state/evidence directory.
- `RunResult.init_ran` and `RunResult.init_output`: whether bootstrap happened
  and the exact init transcript.
- `SpawnResult.spawn_id`, `SpawnResult.child_run_id`, and
  `SpawnResult.child_workspace`: recursive lineage and workspace evidence.
- Provider policy files written under `.mini-ork/config/agents.yaml`.

This mirrors mini-ork's operational model: plans, verifier evidence, gates,
and state rows are first-class artifacts.

## Migration Plan

1. Keep the CLI/Bash runtime as the execution backend.
2. Make Python the stable integration surface: typed API, extension registry,
   provider policy, transparent result model.
3. Move production validation code to consume `mini_ork.MiniOrk` rather than
   constructing subprocess calls itself.
4. Add recipe materialization from `RecipeSpec`.
5. Gradually replace shell internals behind the Python API where that reduces
   complexity without breaking existing CLI users.

## References

- LangGraph persistence: <https://docs.langchain.com/oss/python/langgraph/persistence>
- CrewAI architecture overview: <https://docs.crewai.com/introduction>
- AutoGen AgentChat teams: <https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/index.html>
- Pydantic AI overview: <https://pydantic.dev/docs/ai/overview/>
- Pydantic graph overview: <https://pydantic.dev/docs/ai/graph/graph/>
