# Recipe: repo-practices-benchmark

Benchmark a reference repository's engineering practices against ours, and
emit a **ranked, evidence-cited improvement list** — not a book report.

## The four lenses

| Lens | Surface it owns | Family |
|---|---|---|
| `conventions_lens` | `AGENTS.md`, `CONTRIBUTING.md`, `.agents/`, `specs/` — what the repo writes down, and whether it is enforced | glm |
| `pipeline_lens` | CI workflows, lint/format config, hooks, release scripts — what the repo *enforces* | deepseek |
| `architecture_lens` | crate/module boundaries, dependency rules, testing strategy, flag lifecycle | minimax |
| `agent_ux_lens` | what the repo hands an AI agent: skills, MCP config, index ignores, validation order | opus |

Then `synthesizer` (reviewer) produces `synthesis.md`: a ranked list where
every item pairs *reference evidence* with *our current state*, then a
required section on **what we already do as well or better**, then **rejected
candidates**. Each lens runs on a distinct model family; two models agreeing
is not evidence.

## Why the citation gate exists

`verifiers/evidence-citations.py` resolves backticked `path:line` citations in
every lens report and in the synthesis against `BENCH_REFERENCE_ROOT` and
`BENCH_SUBJECT_ROOT` (falling back to `reference root:` / `subject root:`
lines in the kickoff). Each lens must contribute ≥8 *distinct paths that
exist*; a report that cites invented files fails the run instead of shipping
confident prose.

Unresolved tokens are logged and counted rather than fatal: a glob
(`.agents/skills/*`), a directory, or a bare filename (`ci.yml`) is legitimately
unresolvable and appears in real reports. The floor is what separates "some
noise" from "mostly invented" — the run log carries the per-file ratio so a
reviewer can judge it.

It cannot tell you the cited line says what the claim says. That stays a
reviewer's job — the gate removes the failure mode that is mechanically
removable, and says so.

## Quickstart

```bash
export BENCH_REFERENCE_ROOT=/path/to/reference-checkout
export BENCH_SUBJECT_ROOT=/path/to/mini-ork
export MO_TARGET_CWD=$BENCH_SUBJECT_ROOT      # or a scratch repo, see below
mini-ork run repo-practices-benchmark kickoffs/bench.md
```

Output: `${MINI_ORK_RUN_DIR}/lens-*.md` + `synthesis.md`, published to
`docs/research/repo-practices-latest.md`.

**Publisher note.** The publisher commits the output path *in
`MO_TARGET_CWD`*. Point `MO_TARGET_CWD` at a scratch checkout when you do not
want the run committing into the repo you are benchmarking from — the lenses
read the reference and the subject by absolute path, so the target only
decides where the document lands.

## Scope

This recipe is read-only against both repos; it never edits them. `risk_class:
low`. Cost: 4 researcher lenses + 1 synthesizer, ~$3-10 depending on how
large the reference repo is and how much of it a lens opens.
