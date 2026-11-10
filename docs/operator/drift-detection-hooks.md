# Drift-detection hooks — operator setup

mini-ork ships a 3-layer git-hook system to prevent README claims drifting
out of sync with the live repo state. Layer 1 is mechanical (free,
sub-second, runs on every push). Layers 2a + 2b are LLM-based (small cost,
fires only on push-to-main with structural diff).

The whole pipeline is **local-first** — no GitHub Actions, no secrets
shipped to cloud CI, no third-party telemetry. The LLM calls dispatch
in-process through `mini_ork.dispatch.dispatch_model`, using the lanes in
`config/providers.yaml` and the keys in your local store: `$MINI_ORK_SECRETS`
if set, else `$MINI_ORK_HOME/config/secrets.local.sh` (`.mini-ork/…` by
default). The store is read, never `source`d, and fails closed — a symlink,
a group- or world-readable file, or a file owned by someone else is an error,
not an empty credential.

`make worktree-merge` pushes with `MINI_ORK_HOME` set to the **engine's**
home, not the worktree's: `.mini-ork/config/` is committed so every worktree
has one, while the store, the state DB and the approvals ledger do not. An
explicitly exported home still wins, and a hand-run `git push` from inside a
worktree needs it exported.

## One-time setup

```bash
make install-hooks
```

That command:
1. Sets `git config core.hooksPath .githooks` (project-local; doesn't
   touch other clones or the global git config).
2. Marks `.githooks/pre-push`, `scripts/readme_claim_check.py`,
   `scripts/readme_drift.py` and `scripts/mini_ork_worktree.py` executable.

Verify with `git config core.hooksPath` — should print `.githooks`.

To uninstall: `make uninstall-hooks` resets to the git default.

## What fires when

| Event | Layer 1 (mechanical) | Layer 2a (gatekeeper) | Layer 2b (panel) |
|---|---|---|---|
| `git push origin feature-branch` | ✓ runs | — | — |
| `git push origin main`, no README/structural changes | ✓ runs | — | — |
| `git push origin main`, README or `lib/`/`bin/`/`recipes/`/`db/migrations/` changed | ✓ runs | ✓ runs (~$0.005) | only if gatekeeper says PANEL_NEEDED (~$0.30) |

**Hard block** on Layer 1 drift, or on Layer 2b `DRIFT` / `INDETERMINATE`
(push to main). A `DRIFT` means a claim no longer holds; an `INDETERMINATE`
means nothing was measured, and a gate that cannot measure does not pass.

**Fail-open** is opt-in: `MO_README_PANEL_INDETERMINATE=fail-open git push`
proceeds through a provider outage with a warning. A dead gatekeeper is not
an outage of the audit — the hook runs the panel instead.

## Bypass options (use carefully)

```bash
# Skip all 3 layers (use when README state is intentionally stale)
MO_README_DRIFT_SKIP=1 git push

# Run L1 only, skip the LLM gatekeeper + panel
MO_README_PANEL_SKIP=1 git push

# Bypass all git hooks (most aggressive; works for any hook, not just this one)
git push --no-verify
```

## What each layer catches

### Layer 1 — mechanical (`scripts/readme_claim_check.py`)

Probes:
- `lib/*.sh` count claim in README matches `$(ls lib/*.sh | wc -l)`
- `bin/mini-ork-*` entrypoint count
- `db/migrations/*.sql` count
- Recipes table row count matches `recipes/<name>/` subdir count
- `lib/providers/cl_*.sh` count
- Regression-guard: `install.sh --check` phrase MUST NOT appear (this was
  closed in the 2026-06-05 audit at commit `d0aa8f4`; any reintroduction
  is treated as drift).
- Every backtick-quoted path under `recipes/ docs/ lib/ bin/ schemas/
  db/ examples/ kickoffs/` exists on disk.
- Every path cited in `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md`
  and `tests/README.md` exists on disk — those are instructions, and a
  contributor (or an agent) follows them literally. Tokens containing a glob
  or placeholder (`*`, `<`, `$`, …) are not claims and are skipped;
  `DOC_CLAIM_SKIP` holds the few paths that are legitimately absent from a
  fresh checkout (`.mini-ork/`, `config/secrets.local.sh`).

Tunable via env:
- `MO_README_DRIFT_TOLERANCE=0` (default; counts must match exactly)
- `MO_README_DRIFT_TOLERANCE=2` (allow ±2 drift before flagging)
- `MO_README_SKIP_MIGRATIONS=1` (suppress the migration-count probe)

Run ad-hoc:
```bash
python3 scripts/readme_claim_check.py
python3 scripts/readme_claim_check.py --verbose
python3 scripts/readme_claim_check.py --json
```

### Layer 2a — gatekeeper (`scripts/readme_drift.py gatekeeper`)

A single MiniMax-M3 call (~$0.005, ~5-10 sec) that decides whether the
expensive 4-lens panel should fire at all. Most pushes touch typos /
formatting / link updates that don't move a claim's truth value — the
gatekeeper waves those through without paying for the panel.

The gatekeeper reads:
- `git diff --name-only origin/main..HEAD` (what changed)
- README + ROADMAP diff stats
- Count of structural files changed

Then asks MiniMax-M3 to answer:
- **PANEL_NEEDED** if the diff plausibly invalidates a load-bearing
  claim.
