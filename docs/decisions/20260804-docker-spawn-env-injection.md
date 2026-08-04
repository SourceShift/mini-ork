# Decision — DockerWorkspace.spawn env injection (SE-3 Increment 2)

**Date:** 2026-08-04
**Status:** DECIDED (1/1/1 split → author synthesis across all three lenses)
**Plan:** `docs/plans/2026-08-04-isolation-selector-phase.md`
**Consensus run:** 3 role-differentiated subagents (Security · Reliability · Builder/Maintainer), model sonnet, parallel.

## Problem

`DockerWorkspace.spawn(argv, *, stdin, timeout, env, cwd)` (SE-3 Increment 2)
runs the coding-agent CLI inside a long-lived container via `docker exec -i`.
The `env` it receives is the FULL host process environment
(`proc_env = dict(os.environ)` + per-dispatch overrides), because
`dispatch.core.dispatch` builds ONE `proc_env` for both the host and the
container transports (`core.py:161`). Passing that whole env into the container
verbatim (`-e KEY=VALUE` for every key) both (a) **clobbers** the container's
own `PATH`/`HOME`/`USER`/`SHELL` with host values (`/Users/admin/.pyenv/...`,
`/Users/admin`) that don't exist in a Linux image — breaking `sh` startup /
making the CLI unfindable — and (b) **leaks** every unrelated host secret in
`os.environ` (AWS creds, SSH sockets, npm/GitHub tokens) into the container =
confused-deputy blast-radius expansion. So: which env keys cross the boundary,
and how?

## Options

- **(a) pass-all** `-e KEY=VALUE` for every key. Simplest; clobbers container
  shell env; maximal leak.
- **(b) denylist** — pass all EXCEPT a fixed host-shell set (PATH, HOME, PWD,
  OLDPWD, SHELL, USER, LOGNAME, TERM, TMPDIR, SHLVL, _, HOSTNAME). Never drops a
  key the CLI needs; **silently leaks any novel host secret** not on the list.
- **(c) allowlist** — pass only agent-relevant keys (provider-namespace prefixes
  + `MO_*` + `*_API_KEY`). Never leaks unrelated host secrets; risks **silently
  dropping** a newly-added key the CLI needs.
- **(d) pass-nothing** — ignore `env`, mirror today's `exec`, rely on
  image-baked / `up()`-injected keys. Most codebase-consistent; makes the `env`
  arg a dead lie and diverges from the host spawn path.
- **(other) shared `_container_env(env)` helper** — factor the policy (whatever
  it is) into ONE testable function both docker + the Increment-3 microvm
  backend import, so they can't drift.

## The three votes (verbatim)

**Security → (c) allowlist.**
LOAD-BEARING RISK: "(b)'s denylist will silently pass any novel host secret
(SSH keys, AWS credentials, cloud tokens) that wasn't anticipated when the
denylist was written, expanding the blast radius into the container with no
mechanism to catch the leak." Reasoning: the host `os.environ` is untrusted
input at the container boundary — a grab-bag of creds for services the container
has no business touching; a denylist requires knowing every dangerous name in
advance and one miss is a silent breach; an allowlist inverts the assumption so
nothing crosses unless categorized agent-relevant. Refinement suggested:
`--env-file` (root-owned, unlinked) over `-e` to avoid the host-`ps` argv leak.
ALTERNATIVE-IF-WRONG: if the allowlist is too brittle, combine (b) with a
secret-scanner that errors loudly on any non-allowlisted var whose value matches
known secret patterns.

**Reliability → (b) denylist.**
LOAD-BEARING RISK: "(a) clobbers container PATH/HOME with host values that don't
exist in the Linux image, making the CLI unfindable or `sh -lc` startup broken —
a silent hang-to-timeout nearly impossible to distinguish from an auth failure."
Reasoning: the denylist preserves the container's own shell env while forwarding
everything the CLI actually needs (all keys + MO_*), keeping `workspace=host`
and `workspace=docker` behaviorally consistent; the denylist set is stable POSIX
shell vars, not a moving target; a missed denylist entry surfaces immediately in
smoke, whereas (c)'s missing key surfaces only in production under load.
ALTERNATIVE-IF-WRONG: fall back to (c) but enforce the allowlist as a
schema-validated config with a CI gate that fails if a known-required key is
absent.

**Builder/Maintainer → (other) shared `_container_env` helper.**
LOAD-BEARING RISK: "Without a single shared policy function, docker and microvm
diverge silently — someone fixes a missing key in one backend and forgets the
other, and the bug only surfaces at runtime against a real container." Reasoning:
(a)-(d) all embed the policy inside `spawn`, forcing a copy-paste or refactor
when `MicrovmWorkspace.spawn` arrives; (d) makes the `env` arg a lie (worst
maintainability); a small `_container_env(env)->dict` stripping shell-identity
vars while forwarding MO_*/provider keys is deterministic, daemon-free testable,
and gives Increment 3 a free import — one auditable function, one test file.
ALTERNATIVE-IF-WRONG: if the helper is too abstract before microvm exists, fall
back to (b) with the denylist as a module-level named set.

## Synthesis (author decision on a 1/1/1 split)

