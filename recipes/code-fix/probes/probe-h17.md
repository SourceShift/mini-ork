---
oracle: git
holdout: true
---
# Frozen probe probe-h17 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(env): the run contract must survive the transport's second filter

Found by the S1 live-container probe, not by the unit tests.

S1 routes the recursive child through the Workspace axis: `cli/spawn.py` filters
the AMBIENT host env through `container_env`, then injects the child's identity
on top. But a backend's `spawn` filters the env it is handed a SECOND time — and
`MINI_ORK_*` matches neither the `MO_*` prefix nor the `*_API_KEY` suffix, so that
second pass stripped all five keys and the child launched anonymous.

Proved live under MO_SANDBOX_BACKEND=docker with a probe CLI on the drive: the
child saw ANTHROPIC_API_KEY (allowlisted, correctly crossed), no host secret and
the container's own PATH (correctly excluded) — and every one of MINI_ORK_HOME /
_DB / _RUN_ID / _PARENT_RUN_ID / _ALLOW_CHILD_SPAWN as `<unset>`. A real mini-ork
child in that state resolves a fresh MINI_ORK_HOME off its cwd and loses the whole
run lineage. The same re-filter is on the shipped SE-3 dispatch path
(`_spawn_in_workspace` hands the backend proc_env), so this was a pre-existing
hole that S1 made load-bearing.

`_RUN_CONTRACT_KEYS` re-admits exactly those five key NAMES, which makes filtering
an already-assembled env idempotent. Deliberately an exact-name set and NOT a
`MINI_ORK_` prefix: the namespace stays closed, so MINI_ORK_ROOT / a host
MINI_ORK_HOME / MINI_ORK_SECRETS still cannot ride, and a forgotten override keeps
failing LOUD in the child rather than silently pointing it at a host path that
does not exist inside the sandbox.

One place covers both backends — docker and microvm import the SAME policy object
(asserted by [hidden]).

Verified: [hidden] + test_docker_spawn.py +
test_microvm_spawn.py + test_spawn_workspace_isolation_py.py → 50 passed,
3 skipped. Live re-probe on docker: all five contract keys now arrive with the
CONTAINER view (MINI_ORK_HOME=/workspace, RUN_ID=c-env), MINI_ORK_ROOT still
absent, host secret still absent, rc=0, no leftover containers.

## Files in scope

- mini_ork/runtime/backends/_workspace_env.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
