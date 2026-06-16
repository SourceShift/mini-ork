# Arxiv-driven recursive self-improvement roadmap

Each epic below maps one or more SOTA arxiv papers (Apr–Jun 2026) to a
concrete mini-ork change. The autonomous scheduler walks these in dep order,
opens PRs (when `MO_OPEN_PR=1`), and auto-merges after CI + soak (`MO_AUTO_MERGE=1`).

The first epic (**ARX-0**) is foundational: it teaches mini-ork to scan
arxiv on a schedule and propose its own follow-on epics — so this roadmap
becomes self-extending after the first cycle.

Ingest:

```
bin/mini-ork epics ingest kickoffs/roadmap-arxiv-self-learn.md
bin/mini-ork epics ready
bin/mini-ork scheduler --once   # then --once again, or daemonize
```

## ARX-0 arxiv-scout recipe — autonomous paper-to-epic discovery (id: arx-0-scout)

Build `recipes/arxiv-scout/` that, given a topic query, performs:

1. arxiv search via `mcp__jina__parallel_search_arxiv` (or fallback curl).
2. ranks papers by fit-to-mini-ork (cosine of paper abstract vs existing
   recipe prompts).
3. for each top-N paper, drafts a candidate epic kickoff under
   `kickoffs/arx-auto/<arxiv-id>.md`.
4. writes rows to a new `arxiv_candidates` table:
   `(arxiv_id, title, fit_score, mapped_recipe, status)`.

After ARX-0 ships, a cron-style scheduler run can call
`mini-ork run arxiv-scout` weekly and the system grows its own roadmap.

Foundation epic — every other ARX epic could derive from it after first
cycle, so ship this first.

## ARX-1 Adopt VeRO verifier-guided self-improvement harness (id: arx-1-vero-harness)

**Paper:** Schmidt et al., *VeRO: A Harness for Agents to Optimize Agents*,
arXiv:2602.22480 (Jun 2026, conf 0.92).
*"Applying VeRO to enable a coding agent to optimize its own code opens the
possibility of recursive self-improvement."*

mini-ork already has the harness shape (`bin/mini-ork-self-improve` →
worktree → dispatch → verifier → promote). VeRO's key contribution is
**verifier-conditioned reward shaping** — only credit improvements that
the verifier can prove are net-positive on a held-out benchmark suite.

**Mini-ork change:** Add a `verifier_credit_score` column to
`learning_record` populated by re-running the prior benchmark suite after a
promoted patch lands. Block subsequent promotions on the same target until
the prior credit_score ≥ 0.5.

- depends on: arx-0-scout

## ARX-2 GRASP — gated regression-aware skill proposer (id: arx-2-grasp-skills)

**Paper:** Zhu et al., *GRASP: Gated Regression-Aware Skill Proposer for
Self-Improving Agents*, arXiv:2605.29668 (May 2026, conf 0.91).
*"Treats agent self-improvement as a sequence of validated edits to a small
skill library."*

mini-ork's `pattern_records.output_type` already enumerates 6 skill kinds
(`adr|verifier_addition|workflow_change|prompt_change|best_practice_rule|other`).
What's missing: a regression test PER skill kind that runs before promotion.

**Mini-ork change:** Add `skill_regression_suite/` directory keyed by
output_type. `lib/promotion_gate.sh` calls the matching suite before
promotion. If suite has no test for that output_type, log warning + block
(opt out via `MO_GRASP_GATE=0`).

- depends on: arx-0-scout
- depends on: arx-1-vero-harness

## ARX-3 TacoMAS — split fast capability loop from slow topology loop (id: arx-3-tacomas-split)

**Paper:** Liang et al., *TacoMAS: Test-Time Co-Evolution of Topology and
Capability in Multi-Agent Systems*, arXiv:2605.09539 (May 2026, conf 0.90).
*"Fast capability loop ... slow topology loop."*

mini-ork currently runs reflect→improve→promote as one chain. TacoMAS shows
they should be **two cadences**:
- **Fast (per-run):** mine gradients + inject failure_modes into next dispatch (already shipped today via D1-D5).
- **Slow (per-N-runs):** mutate workflow.yaml DAG, propose new lane assignments. Should be rate-limited.

**Mini-ork change:** Add `MO_TOPOLOGY_LOOP_EVERY=10` knob. Workflow mutations
(`group_evolver` calls) only fire every Nth `reflect` invocation, not every
one. Reduces noise + saves cost during burn-in.

