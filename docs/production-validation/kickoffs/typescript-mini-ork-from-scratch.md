# Build a TypeScript Mini-Ork From Scratch

## Goal

Create a new TypeScript command-line project that implements a small
mini-ork-like agent orchestration runtime from a blank repository.

The result should be self-contained, dependency-free, and runnable with Node.js
and the globally available TypeScript compiler.

## Required Product Behavior

The generated project must expose a CLI command:

```bash
node dist/cli.js run kickoff.md
```

The CLI must:

1. Read exactly one markdown kickoff file.
2. Classify the task from the markdown.
3. Build a run profile with confidence, missing fields, and at most three
   human questions.
4. Plan a workflow DAG with typed nodes and dependencies.
5. Execute runnable nodes in dependency order.
6. Support bounded recursive child orchestration.
7. Record every run event in a JSONL trace log.
8. Extract at least one learning signal from the trace log after a run.
9. Print a final JSON summary.

## Required Technical Architecture

Create these files:

- `package.json`
- `tsconfig.json`
- `src/types.ts`
- `src/profile.ts`
- `src/planner.ts`
- `src/orchestrator.ts`
- `src/learning.ts`
- `src/cli.ts`
- `tests/orchestrator.test.ts`

Use TypeScript only. Do not add external dependencies.

## Minimum Feature Contract

### Profile

`buildRunProfile(markdown: string)` must return:

- `goal`
- `confidence`
- `missingFields`
- `questions`

Confidence must increase when the markdown includes success criteria, scope,
and verification commands.

### Planner

`planWorkflow(markdown: string, profile: RunProfile)` must return a DAG with:

- `planner`
- `implementer`
- `verifier`
- `learner`

The learner must depend on the verifier.

### Recursive Orchestration

`Orchestrator` must support:

- `maxDepth`
- `maxChildrenPerRun`
- `spawnChild(parentRunId, markdown)`

It must reject:

- a child deeper than `maxDepth`
- more children than `maxChildrenPerRun`

### Learning

`extractLearningSignals(events)` must identify at least:

- a low-confidence profile signal
- a failed verifier signal
- a recursive spawn signal

## Verification Commands

The generated project must pass:

```bash
tsc --noEmit
npm test
node dist/cli.js run examples/kickoff.md
```

`npm test` must run:

```bash
npm run build && node --test dist/tests/*.test.js
```

## Scope

Only create or modify files in the generated blank project. Do not edit the
mini-ork repository itself from inside the generated project run.

## Provider Policy For This Validation

The parent mini-ork validation run must use only these model families:

- GLM
- Kimi
- Codex
- MiniMax

Do not use Opus, Sonnet, Haiku, or any Anthropic-native model for this
validation run.

## Definition of Done

- The TypeScript project is created from this markdown spec alone.
- `package.json` and all required source/test files exist.
- TypeScript typecheck passes.
- Tests pass.
- The generated CLI can run an example kickoff and emit final JSON.
- Recursive child spawning is tested.
- Learning signal extraction is tested.
- The parent mini-ork run records execution traces.
- A reflection step runs after the build and stores at least one learning
  artifact or reports an explicit learning blocker.
