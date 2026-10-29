# Synthesis: refactor-audit of `wt/t1-kernel-hole-3` @ `e178d24e`

**Diff:** "feat(kernel): freeze the self-driving policy's inputs". It adds 8 paths to the frozen kernel in `config/kernel-frozen.yaml:43-50` and `mini_ork/vcs/repo_integrity_guard.py:72-80`, edits one count pin and adds one test file (4 files, +198/−13).

**Inputs:** the five anonymous panel responses A–E (`panel_reports/panel-responses.md`). Every `path:line` is relative to `/Volumes/docker-ssd/ps/mini-ork-worktrees/review-t1-kernel-hole-3`.

## Consolidated merge verdict: **ADJUST**

The panel split 3 to 2: B, C and D said ADJUST; A and E said APPROVE.

Nothing in the diff makes `main` less safe. Every entry is a pure restriction, it fails closed, and the tests pass (46/46). ADJUST is about whether the commit's **claim** is true, not about safety.

- **The headline claim is false.** "Freeze the policy's inputs" leaves out `mini_ork/triage/failures.py`. That module is imported directly by the frozen gatherer (`mini_ork/recovery/escalation.py:51`), computes one of its fields (`:285`), and writes the rows the daily cap counts.
- **The new comment is false on one branch.** It says "a consumer that cannot read the yaml still enforces these" (`repo_integrity_guard.py:47-50`), but this is untrue on the framework-edit verifier's fallback branch.

