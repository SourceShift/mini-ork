# `mini-ork recipes check` — recipe integrity as a gate

A recipe is a set of **references**: `prompt_ref` and `verifier_ref` in
`workflow.yaml` name files that sit beside it, and the whole directory is bound
together by `task_class.yaml` + `artifact_contract.yaml`. Nothing local checks
that those references still resolve. Rename a prompt, delete a verifier, or
leave an edge pointing at a node that was removed, and the recipe still parses;
`mini-ork recipe-eval` still scores it (correctly — that command scores *style*
and always exits 0); and the break surfaces as a dispatch error inside a paid
run, after the plan has been billed.

`mini-ork recipes check` makes that class of rot a red exit code. It is the
deterministic half of "a recipe is reviewable"; `recipe-eval` is the other half.

## What it checks

| Kind | Severity | Means |
|---|---|---|
| `dangling-ref` | error | a `prompt_ref` / `verifier_ref` names a file that is not there |
| `unparsable` | error | `workflow.yaml`, `task_class.yaml` or `artifact_contract.yaml` does not parse |
| `uncompilable` | error | `mini_ork.workflow.compiler.compile_workflow` refuses it — the recipe cannot be dispatched |
| `missing-recipe` | error | a recipe you named by hand does not exist (and you asked for it) |
| `incomplete` | warning | the directory is missing `task_class.yaml` or `artifact_contract.yaml` |

Errors fail the run. A warning is reported and shown in the summary but does
not change the exit code — an incomplete directory is visible as an inventory
entry, and turning that into a hard failure is a separate decision. `--strict`
opts into it.

Findings carry a `fix` string, so the report tells you what to do rather than
only what is wrong:

```
✗ code-fix
  [error] dangling-ref: node implementer: prompt_ref -> prompts/implement.md does not exist
    Fix: create prompts/implement.md, or repoint prompt_ref at the file that replaced it

52 recipe(s), 1 error(s), 2 warning(s) — FAILED
```

## Two tiers

This is a **repo-local static check**: it reads the working tree, opens no
sockets, reads no credentials, and writes nothing. The simple tier and the
trust-nothing tier are therefore the same binary, and there is nothing to
configure and nothing to leak. Both CI and a laptop run the identical command.

**Simple / local** — no server, no account, stdlib + PyYAML:

```bash
mini-ork recipes check                 # every recipe under recipes/
mini-ork recipes check code-fix        # only this one (repeatable)
mini-ork recipes check --strict        # warnings fail too
mini-ork recipes check --json          # machine-readable, for a gate or a dashboard
```

Exit `0` clean, `1` findings, `2` usage error or no `recipes/` directory.

**Secure / CI** — the `python-lint` job runs it next to `check-skill`:

```yaml
- name: Recipe integrity
  run: python -m mini_ork.cli.main recipes check
```

A run's failure names the recipe, the node and the missing file, so it is
diagnosable from the log without a checkout.

## What it deliberately does not check

**Schema conformance.** `schemas/workflow.schema.json` and
`task_class.schema.json` exist and the tree does not satisfy them — 74
pre-existing violations at the time this command shipped, mostly optional
fields the schema requires. A gate written against that today would be red on
`main` on day one, which is how a gate gets disabled. Schema validation stays
out until the tree is migrated; the check is the natural place for it to land
afterwards.

**`success_verifiers` entries.** Those entries in `artifact_contract.yaml` are
**shell commands**, not file paths — `lens-exists`, `fail-probe` and friends are
legitimate, so a "does this path exist" probe over them would be wrong. Whether
a command is sane is the contract runner's question, not this command's.

**Recipe quality.** Prompt sizes, missing examples, prose that has drifted from
the schema — that is `recipe-eval`, which is deliberately a score and not a
gate. A style score that fails a build teaches people to pad prompts.

**Cross-recipe drift.** Output-path collisions between recipes, oversized
workflows and stale reviewer evals are `mini-ork garden`'s territory. The two
are complements: `garden` asks whether the recipe *collection* is coherent,
`recipes check` asks whether one recipe can still be dispatched at all.

## Why it is not a reviewer instruction

"Check that the refs resolve" is easy to write in a prompt and easy to skip
under budget pressure. `recipes check` walks the YAML and the compiler says yes
or no in under a second, on every push, for free. Same principle as the
[fallback guard](fallback-guard.md): give the rule a machine before giving it a
paragraph.
