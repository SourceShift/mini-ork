# Production scenario: code fix for route mismatch

## Goal

Fix a bug in `scripts/run_production_scenarios.py` where a scenario can exit
successfully even when the dispatcher selected the wrong task class.

## Target repo

This repository: `mini-ork`.

## Scope allow

- `scripts/run_production_scenarios.py`
- `bin/mini-ork`
- `bin/mini-ork-classify`

## Scope deny

- `lib/providers/**`
- `db/migrations/**`
- `recipes/**/prompts/**`

## Success criteria

- Production scenario runner fails if emitted `task_class=` differs from the
  expected recipe task class.
- `.md-only` scenario mode still reaches verify when routing is correct.
- The fix is covered by a shell-level scenario command.

## Verification command

```bash
python3 scripts/run_production_scenarios.py --mode dry-run --md-only code-fix
```

## Risk tolerance

Medium. Commit is allowed only if the diff is limited to dispatcher or scenario
runner logic.
