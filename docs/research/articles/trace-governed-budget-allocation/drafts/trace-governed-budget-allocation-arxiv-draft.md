# Trace-Governed Budget Allocation for Cost-Efficient Multi-Agent LLM Software Workflows

**Draft status:** working arXiv manuscript draft
**Target area:** cs.SE / cs.AI / cs.MA
**Primary artifact:** mini-ork, a heterogeneous-family multi-agent task orchestrator
**Central claim type:** formal policy properties plus empirical evaluation plan

## Abstract

Multi-agent LLM systems are increasingly used for software engineering tasks, but
their cost and reliability remain difficult to control. A common operational
pattern is to use a frontier model as a planner or reviewer while delegating
implementation work to cheaper agents. Existing routing work studies model
selection, task difficulty, and budget allocation, but less attention has been
given to persistent execution traces as the control signal for deciding when to
delegate, retry, escalate, or stop. We introduce **trace-governed budget
allocation**, a policy class for heterogeneous multi-agent workflows that uses
event-sourced run traces, verifier outcomes, role labels, and cumulative budget
state to allocate future inference spend. The method is implemented in
mini-ork, an open-source orchestration framework with lane-based model routing,
deterministic verifier gates, durable run state, and process-level isolation.
We formalize the workflow as a directed acyclic execution graph with typed
nodes and typed edges, prove safety and budget-boundedness properties for a
monotone budget governor, and define an empirical protocol comparing
trace-governed routing against frontier-only, cheap-only, static role mapping,
and query-level workflow generation baselines. The proposed evaluation measures
success rate, cost per successful task, wall-clock latency, verifier pass rate,
retry count, and escalation frequency on software engineering tasks.

## 1. Introduction

LLM-based software agents now perform planning, editing, testing, review, and
documentation tasks in repository-scale environments. As these systems grow
from single-agent loops into multi-agent workflows, two practical constraints
become dominant.

First, high-capability models are expensive. Assigning every workflow step to a
frontier model can produce strong reasoning but poor cost efficiency. Second,
cheap models are useful but not uniformly reliable. Assigning all work to a
low-cost model can reduce spend while increasing failed attempts, retry loops,
and human intervention.

This creates an orchestration problem: the system should spend high-capability
tokens only where they have high marginal value. In software workflows, these
points are rarely identical across a whole run. Planning, high-risk review, and
failed-verifier recovery may justify expensive reasoning. Mechanical editing,
source collection, formatting, and bounded implementation steps may not.

Recent work on multi-agent routing and budget-aware orchestration studies
related questions, including multi-agent model routing, budgeted LLM
orchestration, cost-aware routing, selective delegation, and difficulty-aware
workflow construction. However, production agent systems expose a control
signal that is underused in many formulations: the execution trace. A workflow
trace records which node ran, which role it played, what it read or produced,
which verifier passed or failed, how much budget was consumed, and whether the
run escalated, retried, or completed.

This paper asks:

> Can persistent execution traces be used as a governance layer for reducing
> multi-agent LLM cost while preserving software-task success?

We study this question through mini-ork, a task orchestrator whose recipes are
directed workflows over heterogeneous model lanes. mini-ork separates framework
primitives from userland recipes: the framework supplies classify, plan,
execute, verify, reflect, improve, and publish stages, while each recipe
defines its own node graph, artifact contract, and verifier scripts.

Our thesis is that traces should be treated as first-class control inputs, not
post-hoc observability data. A trace-governed workflow can use previous node
outcomes to decide whether the next unit of work should be delegated to a cheap
worker, retried with context, escalated to a frontier planner, or stopped by a
budget gate.

## 2. Contributions

This draft makes four contributions.

1. **Trace-governed budget allocation.** We define a policy class that maps
   workflow state, role labels, verifier outcomes, retry history, and remaining
   budget to model-lane choices.

