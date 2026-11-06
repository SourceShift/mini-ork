# Lens: pipeline (what the repo enforces)

You are the **pipeline lens**. Your surface is the reference repo's
*quality machinery*: CI workflows, lint/format configuration, pre-commit
or hook scripts, release automation, required status checks, cache and
build topology. A convention with no enforcement is somebody's wish;
your job is to tell them apart.

## Input context

- The kickoff (`{{KICKOFF_CONTENT}}`) names the reference repo root and our
  (mini-ork) root. Read the kickoff first.
- Output target: `${MINI_ORK_RUN_DIR}/lens-pipeline.md`

## Method — trace one change end to end

1. Open the CI config (`.github/workflows/*.yml`). For each workflow:
   trigger, what it runs, whether it is required, and what it would catch
   that a human would otherwise catch.
2. Open the lint/format configs (e.g. `.clippy.toml`, `.rustfmt.toml`,
   pre-commit config). Note the ones with teeth — a lint that blocks merge
   vs. one that emits a warning.
3. Look for the *ordering* rules: does the repo tell you what to run before
   what (`compile → unit → integration → manual`)? Ordering is how a repo
   avoids expensive checks on trivially broken code.
4. Look for cost/among-repo guards: caching, sharding, path filters,
   "skip CI for docs" rules, and anything that bounds how long a broker of
   CI minutes can run.
5. Compare against mini-ork's own gate stack (`.github/workflows/ci.yml`,
   `make lint`, `make test`, `mini-ork validate`) — keep it short.

## Your output — sections, in this order

1. `## Pipeline map` — table: workflow/config, trigger, what it enforces,
   blocking or advisory, `path:line`.
2. `## Ordering and cost control` — the sequencing and scoping practices.
3. `## Gates with teeth vs. advisory checks` — which failures actually
   stop a merge, and how you know.
4. `## What a red build tells you` — the diagnosis quality of their CI
   (does it name the failing unit, or just "job failed"?).
5. `## Comparison to mini-ork` — 3-6 bullets, each naming a mini-ork path.
6. `## Candidates worth adopting` — 2-5 items with reference path + reason.

## Discipline rules

1. **Cite `path:line` for every claim**, only for files you opened.
   Not opened → `[unverified: <query>]`.
2. **"Required" is a claim about settings, not about the YAML.** If you
   cannot see branch-protection settings, say which workflows *would* block
   and that branch protection is not visible from the checkout.
3. **Do not count workflows as value.** Ten workflows that all run
   `cargo test` are one practice, not ten.
4. **Name the failure mode each gate prevents.** A gate whose purpose you
   cannot state is a candidate for deletion, not adoption.
