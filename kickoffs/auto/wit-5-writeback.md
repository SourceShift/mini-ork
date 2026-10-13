# Ticket write-back, capability-gated

## Goal

Let mini-ork comment on a source ticket and move it between states — Linear and Jira first —
with every write passing one gate that refuses unless the adapter can do it **and** the operator
opted that source in with `write_back: true`.

**Deliverable.** A new `mini_ork/pm/writeback.py` gate, `comment` + `transition` on the Linear
and Jira adapters, the `write_back` setting documented in `config/pm.yaml`, and
`tests/unit/test_pm_writeback.py`.

**Scope:**
- **The gate — `mini_ork/pm/writeback.py`.** `comment(uid, body)`, `transition(uid, state)` and
  `update(uid, patch)` resolve the source from the uid via the registry, then check, in order:
  the source's `config/pm.yaml` stanza has `write_back: true` (read from `SourceConfig.settings`,
  so `mini_ork/pm/config.py` needs no schema change unless a helper reads cleaner); the adapter's
  `Capabilities` flag for that operation is true. Either check failing raises `Unsupported`
  naming the source, the operation and which check failed — and **no adapter method is called**.
  Do not use `hasattr`: a method can exist and still be a no-op. `update` takes a `Patch`
  (already in `mini_ork/pm/provider.py`), never `**kwargs`, so `description=""` (clear it) stays
  distinguishable from `description=None` (leave it).
- **Linear — `mini_ork/pm/providers/linear.py`.** `comment` → the `commentCreate` mutation;
  `transition` → look up the team's workflow states once, pick the first whose type maps to the
  requested `IssueState`, then `issueUpdate(stateId)`. No matching state raises `Unsupported`.
  Flip `capabilities.comment`/`transition` to true. Same stdlib `pm/http.py` transport, same
  credential scrubbing as the reads.
- **Jira — `mini_ork/pm/providers/jira.py`.** `comment` → `POST /issue/{key}/comment` with the
  body converted to a minimal ADF document (paragraphs; keep the converter beside
  `adf_to_markdown`); `transition` → `GET /issue/{key}/transitions`, pick the first whose target
  status category maps to the requested state, `POST` it. No matching transition raises
  `Unsupported`. Keys go through the existing `_locate` validation. Flip the capabilities.
- **Unchanged on purpose.** `github.py` stays read-only (it is the "incapable adapter" case);
  `local.py` keeps `transition=False` because `trg_epics_no_revert_done` refuses a done epic going
  back — a board that let you drag a card out of Done would be lying.
- **`config/pm.yaml`.** Document `write_back: false` on the linear and jira stanzas: it needs a
  token with write scope, and it is off by default.
- **Two tiers** (CLAUDE.md): the same flag on both; the secure tier differs only in where the
  write-scoped token lives (sops/OpenBao vs `secrets.local.sh`). A missing token fails closed
  exactly as the reads do.

**Out of scope.** Any caller (intake or a post-verdict hook posting run results), and any IDE
control. This slice is the gated API those will call.

**Acceptance.** Offline, with `pm.http.request_json` monkeypatched:
`write_back` unset → `Unsupported` for every source and operation, zero HTTP calls; set on a
Linear and a Jira source → one comment and one transition request each, with the expected
mutation / endpoint and body; set on the GitHub source → `Unsupported` (capability), zero calls;
`Patch(description="")` and `Patch()` produce different requests; a 401 on a write does not leak
the token; the existing Linear and Jira read tests stay green.

- max attempts: 2

---

## Scope Hint

- `mini_ork/pm/writeback.py`
- `mini_ork/pm/providers/linear.py`
- `mini_ork/pm/providers/jira.py`
- `config/pm.yaml`
- `tests/unit/test_pm_writeback.py`
- `mini_ork/pm/provider.py` — read-only reference: `Capabilities`, `Patch`, `Unsupported`
- `mini_ork/pm/registry.py` — read-only reference: uid → provider
- `mini_ork/pm/providers/github.py` — read-only: stays incapable

## Verification commands
- `python3.11 -c "import mini_ork.pm.writeback"`
- `python3.11 -m pytest -q tests/unit/test_pm_writeback.py tests/unit/test_pm_linear.py tests/unit/test_pm_jira.py tests/unit/test_pm_github.py`

Run these from the run's working tree (the isolated worktree), so `mini_ork` resolves to the tree under test. A syntax check is not acceptance.

## Done When

- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"` with all verifiers passing.
- All verification commands pass in the isolated worktree.