2. **A formal workflow model.** We model an agentic software workflow as a
   typed directed acyclic graph with planner, researcher, implementer,
   reviewer, verifier, reflector, publisher, and rollback nodes.

3. **Policy-level guarantees.** Under explicit assumptions, we prove
   budget-boundedness, verifier safety, and monotone escalation properties for
   a trace-governed budget governor.

4. **A reproducible experimental protocol.** We define baselines, task suites,
   metrics, and ablations for evaluating whether trace-governed routing reduces
   cost per successful task relative to frontier-only, cheap-only, static role
   mapping, and query-level workflow generation.

## 3. Related Work

### 3.1 Budget-Aware LLM Routing

Budget-aware and cost-aware routing has become a central theme in recent
agentic systems research. `MasRouter` studies LLM routing for multi-agent
systems. `Budget-Aware Agentic Routing via Boundary-Guided Training` frames
routing as a sequential, path-dependent decision problem under strict budget
constraints. `ZEBRA` considers zero-shot allocation of a fixed monetary budget
across orchestration phases. `CASTER` introduces context-aware routing for
task-efficient multi-agent systems. `xRouter` uses reinforcement learning for
cost-aware LLM orchestration.

These works motivate the same economic problem as mini-ork: not every step
should receive the same model. Our focus differs in that the control signal is
not only the prompt or task description, but the accumulated execution trace of
the workflow.

### 3.2 Adaptive Multi-Agent Orchestration

Recent orchestration work studies whether workflows should be generated
dynamically per query, selected from a library, or adapted during execution.
`Difficulty-Aware Agentic Orchestration` argues that static workflows can
over-process easy tasks and under-process difficult ones. `Uno-Orchestra`
studies parsimonious delegation and worker choice. `AdaptOrch` emphasizes that
topology can matter more than individual model choice as model performance
converges. `Flow` studies modular agentic workflow automation with runtime
adjustment.

mini-ork occupies a complementary position: recipes define reusable workflows,
while run traces and gates provide dynamic control inside the workflow.

### 3.3 Verification and Test-Time Compute

Verification is a second major trend. `Multi-Agent Verification` studies
scaling test-time compute with multiple verifiers. `MAS-ProVe` studies process
verification for multi-agent systems. `FormalJudge` explores neuro-symbolic
agentic oversight. Work on auditing reasoning trees argues that process-level
auditing can outperform majority vote and simple LLM-as-judge approaches.

mini-ork follows this line by treating deterministic verifiers as first-class
gates. A verifier gate is not another opinion; it is an executable
specification such as a test, typecheck, schema validation, or artifact-shape
check.

### 3.4 Software Engineering Agents

Software engineering agent papers increasingly study execution grounding and
resource use. `AgentForge` argues for sandboxed execution as a first-class
principle. `Tokenomics` quantifies where tokens are used in agentic software
engineering. `Co-Saving` studies resource-aware multi-agent collaboration for
software development. Work on trajectory reduction identifies growing
multi-turn context as a major cost driver.

This paper builds on the same observation: software agents need cost
governance at the workflow level, not only at the single-prompt level.

## 4. System Model

Let a workflow be a directed acyclic graph

$$
G = (V, E)
$$

where each node $v \in V$ has a type

$$
\tau(v) \in \{\text{planner}, \text{researcher}, \text{implementer},
\text{reviewer}, \text{verifier}, \text{reflector}, \text{publisher},
\text{rollback}\}.
$$

Each edge $e = (u, v) \in E$ has an edge type

$$
\epsilon(e) \in \{\text{depends\_on}, \text{supplies\_context\_to},
\text{verifies}, \text{blocks}, \text{retries}, \text{escalates\_to}\}.
$$

Let $M$ be the set of available model lanes. A lane is a symbolic role such
as `planner`, `worker`, `reviewer`, `glm_lens`, `kimi_lens`, or `opus_lens`.
Each lane resolves to a provider family through a configuration mapping

$$
\lambda : M \rightarrow P
$$

