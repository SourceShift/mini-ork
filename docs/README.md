# The docs, in reading order

Six pages, in the order a person needs them. Everything else in `docs/` is a
reference for a question you only have once one of these six has answered the
previous one.

| # | Page | Read it when | What it answers |
|---|---|---|---|
| 1 | [Why mini-ork exists](WHY.md) | first | What a task operating system is for, and what the classify → plan → execute → verify → reflect loop buys over a single prompt |
| 2 | [Install](../README.md#quick-start) | you want to run it | Dependencies, `mini-ork onboard`, and the doctor verdict that says the install is sound |
| 3 | [Your first verified run](../README.md#your-first-verified-workflow) | you have it installed | One kickoff through the whole loop, what the verifier refuses, and how to read the result |
| 4 | [Operating](OPERATOR-STEERING.md) | something is running | `mini-ork inbox`, the board, approvals, cost pauses, and recovery |
| 5 | [Extending](EXTENSION.md) | you want a new recipe, node, provider or gate | The registries: recipes, node types, provider kinds, gate evaluators, subcommands |
| 6 | [Reference](operator/env-vars.md) | you know what you are looking for | Env vars, [schemas](SCHEMA.md), the [API surface](UI.md), and [features](reference/FEATURE-INVENTORY.md) |

## The pages people look for by name

- **[Architecture](architecture/)** — the system's shape, with the verification
  stack and the artifact-graph contracts.
- **[UI and API](UI.md)** — the IDE, the JSON API, and the attention taxonomy
  behind every "needs you".
- **[Python SDK](PYTHON-SDK.md)** — embedding the primitives, or driving
  `MiniOrk` from Python.
- **[Work intake](operator/work-intake.md)** — tickets → threads → runs, and
  who admitted each one.
- **[Verification stack](architecture/verification-stack.md)** — which
  instrument runs where, pinned to the code by
  `tests/unit/test_verification_stack_doc_py.py`.
- **[Learning loop](LEARNING-LOOP-LIFECYCLE.md)** — what a run teaches the next
  one, and the Python check that proves it is closed.

## Docs that are checked

A doc whose claims nothing checks rots silently. Three of these are pinned by
tests, and they are the ones to trust without re-reading the code:

| Claim | Check |
|---|---|
| Every module, knob and state row in the verification-stack table | `tests/unit/test_verification_stack_doc_py.py` |
| The learning loop is closed and default-ON, cold start included | `tests/unit/test_learning_loop_closure_py.py` |
| Every `mini-ork <verb>` this guide cites exists, every link resolves, and every recipe declares a task class | `tests/unit/test_docs_commands_py.py` |
