# 2026 agent-security futures briefing

Collect the top 2000 distinct 2026 papers from the LibWit arXiv corpus on THE
SECURITY OF AND BY AI AGENTS — prompt injection and instruction hijacking
through untrusted content, tool-call and capability abuse, sandboxing and
isolation of autonomous workloads, agent-generated code that ships a
vulnerability, multi-agent trust, delegation and collusion, memory and
knowledge-base poisoning, data exfiltration, supply-chain and model-artifact
attacks, agent identity, authorization, provenance and audit, monitoring of
autonomous behaviour, agent security evaluation and red teaming, incident
response and governance, and the measured economic, labour-market,
infrastructure and societal consequences of deploying agents.

Deliver one forward-looking briefing that answers three questions:

1. What are the trending topics in agent security right now, and what is the
   evidence for each?
2. How will the security of agents develop over the next 12-36 months — which
   fronts move fastest, what fails first, and how confident is each forecast?
3. How will that affect people, and what should a reader do to be prepared?

The reader is an engineer or decision-maker deploying agents, not a
mini-ork operator. Recommendations must be actionable by them: name the
control, where it sits, what it must reject or flag, and how to prove it works.

## Goal

Produce `agent-security-futures.md`: a section-per-question briefing grounded in
a 2000-paper 2026 corpus, with a ranked technique appendix, an explicit split
between what the literature measured and what is extrapolated, and a forecast
section whose claims carry the indicator that would refute them.

## Scope

- Files in scope: only `recipes/agent-security-futures/**` (read-only inputs)
  and the run-local `.mini-ork/runs/<id>/` artifacts. No repository source file
  is modified by this run.
- Sources: only the 2026 LibWit/arXiv records returned by
  `recipes/agent-security-futures/collection-plan.json`; no web results, no
  invented citations, no products or standards the corpus does not contain.
- This is a literature-and-analysis run. Do not write exploit code, do not
  contact any third-party system, and do not test anything outside the run dir.

## Done When

1. `source-corpus.json` holds all `required_source_count` (2000) distinct
   unversioned arXiv URLs with dates, titles, abstracts, and retrieved body text
   (`evidence_mode`, `evidence_path`, `evidence_coverage`); the collector fails
   closed rather than summarizing a partial corpus, and no paper is extracted
   from its abstract alone when body text was retrievable.
2. Shard extraction covers at least 95% of the corpus
   (`technique-catalog.json` coverage block).
3. `clusters.json` has exactly ten themes covering at least 95% of catalog
   techniques, each theme stating its front of work.
4. `merged-review.json` carries two independent reviewer scorings — severity,
   momentum, evidence confidence, readiness, gap status, verdict — for every
   technique, with disagreement flagged.
5. `agent-security-futures.md` contains Executive Summary, Trending Now,
   Forecast, Human Impact, How to Prepare, and What Would Change This
   Forecast, plus the assembler's ranked appendices; every forecast states its
   confidence and its refuting indicator; every cited arXiv id exists in the
   corpus and every technique id exists in `merged-review.json`.

## Verification Command

- `python3 recipes/agent-security-futures/lib/review.py verify --final
  "$MINI_ORK_RUN_DIR/agent-security-futures.md" --merged
  "$MINI_ORK_RUN_DIR/merged-review.json" --corpus
  "$MINI_ORK_RUN_DIR/source-corpus.json"` exits 0.