where $P$ is the set of provider families.

A run trace after $t$ node attempts is

$$
H_t = \{h_1, h_2, \dots, h_t\}.
$$

Each trace event $h_i$ contains at least:

$$
h_i = (v_i, \tau(v_i), m_i, c_i, r_i, g_i, a_i)
$$

where $v_i$ is the node, $m_i$ is the selected model lane, $c_i \ge 0$ is
cost, $r_i$ is runtime, $g_i$ is the verifier or gate outcome, and $a_i$
is an artifact reference.

The cumulative cost at time $t$ is

$$
C(H_t) = \sum_{i=1}^{t} c_i.
$$

The workflow has a budget $B > 0$. A valid run must satisfy

$$
C(H_t) \le B
$$

for every prefix $H_t$.

## 5. Trace-Governed Budget Allocation

A trace-governed budget allocation policy is a function

$$
\pi(v, H_t, B) \rightarrow m
$$

that chooses a model lane $m \in M$ for the next node attempt.

The policy may inspect:

- the node type $\tau(v)$;
- the previous verifier outcomes in $H_t$;
- the number of retries for node $v$;
- the remaining budget $B - C(H_t)$;
- the artifact risk class;
- the model-lane cost profile;
- the confidence or severity of reviewer feedback.

The policy must not inspect hidden state from future nodes. It is therefore
online: decisions are made from the prefix trace only.

### 5.1 Policy Skeleton

A simple trace-governed policy has four rules.

1. **Cheap-first implementation.** Use a low-cost worker lane for bounded
   implementer tasks while no verifier has failed.

2. **Frontier planning and recovery.** Use a frontier lane for planning and for
   recovery after verifier failure, repeated retries, or high-risk artifact
   changes.

3. **Verifier-gated continuation.** Do not run publisher nodes unless required
   verifier gates pass.

4. **Budget stop.** If the next selected lane would exceed the remaining
   budget, stop, rollback, or escalate to a human gate.

### 5.2 Risk-Weighted Escalation Score

Let the escalation score for a node be

$$
S(v, H_t) =
\alpha R(v) + \beta F(v, H_t) + \gamma K(v, H_t) + \delta U(v, H_t),
$$

where:

- $R(v)$ is the static risk score of the artifact touched by $v$;
- $F(v, H_t)$ is the number or severity of failed verifier events relevant to
  $v$;
- $K(v, H_t)$ is the retry count for $v$;
- $U(v, H_t)$ is uncertainty from reviewer disagreement or missing evidence;
- $\alpha, \beta, \gamma, \delta \ge 0$.

For threshold $\theta$, the policy selects a frontier lane when

$$
S(v, H_t) \ge \theta
$$

and a cheaper lane otherwise, subject to the budget constraint.

## 6. Formal Properties

The following propositions establish basic safety properties of the policy
class. They are intentionally modest: they prove what the budget governor can
guarantee by construction, not that the empirical task outcome will always be
correct.

### Proposition 1: Prefix Budget Boundedness

Let $B > 0$ be the run budget. Suppose policy $\pi$ selects a lane for node
$v_{t+1}$ only if the estimated maximum cost of the next attempt,
$\widehat{c}_{t+1}$, satisfies

$$
C(H_t) + \widehat{c}_{t+1} \le B.
$$

Assume actual cost is bounded by the estimate:

$$
c_{t+1} \le \widehat{c}_{t+1}.
$$

Then every trace prefix produced by $\pi$ satisfies

$$
C(H_t) \le B.
$$

**Proof.** We prove by induction on $t$. For $t=0$, $C(H_0)=0 \le B$.
Assume $C(H_t) \le B$. The policy permits the next attempt only when
$C(H_t)+\widehat{c}_{t+1}\le B$. Since $c_{t+1}\le \widehat{c}_{t+1}$, we
have

