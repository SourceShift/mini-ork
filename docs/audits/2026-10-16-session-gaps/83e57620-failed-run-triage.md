# Session gaps — 83e57620 (48h failed-run triage → C/D/E/F, plus the regression that fix caused)

Session: `83e57620-6d0b-41ec-9619-ff44c3c92318` (Claude Code, mini-ork primary checkout)
Reported: 2026-10-16. Repo head at audit time: `63fc80a0` on `origin/main`
(local `main` is one commit ahead at `beca73ef` — another session's audit doc, unpushed, not mine).

This session triaged every failed mini-ork run in the prior 48h, recommended four
follow-ups (C → E → F → D, skip B), then executed all of them at the user's
instruction ("do all based on rec"). Two of those fixes (D + E) introduced a
process-wide test-isolation regression on merge; that regression was then found,
isolated, fixed, and verified at full-suite scale. This report is the audit of
that work — every item below is receipted against git or a command that ran.

## (a) Task table

| id | description | recorded status | VERIFIED status | evidence |
|---|---|---|---|---|
| `C-frozen-path-floor` | Enforce the frozen/read-only probe-fixture declaration at **write** time, not just at review | completed | **DONE, verified** | `git merge-base --is-ancestor e0e99542 origin/main` → IN. `git show --stat e0e99542` → `config/kernel-frozen.yaml`, `mini_ork/cli/publisher_authored_patch.py` (+90), `mini_ork/vcs/repo_integrity_guard.py` (+48), `tests/unit/test_frozen_paths_enforced.py` (353 lines); **4 files, +500/−5** |
| `D-host-path-text` | Reproduce + fix `host path in 'text' survives translation to remote backend` | completed | **DONE, verified** | `59f14447` IN `origin/main`. `mini_ork/dispatch/core.py:424 def _sanitize_prompt_host_paths(text, path_map)`, called at `:562` for the applied stdin; `tests/unit/test_dispatch_text_sanitize.py` (194 lines) |
| `E-node-provider-lock` | Clear the stale `.node-provider.lock` after a dirty-engine death, and surface that early death in `task_runs.notes` | completed | **DONE, verified** | `59f14447` IN. `mini_ork/orchestration/run_reaper.py:252` documents the lock clear; `mini_ork/remote/providers/base.py` (+28); `tests/unit/test_run_reaper_notes.py` (224 lines) + `tests/unit/test_node_provider_lock_cleanup.py` (257 lines) |
| `F-RC_BLOCKED-keep` | Product call: should one failed run hard-block the whole queue? | decided | **DECIDED — keep** | `RC_BLOCKED = 75` unchanged at `mini_ork/cli/lane_preflight.py:49` and `mini_ork/cli/main.py:1031`; still returned at `execute.py:1226`. Decision: it parks one task, not the queue |
| `B-cosmetic` | Cosmetic-only item (lowest priority) | skipped | **intentionally NOT DONE** | my own recommendation; no code change |
| `path-map-wiring-pollution` | Fix the process-wide test-isolation regression that the **D+E merge itself introduced** | completed | **DONE, verified** | `778a0686` IN (`git merge-base --is-ancestor`). See §b G1 for the mechanism and the before/after counts |
| `full-suite-postfix-verify` | Confirm the fix at full-suite scale, not by probe | completed | **DONE, verified** | `python3.11 -m pytest -q tests/unit` → **2 failed, 9593 passed, 11 skipped** (3286s); both survivors (`test_acp_commands::test_serve_down_returns_command`, `test_node_agent::test_real_container_session`) already red at the pre-merge base |
| `triage-doc` | Document the 48h triage and the regression, in a form the user can read and act on | completed | **DONE, verified** | `2d786fec`, `24893d48`, `6801221c` all IN `origin/main`; `docs/todos/2026-10-14-failed-runs-48h-triage.md` (§§1–10) |
| `memory:registry-fixture-blind-pop` | Capture the fixture-teardown defect as durable feedback | completed | **DONE** | `feedback_registry_fixture_blind_pop_deletes_real_backend.md` (3495 B) + MEMORY.md pointer on line 16 |
| `fix-main-buddy-f841` | Blocking `F841` at `mini_ork/ide_pages/run.py` (`_wall()` computes `end`, returns nothing) | pending (reported by me, **not owned by me**) | **CLOSED by another session** | `python3.11 -m ruff check --select F,E9 .` → **All checks passed!** `_wall()` complete at `origin/main:mini_ork/ide_pages/run.py:831-835`; worktree `fix-main-buddy` is gone (cleaned) |

Commands actually run (verbatim, this audit):

```
git fetch origin main
git merge-base --is-ancestor <sha> origin/main   → IN, for e0e99542 59f14447 778a0686 24893d48 6801221c 2d786fec
git show --stat --oneline e0e99542               → 4 files changed, 500 insertions(+), 5 deletions(-)
git show --stat --oneline 59f14447               → 7 files changed, 1006 insertions(+), 19 deletions(-)
git show --stat --oneline 778a0686               → 1 file changed, 19 insertions(+), 3 deletions(-)
python3.11 -m ruff check --select F,E9 .         → All checks passed!   (blocking lint tier GREEN)
git show origin/main:mini_ork/ide_pages/run.py | sed -n '831,852p'   → _wall() returns f"{max(0, end - node.start)}s"
python3 -c "…select id,status from task_runs where id like '%1792092528%'…"
                                                 → ('run-1792092528-89023', 'published')
```

## (b) Missing features & gaps

**G1 — (now closed) The D+E merge shipped a process-wide test-isolation defect, caught only after merge.**
The `remote_recorder` fixture added by `59f14447` installed a double under the **real**
`remote` backend name and released it with a blind `pop` in teardown. Because
`mini_ork/runtime/backends/remote.py` registers at **import** time and
`get_workspace`'s lazy re-import is a no-op on an already-cached module, the real
factory was deleted for the life of the process and could not come back — every
later remote test in the suite died with a self-contradicting error
(`unknown workspace backend 'remote'; registered: docker, local, microvm, remote`).
Suite went 4 failures at base → 18 after the merge. Fixed in `778a0686`
(`monkeypatch.setitem`, restoring the prior value); post-fix full suite = 2
failed / 9593 passed, both survivors pre-existing. **Residual gap this exposes:**
the scoped green gate cannot catch cross-file pollution — the polluter's own file
passes. A broad or combined run is the only detector, and the merge gate does not
do one by default.

**G2 — the session's own triage doc now contains one stale claim.**
`docs/todos/2026-10-14-failed-runs-48h-triage.md:375-376` states that
`run-1792092528-89023`'s `task_runs` row "still reads `executing`". The DB now reads
**`published`** (`select id,status from task_runs` → `('run-1792092528-89023','published')`).
The wart self-healed after the doc was written. One-line correction, not yet made —
see §c.

**G3 — real defects this session found and deliberately did NOT fix (out of scope).**
- The `--select F,E9` red on `main` from another session's `9bd29ede` — **now resolved**
  (green above), so this is closed rather than outstanding.
- The load-sensitivity of the green gate on this machine (a clustered wall of
  failures under concurrent load that vanishes when re-measured at the true base).
  Documented in the triage doc; no mechanism change made.

**G4 — work parked, never authorized (not this session's to start).**
Peer `mini-ork-7c`'s learning-loop epic Waves 1–3; resume run `61320`; the aborted
run directory `run-1792047098-61635`; and the stale-board-state observation that is
now G2. All remain untouched; none is blocked on anything technical.

## (c) Blocked on

- **User decision — one optional one-line doc fix (G2).** Correcting the stale
  `executing` → `published` claim at
  `docs/todos/2026-10-14-failed-runs-48h-triage.md:375` costs a worktree + a
  docs-only merge cycle. No technical block; awaiting a "worth it / skip it".
- **Everything this session owns is shipped and pushed.** All six commits are
  ancestors of `origin/main`; no uncommitted work of mine is left in any worktree.
  All worktrees this session minted (`path-map-wiring-pollution`,
  `triage-doc-postfix`, `triage-doc-fullsuite`) and all scratch trees are removed.
- **Not blocked, but not mine to start:** G4 (peer-owned programme), the
  broad-suite-at-merge gate-strengthening implied by G1, and the held-out/resume
  runs — each needs the user's go, not a dependency cleared.
