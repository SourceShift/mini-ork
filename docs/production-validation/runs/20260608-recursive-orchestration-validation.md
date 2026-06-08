# Recursive Orchestration Validation — 2026-06-08

## Scope

Validate the new recursive mini-ork control plane without Anthropic-family
provider calls:

- Root run can delegate a child run.
- Child run can delegate a grandchild when explicitly allowed.
- Depth, child-count, orphan-parent, and full-authority limits block unsafe
  recursion.
- Python integrations can drive the same recursive path through `MiniOrk.spawn`.

## Commands

```bash
bash tests/integration/test_bin_spawn.sh
bash tests/security/test_sec_recursive_spawn_limits.sh
bash tests/e2e/test_e2e_recursive_orchestration.sh
PYTHONPATH=. python3 tests/live/recursive_live_validation.py
PYTHONPATH=. timeout 1200 bash tests/run-all.sh
```

## Results

| Check | Result |
|---|---|
| Spawn CLI integration | PASS, 9 OK / 0 FAIL |
| Recursive spawn security | PASS, 3 OK / 0 FAIL |
| Recursive e2e chain | PASS, 6 OK / 0 FAIL |
| Live Python recursive validation | PASS, `spawn_count=2`, `completed_events=2` |
| Full test pyramid | PASS, 60 files, 543 OK / 0 FAIL |

## Notes

The live validation uses `MINI_ORK_DRY_RUN=1`, so it executes real mini-ork CLI
and Python facade paths without external model calls. This matches the current
provider constraint for validation: Codex/local runtime only, no Anthropic model
invocation.