- depends on: arx-0-scout

## ARX-4 SkillCAT — three-decision skill pipeline rename (id: arx-4-skillcat-stages)

**Paper:** Park et al., *SkillCAT: Contrastive Assessment and Topology-Aware
Skill Self-Improvement*, arXiv:2606.13317 (Jun 2026, conf 0.91).
*"Three key decisions in offline agent skill improvement: evidence
extraction, patch validation and integration, and test-time skill."*

This matches mini-ork's reflect→improve→promote almost 1:1. SkillCAT adds:
- **contrastive assessment** — compare candidate skill against the
  most-similar prior skill that succeeded
- **topology-aware** — skill applicability scored by the workflow node
  where it would inject

**Mini-ork change:** Extend `lib/gradient_extractor.sh` to compute cosine
similarity between candidate gradient and prior `gradient_records` rows for
the same target; reject candidates with similarity > 0.85 as duplicates.
Already partially done (D2 has fuzzy@0.55 dedup); raise threshold to 0.85
and gate on it.

- depends on: arx-1-vero-harness

## ARX-5 SIGA — self-rewriting grounding from trajectories (id: arx-5-siga-context-self-update)

**Paper:** Chen et al., *SIGA: Self-Evolving Coding-Agent Adapters for
Scientific Simulation*, arXiv:2606.09774 (Jun 2026, conf 0.89).
*"Self-evolved variant that rewrites its own grounding components from
prior trajectories."*

**Mini-ork change:** `lib/context_assembler.sh` currently reads
`execution_traces` + `gradient_records` per request. Add a `groundings/`
directory of canonical snippet templates (e.g., "When implementing a
verifier in this repo, prefer X over Y because ..."). On every successful
run, append the run's reviewer-approved rationale to the matching
grounding file. Future context packs include the top-3 most-cited
groundings for the task_class.

- depends on: arx-3-tacomas-split

## ARX-6 MemMachine + APO — auto-prompt optimization per role (id: arx-6-apo-prompt-mutation)

**Paper:** Wang et al., *MemMachine: A Ground-Truth-Preserving Memory
System*, arXiv:2604.04853 (Apr 2026, conf 0.87).
*"We further tuned Retrieval Agent prompts using the APO (Auto Prompt
Optimization) algorithm."*

**Mini-ork change:** New recipe `recipes/apo-prompt-tune/` that takes
(role, task_class) and:
1. samples 5 recent successful + 5 recent failed `execution_traces`.
2. dispatches an opus_lens synthesizer to propose a prompt variation.
3. runs the variation against the same task_class via shadow workflow.
4. if benchmark improves, write a `pattern_records` row with
   `output_type='prompt_change'` and trigger normal promotion gate.

Pure plug-in on existing infra. Opus_lens chosen explicitly per the
2026-06-15 lifting of the no-opus rule for deep reasoning roles.

- depends on: arx-3-tacomas-split
- depends on: arx-4-skillcat-stages

## ARX-7 ENGRAM — typed lightweight memory orchestration (id: arx-7-engram-typed-memory)

**Paper:** Rao et al., *ENGRAM: Effective, Lightweight Memory Orchestration
for LLM Agents*, arXiv:2511.12960 (Feb 2026, conf 0.85).
*"Careful memory typing and straightforward dense retrieval enable
effective long-term memory management."*

mini-ork has 8 typed memory namespaces (task_memory, failure_memory,
agent_performance_memory, recovery_memory, user_preference_memory,
artifact_memory, workflow_memory, benchmark_memory). 4 are still cold.

**Mini-ork change:** Wire writers for the 4 cold tables, then add an
ENGRAM-style typed retrieval API: `mo_memory_retrieve --type <kind>
--task-class X --top-k N`. Replaces ad-hoc `sqlite3 SELECT` calls in
context_assembler with a typed retrieval layer.

- depends on: arx-0-scout

## ARX-8 Cron-style daemon for arxiv-scout (id: arx-8-arxiv-cron)

After ARX-0 + ARX-7 ship, `mini-ork run arxiv-scout` should fire weekly via
a system cron OR an internal scheduler tick. Adds `lib/cron-tick.sh` and a
`mini-ork tick` subcommand that the host scheduler triggers.

- depends on: arx-0-scout
- depends on: arx-7-engram-typed-memory