- **PANEL_SKIP** if the diff is trivial.

Fails **measured**: if MiniMax-M3 is unreachable the router has not decided
the audit is unnecessary, so the hook reads the missing verdict as
`PANEL_NEEDED` and runs the panel. An outage costs ~$0.30, not a push with
the README unmeasured. (The gatekeeper's own JSON on stdout is still
authoritative when it does answer.)

Run ad-hoc:
```bash
python3 scripts/readme_drift.py gatekeeper | jq .
```

### Layer 2b — 4-lens panel (`scripts/readme_drift.py panel`)

Heterogeneous-family panel (Rajan 2025 submodularity precondition met by
construction — see [docs/positioning/why-mini-ork.md](../positioning/why-mini-ork.md)):

- **codex_lens** (OpenAI Codex): technical accuracy claims — numbers,
  paths, schema enums
- **kimi_lens** (Moonshot): narrative consistency — does the architecture
  diagram match the recipe table, does v0.3 status match the Roadmap
  pointer
- **minimax_lens** (MiniMax): comparison-table fairness — is the vs-Claude-Code
  / vs-OpenAI-Agents / vs-LangGraph table still fair given current
  external tools
- **glm_lens** (Zhipu): citation accuracy — do the 6 arXiv papers still
  support the claims they're attached to

The 4 lens calls fan out in parallel (each times out at 90s). After all 4
complete, an **opus arbiter** synthesizes the verdicts into one final
`NO_DRIFT` or `DRIFT` with deduplicated drifted claims + suggested fix
paragraphs.

Cost: ~$0.20-0.30 per panel run. Wall time: ~30-60 sec.

The full report (per-lens JSON + arbiter synthesis + markdown summary)
lands at `/tmp/readme-drift-<timestamp>-<pid>/report.md` for operator
review.

Run ad-hoc (any time, not just from the hook):
```bash
python3 scripts/readme_drift.py panel | jq .
python3 scripts/readme_drift.py doctor | jq .   # pre-flight liveness probe
```

## Cost budgeting

| Push pattern | Daily LLM cost (5-20 pushes) |
|---|---|
| All to feature branches | $0 (Layer 1 only) |
| Mix: 5 to main, 15 to feature | $0.05 / day (gatekeeper × 5; panel almost never fires) |
| Heavy on main: 20 to main, all touch lib/ or README | $1-3 / day (panel fires ~20-30% of the time after gatekeeper filter) |

The gatekeeper is the load-bearing cost-control. If you find the panel
firing too often on benign diffs, tune the gatekeeper prompt in
`scripts/readme_drift.py` (`gatekeeper()`; the prompt is inline + versioned).

## When Layer 1 disagrees with Layer 2b

This is a feature, not a bug. Layer 1 catches numerical drift; Layer 2b
catches qualitative drift. A push where:

- L1 says clean (numbers match), L2b says DRIFT (a claim's gloss became
  misleading) → fix the prose, re-push.
- L1 says drift (a count's off), L2b not consulted (gatekeeper triages
  but L1 blocks first) → fix the count.
- Both clean → push proceeds.

## Failure modes you should know

1. **MiniMax provider outage**: the gatekeeper cannot answer, so it does not
   decide the audit is unnecessary — the hook runs the 4-lens panel instead.
   A router outage costs a panel run (~$0.30), never a push with the README
   unmeasured. The gatekeeper's own error path (return 2) only skips the
   panel when the hook reads `PANEL_SKIP` from its JSON on stdout.

2. **Opus provider outage during panel arbitration**: the arbiter output
   is unparseable, so the panel reports
   `{"verdict": "INDETERMINATE", "notes": "arbiter output unparseable — …"}`,
   and a separate honesty net forces INDETERMINATE whenever every lens
   failed — absence of evidence is never reported as evidence of absence.
   The hook treats a panel that printed no verdict at all the same way.
   INDETERMINATE **blocks** a push to main by default; set
   `MO_README_PANEL_INDETERMINATE=fail-open` to push with the README
   unmeasured, or use one of the bypasses above. Lens outputs are preserved
   in the run dir for manual review.

3. **The panel never runs (providers unresolvable)**: the providers-doctor
   pre-flight reports fewer responsive lenses than its threshold, which is
   also INDETERMINATE — blocked by default for the same reason, with the
   same `MO_README_PANEL_INDETERMINATE=fail-open` opt-out.

4. **False-positive panel verdict on a clean README**: tune the lens
   prompts in `scripts/readme_drift.py` (`LENS_PROMPTS`; inline +
   versioned, so prompt edits are reviewable in commit history).

5. **Pre-push hook silent disable**: someone runs `git config
   --unset core.hooksPath` to bypass without telling anyone. The
   `make install-hooks` target is idempotent — run it again to restore.
   Consider adding a daily check (`make readme-claim-check` in a cron)
   for belt-and-suspenders.

## Future work

- `make release-tag-audit` — a manual L3 gate run before cutting a
  version tag, producing a permanent audit doc at
  `docs/audits/YYYYMMDD-readme-claims-audit.md`.
- Prompt-versioning + A/B-testing the gatekeeper prompt to minimize
  false-positive panel triggers.
- Push-hook installation auto-check on `git pull` (warn if hooks-path
  isn't set).
