# Changelog

All notable changes to mini-ork are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.1.0] - 2026-05-30

Initial extraction from internal orchestrator.

### Added
- `mini-ork deliver <kickoff.md>` — end-to-end decompose → workers → review → BDD → merge
- `mini-ork init` — scaffold `.mini-ork/` config directory in any repo
- `lib/dispatch` — epic claim + lane subprocess manager
- `lib/memory` — sqlite WAL read/write helpers
- `lib/auto-merge` — rebase-guard + git merge with audit metadata
- `lib/bdd-runner` — Gherkin scenario executor
- `lib/spec-author` — LLM-backed BDD spec generation
- `lib/spec-reviewer` — adversarial diff reviewer
- `lib/rebase-guard` — conflict detection before merge
- `lib/scope-overlap` — prevents two epics from claiming the same file
- `lib/llm-dispatch` — model routing by epic complexity tag
- `lib/contract` — kickoff constraint extraction
- `lib/self-correction` — structured feedback loop for failed BDD gates
- `lib/cache` — prompt + response caching keyed by content hash
- `lib/healer` — self-heal iter on BDD failure
- `lib/finalize` — post-merge cleanup + state.db verdict write
- `agents.yaml` config schema (max_iters, model overrides, lane cap)
- sqlite `state.db` schema: runs, epics, epic_reviews, bdd_runs, events, model_costs
- `.mini-ork/INBOX/` escalation for unresolvable failures
- `examples/` directory with smoke-testable kickoff fixtures
- `tests/smoke.sh` — offline smoke test with mocked claude binary
- Apache-2.0 license
