# 2026 security technique review for mini-ork

Collect the top 2000 distinct 2026 papers from the LibWit arXiv corpus on
SECURITY OF AND BY AI SYSTEMS — LLM and agent vulnerability discovery in source
code (SAST, taint/dataflow, CWE detection, LLM code review), agentic penetration
testing and repository audit agents, automated red teaming, exploit and
proof-of-concept generation, patch verification by reproduction, fuzzing and
harness synthesis, prompt injection and untrusted-content handling for
tool-using agents, sandboxing and capability restriction (filesystem, shell,
network egress, tool allowlists), secrets and supply-chain detection, secure
code generation and vulnerability repair, security operations triage and
incident response, agent audit trails and provenance, and the validity of
security benchmarks. Extract the techniques every paper contributes, cluster
them into ten technique groups, distill each group into implementable
techniques mapped to mini-ork's security surfaces, and have two independent
reviewers (glm and minimax — no Anthropic lanes) score each technique's impact,
evidence, effort, and novelty against mini-ork's shipped security state.
Deliver a ranked implement-now / next / watch / skip decision report.

mini-ork ships code it writes. Its posture is FAIL-CLOSED and
EVIDENCE-ANCHORED: a guard that cannot measure blocks, an unverified result is
an abstention, and nothing is promoted on a model's opinion. The measured hole
this review must attack: the `code-fix` verifier set checks tests, types and
lint and has NO security check at all. Nothing detects a vulnerability an agent
just introduced, nothing replays an exploit against a patched tree, and nothing
measures whether the agent follows instructions planted in a repo file, an issue
body, or a tool result.

## Goal

Produce `security-technique-review.md`: a ranked, evidence-cited decision report
on which 2026 security techniques mini-ork should adopt, scored against what it
already ships.

## Scope

- Files in scope: only `recipes/security-technique-review/**` (read-only
  inputs) and the run-local `.mini-ork/runs/<id>/` artifacts. No repository
  source file is modified by this run.
- Sources: only the 2026 LibWit/arXiv records returned by
  `recipes/security-technique-review/collection-plan.json`; no web results,
  no invented citations.
- This is a literature-and-design review. Do not write exploit code, do not
  contact any third-party system, and do not test anything outside the run dir.

## Done When

1. `source-corpus.json` holds 2000 distinct unversioned arXiv URLs with dates,
   titles, abstracts, and retrieved body text (`evidence_mode`, `evidence_path`,
   `evidence_coverage`); no paper is extracted from its abstract alone when body
   text was retrievable.
2. Shard extraction covers at least 95% of the corpus
   (`technique-catalog.json` coverage block).
3. `clusters.json` has exactly ten groups covering at least 95% of catalog
   techniques.
4. `merged-review.json` carries scores from two reviewers.
5. `security-technique-review.md` has Executive Summary, Implement Now, and
   both appendices; lists all ten groups; cites only corpus arXiv ids and only
   known technique ids.

## Verification Command

- `python3 recipes/security-technique-review/lib/review.py verify --final
  "$MINI_ORK_RUN_DIR/security-technique-review.md" --merged
  "$MINI_ORK_RUN_DIR/merged-review.json" --corpus
  "$MINI_ORK_RUN_DIR/source-corpus.json"` exits 0.
