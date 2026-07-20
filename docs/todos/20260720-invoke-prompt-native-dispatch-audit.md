# Invoke-prompt native-dispatch requirements audit

Status: completed

## Task: remove the remaining Bash dispatcher edge from invoke-prompt

Status: completed
Last worked on: 2026-07-20
Remaining parts: none for this caller; library retirement remains a separate
program track.

### Subtasks

1. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. The stable executable path is now a thin Python
   launcher and the implementation calls the native dispatcher in-process.
2. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. Standalone golden tests cover the provider seam,
   environment, output, failure, role-pack, trace, and launcher contracts
   without creating `lib/llm-dispatch.sh`.
3. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. The migration plan's stale MiniMax tooling guidance
   was replaced by the approved temporary Kimi/Codex/GLM policy.
4. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. Focused tests, Pyright, a real GLM 5.2 public-path
   probe, closure scanning, and OSS secret/scope scanning passed.
5. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. The three BDD-first inbound callers now pass
   `MINI_ORK_PROMPT_FILE`, matching the public utility contract.
