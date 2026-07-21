# mini-ork Tests

## Quick Start

```bash
# From the repo root:
bash tests/smoke.sh
```

Exit 0 = all checks passed or were cleanly skipped.
Exit 1 = at least one check failed (see `[FAIL]` lines).

---

## Test Files

| File | What it tests | Requires |
|---|---|---|
| `tests/smoke.sh` | Deps, DB init, bash syntax, shellcheck | `sqlite3`, `jq`, `git`, `bash 4+` |
| `tests/unit/test_dispatch.sh` | `lib/dispatch.sh` error-handling | `lib/dispatch.sh` + `db/init.sh` |
| `tests/integration/test_bin_spawn.sh` | `mini-ork spawn` CLI lineage, child workspace, and child cap | `sqlite3`, `git`, dry-run mode |
| `tests/e2e/test_e2e_recursive_orchestration.sh` | root -> child -> grandchild recursive orchestration | `sqlite3`, `git`, dry-run mode |
| `tests/security/test_sec_recursive_spawn_limits.sh` | depth, authority, and orphan-parent spawn blocking | `sqlite3`, `git` |

---

## Running Individual Tests

```bash
bash tests/unit/test_dispatch.sh
bash tests/integration/test_bin_spawn.sh
bash tests/e2e/test_e2e_recursive_orchestration.sh
bash tests/security/test_sec_recursive_spawn_limits.sh
```

All test scripts follow the same convention:
- `[OK]` — assertion passed
- `[SKIP]` — precondition not met (dependency not yet present); not a failure
- `[FAIL]` — assertion failed; exit 1

---

## CI Integration

### GitHub Actions

```yaml
# .github/workflows/smoke.yml
name: smoke
on: [push, pull_request]
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install deps
        run: sudo apt-get install -y sqlite3 jq shellcheck
      - name: Run smoke tests
        run: bash tests/smoke.sh
      - name: Run unit tests
        run: |
          bash tests/unit/test_dispatch.sh
```

### GitLab CI

```yaml
smoke:
  image: ubuntu:24.04
  script:
    - apt-get install -y sqlite3 jq shellcheck git bash
    - bash tests/smoke.sh
    - bash tests/unit/test_dispatch.sh
```

### Makefile target

```makefile
test:
	bash tests/smoke.sh
	bash tests/unit/test_dispatch.sh
```

---

## Skips vs Failures

A `[SKIP]` means a precondition was not met — typically because another
agent is still building the dependency being tested. Skips are **not**
counted as failures. The smoke test exits 0 if all results are `OK` or
`SKIP` with zero `FAIL`.

This design lets CI run cleanly on a partial repo while agents are still
in-flight.

---

## Adding New Tests

1. Create `tests/unit/test_<module>.sh` using the same `_ok` / `_fail` /
   `_skip` pattern as the existing unit tests.
2. Add a guard at the top: skip if the library under test is not present.
3. Use a `mktemp -d` isolated `MINI_ORK_HOME` — never write to the caller's
   `.mini-ork/` directory.
4. Clean up with `rm -rf "$TMP_DIR"` at the end.
5. Add a row to the table in this README.

The guard + skip pattern means tests can be added for features not yet
implemented — they stay in the repo as forward documentation and flip to
`[OK]` automatically once the implementation lands.
