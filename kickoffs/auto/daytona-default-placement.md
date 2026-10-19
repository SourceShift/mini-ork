# Default placement: a bare `mini-ork run` executes on Daytona when configured

## Goal

Today cloud execution is opt-in: `mini-ork run <recipe> <kickoff> --placement
remote --env daytona`. The operator wants a bare `mini-ork run` to use a
Daytona sandbox **by default**, while a laptop or CI checkout with no cloud
profile keeps working unchanged (CLAUDE.md "feature delivery: two tiers").

Resolution rule — implement exactly this:

1. An explicit `--placement` / `MO_PLACEMENT` (including `--placement local`)
   and an explicit `--env` / `MO_NODE_ENV` always win. No new behavior.
2. When the run named neither, adopt `MO_DEFAULT_NODE_ENV` (default value
   `daytona`) **iff** a profile file for that name exists in the visible
   config dirs. The adoption is exactly equivalent to passing
   `--placement remote --env <name>`: the existing validation, publishing of
   `MO_PLACEMENT`/`MO_NODE_ENV`/`MO_REMOTE_SESSION_SCOPE=lifecycle`, node
   acquisition and run-end release all run unchanged.
3. No such profile → publish nothing → local run, exactly as today. The
   committed repo ships only `*.yaml.example`, so a fresh clone and CI stay
   local automatically.
4. `MO_DEFAULT_NODE_ENV` set to an empty value or one of
   `0|false|no|off|none|local` (case-insensitive) disables the default
   entirely. `--placement local` remains the per-run opt-out.
5. Inside a remote node (`MO_REMOTE_NODE=1`) the default is skipped: a
   sandboxed run must never create a sandbox inside itself.

Fail closed: when the default profile exists but is invalid (schema violation,
lookalike-secret key), the run exits 2 with a message that names
`MO_DEFAULT_NODE_ENV` and the profile; it must not silently fall back to local.
A missing provider credential is already a `ProviderUnavailableError` naming
`DAYTONA_API_KEY` at node acquisition — do not weaken it, do not add a
pre-flight that turns it into a host run.

## Mechanism (exact)

### 1. `mini_ork/remote/environments.py`

Add, next to `list_profiles`:

```python
DEFAULT_NODE_ENV = "daytona"
_DEFAULT_ENV_OFF = frozenset({"", "0", "false", "no", "off", "none", "local"})


def default_node_env(env: Mapping[str, str] | None = None) -> str:
    """The environment profile a run adopts when it named none.

    ``MO_DEFAULT_NODE_ENV`` names the profile (default ``daytona``); an empty
    value or one of the off-words disables the default. The profile must exist
    in the visible config dirs — ``*.yaml.example`` files do not count, so a
    fresh checkout stays local. An EXISTING but invalid profile is returned
    anyway: the caller's ``load_profile`` validation reports it (fail closed)
    instead of this helper silently downgrading the run to the host.
    """
    src = os.environ if env is None else env
    raw = src.get("MO_DEFAULT_NODE_ENV")
    name = DEFAULT_NODE_ENV if raw is None else raw.strip()
    if name.lower() in _DEFAULT_ENV_OFF:
        return ""
    return name if name in list_profiles(env=src) else ""
```

Export both names in `__all__`.

### 2. `mini_ork/cli/main.py`

Add immediately before `_run_lifecycle_impl`:

```python
def _apply_default_placement(
    placement: str, node_env: str, env: Mapping[str, str]
) -> tuple[str, str, bool]:
    """Adopt ``MO_DEFAULT_NODE_ENV`` when the run specified no placement.

    Returns ``(placement, node_env, defaulted)``. Explicit input wins;
    ``MO_REMOTE_NODE=1`` (already inside a sandbox) skips the default.
    """
    if placement or node_env:
        return placement, node_env, False
    if (env.get("MO_REMOTE_NODE") or "").strip() == "1":
        return placement, node_env, False
    from mini_ork.remote.environments import default_node_env  # noqa: PLC0415

    name = default_node_env(env)
    return ("remote", name, True) if name else (placement, node_env, False)
```

In `_run_lifecycle_impl`, after the two existing reads

```python
placement = placement or context_env("MO_PLACEMENT", "").strip()
node_env = node_env or context_env("MO_NODE_ENV", "").strip()
```

insert

