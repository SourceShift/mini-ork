# Refresh README.md — clean, cohesive, engineer-integration-focused

## Goal
Rewrite `README.md` so an engineer can understand mini-ork and integrate it into their existing
agentic-CLI workflow (Claude Code, Codex, Gemini CLI, etc.) in minutes. Trim marketing bloat;
lead with how it works and how to run it. Keep it cohesive and skimmable.

## Required structure (in this order)
1. **Title + one-sentence definition + the motto.** Keep the existing motto line. One tight
   paragraph: mini-ork is a "task operating system for agents" — goal → classify → plan → execute →
   verify → reflect → improve, dispatched across *distinct model families*, verifier-gated, with
   persistent learning in `state.db`.
2. **How it works (short).** The classify→plan→execute→verify→reflect→improve loop in ~6 bullets
   or a small diagram; the three load-bearing ideas: cross-family review independence, executable
   (deterministic) verification before LLM opinion, persistent trajectory memory. Link
   `docs/FEATURES.md` and `docs/ARCHITECTURE.md` for depth. Keep this section tight — no walls of text.
3. **Quickstart (copy-paste).**
   - 30-second no-keys demo: `bash examples/00-demo.sh` (dry-run, no LLM calls).
   - Install: clone + `./install.sh` (or the documented path); note `bin/mini-ork` is the entrypoint.
   - First real run: `./bin/mini-ork run <recipe> <kickoff.md>` with a concrete tiny example.
4. **Setup credentials.** Explain `config/secrets.local.sh` (gitignored): the curl-lane keys
   `GLM_API_KEY` / `KIMI_API_KEY` / `MINIMAX_API_KEY`; that claude/opus/sonnet + codex lanes reuse
   the local `claude`/`codex` CLIs (no key needed if those CLIs are already authed); and
   `MINI_ORK_SECRETS` for pointing at a secrets file when vendored into another repo. Show a minimal
   `secrets.local.sh` template. Mention `./bin/mini-ork doctor` to verify the environment/lanes.
5. **Integrate into your agentic CLI (Claude Code / Codex / others).** The key new section:
   - mini-ork dispatches each node to a *lane*; lanes map to model families via `config/agents.yaml`,
     and providers live in `lib/providers/cl_*.sh` (claude, sonnet, opus, codex, kimi, minimax, glm,
     deepseek). So it drives your existing `claude` / `codex` / `gemini` CLIs under the hood.
   - How to vendor mini-ork into an existing repo (its own `.mini-ork/`), point it at that repo, and
     run recipes against your codebase; note the safe-usage basics (cwd, `MINI_ORK_ROOT`).
   - How to invoke it from a "master" Claude Code / Codex session (dispatch a recipe, monitor the
     run, read artifacts) — short, practical.
6. **Recipes (brief).** What a recipe is (`recipes/<name>/` = workflow.yaml + prompts + verifiers +
   artifact_contract), how to list them, and 3-4 high-value examples (code-fix, framework-edit,
   research-synthesis, recursive-self-improve). Link `recipes/`.
7. **Safety / cost one-liner + links.** Verifier-gated, cross-family, `MO_DAILY_BUDGET_USD` cap,
   opt-in sandbox backends (`MO_RUNTIME_BACKEND`). Link CONTRIBUTING / docs.

## Hard constraints (must pass the pre-push README claim-check)
- `scripts/readme-claim-check.sh` MUST still pass. Preserve these EXACT current claim numbers (or
  update to the true repo counts if you restate them): **lib/*.sh = 88**, **bin/mini-ork-* = 31**,
  **db/migrations = 47**, **recipes table rows = 28**, **lib/providers/cl_*.sh = 7**. If you keep a
  "framework primitives" count line, keep it terse and correct. Do NOT introduce any cited path that
  doesn't exist (the "cited paths exist" probe).
- All relative links must resolve (the `docs` recipe's link_verifier checks this) — only link files
  that exist.
- Keep it substantially SHORTER and cleaner than the current 470 lines where possible without
  dropping the required sections.

## grep-assert: the refreshed README MUST contain these markers
`## Quickstart`, `config/secrets.local.sh`, `Claude Code`, `bin/mini-ork run`, `MINI_ORK_RUNTIME`
or `MO_RUNTIME_BACKEND`, `docs/FEATURES.md`, `./bin/mini-ork doctor`.

## Scope
Edit ONLY `README.md`. Do not touch code, recipes, or other docs.
