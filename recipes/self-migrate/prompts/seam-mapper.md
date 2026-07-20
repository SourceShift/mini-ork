# Seam mapper — map ONE integration fork's full surface

You are mapping a single **integration fork** so it can be closed completely,
with no dangling edge left behind. The fork is named in the kickoff (e.g.
`verify` → the `bin/mini-ork-verify` ↔ `mini_ork/cli/verify.py`
seam).

## Produce `${MINI_ORK_RUN_DIR}/integration-map.json`

Reason about the seam — do not apply a fixed template. Compute, from the actual
repo:

1. **Outbound seams** — every place the Python entrypoint shells to bash:
   `_bash_lib_call(...)`, `subprocess.run([... "source lib/X.sh" ...])`,
   `bin/mini-ork-*` invocations. For each: the bash symbol, the args/env passed,
   any side-effect (db write, file, stdout the caller captures).
2. **Inbound refs** — every reference to the bash entrypoint `bin/mini-ork-<fork>`
   across `bin/`, `lib/`, `mini_ork/`, `scripts/`, `tests/`, and the web UI
   (`mini_ork/web/`). Classify each: test/parity-oracle, lib caller, script,
   sandbox, UI route.
3. **runtime-select coupling** — how `lib/runtime-select.sh` delegates this fork
   and what the bash-fallback path is.

```json
{
  "fork": "verify",
  "outbound_seams": [
    {"symbol": "lib/X.sh::fn", "args": "...", "side_effect": "db|file|stdout-captured|none", "port_exists": true, "port_native": true}
  ],
  "inbound_refs": [
    {"path": "tests/integration/test_bin_verify.sh", "kind": "test|parity-oracle|lib|script|sandbox|ui", "how": "invokes bin/mini-ork-verify"}
  ],
  "runtime_select": {"delegates": true, "fallback": "bin/mini-ork-verify"},
  "close_blockers": ["<what must be resolved before the bash entrypoint can retire>"]
}
```

## Rules
- **The `bin/` and `mini_ork/web/` scans are non-negotiable.** The failure mode
  this whole recipe exists to prevent is retiring an entrypoint while a lib,
  test, or UI route still references it.
- Use `codegraph_explore` / grep to ground every edge in a real file:line. Do
  not infer edges you cannot cite.
- A fork cannot be declared closeable while any `close_blocker` is unresolved.
