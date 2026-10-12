# determinism-lens — prove the engagement leg is rc-deterministic

You are the **kimi** lens for the orch-smoke-default-on engagement
harness. Your job is to prove the engagement leg is rc-deterministic
under repeated invocations: same `state.db`, same run id, same row
count, same `event_type`, same `MO_RUN_ORCHESTRATOR` status.

## Inputs

- `verifiers/fail-probe.py` (orch-smoke) — the canonical failing
  verifier. Re-read it: rc=1, sidecar write is wrapped in `try /
  except OSError`, and there is NO `sys.exit(0)` path.
- `verifiers/engagement-check.py` (orch-smoke-default-on) — the new
  pure-python verifier that asserts the orchestrator row exists.
- The two-bash-removal-world axiom: no `mktemp -d` races, no
  `sleep`/timing dependencies, no `curl`/`wget` calls, no env-derived
  randomness. Side effects only via the run dir.

## Output

Write a markdown file with:

1. A first non-blank line that is a markdown header (`# …`).
2. ≥ 4 non-blank lines total.
3. No chat-transcript markers (no fenced chat blocks).
4. Sections covering:
   - **fail_probe determinism** — re-state why `fail-probe.py` exits 1
     on every invocation: the `try / except OSError` wraps the sidecar
     write but not the `sys.exit(1)`. Re-running under the same
     `${MINI_ORK_RUN_DIR}` overwrites the sidecar with byte-identical
     content (same `pass: false`, same `reasons` list).
   - **engagement_check determinism** — re-state why the SQL is
     read-only: `SELECT COUNT(...)` does not mutate `state.db`; the
     verifier's only side effect is writing
     `verifier-result-engagement-check.json` to the run dir.
   - **MO_RUN_ORCHESTRATOR** — re-state why the engagement check is
     meaningful only when the variable is unset: the orchestrator's
     `enabled()` is ON for unset/empty/unrecognized, OFF only for
     explicit falsy. Asserting `not in os.environ` makes "default-on"
     a loadbearing check, not a vacuous one.

## Hard rules

- The engagement_check MUST NOT introduce timing dependencies. No
  `time.sleep(...)`, no retry loops, no exponential backoff.
- The engagement_check MUST NOT depend on the orch-smoke sidecar
  being parseable. The query stands on `run_events` alone.
- The engagement_check MUST NOT mutate `state.db`. `SELECT COUNT(...)`
  is read-only; the verifier's `sqlite3.connect` opens in default
  mode (no `isolation_level=None`).

## Output path

Write your lens to the run dir as
`${MINI_ORK_RUN_DIR}/lens-determinism.md`.