```python
placement, node_env, placement_defaulted = _apply_default_placement(
    placement, node_env, context_env_snapshot())
```

before the `placement not in ("", "local", "remote")` check. Then, in the
`if problem:` branch, when `placement_defaulted` and the failure is a
`ValueError`, build:

```python
problem = (f"default environment '{node_env}' (MO_DEFAULT_NODE_ENV) "
           f"is invalid: {exc}")
```

(keep the existing `--env:` wording for explicit profiles). The
`FileNotFoundError` / missing-name branches cannot fire for a defaulted run
(existence was pre-checked); leave them as they are. Everything after the
validation block — `publish_env`, `MO_REMOTE_SESSION_SCOPE=lifecycle` — is
unchanged and is what makes the adopted default reach dispatch, the session
factory, gen_profile and child processes.

Do not change `_release_remote_session`, `gen_profile`'s badge logic, resume,
`nodes.py`, or the dispatch placement policy.

### 3. `docs/architecture/cloud-sandbox-providers.md`

- Goal paragraph: note that a bare `mini-ork run` now adopts the `daytona`
  profile when one is configured, and that `--placement local` opts out.
- The two-axis table's **Remote placement** row: selector is
  `--placement remote --env <profile>`; add that `<profile>` defaults to
  `MO_DEFAULT_NODE_ENV` (`daytona`) when configured.
- New subsection after the provider-selection paragraph, titled
  `### Default placement`: the five resolution rules above (explicit wins,
  default env, no profile → local and why fresh checkouts are local, off-words,
  `MO_REMOTE_NODE=1` guard), plus: an invalid default profile fails the run
  closed; `MO_DEFAULT_NODE_ENV=e2b` selects a different provider profile.

### 4. `tests/unit/test_default_placement.py` (new, hermetic)

Use `tmp_path` homes, monkeypatch `MINI_ORK_HOME` / `MINI_ORK_ROOT`, and clear
`MO_PLACEMENT`, `MO_NODE_ENV`, `MO_DEFAULT_NODE_ENV`, `MO_REMOTE_NODE` per test.

- `default_node_env` → `"daytona"` for a home with
  `config/environments/daytona.yaml`; `""` for an empty home; `""` when the
  only file is `daytona.yaml.example`; the template dir (`MINI_ORK_ROOT`)
  counts.
- off-words: each of `""`, `off`, `LOCAL`, `0`, `none` disables even with the
  profile present; a custom name (`e2b`) is returned only when `e2b.yaml`
  exists, else `""`.
- `_apply_default_placement`: `("", "", env)` → `("remote", "daytona", True)`;
  `("local", "", env)` → unchanged `False`; `("remote", "alpha", env)` →
  unchanged `False`; `("", "alpha", env)` → unchanged `False`;
  `MO_REMOTE_NODE=1` → unchanged `False`.
- one production-entrypoint test through `main._run_lifecycle_impl`: a temp
  home whose `daytona.yaml` is schema-invalid (`image: x` + `colour: blue`)
  and a kickoff file; assert rc 2, stderr contains `MO_DEFAULT_NODE_ENV` and
  `is invalid`, and `os.environ.get("MO_PLACEMENT")` is unset afterwards
  (nothing published on a refused run). Mirror the setup of
  `tests/unit/test_placement_policy.py:test_cli_remote_without_resolvable_env_exits_2_listing_profiles`.

## Files in scope

- `mini_ork/remote/environments.py`
- `mini_ork/cli/main.py`
- `docs/architecture/cloud-sandbox-providers.md`
- `tests/unit/test_default_placement.py` (new)

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_default_placement.py tests/unit/test_placement_policy.py tests/unit/test_environments.py tests/unit/test_mini_ork_cli_py.py`
- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m ruff check --select F,E9 mini_ork/cli/main.py mini_ork/remote/environments.py tests/unit/test_default_placement.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_default_placement.py tests/unit/test_placement_policy.py --junitxml="${MINI_ORK_RUN_DIR}/daytona-default-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/daytona-default-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not alter the semantics of an explicit
`--placement`/`--env` (byte-for-byte today's behavior), the dispatch placement
policy, `MO_PLACEMENT_LOCAL_ROLES`, resume, or `nodes.py`. Do not add a
pre-flight secret check; the provider's fail-closed `ProviderUnavailableError`
stays the single place that names a missing credential. Do not write to the
live DB under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