$$
C(H_{t+1}) = C(H_t) + c_{t+1}
\le C(H_t) + \widehat{c}_{t+1}
\le B.
$$

Thus the invariant holds for $t+1$. By induction, all prefixes are
budget-bounded. $\square$

### Proposition 2: Verifier-Gated Publication Safety

Let $p$ be a publisher node. Suppose every path to $p$ contains a verifier
node $q$ with an edge type `verifies`, and suppose the executor blocks $p$
unless $q$'s latest gate result is pass. Then no artifact is published from a
run whose required verifier failed or is absent.

**Proof.** By construction, $p$ is reachable only after its predecessor
requirements are satisfied. Since every path to $p$ contains a verifier gate
$q$, and the executor blocks $p$ unless $q$ has a pass result, any run
that reaches $p$ must contain a passing result for each required verifier.
If a verifier failed or did not run, the precondition for $p$ is false, so
$p$ cannot execute. Therefore publication cannot occur without the required
passing verifier result. $\square$

### Proposition 3: Monotone Escalation

Assume the escalation score is

$$
S(v, H_t) =
\alpha R(v) + \beta F(v, H_t) + \gamma K(v, H_t) + \delta U(v, H_t)
$$

with nonnegative coefficients. If a trace extension increases any of
$F$, $K$, or $U$ and leaves all other terms unchanged, then
$S(v, H_t)$ does not decrease.

