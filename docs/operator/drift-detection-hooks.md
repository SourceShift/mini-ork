# Drift-detection hooks — operator setup

mini-ork ships a 3-layer git-hook system to prevent README claims drifting
out of sync with the live repo state. Layer 1 is mechanical (free,
sub-second, runs on every push). Layers 2a + 2b are LLM-based (small cost,
fires only on push-to-main with structural diff).

The whole pipeline is **local-first** — no GitHub Actions, no secrets
shipped to cloud CI, no third-party telemetry. The LLM panel dispatches
through your existing `lib/providers/cl_*.sh` wrappers using your local
API keys from `.mini-ork/config/secrets.local.sh` (or
`~/.config/mini-ork/secrets.local.sh`).

## One-time setup

```bash
make install-hooks
```

That command:
1. Sets `git config core.hooksPath .githooks` (project-local; doesn't
   touch other clones or the global git config).
2. Marks `.githooks/pre-push` and `scripts/readme-*.sh` executable.

Verify with `git config core.hooksPath` — should print `.githooks`.

To uninstall: `make uninstall-hooks` resets to the git default.

## What fires when

| Event | Layer 1 (mechanical) | Layer 2a (gatekeeper) | Layer 2b (panel) |
|---|---|---|---|
| `git push origin feature-branch` | ✓ runs | — | — |
| `git push origin main`, no README/structural changes | ✓ runs | — | — |
| `git push origin main`, README or `lib/`/`bin/`/`recipes/`/`db/migrations/` changed | ✓ runs | ✓ runs (~$0.005) | only if gatekeeper says PANEL_NEEDED (~$0.30) |

**Hard block** on Layer 1 drift OR Layer 2b DRIFT verdict (push to main).
**Fail-open** on any LLM provider outage (push proceeds, operator sees a
warning).

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

### Layer 1 — mechanical (`scripts/readme-claim-check.sh`)

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

Tunable via env:
- `MO_README_DRIFT_TOLERANCE=0` (default; counts must match exactly)
- `MO_README_DRIFT_TOLERANCE=2` (allow ±2 drift before flagging)
- `MO_README_SKIP_MIGRATIONS=1` (suppress the migration-count probe)

Run ad-hoc:
```bash
bash scripts/readme-claim-check.sh
bash scripts/readme-claim-check.sh --verbose
bash scripts/readme-claim-check.sh --json
```

### Layer 2a — gatekeeper (`scripts/readme-drift-gatekeeper.sh`)

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

Fails open: if MiniMax-M3 is unreachable, treats as `PANEL_SKIP` so a
provider outage doesn't block your push.

Run ad-hoc:
```bash
bash scripts/readme-drift-gatekeeper.sh | jq .
```

### Layer 2b — 4-lens panel (`scripts/readme-drift-panel.sh`)

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
bash scripts/readme-drift-panel.sh | jq .
```

## Cost budgeting

| Push pattern | Daily LLM cost (5-20 pushes) |
|---|---|
| All to feature branches | $0 (Layer 1 only) |
| Mix: 5 to main, 15 to feature | $0.05 / day (gatekeeper × 5; panel almost never fires) |
| Heavy on main: 20 to main, all touch lib/ or README | $1-3 / day (panel fires ~20-30% of the time after gatekeeper filter) |

The gatekeeper is the load-bearing cost-control. If you find the panel
firing too often on benign diffs, tune the gatekeeper prompt at
`scripts/readme-drift-gatekeeper.sh` (the system prompt is inline +
versioned).

## When Layer 1 disagrees with Layer 2b

This is a feature, not a bug. Layer 1 catches numerical drift; Layer 2b
catches qualitative drift. A push where:

- L1 says clean (numbers match), L2b says DRIFT (a claim's gloss became
  misleading) → fix the prose, re-push.
- L1 says drift (a count's off), L2b not consulted (gatekeeper triages
  but L1 blocks first) → fix the count.
- Both clean → push proceeds.

## Failure modes you should know

1. **MiniMax provider outage**: gatekeeper fails open, push proceeds
   without the panel check. If you want a hard block on outage instead,
   patch `scripts/readme-drift-gatekeeper.sh` exit code 2 → exit 1.

2. **Opus provider outage during panel arbitration**: arbiter emits
   `{"verdict": "NO_DRIFT", "notes": "arbiter output unparseable — fail-open"}`.
   The push proceeds. Lens outputs are preserved in the run dir for
   manual review.

3. **False-positive panel verdict on a clean README**: tune the lens
   prompts in `scripts/readme-drift-panel.sh` (the prompts are inline +
   versioned, so prompt edits are reviewable in commit history).

4. **Pre-push hook silent disable**: someone runs `git config
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
