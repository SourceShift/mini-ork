# Connect Linear and Jira from the UI: a Sources pane, secret-only

## Goal

Today a PM source exists only if a person hand-edits `config/pm.yaml` and puts a
token in the environment. Build the UI path: a **Sources** pane that lists every
declared source with its state, and an **Add source** flow for Linear and Jira
that validates the credential and writes it to the store the vault already
reads. The Board's `Sources` tab is the natural home; the Setup page's
`_settings_tab` gains the pointer to it.

Ticket **write-back stays out of scope** — this epic writes *credentials and
config*, never issue content.

- recipe: framework-edit

## Design decisions already taken (do not re-litigate)

- **Secret only, both tiers.** The page writes the credential value and nothing
  else secret: a 0600 `secrets.local.sh` for the simple tier (the file
  `FileBackend` already reads), and for the secure tier it writes *nothing* —
  it reports the exact `sops`/`openbao` command for an operator to run. The
  backend is chosen by `config/vault.yaml`, which this epic reads and never
  guesses. **Fail closed**: an unknown backend, or a secure-tier backend whose
  CLI is absent, is a named error, never a quiet fall back to the 0600 file.
- **`config/pm.yaml` is written**, with `id`, `enabled`, `token_env` and
  `settings` (Linear team key, Jira site/project) — but a credential **value**
  never appears in it, in a log, in a page payload or in an error. An error
  names the key and the backend, never the secret.
- `mini_ork/dispatch/vault.py` is read-only **by design** and stays the reader.
  The write path is a new, separate module so no reader grows a `set()`.
- **Test connection is an explicit button**, never a side effect of opening the
  pane — the deliberate Orca choice, and it matters more here because reading a
  secret may unseal one.

## Work

1. **`mini_ork/dispatch/vault_write.py`** (new) — `set_secret(name, value, *,
   config)` dispatching on the configured backend:
   - `file` → read `secrets.local.sh`, replace or append the one `export NAME=`
     line, write through a `0600` temp file and `os.replace`, so a crash cannot
     truncate the store and the mode is never briefly world-readable. Quote the
     value; refuse a name that is not a shell identifier; a value containing a
     newline is refused rather than mangled.
   - `sops` / `openbao` / `command` → **refuse**, returning the exact argv an
     operator should run. Never shell out to a writer this epic has not tested.
   - `env` → refuse; an environment variable is the operator's to set.
   - Every refusal is a distinct exception naming the backend.
2. **`mini_ork/pm/config.py`** — `write_config(...)` writing `config/pm.yaml`
   from the same dataclasses `parse_config` reads, so a written file round-trips
   through the reader byte-for-byte. Write atomically; preserve unrelated keys.
3. **`mini_ork/cli/pm.py`** (new subcommand, promised by
   `pm/providers/__init__.py:55` and never registered) — `pm sources`,
   `pm add <id> [--token-env K --set k=v ...]`, `pm test <id>`, `pm remove <id>`.
   `main.py` gains the one dispatch line. `pm add` prompts for the value only
   when stdin is a tty; otherwise it reads `--token-stdin`.
4. **`mini_ork/ide_pages/kanban.py`** — the `Sources` tab grows from a status
   line into a real pane: one row per declared source with enabled state, last
   sync, recorded error, and its capability badges from
   `registry.capabilities()`; a **Test** button (explicit), and for `linear`
   and `jira` an **Add source** action. A `skipped_sources()` row explains a
   source declared but not built, in its own words.
5. **The add flow** — a page taking `provider`, then `site`/`team`/`project`
   and the secret in a `type="password"` field. It calls `pm test` before
   writing, and on success calls `vault_write.set_secret` + `write_config`. The
   payload carries the secret **once**, and the page that renders any error
   renders only the field name.
6. **`mini_ork/ide_pages/setup.py:526`** — the Credentials line currently says
   *"a credential is never entered through this page"*. It may stay true of
   *this* page, but the project-wide claim is now false: reword it to say
   credentials are entered through the Board's Sources tab and list the vault
   backend in force.

## Constraints

- Nothing in this epic may put a secret into a page payload, a log line, an
  exception message or a CLI echo. Write the test that greps the built page for
  a sentinel token before writing the UI.
- `S.guarded` returns a **list** — `extend`, never `append`.
- A `cli` action must pass `home=False` for any head whose flag loop rejects a
  trailing `--home` (rc 2); check `mini_ork/cli/pm.py`'s own parser first.

## Verification commands

- `python3 -m compileall -q mini_ork`
- `python3 -m pytest -q tests/unit/test_vault.py tests/unit/test_pm_config.py tests/unit/test_ide_pages_kanban_tickets.py`

## Done When

- `mini-ork pm sources` lists declared, enabled, skipped and errored sources.
- `pm add linear --token-stdin` against a temp home writes a 0600
  `secrets.local.sh` holding the value and a `config/pm.yaml` holding only the
  key *name*; a second `load_config()` reads both back.
- With `config/vault.yaml` naming `sops`, the same command writes no file and
  exits non-zero printing the `sops` argv.
- The Sources pane renders a source row, a Test button, and no token value
  anywhere in its JSON.
- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"` with all verifiers passing.
- All verification commands pass in the isolated worktree.
