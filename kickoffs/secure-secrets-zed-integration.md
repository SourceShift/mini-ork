# Secure secret configuration, integrated with Zed

**Status:** plan · **Scope:** `mini_ork/dispatch/{secrets,mcp_registry,run_env}.py`,
`mini_ork/cli/{mcp_cmd,zed_cmd}.py`, `docs/CONFIG.md`, `docs/ZED.md`

## 1. The requirement

One vault that holds every credential an MCP server or a skill needs — the
arXiv/LibWit token is the motivating case — such that:

* a node gets the credential it was granted and no other node gets it;
* a run can supply a different value than the previous run, without editing
  anything shared and without exporting into a parent shell where concurrent
  runs would see it;
* the file that *names* a server or a skill stays committable (no secrets in
  git);
* the same credential serves a mini-ork node **and** the same server inside the
  Zed editor, so there is one place to rotate it.

## 2. What Zed actually does (researched, not assumed)

| Question | Answer | Source |
|---|---|---|
| Where does a `context_servers` entry keep a token? | In the entry itself — `headers: {"Authorization": "Bearer <token>"}` for remote, `env: {...}` for stdio. Literal values in `settings.json`. | `docs/src/ai/mcp.md` |
| Does Zed expand `${VAR}` or reference a keychain in settings? | **No.** No interpolation is documented or implemented. | `docs/src/ai/mcp.md`; issue [#56870](https://github.com/zed-industries/zed/issues/56870) is an open *proposal* for a `$keychain` reference |
| What does Zed use the keychain for? | **AI provider API keys only.** `zed_credentials_provider` (an OS-keyring abstraction) used via `ClientCredentialsProvider` in `crates/client/src/client.rs`. Also reads provider-specific env vars, which take precedence over the keychain. | `docs/src/ai/use-api-access.md`; `crates/client/src/client.rs` |
| Can an extension declare a keychain-backed secret setting? | **No.** Extensions can prompt for a value, but that does not make it keychain-backed. | Zed settings/extension API |
| How does a remote server authenticate then? | Zed runs the standard MCP **OAuth** flow — but only when the entry has *no* `Authorization` header. | `docs/src/ai/mcp.md` |
| Where do skills live? | `~/.agents/skills/<name>/` (global) or `<project>/.agents/skills/<name>/` — a directory of `SKILL.md` + rules/scripts. No secret surface of its own. | `docs/src/ai/skills.md` |
| Can Zed spawn an arbitrary command for a context server? | **Yes** — `"command"` is any executable, with `"args"` and a literal `"env"` map. Zed has no opinion about what it launches. | `docs/src/ai/mcp.md` |

**Conclusion that shapes the whole design:** Zed has no vault and cannot be
given one (an extension runs in a WASM sandbox with no filesystem access, and
its settings are plaintext). Its one useful property is that it will *spawn
anything*. So the bridge is **not** a setting we write into Zed — it is a
**command Zed runs**, which resolves the credential from mini-ork's vault at
spawn time. The secret never reaches `settings.json`.

## 3. The four layers

### L1 — One vault (built)

`$MINI_ORK_HOME/config/secrets.local.sh`, owner-only 0600, `export NAME=value`
lines, refuses symlinks, atomic writes (`mini_ork/dispatch/secrets.py`).
`${NAME}` / `${NAME:-fallback}` is the one placeholder primitive, shared by
every consumer below.

### L2 — The registry that names, never holds (built)

`mini_ork/dispatch/mcp_registry.py` + `mini-ork mcp`:
`mcp_servers.json` holds `command`/`url`/`headers` with `${NAME}` references;
a node reaches a server by name is resolved to a node-scoped
`.mcp-config.json` at dispatch, under `--strict-mcp-config`. An unresolvable
`${NAME}` is a hard error at dispatch, not an empty header discovered mid-call.

### L3 — Per-run and per-node values (built)

`<run_dir>/config/env.yaml` — `env:` (whole run) + `nodes.<id>:` (one node),
with `null` masking. Precedence: recipe node `env:` < run `env:` < run
`nodes.<id>`. Leak-free because the node boundary publishes `None` for every
declared key the node is not supplied.

### L4 — The Zed bridge (to build)

**L4a. `mini-ork mcp launch <name>` — the shim.**

```
mini-ork mcp launch arxiv        # stdio: resolve, then execvp the real server
```

Resolves the registry entry exactly as a node dispatch does (store overlaid
with `os.environ`), merges the resolved `env` onto the inherited environment,
and `execvp`s the real `command`. Zed calls *this*, so:

* `settings.json` contains `/abs/path/mini-ork mcp launch arxiv` and nothing
  secret;
* rotating the token in `secrets.local.sh` rotates it for Zed and for every
  node at once — no Zed restart, because resolution happens per spawn;
* the same entry definition serves both, so they cannot drift.

For a **remote** (`http`/`sse`) server there is no process to exec. Two options,
in order:

1. **Prefer Zed's OAuth** when the server supports it — omit the header, let
   Zed run the flow. Nothing to bridge.
2. **`mini-ork mcp proxy <name>`** — a stdio MCP server that connects to the
   remote URL with the resolved headers and pipes both directions. Zed sees a
   local stdio server; the token stays in the vault. This is ~100 lines and
   reuses the stdio MCP machinery `mini_ork/mcp_context/server.py` already has.

**L4b. `mini-ork zed setup` emits one context server per registered MCP server.**

`mini_ork/cli/zed_cmd.py` already merges `context_servers["mini-ork"]` →
`mini-ork mcp-context` with a backup, `--dry-run`, `status`, and `uninstall`.
Extend the same merge to add, for each entry in the registry:

```json
"context_servers": {
  "mini-ork":      { "command": "<launcher>", "args": ["mcp-context"], "env": {...} },
  "mcp:arxiv":     { "command": "<launcher>", "args": ["mcp", "launch", "arxiv"], "env": {...} },
  "mcp:libwit":    { "command": "<launcher>", "args": ["mcp", "launch", "libwit"], "env": {...} }
}
```

Namespaced `mcp:<name>` so an operator's own entries are never clobbered;
`uninstall` removes exactly the `mcp:`-prefixed ones it wrote. Servers that do
not resolve (`mini-ork mcp list` → `unresolved`) are **skipped with a warning**,
not written as a broken entry. `status` grows the same resolution check.

**L4c. No Zed extension.** Stated explicitly so nobody builds one: an extension
cannot read the vault, cannot store a secret, and cannot do anything the
`command` shim does not already do. A `secret`-typed extension setting would
still land in `settings.json`.

### Skills

A skill is a directory (`<root>/.agents/skills/<name>/`) with no secret surface;
its *scripts* need env, which they get from the same vault. So the Zed side
needs **no** wiring — Zed already reads `.agents/skills/`.

The mini-ork side has a real hole to close: `MO_SKILLS` is published at the
node boundary (`execute_handlers.py`) and claude **rejects** the axis
(`unsupported_axes() == ("skills", "agent_doc")`), so a granted skill today
reaches nothing. Fix it the same way MCP access is fixed — **materialize, do
not request**:

* a skill registry `skills.yaml` (same `${NAME}` primitive, same `mini-ork
  skills list|add|rm|check` shape as `mini-ork mcp`);
* at the node boundary, materialize `<run_dir>/nodes/<id>/skills/` containing
  only the granted skill directories (symlink on POSIX, copy as the fallback),
  and publish its path so the harness's skill loader finds the ceiling and
  nothing above it — the analogue of `--strict-mcp-config`;
* record `approved` vs `used` in the node event so a skill that ran without
  being granted is visible.

## 4. Phases

| Phase | Deliverable | Files |
|---|---|---|
| P1 | `mini-ork mcp launch` (stdio exec shim) + tests | `cli/mcp_cmd.py` (new `launch` action), `dispatch/mcp_registry.py` (a `merged_env` helper) |
| P2 | `mini-ork zed setup` emits `mcp:<name>` entries; `status`/`uninstall` aware | `cli/zed_cmd.py`, `tests/unit/test_zed_cmd.py` |
| P3 | `mini-ork mcp proxy <name>` for remote servers (stdio↔http) | `cli/mcp_cmd.py`, `mini_ork/mcp_context/proxy.py` |
| P4 | Skill registry + node-boundary materialization + `approved`/`used` | `dispatch/skill_registry.py`, `cli/skills_cmd.py`, `cli/execute_handlers.py`, `schemas/workflow.schema.json` |
| P5 | Docs | `docs/CONFIG.md`, `docs/ZED.md` |

### Status (2026-10-10)

**Landed — the vault (the layer P1–P4 all sit on).** `mini_ork/dispatch/vault.py`
replaces "the secret store" with a chain of backends behind the same read-only
`Mapping` that `expand_secret_references` already consumed, so every existing
`${NAME}` caller — MCP registry headers, `env.yaml`, node boundaries — gained
the vault without changing its own logic:

* backends: `env`, `file` (0600, the zero-setup default), `sops`, `command`
  (`pass`/`op`/`bws`/`infisical`), `openbao` (self-hosted, path-scoped tokens);
* `VaultError` is a `SecretStoreError`, so a backend failure reports through the
  same path as any other unresolvable `${NAME}` and fails closed;
* `mini-ork vault status|which` report the chain and the holder of a name,
  never a value; `config/vault.example.yaml` documents every backend;
  `docs/CONFIG.md § Secret Vault` explains precedence and the secure/simple split.
* Wired at: `cli/main.py` (run boundary), `cli/execute_handlers.py` (node
  boundary), `dispatch/run_env.py`, `dispatch/providers.py`, `cli/mcp_cmd.py`.

This is the reference implementation of the two-tier rule now in `.rules`:
the simple tier is `env` + a 0600 file, and the secure tier is OpenBao (or
`sops`/`command`) chosen by editing one `vault.yaml`, with no call site changed.

**Landed — P1, P2, P3 (2026-10-10, after your go-ahead):**

* `mini-ork mcp launch <name>` — resolves the registry entry against the vault
  and `execvp`s the stdio server, so settings.json names a server and holds no
  token (`cli/mcp_cmd.py`).
* `mini-ork mcp proxy <name>` — the stdio↔streamable-HTTP bridge for a
  URL-based server (`dispatch/mcp_proxy.py`): JSON and SSE replies, the
  `Mcp-Session-Id` echo, and 202-with-no-body notifications. `launch` delegates
  to it for a `url` entry, so the editor names one command either way.
* `mini-ork zed setup` wires each registry server as `context_servers["mcp:<name>"]`
  pointing at the shim — stdio and URL alike, both secret-free; `--no-mcp` opts
  out, `uninstall` removes everything under the `mcp:` prefix, `status` lists it.
* `docs/ZED.md § MCP servers inside Zed` explains the token path and the two
  deliberate exclusions (extension-provided and OAuth servers).

Tests: `tests/unit/test_mcp_launch_py.py`, `test_mcp_proxy_py.py` (a real
in-process HTTP endpoint), and six new cases in `test_zed_cmd.py`. 116 tests
pass across the touched suites; `ruff check mini_ork/ tests/` clean.

**Landed — P4 (2026-10-10).** `mini_ork/dispatch/skill_registry.py`: skill
registries resolve `name -> directory` from `<run_dir>/config/skills`,
`$MINI_ORK_HOME/config/skills`, then `$MINI_ORK_ROOT/.agents/skills` (the
directory Zed reads), and a node's granted skills are materialized as symlinks
in `<node cwd>/.claude/skills/` around exactly the handler call — created
before the node runs, released when it ends, including on failure. Two rules
keep it safe in a working tree: an existing entry is never overwritten (yours
wins), and release removes only links it created — a link the node turned into
a real directory is that node's output and stays. Tests:
`tests/unit/test_skill_registry_py.py`; docs: `docs/CONFIG.md § Skills`.

### Compared against `sources/orca` (2026-10-10)

Requested: check how orca handles secrets/skills and adopt its approach where it
is better, keeping the cloud-secure tier. Findings:

* **Secrets — orca is worse, and confirms the design.** orca keeps MCP
  credentials as *literal plaintext* in its MCP JSON configs (`env?: Record<str,
  str>`, `src/shared/mcp-config.ts`); masking (`maskMcpEnv`) is display-only. Its
  `safeStorage` keychain store serves integrations (Linear/Jira/plugins/cloud
  session), **not** MCP or skills, and there is no `${ENV}` interpolation on the
  MCP/skill path. So orca does the exact thing this feature exists to prevent;
  the vault is not replaced.
* **Skills — mini-ork is better.** orca scopes skills global-vs-workspace
  (`skill-install-destinations.ts`), with no per-agent isolation; mini-ork's
  per-node materialization enforces exactly the ceiling orca lacks. orca *copies*
  with a digest check + atomic rename because it installs over SSH; mini-ork
  symlinks, which is right for a local checkout and keeps one source of truth.
* **Adopted — honest degradation reporting.** orca's `describeProtectionGap()` /
  `reportSecretProtectionGap` tells the user once per change when secrets are
  unprotected, and never blocks. mini-ork had `vault status` (pull) but no push.
  Added: `SecretBackend.protected`, `VaultEnvironment.protection_gap()`,
  `report_protection_gap()` in `vault.py` — printed once at the run boundary of
  `mini-ork run` and shown in `mini-ork doctor`, deduped by a chain-signature
  marker at `$MINI_ORK_HOME/state/vault-protection.sig`. Docs:
  `docs/CONFIG.md § Knowing which tier you are on`.
* **Already aligned:** fail-closed on an unreadable store (orca's "never
  downgrade ciphertext" ⇔ mini-ork's "fail loud, not empty"); a capability
  cannot be self-granted (orca's RPC test ⇔ mini-ork's grants come only from the
  compiled recipe).
* **Considered, not adopted:** a gitleaks/CI secret-scan gate on the repo (orca
  runs one in cloud CI) — orthogonal to the runtime; noted as a follow-up.

### Landed — the secure tier's deployable half (2026-10-10)

The OpenBao *client* existed but had never met a real server, and nothing shipped
the server-side half — the policies and scoped tokens, which is where least
privilege actually lives. Both closed:

* `docker/openbao/` — a one-command dev OpenBao (`docker-compose.yml`) plus
  `bootstrap.sh`: one KV-v2 path per node role, a read-only policy per role, an
  audit device, and a short-lived token per role written to a `0600` env file
  (never printed, so nothing lands in shell history). `README.md` covers the run
  and the production swap (storage + seal + TLS), which leaves every policy and
  the whole mini-ork side unchanged.
* `tests/integration/test_openbao_live.py` — proves the wire, not the parse: a
  seeded value returns over HTTP, and the collector token is *refused* at the
  reviewer path (least privilege enforced by the server). Skips when
  `OPENBAO_ADDR` is unset, so the default suite is unchanged.
* `docs/CONFIG.md § Backends` points at the kit; `docker/openbao/openbao.env`
  is gitignored.

## 5. Security properties this buys

* **Committable config.** No file that names a server or a skill contains a
  credential; the vault is one 0600 file outside any repo.
* **Node-scoped reach.** A node sees exactly the servers its recipe granted —
  enforced by `--strict-mcp-config` + a node-scoped config, not by prompt.
* **Leak-free siblings.** A key published for node A is actively cleared before
  node B, including on the serial path where both share one process.
* **Fail loud, not empty.** An unset `${NAME}` stops the run at the boundary;
  there is no code path that produces an empty credential.
* **No plaintext in Zed.** The token is resolved at process spawn, so
  `settings.json`, its backups, and Zed's own settings UI never hold it.
* **One rotation point.** The same `${NAME}` serves nodes and the editor.

## 6. Explicitly refused

* Writing a resolved secret into Zed's `settings.json` (or into
  `mcp_servers.json`) — including under a `--allow-plaintext` flag.
* `oauth` in a registry entry: mini-ork nodes run outside the editor, so there
  is no browser flow to complete; a remote server authenticating by OAuth is
  steered to Zed's native OAuth, and a node that needs it must carry a
  pre-obtained token.
* A Zed extension for any part of this.
* Reusing `mini_ork/remote/environments.py` — it is remote/Docker-only and
  raises `LookalikeSecretError` on `*_KEY`/`*_TOKEN`/`*_SECRET`/`*PASSWORD*`
  keys, i.e. it is the opposite of what this needs.
