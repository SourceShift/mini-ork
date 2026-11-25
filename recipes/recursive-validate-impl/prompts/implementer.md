# Implementer prompt

**Rust guidelines.** This recipe grants the `rust-skills` skill; it is
materialized at `.claude/skills/rust-skills/` for the duration of this node.
When the target repository contains Rust, read `SKILL.md` before writing code
and apply its rules (ownership, error handling, unsafe, API design, async,
numeric safety, lints). A change that violates a rule needs an explicit reason
in the plan output, not silence.

Apply the current plan in an isolated worktree or equivalent clean tree. Keep
the patch scoped to the kickoff, the plan, and any reflector/replanner guidance
from the current iteration. Preserve user changes in the main checkout.

**Verification budget.** Verifier tiers own the full suite after you stop, so
spend your own time on the edit, not on re-proving the repository. Run at most
four commands, all scoped to the crates you touched: one type check
(`cargo check -p <crate>`), the new or changed test target
(`cargo test -p <crate> --test <file>`), `cargo fmt --all -- --check`, and —
only when the kickoff's DoD names it — the documented demo/parity check. Do
not run `--workspace`, `--all-targets`, `clippy --workspace`, benchmarks, or
repeated full builds; they duplicate tier1/tier2, dominate node wall-clock,
and their result is the verifiers' verdict, not yours. Re-read only the files
you are about to edit; the kickoff and plan already name them.

After editing, write `${MINI_ORK_RUN_DIR}/implementer-summary.json` as strict
JSON with a mandatory `touched_files[]` array AND a mandatory `worktree_path`.
Verifier tiers `cd` into `worktree_path` and read `touched_files[]` to scope
compile, lint, unit, property, and mutation checks — so the relative paths must
resolve against that tree.

Required JSON shape:

```json
{
  "iteration": 1,
  "worktree_path": "/absolute/path/to/the/worktree/or/clean/tree/you/wrote/in",
  "summary": "one paragraph",
  "touched_files": [
    "relative/path/from/repo/root.ts"
  ],
  "dod_probe_notes": [
    {
      "id": "P1",
      "status": "not_run | pass | fail",
      "evidence": "command output path or reason"
    }
  ],
  "verification_evidence": [
    {
      "command": "reproducible command from repo root",
      "status": "pass | fail | not_run",
      "evidence": "path to log or concise reason"
    }
  ],
  "risk_notes": [],
  "ready_for_tier1": true
}
```

Rules:
- `worktree_path` MUST be the ABSOLUTE path of the tree where you created +
  committed the files — the isolated worktree you applied the plan in, or the
  main checkout if you worked there directly. Verifier tiers `cd` into it before
  running scoped checks; an absent/wrong `worktree_path` makes them run in the
  main checkout and fail-closed ("No files matching the pattern …") on changes
  that live only in your worktree. Get it from `git rev-parse --show-toplevel`
  in the tree you edited.
- Do not mark `ready_for_tier1` true when `touched_files[]` is empty.
- `touched_files[]` must include every file this child created or modified,
  including migrations, tests, documentation/blame records, fixtures, scripts,
  config, and generated support files. Do not list only TypeScript/source
  files.
- Record every verification command you ran in `verification_evidence[]`; when
  a command cannot run, include the exact reason and the narrower equivalent
  verifier, if any.
- Do not edit files outside the kickoff scope unless the plan explicitly names
  them.
- Do not claim verifier success. Verifier nodes own pass/fail.
- Keep commands and evidence paths reproducible from the repo root.