**Chosen: allowlist policy (c), expressed as a named, fully-tested module-level
helper `_container_env` (the Builder's structural point), injected via
`-e KEY=VALUE` (transport).**

The three lenses are not actually in conflict on shape — the Builder's "shared
helper" is *orthogonal* to the allowlist-vs-denylist policy question, and both
Security and Builder are compatible with "an allowlist inside a named helper."
The only real fork is allowlist vs denylist, and it breaks on **failure-mode
asymmetry**:

- allowlist's failure = a *dropped* key → the CLI can't auth → **loud**, caught
  by the live docker smoke test at build time (rc≠0 / stderr).
- denylist's failure = a *leaked* secret → **silent**, invisible to every test,
  a high-consequence security blast-radius expansion on a security axis.

"Truth at the root" + the security axis being explicitly high-consequence means
we bias to the failure mode that is loud and caught. Reliability's real concern
(silently dropping a needed key) is mitigated, not ignored: the allowlist is
**generous and pattern-based**, not a hand-enumerated provider list —
`*_API_KEY` (suffix) catches any future `FOO_API_KEY` provider, and a broad set
of provider-namespace prefixes + `MO_*` covers config. Critically, `*_API_KEY`
does **not** match AWS's `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, so the
generous catch-all does not re-open the leak the Security voter flagged. The
live smoke test is the detection net for any genuinely novel pattern.

**Rejected:**
- **denylist (b)** — its silent-leak failure is undetectable and on a security
  axis; the whole reason to bound what crosses the boundary is that host
  `os.environ` is untrusted.
- **pass-all (a)** — clobbers container shell env AND maximal leak; strictly
  worse than (b).
- **pass-nothing (d)** — the dispatch env is the per-run source of truth for
  keys; `up()`'s static `env_passthrough` can't know per-dispatch config, and a
  dead `env` arg diverges host↔docker behavior (exactly the silent divergence
  Reliability warned about).

**Transport = `-e KEY=VALUE`, not `--env-file`, for Increment 2.** Rationale:
(1) it is *consistent with this very file* — `up()` already injects secrets via
`docker run -e KEY=$host_value` (`docker.py` env_passthrough), so `-e` in spawn
is not a new posture; (2) it is deterministic and daemon-free unit-testable
(sorted argv), which the task requires. The host-side `ps` argv-exposure the
Security voter flagged is a real but *bounded, single-tenant, brief-window*
exposure, and hardening it belongs at **both** `-e` sites uniformly, not just
spawn. Recorded as a follow-up.

**UPDATE (2026-08-04, follow-up #2 RESOLVED — transport hardened to bare `-e KEY`).**
The Security voter's `ps` argv-exposure is now closed at **both** sites (`spawn`
*and* `up()`), but with a *simpler* mechanism than the `--env-file` this doc
proposed: docker's `-e KEY` **without** a value forwards the value from the
docker *client's own process environment* rather than argv. So `spawn` emits bare
`-e KEY` (sorted, allowlisted names) and launches `subprocess.run(env={**os.environ,
**allowed})`; the secret lives only in `/proc/<pid>/environ` (owner+root-only —
the same protection a 0600 `--env-file` gives) with **no tempfile to create,
chmod, or unlink-in-`finally`, and nothing left on disk if the process crashes**.
Verified live: `docker exec -e MO_SECRET_PROBE <cid> …` forwarded the value into
the container while `ps aux` showed only the bare `-e MO_SECRET_PROBE` name, never
the value. The allowlist still fully governs *what* crosses (only named keys are
forwarded); this change only moves the *transport* of the value off argv. Daemon-
free unit tests remain deterministic (assert the bare `-e KEY` names + that no
value string appears in argv); a dedicated regression test
(`test_spawn_never_puts_a_secret_value_in_argv`) makes a value-in-argv leak
un-mergeable.

## Decision-risk audit

| Risk | Mitigation |
|---|---|
| A new provider key silently dropped | `*_API_KEY` suffix + broad provider prefixes; live smoke catches an auth failure loudly at build time |
| Generous suffix re-opens the leak | `*_API_KEY` deliberately does NOT match AWS `*_ACCESS_KEY_ID`/`*_SECRET_ACCESS_KEY`; prefixes are namespaced |
| Container shell env clobbered | PATH/HOME/USER/SHELL match no prefix/suffix → dropped → container keeps its own |
| docker/microvm policy drift (Builder's risk) | `_container_env` is one named, tested symbol; Increment 3 EXTRACTS it to a shared module (see follow-up), not import-from-docker |
| Host `ps` argv secret exposure | RESOLVED — bare `-e KEY` forwards the value from the client's own env (`/proc/environ`, owner+root-only), never argv; applied at both `-e` sites; regression test blocks any value-in-argv reintroduction |

## Standards uplift

- Env crossing an isolation boundary is **allowlist-by-default** in mini-ork:
  host `os.environ` is untrusted at the container/VM edge; only the harness
  (`MO_*`) + LLM-provider namespaces + `*_API_KEY` cross.

## Follow-ups (minimum-durable-change → known optimum)

1. **Increment 3:** extract `_container_env` + its `_AGENT_ENV_PREFIXES/SUFFIXES`
   constants OUT of `docker.py` into a shared `mini_ork/runtime/backends/
   _workspace_env.py` (or `runtime/workspace_env.py`); docker + microvm both
   import it. Do it under a worktree that claims both backend files.
2. **Both `-e` sites:** ~~evaluate `--env-file` (root-owned tempfile, unlinked in
   `finally`) to close the host-`ps` argv exposure at `up()` *and* `spawn`
   uniformly.~~ **DONE (2026-08-04)** — closed with bare `-e KEY` (value from the
   client env, not argv) at both sites; strictly simpler than `--env-file` (no
   tempfile/chmod/unlink, nothing on disk). See the UPDATE note in Synthesis.
3. **Egress/proxy:** if a container needs `HTTP(S)_PROXY`/`NO_PROXY`, add those
   names to the allowlist explicitly (deliberately not forwarded today).