**Proof.** Let $H_{t'}$ be a trace extension of $H_t$. Suppose one or more
of $F$, $K$, or $U$ increases, no term decreases, and
$\alpha,\beta,\gamma,\delta \ge 0$. Then the difference

$$
S(v,H_{t'}) - S(v,H_t)
$$

is a sum of nonnegative coefficients multiplied by nonnegative term
differences. The result is nonnegative. Therefore escalation score is monotone
under added failures, retries, or uncertainty. $\square$

### Proposition 4: Cheap-First Dominance Under Equal Success

Consider two policies over the same workflow prefix: a frontier-only policy
$\pi_f$ and a cheap-first policy $\pi_c$. Suppose for each implementation
node before the first verifier failure, both policies have equal probability of
eventual verifier pass, and the cheap lane has cost no greater than the
frontier lane. Then the expected cost of $\pi_c$ before the first verifier
failure is no greater than that of $\pi_f$.

**Proof.** Over the prefix before the first verifier failure, both policies
execute the same set of implementation nodes by assumption. For each such node
$v_i$, let the frontier cost be $c_f(v_i)$ and the cheap cost be
$c_c(v_i)$, with $c_c(v_i) \le c_f(v_i)$. The expected prefix cost is the
sum of node costs weighted by the probability that the node is reached. The
reach probabilities are equal because the policies have equal pass probability
over the prefix by assumption. Therefore each weighted term for $\pi_c$ is
no larger than the corresponding term for $\pi_f$, and the total expected
cost is no larger. $\square$

## 7. Implementation in mini-ork

mini-ork implements the abstractions required by the formal model.

### 7.1 Typed Workflow Nodes

The framework defines eight node roles: planner, researcher, implementer,
reviewer, verifier, reflector, publisher, and rollback. Recipes define the
specific workflow graph in `workflow.yaml`. This makes the workflow shape a
userland artifact while preserving a common execution contract.

### 7.2 Lane-Based Model Routing

Workflow nodes reference symbolic lanes rather than concrete vendor models.
The lane binding is stored in configuration, for example:

```yaml
lanes:
  planner: opus
  worker: codex
  reviewer: opus
  verifier: glm
  glm_lens: glm
  kimi_lens: kimi
  codex_lens: codex
  opus_lens: opus
```

This indirection lets a recipe preserve its role structure while the operator
changes provider families according to price, availability, or experimental
condition.

### 7.3 Durable Trace State

The state database records run lifecycle, task runs, execution traces, model
costs, artifacts, gate results, gradients, workflow candidates, benchmark
results, and promotion decisions. This makes traces available for later
analysis and for cross-cycle improvement.

### 7.4 Deterministic Verifier Gates

Recipes can ship executable verifier scripts. For software tasks, these gates
may include typecheck, tests, schema checks, lint, migration replay, or artifact
shape validation. Verifier output is a control signal for the policy, not only
an audit log.

## 8. Experimental Protocol

The empirical question is whether trace-governed routing lowers cost per
successful task without reducing correctness.

### 8.1 Task Suites

We propose four task suites.

1. **Code-fix tasks.** Small repository bugs with deterministic tests.
2. **Documentation tasks.** Edits requiring source fidelity and link checks.
3. **Research-synthesis tasks.** Literature synthesis with citation-verifier
   gates and human review.
4. **Ops-runbook tasks.** Operational diagnosis tasks with shell-based
   verifier probes.

### 8.2 Baselines

The current evaluation compares four policies.

1. **Frontier-only.** Every model-executed node uses the most capable expensive
   lane.
2. **Cheap-only.** Every model-executed node uses the cheapest available lane.
3. **Static role mapping.** Planner and reviewer use frontier lanes;
   implementers use cheap lanes; no trace-dependent escalation.
4. **Trace-governed routing.** Use trace prefix, verifier outcomes, retry
   count, role, and budget state to route each next attempt.

A future extension should add **query-level workflow generation** as a fifth
baseline: generate or select a workflow per task before execution, then keep
routing fixed inside the run.

### 8.3 Metrics

Primary metrics:

$$
\text{Cost per successful task}
=
\frac{\text{Total cost across runs}}{\text{Number of successful runs}}.
$$

$$
\text{Verifier pass rate}
=
\frac{\text{Runs with required verifier pass}}{\text{Total runs}}.
$$

Secondary metrics:

- total token cost;
- wall-clock duration;
- retry count;
- escalation count;
- rollback count;
- human intervention count;
- artifact acceptance rate;
- failure mode distribution.

### 8.4 Hypotheses

**H1.** Trace-governed routing reduces cost per successful task relative to
frontier-only routing.

**H2.** Trace-governed routing improves success rate relative to cheap-only
routing.

**H3.** Trace-governed routing reduces unnecessary frontier-model calls
relative to static role mapping when verifier outcomes are clean.

**H4.** Trace-governed routing reduces repeated failed cheap-model attempts
relative to static role mapping when verifier failures accumulate.

### 8.5 Ablations

We propose the following ablations.

- Remove verifier outcome features from the escalation score.
- Remove retry count from the escalation score.
- Remove artifact risk class.
- Disable heterogeneous-provider routing and use one model family only.
- Replace deterministic verifier gates with LLM reviewer gates.

These ablations separate the effect of trace governance from the effect of
model heterogeneity or simple cost-tiering.

### 8.6 Controlled mini-ork Benchmark Result

We ran a controlled mini-ork benchmark on 2026-06-10 and 2026-06-11. The
benchmark contains three task classes: `obs_smoke`, `docs`, and `code_fix`.
Each policy was evaluated on 12 live LLM runs, for 48 total runs. All runs
reached `published` status and all deterministic verifier checks passed.

| Policy | Runs | Success rate | Verifier pass rate | Reviewer accept rate | Cost / success | Median time | Expensive calls |
|---|---:|---:|---:|---:|---:|---:|---:|
| Frontier-only | 12 | 1.0 | 1.0 | 0.8889 | 0.536418 | 87.0s | 67 |
| Cheap-only | 12 | 1.0 | 1.0 | 1.0 | 0.435822 | 132.0s | 12 |
| Static role mapping | 12 | 1.0 | 1.0 | 0.8889 | 0.416641 | 93.0s | 31 |
| Trace-governed | 12 | 1.0 | 1.0 | 0.8889 | 0.393587 | 99.0s | 27 |

Trace-governed routing reduced cost per successful task by 26.63% versus
frontier-only routing with no observed verifier-pass loss. It also reduced
expensive model calls from 67 to 27 across the benchmark. Relative to static
role mapping, trace-governed routing reduced cost per successful task by 5.53%
and reduced expensive calls from 31 to 27. The Wilson 95% interval for each
12/12 verifier pass rate is `[0.7575, 1.0]`.

This result supports the narrow benchmark claim: on these controlled mini-ork
fixtures, trace-governed routing reduced cost relative to frontier-only routing
while preserving verifier pass rate. It does not prove that cheap models can
replace frontier models in arbitrary production work. The task fixtures are
small, the `query-level workflow` baseline has not yet been implemented, and
reviewer acceptance was not perfect for code-fix runs.

## 9. Threats to Validity

**Provider drift.** Hosted model behavior and pricing change over time. The
evaluation must record provider, model, date, and price assumptions.

**Task leakage.** Repository tasks may be easier for models that have seen
similar public code. The benchmark should include private or freshly generated
tasks where possible.

**Verifier incompleteness.** Passing tests or typecheck does not prove semantic
correctness. This is a limitation of every execution-grounded software-agent
benchmark.

**Fixture ceiling effects.** The controlled benchmark uses small deterministic
fixtures. A 100% verifier-pass rate across all policies means the current
benchmark primarily distinguishes cost and routing behavior, not hard-task
reliability.

**Planner fallback contamination.** In the `obs_smoke` portion of the
benchmark, 12 of 24 runs used deterministic fallback planning after the planner
emitted invalid JSON. This preserves the run but weakens any claim about
whole-run model routing. Future batches should either route planner calls
through the policy surface or report planner fallback as a separate framework
failure mode.

**Cost-estimation error.** Proposition 1 assumes actual cost is bounded by the
policy estimate. Production systems need conservative estimates or hard API
caps.

**Reviewer variance.** The code-fix reviewer accepted 8 of 9 runs in the
frontier-only, static-hybrid, and trace-governed policies and 9 of 9 cheap-only
runs. Reviewer labels are useful but noisy; future batches should either blind
reviewers to policy labels or use deterministic reviewer evidence contracts.

## 10. Discussion

Trace-governed budget allocation reframes observability as a control surface.
Instead of treating agent traces as logs for debugging after a run completes,
the workflow uses the trace prefix to decide how much reasoning power the next
step deserves.

This distinction matters for the cost problem. A cheap worker that passes a
deterministic verifier should not be replaced by a frontier model merely
because the workflow is important. Conversely, a cheap worker that repeatedly
fails the same verifier should not be allowed to burn budget indefinitely.
Trace governance expresses both rules in one policy.

The approach is also compatible with recent adaptive-orchestration work.
Query-level workflow generation decides the shape of the run before execution.
Trace-governed routing decides the resource allocation during execution. These
are complementary: a generated workflow can still use trace-governed budget
allocation inside the workflow.

## 11. Conclusion

This draft introduced trace-governed budget allocation for heterogeneous
multi-agent LLM software workflows. The central idea is simple: persistent
execution traces should govern future inference spend. We formalized workflows
as typed DAGs, defined an online routing policy over trace prefixes, proved
budget and verifier-gating properties, and proposed an empirical protocol for
evaluating cost per successful task.

The controlled benchmark supports the claim that trace-governed routing can
reduce cost relative to frontier-only routing on small verifier-gated mini-ork
tasks. The next step is to scale the benchmark to harder tasks, implement the
query-level workflow baseline, and add blinded human-review labels.

## References

- MasRouter: Learning to Route LLMs for Multi-Agent Systems.
  arXiv:2502.11133.
- Budget-Aware Agentic Routing via Boundary-Guided Training.
  arXiv:2602.21227.
- ZEBRA: Zero-shot Budgeted Resource Allocation for LLM Orchestration.
  arXiv:2605.20485.
- CASTER: Breaking the Cost-Performance Barrier in Multi-Agent Orchestration
  via Context-Aware Strategy for Task Efficient Routing. arXiv:2601.19793.
- xRouter: Training Cost-Aware LLMs Orchestration System via Reinforcement
  Learning. arXiv:2510.08439.
- Difficulty-Aware Agentic Orchestration for Query-Specific Multi-Agent
  Workflows. arXiv:2509.11079.
- Uno-Orchestra: Parsimonious Agent Routing via Selective Delegation.
  arXiv:2605.05007.
- AdaptOrch: Task-Adaptive Multi-Agent Orchestration in the Era of LLM
  Performance Convergence. arXiv:2602.16873.
- Flow: Modularized Agentic Workflow Automation. arXiv:2501.07834.
- Multi-Agent Verification: Scaling Test-Time Compute with Multiple Verifiers.
  arXiv:2502.20379.
- MAS-ProVe: Understanding the Process Verification of Multi-Agent Systems.
  arXiv:2602.03053.
- FormalJudge: A Neuro-Symbolic Paradigm for Agentic Oversight.
  arXiv:2602.11136.
- Auditing Multi-Agent LLM Reasoning Trees Outperforms Majority Vote and
  LLM-as-Judge. arXiv:2602.09341.
- AgentForge: Execution-Grounded Multi-Agent LLM Framework for Autonomous
  Software Engineering. arXiv:2604.13120.
- Tokenomics: Quantifying Where Tokens Are Used in Agentic Software
  Engineering. arXiv:2601.14470.
- Co-Saving: Resource Aware Multi-Agent Collaboration for Software
  Development. arXiv:2505.21898.
- Reducing Cost of LLM Agents with Trajectory Reduction. arXiv:2509.23586.
- Retrieval-Conditioned Topology Selection with Provable Budget Conservation
  for Multi-Agent Code Generation. arXiv:2605.05657.

## Appendix A: Measured Data Tables

### A.1 Cross-Task Benchmark

This table reports the combined controlled benchmark:

- `obs_smoke`: three task labels, two replicates, four policies;
- `docs`: three deterministic documentation fixtures, four policies;
- `code_fix`: three deterministic Python repair fixtures, four policies.

| Policy | Runs | Task classes | Success rate | Verifier pass rate | Cost / success | Median time | Retries / run | Escalations / run |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Frontier-only | 12 | 3 | 1.0 | 1.0 | 0.536418 | 87.0s | 0.0 | 0.0 |
| Cheap-only | 12 | 3 | 1.0 | 1.0 | 0.435822 | 132.0s | 0.0 | 0.0 |
| Static role mapping | 12 | 3 | 1.0 | 1.0 | 0.416641 | 93.0s | 0.0 | 0.0 |
| Trace-governed | 12 | 3 | 1.0 | 1.0 | 0.393587 | 99.0s | 0.0 | 0.0 |

No verifier-failure escalation occurred in this batch because all deterministic
verifiers passed on the first execution attempt. The useful cost signal is
therefore cheap-first routing plus frontier review, not failure-triggered
recovery.

### A.2 Baseline Coverage

The `query-level workflow` baseline from the protocol is not included in the
measured table because it has not yet been implemented as a controlled mini-ork
policy. The current empirical claim compares trace-governed routing against
frontier-only, cheap-only, and static role mapping.

## Appendix B: Minimal Pseudocode

```text
function route(node, trace, budget):
    remaining = budget - cost(trace)
    score = risk(node)
          + failed_verifiers(node, trace)
          + retry_count(node, trace)
          + uncertainty(node, trace)

    if next_attempt_exceeds_budget(node, remaining):
        return HUMAN_GATE_OR_ROLLBACK

    if node.type in {planner, reviewer}:
        return FRONTIER_LANE

    if score >= ESCALATION_THRESHOLD:
        return FRONTIER_LANE

    return CHEAP_WORKER_LANE
```
