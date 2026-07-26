# Prompt Graph Loop

`prompt-graph-loop` turns a natural-language request into verified,
human-approved final summaries and an aggregation document without replacing
MiniOrk's scheduler, artifact ledger, policy, or harness adapters.

## Topology

| Workflow stage | MiniOrk node | Durable artifact |
| --- | --- | --- |
| PromptIntake | `prompt_intake` | `prompt-brief.json` |
| SemanticFlowExtractor | `semantic_flow_extractor` | `semantic-signals.json` |
| Evidence collection | `source_researcher` | `source-corpus.json` |
| RecursivePlanComposer | `recursive_plan_composer` | `agent-graph.json` |
| DraftExecutor | `draft_executor` | `draft-artifact.md` |
| VerifierGate | `verifier_gate` plus `graph_contract_gate` | `verification-report.json` |
| ReflectionLoop | `reflection_loop` | `refinement-prompt.md` |
| HumanFeedbackGate | `human_review_packet` plus `human_feedback_gate` | `human-decision.json` |
| Summary finalization | `summary_finalizer` | `final-summaries.json` |
| Aggregation | `aggregation_document` | `aggregation.md` |

Each consumer receives a materialized, hash-checked copy of only its declared
inputs. `source_researcher` makes corpus requirements explicit: a request for
200 current sources is incomplete until it has 200 distinct source URLs. The
two recursive edges declare a five-iteration policy: verifier
findings return to the graph composer; a `revise` human decision returns to
semantic extraction.

## Runtime Boundary

This recipe is executable for its first graph pass and enforces artifact
integrity plus approval-gated finalization. MiniOrk's current generic executor compiles
recursive edges but does not yet replay them as an automatic iteration loop.
An outer controller should use the existing recursive orchestration and
`steering_checkpoint` seams to start the next pass from `refinement-prompt.md`
or `human-decision.json`. This is deliberate documentation of a runtime gap,
not permission to treat a reviewer model as human approval.

## Human Decision

The review packet asks an operator to provide this file in the run directory:

```json
{
  "decision": "approved",
  "approver": "alice"
}
```

For revisions, use:

```json
{
  "decision": "revise",
  "approver": "alice",
  "feedback_delta": "Add a source-validation node before drafting."
}
```

`human_feedback_gate` validates this file deterministically. Only `approved`
passes the gate and unlocks summary finalization and aggregation. A valid `revise`
decision is retained as evidence but exits non-zero, so finalization is blocked. In a dashboard or
product integration, pair the packet with MiniOrk's `steering_checkpoint` and
an operator-steering message so the outer driver pauses before the decision and
starts the next bounded iteration with the retained feedback. The current recipe
declaration records that boundary but does not turn a reviewer model into a
human approver.

## Run

```bash
MINI_ORK_DRY_RUN=1 bin/mini-ork run prompt-graph-loop \
  recipes/prompt-graph-loop/example-kickoff.md
```

Use the normal MiniOrk lane configuration to select Codex, Claude Code, Kimi,
or another harness for the planner, worker, researcher, reviewer, and reflector
roles. Harnesses remain execution adapters; MiniOrk owns the graph, artifact
handoffs, verification, and repair policy.
