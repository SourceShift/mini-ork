# Pre-push-review native-dispatch requirements audit

Status: completed

## Task: remove the Python pre-push panel's Bash dispatcher edge

Status: completed
Last worked on: 2026-07-20
Remaining parts: none for this Python caller; the Bash review entrypoint and
library remain a separate migration fork.

### Subtasks

1. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. The sequential panel calls native
   `mo_llm_dispatch` in-process.
2. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. Panel order, exclusions, timeouts, failure policy,
   parsing, normalization, and issue caps are preserved.
3. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. Focused tests, Pyright, no-Bash closure scanning,
   scope/secret scanning, and a real GLM 5.2 probe passed.