Both fixes take a day or less and touch files the commit already edits or their frozen sibling (Top-5 #1 and #2). Everything else is follow-up work.

### What the synthesizer verified itself (read-only, inside the diff and its direct callers)

- **Tests: 46/46 pass on Python 3.12** (`/usr/local/bin/python3.12`), whether or not the lane's `MO_*`/`MINI_ORK_*` environment is set. This matches A's receipt.
  - On this lane's default `python3` (micromamba **3.9.23**) the same three files give **15 failed / 31 passed**. The subprocess tests that drive `static-check.py` die before writing their `.checks.tsv`.
  - That failure comes from the interpreter, not the code (CI runs 3.11 and 3.12), but it is a reproduction trap (§6).
  - The worktree was clean before and after; tests ran with `-p no:cacheprovider` and `PYTHONDONTWRITEBYTECODE=1`.
- **Claims checked against code.** ✔ = confirmed, ~ = partly confirmed, ✖ = refuted:

| Claim | Result |
|---|---|
| K1: the static-check fallback reads neither YAML | ✔ |
| K2: `triage/failures.py` feeds the policy and is unfrozen | ✔ |
| K3: `scheduler.py` writes `epics.status` | ✔ |
| K4: `apply.py` is neither frozen nor high-tier | ✔ |
| K5: brittle count and order pins | ✔ |
| A-2: negative control uses `retry_hint.py` | ✔ |
| B-1: replan dispatch has no `persist_call` | ✔ |
| B-3: no negative cache for failed diagnoses | ✔ |
| B-4: no `cost_pause` in `diagnoser_llm.py`, `graph_patch.py` or `dispatch/providers.py` | ✔ |
| C-5: a branch can loosen its own merge tier | ✖ (bootstrap only) |
| C-8: `select.py` `frozen_paths=()` default | ✔ |
| D-4: L11 harvest names a now-frozen file | ✔ |
| D-5: `review_policy.py` neither frozen nor high-tier | ✔ |
| D-6: `rsi/worktree.py:289` drops `run_id` | ✔ |
| D-7: token consumed only at `publisher_authored_patch.py:1504` | ✔ |
| D-8: labeller query shape at `supervisor.py:274-281` | ~ (query shape confirmed; the indefinitely-unlabelled consequence was not traced) |
| D-13: stale "FORWARD DECLARATION" at `config/kernel-frozen.yaml:22` | ✔ |
| Frozen surface added by the diff | ✔ 7,274 lines across the 8 files |

**ID convention:**
- `A-n`: A's F-n. `A-Rn`: A's recommendations. `A-§7`: A's open question.
- `B-n`: B's finding-n. `B-Xn`: B's architectural item n.
- `C-n`: C's table row n.
- `D-n`: D's F-n.
- `E-n`: E's refactor-n. E's own merge table maps F1→E-1, F2→E-2, F3→E-3, F4→E-7, F5→E-6, F6→E-4.
- `S-1`: a finding of the synthesizer's own.
- `Kn`: a cross-response cluster.

**[f:…] check:** the context block supplies no `[f:…]` ids, so no failure-id repeat can be named. The relevant `[t:…]` lessons are covered in §6.

### Contradictions adjudicated

| Dispute | Claims | What the code says | Ruling |
|---|---|---|---|
| Does the verifier's fallback still read the target's committed YAML? | **B-11:** "not a hole: the union with the target's HEAD yaml covers them". **E-1:** reachable only if import fails *and* HEAD lacks the file. **A-5 / C-3:** neither YAML is read. | The fallback's `parse_frozen_paths` returns `[]` (`recipes/framework-edit/verifiers/static-check.py:74-75`). So `_target_committed_frozen_paths` (`:617-629`) also returns `[]`, and `load_frozen_paths` (`:77-78`) returns the stale 14 entries. | **A and C are right on the mechanics; B and E are wrong.** Reachability is low (A-5): the fallback runs only if importing `repo_integrity_guard` raises, and that module imports only `fnmatch`/`os`/`subprocess` (`repo_integrity_guard.py:37-39`). C ("high") and D ("HIGH") overrate it. It stays P1 because all five responses flag it, it costs 0.5 day, and it makes the diff's own comment false. |
| Can a branch loosen its own merge tier? | **C-5:** yes, when `origin/main` is unavailable. **D:** no. | `load_rules` (`mini_ork/gates/merge_risk.py:150-174`) reads `origin/main` first. If `origin/main` is missing, `changed_files` (`:181-191`, which diffs `origin/main...HEAD`) raises, and the gate fails closed (`:411-412`). | **D is right.** C-5 can only happen while `origin/main` exists but lacks `config/risk-tiers.yaml` (bootstrap), so it drops to P3. Residual: precedence slot 1, `MO_RISK_TIERS_FILE`, is an ambient-environment override with no guard. |
| Is the freeze too broad or too narrow? | **Too broad:** A-4, C-12, C-13 (7,274 lines). **Too narrow:** B-6, C-1/2/4/6/7, D-2/3/5 (freeze more). | Both are true. | **Whole-file freezing cannot satisfy both.** Near term: freeze only the small, direct input (`failures.py`, 499 lines), and put the large enforcement and writer modules in the high risk tier. Long term: kernel extraction (A-R4) plus a computed closure (A-R1 / E-2). See §3, runtime bundle. |
| APPROVE or ADJUST? | A and E: APPROVE. B, C and D: ADJUST, but each asks for a different set of fixes. | — | **ADJUST, limited to the two consensus items** (Top-5 #1 and #2). B's required item, freezing `config/providers.yaml`, `dispatch/providers.py` and `dispatch/telemetry.py`, is a 2-of-5 finding that would deepen the over-breadth problem (K8). It moves to P2 as K14. |

**Which findings the diff caused.** K1 (the diff updated 2 of 3 copies), K5, A-2, K9 and D-4 (L11 dies because the diff froze `diagnosis_ledger.py`) come from this diff. So does K8, as a choice the diff made. D-6 existed before but runs more often now, because the trigger set grew from 14 to 22. **Everything else already exists on `main`.**

---

## Section 1: Severity × leverage matrix

★ = the finding appears in 2 or more responses.

| | **HIGH leverage** | **MED leverage** | **LOW leverage** |
|---|---|---|---|
| **P1** | **K1★** A-5 · B-11 · C-3 · D-1 · E-1: the verifier's fallback floor is stale at 14 entries and reads neither YAML ✔<br>**K2★** A-1b · C-1 · D-3 · E-2: `triage/failures.py` feeds `signature_fix_failed_before` and writes the cap's rows, and is unfrozen ✔<br>**K4★** A-3 · D-2 · E-2: `cli/apply.py` (the apply-loop guard) is neither frozen nor high-tier ✔ | **K3★** A-1c · C-2 · E-2: `scheduler.py` writes the `epics.status` the breaker reads ✔<br>B-1: the replan LLM dispatch is never metered ✔<br>B-3: a failed model diagnosis is never cached, so it is re-paid every sweep ✔<br>B-4: neither frozen LLM call site consults `cost_pause` ✔<br>**K6★** A-7 · C-9 · D-9 · E-7: the tests prove 1 of 5 enforcement points | **K5★** A-6 · C-16 · C-17 · D-9 · E-7: `len == 22` and `[-8:] == NEW` pins ✔<br>**K9★** C-11 · C-13 · D-13 (+ D-7): the YAML comment overclaims (stale forward declaration; effect listed as an input; token scope) ✔<br>A-2: the negative control pins the known input `retry_hint.py` as unfrozen ✔<br>D-4: the L11 harvest candidate can only ever be rejected ✔ |
| **P2** | **K2'★** A-R1 · E-2: a computed import-closure test<br>**K8★** A-4 · A-R4 · C-12 · C-13: whole-file over-breadth (+7,274 frozen lines); extract small kernels instead<br>**K7★** A-R5 · B-11 · C-18 · E-1 · E-9: five copies of one list; make one source<br>**K14★** A-§7 · B-6: `config/providers.yaml` and the dispatch layer steer the policy without touching a frozen path<br>**K11★** C-6 · C-7 · D-5 · D-12: the approval chain is not publish-frozen, and `review_policy.py` is not even high-tier ✔<br>B-9: the escalation policy has a count cap but no dollar ceiling | **K10★** C-10 · D-11 · E-6: the probe-fixtures glob is only in the YAML, not the floor ✔<br>**K12★** D-10 · D-14 · E-3: the merge gate reads the list from the engine's working tree, with two roots inside one cycle ✔<br>C-8: `select.py:166` defaults to `frozen_paths=()`, which fails open ✔<br>D-6: the RSI land drops `run_id`, so safety events are orphaned and not deduped ✔<br>D-8: the revive-path labeller skips NULL `fix_epic_id` rows ~<br>C-4: `db/migrations/**` is merge-high but not publish-frozen<br>A-R8: record which writer produced each policy-input row<br>B-2 · B-5 · B-12: gateway rate table; `max_turns=3`; no tier ladder | B-7 (evidence-pack hash)<br>B-10 (no diagnoser lane fallback)<br>B-13 (provider pre-filter)<br>B-14 (replan cache keyed by file number)<br>C-14 (missing `bug_reports` / `epics` indexes)<br>C-15 (4–5 sqlite connections per escalate) |
| **P3** | A-R7: server-side enforcement bound to a signed kernel manifest (secure tier)<br>A-§7: a decision-replay corpus (outcome-based protection) | A-R9 · A-R10: frozen-lines ratio metric; per-file refusal panel<br>A-8: `docs/SAFETY.md` is silent on the frozen kernel<br>B-X3 · B-8: batch fleet diagnosis; request prompt caching<br>S-1: two stray open safety events in the live `state.db` | **K13★** C-19 · C-20 · E-4 · E-5: YAML parse cache; literal-set matcher<br>E-8 · E-10 · B-15 · D-15<br>E-6 (inline `paths: [...]` parse)<br>C-5 ✖ (reachable only at bootstrap) |

**Consensus ranking** (how many responses raise each cluster):

| Responses | Clusters |
|---|---|
| 5 / 5 | K1 |
| 4 / 5 | K2, K5, K6, K7 |
| 3 / 5 | K3, K4, K10 |
| 2 / 5 | K8, K9, K11, K12, K13, K14 |

The panel agrees most on the single-source and closure problems. It disagrees only on how severe they are, and on whether the fix is "freeze more" or "freeze smarter".

---

## Section 2: Top 5 immediate wins (P1)

Ranked by ROI: severity × leverage ÷ effort.

| # | ID | Title | Responses | One-line fix | Effort |
|---|---|---|---|---|---|
| 1 | **K1★** | Verifier fallback enforces 14 of 22 entries and reads neither YAML | A-5, B-11, C-3, D-1, E-1 | Sync the inline floor at `recipes/framework-edit/verifiers/static-check.py:57-72`, give the fallback a real block-list parser at `:74-75`, and add a fallback-equals-floor sync test | 0.5 d |
| 2 | **K2★ + K5★ + K9★** | Close the direct-import hole and make the YAML honest | C-1, D-3, E-2, A-1b; A-6, C-16/17, D-9, E-7; C-11, C-13, D-13, D-7 | Add `mini_ork/triage/failures.py` to `config/kernel-frozen.yaml:43-50` and `repo_integrity_guard.py:72-80`, switch the pins to set membership, and correct the YAML comment | 1 d |
| 3 | **K4★** | The apply loop's frozen guard is editable and merges at low tier | A-3, D-2, E-2 | Add `mini_ork/cli/apply.py` to the `high:` list in `config/risk-tiers.yaml` (after `:30`) | 0.5 d |
| 4 | **B-1 + B-4** | The two frozen LLM sites are unmetered (replan) and ungated (both) | B | `persist_call` after `mini_ork/recovery/graph_patch.py:703-710`, plus a `cost_pause` check before both dispatches | 1.5 d |
| 5 | **B-3** | A failed model diagnosis is re-dispatched on every sweep | B (B-7 is the companion) | Negative cache keyed on the evidence-pack sha256 at `mini_ork/recovery/diagnose.py:1084-1093` | 2 d |

**Total: about 5.5 engineering days**, well under the 2-week budget. The ones just below the cut are listed after #5.

**#1 K1: verifier fallback.** Responses agree, 5 of 5.
- **Fix:** add the 8 entries to the inline `FROZEN_PATHS_DEFAULT` at `static-check.py:57-72`. Then replace the `return []` stub at `:74-75` with the 15-line block-list parser from `repo_integrity_guard.py:99-118`, so `_target_committed_frozen_paths` (`:617-629`) actually reads the target's HEAD YAML on the degraded path.
- **Test:** add E-1's `test_static_check_fallback_equals_floor`, which is the test that would have caught this.
- `static-check.py` is itself frozen, so this needs a human kickoff with `MO_ALLOW_FROZEN_EDIT=1` and a person's approval at merge (§7).

**#2 K2 + K5 + K9: one commit, same surface.**
- **Freeze `failures.py`.** Add `mini_ork/triage/failures.py` (499 lines) to both lists. `escalation.py:51` imports it, `:285` feeds `diagnosis_fingerprint` into `signature_fix_failed_before`, and `failures.py:449` stamps the `agent_role="self-driving"` rows that `escalation.py:153-158` counts for the daily cap.
- **Fix the pins first.** Adding a 23rd entry breaks `tests/unit/test_frozen_kernel.py:372` (`len(got) == 22`) and `tests/unit/test_frozen_kernel_policy_inputs.py:77` (`[-8:] == NEW`). Replace them with `set(NEW) <= set(FROZEN_PATHS_DEFAULT)` (A-R3). K5 is a prerequisite of K2, not a nicety.
- **Fix the comment in the same commit:**
  - `config/kernel-frozen.yaml:22` still calls `escalation_policy.py` a "FORWARD DECLARATION", but it exists and `escalation.py:45-50` imports it.
  - `:36-42` lists `graph_patch` (an effect) and two enforcement points as "INPUTS".
  - The header should say the operator token works **only at publish** (`publisher_authored_patch.py:1504` is the only consumer of `frozen_edit_allowed`).

**#3 K4: put the apply loop in the high risk tier.** Do not freeze it as a whole file.
- `apply.py` is 1,883 lines and actively edited. Freezing it whole would deepen K8 and pull it out of RSI.
- With high tier, a self-driving edit that removes `_frozen_target` (`mini_ork/cli/apply.py:740`, called at `:1138`) needs a person's approval at merge. That closes the unattended path A-3 describes: the apply loop writes the engine tree without going through `land_patch`.
- Moving `_frozen_target` and `_frozen_by_inode` (`:740-794`) into the frozen `repo_integrity_guard` is P2 (K8).

**#4 B-1 + B-4: meter and gate the two frozen LLM sites.**
- **Meter the replan.** `graph_patch.py` has no `persist_call` (confirmed by grep), so replan spend never reaches the ledger that `cost_pause` and the daily budget read. Copy `diagnoser_llm.py:479-489`.
- **Gate both sites.** Neither `diagnoser_llm.py:525` nor `graph_patch.py:703` consults `cost_pause`, and `dispatch/providers.py` does not either.
- **Unchecked:** B's `cost_pause.check(run_id, 0.0)` behaviour was not read; confirm it against `mini_ork/dispatch/cost_pause.py:59-87` before relying on it.

**#5 B-3: negative-cache failed diagnoses.**
- **The gap:** `_cached_model_diagnosis` (`diagnose.py:1043-1054`) reuses a stored diagnosis only when `source == "model"`. Every `None` return from `diagnose_with_model` (`:1086-1091`) falls through to a rule result, so the next sweep pays again. The "asked at most once per run" docstring at `auto_repair.py:489-490` is true only when the model succeeds.
- **Keep it fail-closed:** the marker records "asked, declined" and never stands in for a diagnosis. That is B's own "what's already right" guard.

**Just below the cut** (all P1):

| ID | Fix | Effort |
|---|---|---|
| K6 | Parametrize `tests/unit/test_frozen_kernel_policy_inputs.py:120-157` over all 8 entries, and add verifier and `merge_risk` tier tests (E-7) | 1.5 d |
| K3 | Put `mini_ork/scheduler.py` in the high tier as a stopgap (data-layer bundle) | 0.25 d |
| D-4 | Make `_l11_diagnosis_unlabelled` raise `_GapClosed` when its only file is frozen (`mini_ork/rsi/harvest.py:606-630`) | 0.5 d |
| A-2 | Point the negative control at a module provably off the policy path (`test_frozen_kernel_policy_inputs.py:94-97`) | 0.1 d |

---

## Section 3: Next-version architectural shifts (P2)

### Bundle R: Runtime and kernel enforcement (≈3 eng-wks)

Goal: make the frozen kernel computed, single-sourced and small.

- **K2' (A-R1 / E-2): computed closure test.**
  - Add `tests/unit/test_frozen_kernel_closure.py`. It walks imports with `ast` from `escalation_policy.py`, `escalation.gather_inputs` (`escalation.py:250-287`) and `diagnose.diagnose`.
  - It must follow lazy imports; the diagnoser is lazily imported at `diagnose.py:1086`.
  - Every module it reaches must be frozen or sit in a `POLICY_INPUT_ALLOWLIST` with a one-line justification.
  - This turns K2/K3-style drift into a failing build.
- **K8 (A-R4): extract small kernels, then unfreeze the large modules.**
  - Move the R2 labeller out of `supervisor.py:813,843` into `diagnosis_ledger.py`, which is already frozen.
  - Move the publish-refusal block at `publisher_authored_patch.py:1503-1518` into `repo_integrity_guard.refuse_frozen`.
  - Move `apply.py:740-794` into the guard.
  - Then unfreeze `supervisor.py` and `publisher_authored_patch.py`, and drop `graph_patch.py` (C-13 / D-13) or re-justify it as an effect.
- **K7 (E-1, E-9, A-R5): one source for the list.** Create a dependency-free data module, `mini_ork/vcs/_frozen_floor.py`, and import it from the guard, the verifier fallback and the tests. Add a `mini-ork kernel frozen` command that prints the effective list.
- **K10, E-6:** move `recipes/code-fix/probes/fixtures/**` (`config/kernel-frozen.yaml:55`) into the floor, and make `_minimal_parse` (`repo_integrity_guard.py:99-118`) handle the inline list form.
- **K12 (E-3 + D-14):** at merge, use the committed `origin/main:config/kernel-frozen.yaml`, the branch's committed additions and the floor. Drop the engine working tree as a source (`merge_risk.py:337`).
- **C-8:** `select.py:166` should default to the floor, or raise on an empty `frozen_paths`. Right now the type's default is the value that fails open.
- **K11:** add `mini_ork/gates/review_policy.py` to `config/risk-tiers.yaml` at minimum. It decides whether an approval must be signed (`merge_approval.py:508-511`), and today it merges at low tier. `merge_approval.py` and `gaming_scan.py` are already high-tier (`config/risk-tiers.yaml:28,30`); freezing them is optional.

**Prerequisite P1s:** #1 and #2.

**Risk if deferred:** every new import from a frozen module into an editable helper silently reopens the hole (A §1, "closure drift"). The frozen surface roughly doubled (+7,274 lines), squeezing RSI's candidate pool further each time.

### Bundle D: Data layer (≈2–2.5 eng-wks)

Goal: the policy reads database rows, so either freeze their writers or record which writer produced each row.

- **K3, the permanent fix.** Do not freeze all 1,239 lines of `scheduler.py`.
  - Route terminal epic transitions (`scheduler.py:756,822,878` write `"escalated"`) through a small frozen `set_epic_terminal_status()`.
  - Have `_collapse_tripped` (`escalation.py:186-206`) count only rows written by frozen writers (A-R8, writer provenance).
  - Remapping a failure to a status outside `{failed, escalated, abandoned}` (`escalation.py:61`) then stops disarming the breaker.
- **C-4:** add `db/migrations/**` to the publish-time frozen list. Today it is merge-high only (`config/risk-tiers.yaml:13`), and a trigger on `diagnosis_ledger` or `epics` would steer the policy without touching any frozen path.
- **D-8:** in `_ledger_ids` (`supervisor.py:274-281`), treat a NULL `fix_epic_id` as `IS NULL`, or key the labeller on the row id stored in `repair.json`.
- **D-4, permanent fix:** give the L11 "unlabelled diagnoses" signal an unfrozen owner. Otherwise the labelled corpus may never reach `MIN_LABELS = 30` (`escalation_policy.py:25`), and the policy stays on `BOOTSTRAP_BAR`. This is D's inference and was not traced end to end.
- **C-14 / C-15:** add indexes on `bug_reports(agent_role, updated_at)` and `epics(created_at)`, and use one connection per escalate instead of 4–5 (`escalation.py:135-138`).

**Prerequisite P1s:** #2, because `failures.py` writes the cap's rows and must be frozen before provenance means anything.

**Risk if deferred:** the daily cap and the collapse breaker can be disarmed through unfrozen writers, and the calibrated bar may never leave bootstrap.

### Bundle L: LLM dispatch (≈2.5–3 eng-wks)

Goal: one gated, metered, cached entry point for every recovery LLM call.

- **B-X2, `dispatch_gated(request, run_id, feature)`.** In order: consult `cost_pause`, dispatch, persist an `llm_calls` row, apply a positive/negative cache keyed on the prompt hash. This folds B-1, B-3, B-4 and B-7 into one edit, and every future recovery call inherits it.
- **B-5 / B-12:** set `max_turns=1` at `diagnoser_llm.py:75` and `graph_patch.py:77`. Both calls are toolless and produce a single JSON reply. Add a tier ladder that escalates when the verbatim-substring validator (`:455-458`) rejects a reply.
- **B-2:** add per-model rates for the `gateway` family in `mini_ork/dispatch/telemetry.py:42-45`. The diagnoser's default lane has zero cache-cost breakdown today.
- **B-9:** add `cost_usd_24h` to `EscalationInputs` (`escalation.py:276-287`). This edits the frozen policy, so it is an operator-owned change.
- **B-10, B-14:** a fallback ladder for the diagnoser lane; key the replan cache on content (`graph_patch.py:685-689`).
- **K14 (A-§7, B-6):** decide whether `config/providers.yaml` counts as a policy input. Recommendation: do not freeze the whole registry, since that would block every operator lane change behind approval. Instead, make the diagnoser and replan lane choice a frozen routing input (B-X1), and pass the resolved model into `EscalationInputs`.

**Prerequisite P1s:** #4 and #5.

**Risk if deferred:**
- Replan spend stays invisible to every cost gate.
- Spend on the failure path is unbounded on chronically unclassifiable runs during a 10-hour autopilot.
- The fix-epic cap stays count-only: 3 framework-edit runs a day with no dollar ceiling.

### Bundle O: Observability (≈1 eng-wk)

- **D-6:** thread the cycle's `run_id` through `mini_ork/rsi/worktree.py:289` into `land_patch`, as `mini_ork/cli/publisher.py:437-440` already does. The dedupe in `safety_events.py:275-291` only runs `if run_id:`.
- **S-1:** resolve the two stray open events described in §6.
- **A-R9:** report frozen lines ÷ engine lines as a first-class metric, and require each freeze kickoff to state how it moves the ratio.
- **A-R10:** a per-file panel of frozen refusals and overrides. Any file that keeps refusing legitimate work becomes the next K8 extraction.
- **A-8:** add a frozen-kernel section to `docs/SAFETY.md`.

**Prerequisite P1s:** none.

**Risk if deferred:** RSI refusals cannot be joined to their runs, and over-freezing stays invisible until someone notices RSI starving.

**Total P2: about 8.5–9.5 eng-weeks.**

---

## Section 4: Long horizon (P3 and advisory)

- **A-R7, server-side enforcement.** All five enforcement points run inside the repo the loop edits, and the worktree merge pushes `HEAD:main` with no server-side gate. Following the two-tier rule in `CLAUDE.md`:
  - **Simple tier:** today's local checks.
  - **Secure tier:** a pre-receive hook or ruleset that refuses commits touching the effective frozen set unless they are signed by the operator key.
  - One config key chooses the tier, in the shape of `config/vault.example.yaml`.
  - At 1000× (A §1) the kernel becomes a signed, content-addressed artifact versioned apart from the code it governs. Like the K14 recommendation, a third-party recipe must never ship its own floor; `static-check.py` currently does exactly that.
- **A-§7, decision-replay corpus.** See §5.
- **B-X3 / B-8:** batch the fleet's diagnoses (a shared prefix across K evidence packs), and request prompt caching on the stable `_prompt` prefix (`diagnoser_llm.py:364-386`). Both are only measurable after B-2.
- **K13 / E-8 / E-10 / B-15:** parse caching, a literal-set matcher, no tree walk per literal pattern in `apply.py:773-794`, one patch parse per publish, and registry memoisation. None of these matters at 22–23 patterns. Revisit when the list passes about 100 entries.
- **D-15:** add one test that pins `fnmatch` glob semantics for `/**`.
- **C-5 ✖:** downgraded (see the contradiction table). Keep a one-line note that `MO_RISK_TIERS_FILE` overrides everything.
- **A-4, advisory:** before merge, tell the autopilot owner that any change to the 8 files now needs `MO_ALLOW_FROZEN_EDIT=1` at publish and a person at merge. The recovery subsystem also drops out of RSI's candidate pool (`mini_ork/rsi/select.py:428-431`).

---

## Section 5: Hardest open question

**Can a sound policy-input closure be computed at all?** (A-§7, sharpened by B-6, C-4 and K3.)

The panel found that the frozen policy can be steered through at least five channels that no import-closure test sees:

| Channel | Evidence |
|---|---|
| Config | `config/providers.yaml` decides the self-diagnosis refusal through `_model_of` / `_independence` (`diagnoser_llm.py:317-350`, B-6) |
| Environment | `MO_FIX_EPIC_DAILY_CAP` and `MO_FIX_EPIC_BREAKER_N` (`escalation.py:85-92`, A-§7) |
| Prompts | the diagnoser prompt (A-§7) |
| Database rows written by unfrozen code | `scheduler.py`'s `epics.status` (K3) |
| Database triggers | a new migration, which the RSI cycle applies automatically (C-4) |

Freezing every channel freezes half the system, which is K8's failure mode taken further. Freezing none leaves the commit's sentence ("decides only from what its inputs hand it") true only in a narrow, syntactic sense.

**Assessment: the proposed mitigations are necessary but not sufficient, and this needs research, not just engineering.**

- **The import-closure test (A-R1 / E-2)** is a cheap, deterministic **tripwire**. Ship it (Bundle R), but treat it as a drift alarm, not a soundness proof.
- **Writer provenance (A-R8)** closes the database-row channel properly, but only if row-writing goes through frozen writers, and that is the K8 extraction work again.
- **A decision-replay corpus** (A-§7: replay a held-out set of failures through the live `diagnose → gather_inputs → decide` path on every merge) catches steering through *any* channel, but only on the cases in the corpus.
  - The corpus then has to be frozen too. The repo already does this for the code-fix probes (`config/kernel-frozen.yaml:51-55`).
  - That makes this the strongest candidate for a secure-tier mitigation.

**Proposed research.** Run a seeded red-team RSI campaign whose objective rewards fix-epic throughput. Count how many of the five channels above each defense catches: closure test, replay corpus, provenance, and server-side enforcement (A-R7). The repo's verification stack already names a "gate hackability" operator (`docs/architecture/verification-stack.md`, per `CLAUDE.md`). Whether it can drive this experiment was not checked; that is its natural home. Until the experiment runs, rank the defenses as **replay corpus + server-side enforcement > provenance > import closure**, and do not let a green closure test be read as "the policy cannot be steered".

---

## Section 6: Dogfood reflection

**Was the audit reproducible through the framework?** Partly.

- **The plan was a fallback.** The planner's output was invalid JSON, so mini-ork generated a deterministic recipe plan. This is a degradation in the coordination layer before dispatch (lesson **`[t:th-5a29ee8386e1]`**). It did not break this run, because the recipe workflow drove the dispatch, but a re-run is not guaranteed to get a plan written by the model.
- **The kickoff file is not durable.** It lives under `/private/tmp/claude-501/…/scratchpad/review/`, a temporary scratchpad. The audit cannot be reproduced once that directory is cleaned.
- **The test receipt depends on the interpreter.** The lane's default `python3` is 3.9.23, and on it the same test files give 15 false failures. A lens that ran tests on the lane default would have reported the diff as broken. A's "46 passed" holds only on Python 3.11 or newer.

**Did lens-level success mean grounded findings?** No.

- All five lenses finished, but **two were mechanically wrong about the only 5-of-5 finding (K1)**. B-11 said "not a hole" and E-1 described a reachability condition the code does not have. Neither noticed that the fallback's `parse_frozen_paths` returns `[]`.
- This repeats **`[t:th-65a39d1b0e3c]`** (a node that finished is not evidence the run's goal was met) and **`[t:th-acd129ae2eed]`** (a lens artifact is not automatically a grounded assessment).
- The downstream `lens_completeness` verifier checks that every lens produced an artifact, not that the artifacts are correct. Per **`[t:th-7d2e92b2ce92]`**, its pass must not be read as a correctness gate.
- This synthesis is the single auditable artifact that **`[t:th-1e42cc51c68c]`** requires.

**Recipe gap the synthesis exposed.** The synthesizer is told to read only `panel_reports`, but settling the K1 and C-5 contradictions required reading `static-check.py:53-81` and `merge_risk.py:150-191`. This synthesis read the diff and its direct callers read-only, within constraint `[c:1]`, and says so openly. The recipe should either declare the diff plus direct callers as a synthesizer input, or add a "verify contradictions" node between `anonymize_panel` and `synthesizer`.

**Did any lens get blocked by something the audit itself found?** No lens was blocked; all were read-only. **The fixes are blocked, by design.**
- P1 #1 edits `static-check.py`. That file is frozen, and its own guard is never unlocked by the token for auto-triage runs (`static-check.py` `_frozen_kernel_guard`: "The ALLOW token can NEVER unlock this check").
- P1 #4 and #5 edit files this diff freezes (`graph_patch.py`, `diagnoser_llm.py`, `diagnose.py`).
- P1 #2 and #3 edit frozen files (`config/kernel-frozen.yaml`, `repo_integrity_guard.py`) or high-tier files (`config/risk-tiers.yaml`).
- So **the autopilot cannot dispatch any of the top-5 fixes**. That is the frozen kernel working as intended, and the loop closes correctly.

**S-1, the synthesizer's own finding.** A read-only query on the live `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db` found two **open** `safety_events` rows:
- `frozen-kernel-edit` (high, id `40c9bde8…`) and `frozen-kernel-override` (medium, id `f02851bf…`);
- both with evidence `{"run_id": "", "paths": ["mini_ork/recovery/supervisor.py"], "boundary": "publish"}`;
- both at ts `1793106641`, which is 2026-10-27 14:10:41 +0100, **25 minutes before** this commit's author time of 14:36:03.

This is exactly the refuse-then-override fixture in `tests/unit/test_frozen_kernel_policy_inputs.py:120-157`. The likely cause is that the change's development run executed an early version of the test against the live database. The current test isolates the database (`:124-125`), and this synthesis's own test runs wrote nothing to the live database (checked by timestamp).
- **Effect:** the two rows inflate the open-event count on the verify page (`mini_ork/ide_pages/verify.py:590-591`), and they show D-6's empty-`run_id` shape.
- **Action (operator, not this lane):** resolve both rows with a note. The cause is attributed with moderate confidence, not proven.

---

## Section 7: How to re-run

```bash
# 1. Re-run the audit from the engine checkout.
#    First copy the kickoff somewhere durable: the /private/tmp scratchpad is temporary (§6).
cd /Volumes/docker-ssd/ps/mini-ork
bin/mini-ork run refactor-audit \
  /private/tmp/claude-501/-Volumes-docker-ssd-Migration-Development-researcher/69c08fc2-54d1-4754-ba39-2d8679676baf/scratchpad/review/kickoff-t1-kernel-hole-3.md
#    The synthesis lands at .mini-ork/runs/<run_id>/synthesis.md (recipes/refactor-audit/README.md:76-82).

# 2. Re-check the test receipt. Needs Python >= 3.11: the lane's default 3.9 gives 15 false failures.
cd /Volumes/docker-ssd/ps/mini-ork-worktrees/review-t1-kernel-hole-3
T=$(mktemp -d)
MINI_ORK_HOME="$T/home" MINI_ORK_DB="$T/state.db" PYTHONDONTWRITEBYTECODE=1 \
  python3.12 -m pytest -q -p no:cacheprovider \
    tests/unit/test_frozen_kernel_policy_inputs.py \
    tests/unit/test_frozen_kernel.py \
    tests/unit/test_repo_integrity_guard_py.py      # expect: 46 passed

# 3. Re-check the two contradiction rulings.
sed -n 53,81p   recipes/framework-edit/verifiers/static-check.py   # K1: fallback parse_frozen_paths -> []
sed -n 150,174p mini_ork/gates/merge_risk.py                        # C-5: origin/main is read first
```

**Does any P1 block self-dispatch?**
- **Of the audit:** no. `refactor-audit` is read-only and touches no frozen path.
- **Of the fixes:** yes, explicitly. **P1 #1 (K1) blocks self-dispatch of itself**: an `origin=auto-triage` framework-edit run that touches `recipes/framework-edit/verifiers/static-check.py` is refused, and the token cannot override that. P1 #2, #4 and #5 touch frozen paths, and #3 touches a high-tier path.

All five must ship as **human-authored** framework-edit kickoffs, with no auto-triage marker. Use a worktree (`make worktree SLUG=…`), set `MO_ALLOW_FROZEN_EDIT=1` at publish, and get a person's approval at merge (`mini_ork/gates/merge_risk.py:333-345`).
