# Environment variables

MiniOrk works with no environment variables for a local project. Set the
variables below only to change where state is stored, separate engine and
target repositories, or enable an explicitly documented runtime behavior.

## Core paths

| Variable | Default | Purpose |
|---|---|---|
| `MINI_ORK_ROOT` | MiniOrk engine root | Locates shipped recipes, migrations, and configuration. |
| `MINI_ORK_HOME` | `.mini-ork/` under the project | Stores project-local state, configuration, and run artifacts. |
| `MINI_ORK_DB` | `$MINI_ORK_HOME/state.db` | Overrides the SQLite state database path. |
| `MINI_ORK_PROJECT_HOME` | current project | Identifies the project MiniOrk is operating for. |
| `MINI_ORK_TARGET_REPO` | current project | Identifies the repository available to agent and verifier work. |
| `MINI_ORK_ENGINE_ROOT` | resolved engine root | Overrides engine discovery for installed launchers. |

`MINI_ORK_DB` takes precedence over the database path derived from
`MINI_ORK_HOME`. Keep each concurrent project or test run on its own home and
database path.

## Credentials

Configure provider credentials with the CLI instead of passing keys on command
lines:

```bash
mini-ork providers configure <lane>
mini-ork providers status <lane>
```

The command writes local credentials to
`$MINI_ORK_HOME/config/secrets.local.sh`, which is ignored by Git. See
[provider triage](provider-triage.md) for provider-specific diagnosis.

## Runtime behavior

Advanced `MO_*` and `MINI_ORK_*` switches are cataloged in
[Feature flags](feature-flags.md). Set only the flags required by a specific
runbook or recipe; defaults are selected to keep the standard CLI path safe.

Two common extension selectors are:

| Variable | Purpose |
|---|---|
| `MO_ROUTING_POLICY` | Chooses a registered native routing policy. |
| `MO_EMBED_PROVIDER` | Chooses a registered semantic-memory embedder. |

`MO_REVISE_ROUNDS` (default `2`) caps the revise-loop rounds a failed
`retries`-edge node may spend re-dispatching its target before the
`escalates_to` rollback edges fire. Set it to `0` to restore the pre-revise
behaviour (a failed node blocks its descendants and rollback runs).

The verification stack — metamorphic relations, the differential check,
mutation suite adequacy, the level vector, the gate-hackability audit, and the
declared equivalence operator — is ON by default. Its knobs are listed under
[Feature flags › Verification stack](feature-flags.md#verification-stack) and
explained in [the verification stack](../architecture/verification-stack.md).

Do not use the retired `MINI_ORK_RUNTIME` selector. The supported framework
runtime is native Python; shell execution remains available only for declared
recipe verifier, target-repository, migration, and sandbox boundaries.
