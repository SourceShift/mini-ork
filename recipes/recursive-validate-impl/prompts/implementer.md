# Implementer prompt

Apply the current plan in an isolated worktree or equivalent clean tree. Keep
the patch scoped to the kickoff, the plan, and any reflector/replanner guidance
from the current iteration. Preserve user changes in the main checkout.

After editing, write `${MINI_ORK_RUN_DIR}/implementer-summary.json` as strict
JSON with a mandatory `touched_files[]` array. Verifier tiers read that array
to scope compile, lint, unit, property, and mutation checks.

Required JSON shape:

```json
{
  "iteration": 1,
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
