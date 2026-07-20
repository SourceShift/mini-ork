# Profile-answerer native-dispatch requirements audit

Status: completed

## Task: remove the Python profile answerer's Bash dispatcher edge

Status: completed
Last worked on: 2026-07-20
Remaining parts: none for the Python caller; Bash library retirement is a
separate ownership unit.

### Subtasks

1. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. The standalone/default path calls the native
   dispatcher in-process.
2. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. DeepSeek primary, Kimi fallback, whitespace fallback,
   stdout isolation, and exception behavior are preserved.
3. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. Focused profile/planner tests, Pyright, closure and
   scope scans, and a real GLM 5.2 probe passed.
