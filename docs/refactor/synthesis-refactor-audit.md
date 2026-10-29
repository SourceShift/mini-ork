# Synthesis — `wt/t1-certificate` @ `4ff3a0aa` (anytime-valid promotion certificate + per-class α ledger)

**Diff under review:** `db/migrations/0086_promotion_certificates.sql`, `mini_ork/cli/apply.py`,
`mini_ork/learning/promotion_certificate.py`, `tests/unit/test_promotion_certificate.py`, plus direct
callers (`apply_run` → `evaluate_gate` → `certify`; `execute.py:2175-2179` → `auto_sweep`).

**Panel verdicts:** A ADJUST · B ADJUST (one required item) · C APPROVE-as-is (take R1 if one ADJUST is
forced) · D ADJUST · E ADJUST → **4 ADJUST / 1 APPROVE.**

**Synthesis verdict: ADJUST.** Merging is safe for the live loop. Four responses (B, C, D, E) independently confirm two things.
First, the flag-off path emits no `cert` key and touches no DB (`apply.py:642`, `:728-729`). Second, every
certificate rewrite is guarded by `if decision == "promoted"` (`apply.py:647-701`), so the overlay can only
demote. The problem is that the feature's only enabled state is a **silent shutdown of the promotion
channel**: the bar is 40 and the e-value ceiling is 3.06 (all five responses). The fixes are cheap now and
expensive later. Migration 0086 and the module are still unmerged, so schema changes cost nothing today and cost a migration after
merge. Land the five P1 wins below (≈8.5 eng-days), then merge.

**ID key.** `A-N` = A's table row N · `B-N` = B's refactor-N · `C-N` = C's R-N (`C-§7` = its hardest
question) · `D-N` = D's finding-N, `D-16` = D's architectural change 1 (model-tier the control arm);
D's architectural changes 2/3 restate D-8/D-3 · `E-N` = E's F-N. ★ = raised by 2+ responses.

---

## Section 1: Severity × leverage matrix

| | **HIGH leverage** | **MED leverage** | **LOW leverage** |
|---|---|---|---|
| **P1** | **Unreachable at shipped config** ★ A-1, A-3, B-1, B-10, C-15, D-1, E-1 · **No pre-flight before probe spend / no inverse of the threshold** ★ C-1, D-2, D-6, D-14 · **Config failures silent or deferred** ★ A-5, A-21, B-2 · **Suite never exercises real probe sizes** ★ A-4, E-16 (+B-10) | **Infra failure → sticky evidence quarantine** ★ A-6, A-12, C-2, C-3, D-7, E-13 · **Cert evidence dropped from the audit trail** ★ A-15, A-27, E-2 · **Prerequisites undocumented / undiscoverable** ★ A-18, A-19, A-25, C-17, D-13, E-15 · **Provenance columns missing (run_id, scorer, dry_run, probe-set hash)** ★ A-9, C-4, C-11, B-8 · B-9 (flag-off test is new-vs-new) | **Duplicate index** ★ A-24, C-7 · **DDL declared twice, substring-only guard** ★ A-7, B-3, E-9 · **`executescript` implicit COMMIT** ★ A-8, B-4, D-9, E-8 · **Ledger key folds `-`→`_`** ★ A-22, C-6, E-6 · C-10 (append-only triggers) · A-14 (timestamp type) · A-20 (α has no guardrail) |
| **P2** | **α-ledger freezes the class** ★ A-2, B-1, D-4, E-4, E-5 · **`ledger=none` / mode switching / per-class policy pinning** ★ A-11, A-16, C-5, D-5, E-14 · **Ledger scope is per-DB-file, no secure tier** ★ C-9, D-15 · C-8 (split `decide()` / `certify()`) | **Write-only ledger, no reader, no structured cert record** ★ E-3, C-16 · E-12 (env-sourced vectors, `ids` ignored) · E-7 (public `evaluate_gate` without `task_class` quarantines) · B-11 (one machine-readable status) · D-8 (serial probe sweep — outside diff) · D-12 (runbook cost 2× off — outside diff) · A-26 (no concurrency test) | **gains/losses computed 2–3×** ★ A-23, B-6 · **Per-call connection / DDL / resolve on hot path** ★ D-10, D-11, A-10 · B-7 (extract demote overlay) · C-13 (λ=0.9 after measurement) |
| **P3** | C-§7 (frozen probe set → adaptive overfitting) · C-14 (e-LOND mode) · E-10 (second, uncoordinated α budget in `rsi/impact.py`) · C-12 (α/λ/ledger per class in `task_class.yaml`) | E-11 (best-of-N control: power loss, docstring H0 inaccurate) · D-3 (control-vector cache — **rejected as stated**) · D-16 (cheap control lane — **rejected as stated**) · A-13 (ledger retention — **rejected**) | A-17 (`log_e` instead of underflowing `e`) · B-5 (binary-outcome validation) |

### 1.1 Where this synthesis overrules the panel

Several proposed fixes would **break the guarantee the module exists to provide**. Do not implement these as written:

1. **E-4 "make the slot idempotent on `(task_class, candidate_id)` and reuse it"** is unsound. A re-test
   runs fresh probe arms, so it is a fresh look. Two looks at one `α_k` double that slot's error. The sound
   form is **optional continuation**: hold one slot per candidate and *multiply* each fresh e-value into
   that candidate's running product. That product is still a test supermartingale, so Ville bounds it at `α_k`.
2. **E-5 "spend α only on tests that reached the promote branch"** is unsound. The branch is chosen from
   the same data as the e-value, so selecting on it inflates `E[E_j | selected]`. The existing
   *zero-discordant* skip (`promotion_certificate.py:191-199`) is sound, and B's soundness note is right,
   for a specific reason: `E_j = 1` *exactly* on the skipped event, so `E[E_τ] ≤ P(τ<∞) ≤ 1`. Non-promote
   tests do not have `E_j = 1`. A gain-heavy test with one regression has `1.75⁶·0.25 ≈ 7.2`.
3. **A-2 "make the spend rule linear (α/k)"** is unsound: `Σ 1/k` diverges. A bounded horizon only works
   when normalized, `α/(k·H_N)`. **D-4(b) / A-2 "reset epoch"** is only sound if epochs get disjoint α
   shares (e.g. `α·2⁻ᵉ` for epoch `e`). A plain reset gives the class unlimited total α.
4. **D-4(a) "make `ledger=none` the documented mode for high-volume classes"** contradicts D-5. `none` is
   the exact fixed-α defect the module docstring (`promotion_certificate.py:3-6`) says it replaces.
5. **A-13 "register the table with retention, keep 90 days"** is unsound. If retention deletes every row for
   an idle class, `MAX(slot)+1` (`promotion_certificate.py:205`) returns to slot 1 and the budget silently resets.
   C-10 is right: the ledger is append-only, and the head counter is never pruned.
6. **D-3 "cache the control vector across candidates"** — D asserts "the statistic is preserved". It is
   not preserved *conditionally*. A cached "baseline failed" on a probe the baseline usually passes is a
   free gain, with probability `q_i`, for **every later** null candidate. The guarantee then becomes an
   average over one cache draw, and the conditional-validity argument (`promotion_certificate.py:10-16`)
   no longer applies. Don't ship it without a new proof.
7. **D-16 "route the control arm to a cheaper lane"** fails open. Under H0 the lane gap appears as
   systematic gains (`E[a−b] > 0`), and a finite calibration cannot prove `E[a−b] ≤ 0`.
8. **B-2's patch as written is inert.** Config errors are only read inside `if cfg["enabled"]`
   (`apply.py:642-644`, the same point A-21 makes). B's patch keeps `enabled = False` for `=true`, so the
   new error never surfaces. The correct fix treats any set value other than `""`/`"0"` as
   *enabled*, then fails on anything that isn't `"1"`. Even better, refuse in the arm-time pre-flight (Win 2).
9. **A-8's Python-version split is wrong.** `executescript` issues its implicit COMMIT on 3.12 too under the
   default legacy transaction control. E reproduced the COMMIT (`in_transaction = False`). The defect is real
   on both CI interpreters; the version difference is not.
10. **C's and D's "the DDL test pins it byte-for-byte" is wrong about the test.** B quotes
    `tests/unit/test_promotion_certificate.py:168-172`: it checks substrings only. D is right that the two
    copies match *today*. The guard is still weak (A-7, B-3, E-9).
11. **A-12 vs D-7:** `sqlite3.connect`'s default `timeout=5.0` equals `busy_timeout=5000`. The timeout
    value is not the bug. The bug is that a lock timeout gets classified as evidence (D-7, C-3).
12. **E-11 "the martingale property does not hold"** is overstated. With `b = max` of N draws,
    `E[a−b] ≤ 0` still holds under H0, so the e-value stays valid. The cost is power, plus a docstring that
    describes the wrong null.

---

## Section 2: Top 5 immediate wins (P1)

Ranked by ROI. Total ≈ **8.5 eng-days** (< 2 weeks).

| # | ID(s) | Title | Source | One-line fix | Effort |
|---|---|---|---|---|---|
| 1 | A-1★, B-1★, B-11, D-14★, E-1★ | **Name the unreachable state; expose the threshold's inverse** | all five | Add `required_gains(slot, alpha, bet, ledger)` and `max_e(n, bet)` to `promotion_certificate.py:109-129`. Return `status ∈ {certified, uncertified, unreachable, no_vectors, misconfigured, unavailable}` plus a `ledger_warning` when `ledger != "spend"` (A-11, D-5). Make `apply.py:691-701` say `certificate unreachable: need ≥g probes, have n`. | 1.5 d |
| 2 | C-1★, D-2★, D-6, B-2★, A-5★, A-21★, A-20, docs ★ (A-18, A-19, C-17, D-13, E-15) | **Arm-time pre-flight in `apply_run`, before any probe dollar** | C, D, B, A, E | Next to the frozen-kernel check, before `_ps.probe_score` (`apply.py:1301`): check `resolve_config` errors (corrected B-2 flag parse), that `_db_path` resolves (`apply.py:199`), that `required_gains(MAX(slot)+1) ≤` the effective non-anchor gate-probe count under `MO_APPLY_PROBE_MAX_TASKS` and `MO_APPLY_PROBE_GIT_ORACLE` (`probe_scorer.py:320-330`), that `MO_APPLY_PROBE_BUDGET_USD` buys `(control_n+1)·n` arms (`probe_scorer.py:1126`), and that α ≤ 0.2 unless explicitly overridden. On any failure: **exit 2 with no `apply_attempts` row** (so `_previously_failed`, `apply.py:1172`, never blacklists). Put the prerequisites into the env block at `apply.py:61-64`, a `docs/operator/feature-flags.md` row, and a `features/registry.py:504` entry. | 2.5 d |
| 3 | D-7★, C-2★, C-3★, E-13★, A-6★ | **Separate infra failure from evidence failure** | D, C, E, A | At `apply.py:661` use `sqlite3.connect(path, timeout=30)` (matches `scheduler.py:99`). Retry `BEGIN IMMEDIATE` (`promotion_certificate.py:203`) on `database is locked` with backoff. Record any residual `unavailable`/`misconfigured` with `reason_class='infra'`, which `_previously_failed` (`apply.py:1172-1187`) ignores. Today one busy second or one bad env var permanently blacklists every directive that *measured* an improvement (C-1). | 2 d |
| 4 | A-24★/C-7★, A-7★/B-3★/E-9★, A-8★/B-4★/E-8★, A-9★/C-4★/C-11★, A-22★/C-6★/E-6★, C-10, A-14 | **Fix 0086 while it is still free** | A, B, C, E | In `0086_promotion_certificates.sql`: drop `idx_promotion_certificates_class` (`:24` duplicates the `UNIQUE` at `:22`). Add `run_id`, `scorer`, `dry_run`, `probe_set_hash`, `probe_ids`, and raw `task_class` next to the folded key. Add no-UPDATE/no-DELETE triggers. Settle `created_at` (`:21`) now. Derive `pc.DDL` from the migration with a byte-equality test (`tests/…:168`). Replace `executescript` (`promotion_certificate.py:66`) with per-statement `execute` behind a once-per-process guard (`apply.py:205 _SCHEMA_INIT`). | 1 d |
| 5 | A-4★/B-10★/E-16★, B-9, A-27★/E-2★, A-26, B-8★ | **Pin the claims the suite currently can't see** | A, B, D, E | (a) A test that loads the shipped probe config (`_load_probes`, `_probe_setting`) and asserts the pre-flight refuses at cap 2, which pins the dead zone. (b) A golden flag-off string captured from `HEAD~1:apply.py`, replacing the new-vs-new test at `tests/…:351`. (c) Fold `e/slot/α_k/threshold` into the rationale on **every** slot-consuming path, including already-quarantined ones (`apply.py:678-701`), assert it lands in `apply_attempts` (`apply.py:1502`), and tag dry-run spends (B-8). (d) A two-connection race test for slot allocation. | 1.5 d |

Why this order: Win 1 makes the failure *legible*. Win 2 makes it *free*: no ~$135 / ~13 h probe run (D-1,
citing `docs/operator/probe-harvest-runbook.md:60`) just to learn the result is 3.06 < 40. Win 3 stops the
fail-closed path from destroying evidence. Win 4 costs one day now and a migration later. Win 5 is what would
have caught A-1 before review.

---

## Section 3: v0.x+1 architectural shifts (P2)

### Bundle R — runtime: certificate-driven sample size (≈2.5 eng-wks)

The panel frames the ledger as "freezing" (A-2, B-1, C-§5, D-4, E-4, E-5). **The binding constraint is the fixed probe count, not the
spend rule.** C's own table shows the required clean gains grow only *logarithmically*: 7 → 14 → 22 → 31 over slots
1 → 10 → 100 → 1000. A spending sequence like `∝ 1/(k·log²k)` saves about 4 gains at k=1000 and *costs*
gains at k=10. Rewriting the spend rule therefore buys little. Make the probe count follow the bar:

- `apply_run` asks `probe_score` for `required_gains(next_slot) + margin` gate probes instead of a static
  `MO_APPLY_PROBE_MAX_TASKS` (`probe_scorer.py:328`). The slot is fixed before any data is seen, so this is valid.
- **Futility stopping inside the sweep.** Stop launching arms once `e·(1+λ)^remaining < 1/α_k`. It is always
  valid (it can only reduce rejections), it is the in-sweep form of D-2's pre-flight, and it is the actual
  payoff of "anytime-valid". Success stopping (`e ≥ 1/α_k`, licensed by Ville) conflicts with the
  tolerance-0 per-task regression gate, which needs full coverage. Leave it off unless that coverage
  requirement is relaxed on purpose.
- Split `decide()` (pure) from `certify()` (spends) — C-8. This absorbs B-7 (extract the four demote arms at
  `apply.py:630-701`), B-6/A-23 (count gains/losses once, `apply.py:520-525`), and E-7 (resolve `task_class` from
  the candidate row when the caller omits it, instead of quarantining with a `ValueError` at
  `promotion_certificate.py:72`).
- C-13 (λ 0.75 → 0.9) only after measuring on a harvested set. It saves one probe at slot 1.
- **Prerequisite P1s:** Win 1, Win 2. **Scope note:** the futility hook lives in `probe_scorer.py`, outside
  this diff (constraint c:1), so file it as its own ticket.
- **Risk if deferred:** every certifiable evaluation costs about 56 launches (D-3's arithmetic), and SDK or what-if callers of
  `evaluate_gate` spend real α (C-8).

### Bundle L — data layer: ledger head + provenance (≈2 eng-wks)

- `ledger_heads(task_class PK, mode, alpha_total, bet, epoch, next_slot)` — C-5, A-16. This gives O(1) slot
  allocation instead of the `MAX()` scan at `promotion_certificate.py:205`. It pins mode/α/λ on first spend,
  and an env value that differs from the head becomes a Win-2 refusal. That closes D-5's leak (`none`-mode tests spending `spend`-mode slots) and C-5's
  silent `none→spend` switch.
- Per-candidate optional continuation: the *sound* form of E-4 (§1.1 item 1).
- Certify only vectors from the probe scorer. Pair them by `ids`, not by position (E-12, `apply.py:496-507`). Skip
  `certify` entirely for `FABRICATING_SCORERS` (C-4), so mock runs stop raising the bar for real ones.
- A `cert_json` column on `promotion_records` (A-15, C-16) and a reader (`mini-ork apply certificates
  --task-class X`, E-3). Today the table's only reader is the writer's own `MAX(slot)`.
- **Prerequisite P1s:** Win 4. **Risk if deferred:** the union bound can be voided by a mode switch, and nothing
  records it. The "auditable" ledger is not queryable.

### Bundle S — secure tier: one class, one ledger (≈3 eng-wks)

- D-15 is the panel's only **fail-open** finding. The ledger lives in whatever `MINI_ORK_DB` the node has, so
  under `--placement remote` each node restarts at slot 1 and the effective α multiplies with the node count.
- C-9: a `CertificateLedger` protocol, `spend(class, e) → (slot, bar)`. The simple tier is SQLite. The secure
  tier is a shared store (`ledger_heads … FOR UPDATE`). One config file chooses between them, following the vault pattern CLAUDE.md
  names as the reference for the two-tier rule. Until this ships, the simple tier must state its per-DB scope
  (module docstring, SAFETY.md §PromotionGate) and refuse to arm off a node-local DB under remote placement.
- **Prerequisite P1s:** Win 2 (the refusal hook), Bundle L (the head row). **Risk if deferred:** the first
  multi-node deployment certifies at up to N·α without any signal.

### Bundle O — observability (≈1 eng-wk)

- An OTel span event `mini_ork.apply.certificate` with slot / e / threshold / mode / probe_set_hash / cost (C-16).
- A dashboard showing α remaining per class, slots per week, certified rate, and **cost per certificate**. A frozen class shows up in cost per certificate
  before it shows up in the promote rate (C §6).
- Alert on any `misconfigured`/`unavailable` count above 0. After Wins 2–3 they should be impossible.
- **Prerequisite P1s:** Win 5(c), Bundle L. **Risk if deferred:** the only freeze detector is someone asking
  why nothing promotes.

### Bundle D — LLM dispatch: probe economics (outside this diff, ≈2 eng-wks, separate ticket)

- D-8: a bounded worker pool with budget *reservation* in place of the sequential pre-flight at
  `probe_scorer.py:1170` (≈4× wall-clock at `control_n=3`). Safe, because it does not change the statistic.
- D-12: reprice `docs/operator/probe-harvest-runbook.md:60` at the `control_n=1` that `:65` prescribes (≈$67, not ≈$135).
- D-3 and D-16 are **not** in this bundle (see §1.1 items 6–7).
- **Risk if deferred:** a ≈13 h serial evaluation per candidate keeps bounding RSI-cycle throughput.

---

## Section 4: Long-horizon (P3 + advisory)

- **C-14** e-LOND (online FDR) ledger mode for productive classes past ~50 slots. It changes the guarantee from
  FWER to FDR, so make it per-class and tied to the risk rung, never a global default.
- **E-10** `rsi/impact.py:113-128` runs a second α budget over a possibly overlapping decision stream. One shared
  per-class spending policy, or a written proof that the streams are disjoint. Advisory, outside this diff: as
  quoted, `alpha0/(Z·k·log2(k+1))` sums to a *divergent* series unless it is horizon-capped. Worth a one-line check.
- **C-12** declare α/λ/ledger per class in `task_class.yaml` (`schemas/task_class.schema.json`). Env stays a
  simple-tier override only.
- **C-11** signed and frozen probe sets for marketplace recipes. Win 4 records the hash; this enforces it.
- **E-11** feed the control's per-retry outcomes (not `max`) or document the actual H0 (`probe_scorer.py:1252`).
  This is a power issue, not a validity issue (§1.1 item 12).
- **A-17** store `log_e` (`0.25**1000 == 0.0` at `promotion_certificate.py:98`). **B-5** reject non-binary
  outcomes in `paired_counts` (`:77-88`).
- **A-10/D-11** gate the import and `resolve_config()` on the flag (`apply.py:640-641`). Also make
  `execute.py:2179`'s `except Exception: pass` log instead of swallowing. That second part is the real hazard; the import is not
  (D: `pyproject.toml:95` ships `mini_ork*`).
- **D-10** reuse the caller's connection, after Bundle R's split makes ownership explicit.
- **Rejected, tracked so they are not re-proposed:** A-13 (retention), D-3 (control cache), D-16 (cheap
  control lane), E-4/E-5 as written, A-2's α/k.

---

## Section 5: Hardest open question

**C-§7: what null does the certificate reject, when one frozen probe set is reused for every candidate in a
class?**

The supermartingale argument is sound for "this candidate is no better than baseline *on these probes*, given
LLM run-to-run noise". The ledger then union-bounds noise-luck across *every* test the class ever runs. That is
real and valuable. It says nothing about generalization. A proposer that over thousands of iterations finds a
directive that is *genuinely* better on 7–14 specific probes, and nowhere else, is not a null under this test.
Certifying it is not a type-I error.

**Assessment: C's three options are not enough on their own, but the structural fix does not wait on research.**

1. **Ship now, inside Win 2's docs:** in SAFETY.md §PromotionGate (C-17), say "noise guard, not a
   generalization guard". That is C's option 3, and it is the honest description of what merges.
2. **Ship with the harvest:** certify on probes the proposer never gets feedback from. Today the leak is
   concrete. `evaluate_gate` names regressed probe ids in its rationale (`apply.py:506-507`, E-12), and the
   rationale is persisted and readable (`apply.py:1502`). Gate probes guide the search; `holdout: true` probes
   (never scored by apply, per C) *certify*, and their per-probe outcomes stay out of every rationale. With
   `probe_set_hash` on each row (Win 4), rotation becomes auditable. Rotation stays sound only with disjoint
   per-epoch α shares (§1.1 item 3). C's worry about R5 epochs is resolved by tying the epoch to the rotation
   *and* to a halving α share.
3. **Research still needed:** how much the 1-bit-per-test decision channel leaks at this probe-set size. The runbook's null calibration
   (D cites §4.1) is the right instrument. Run the real optimizer against a placebo directive generator on a
   frozen set and count certifications. Reusable-holdout results suggest leakage grows roughly like `√(k/n)` in
   the number of looks, so at n=14 and hundreds of tests it is not negligible. Do not move `α` defaults until
   that number exists.

A second question, close behind: D-3 and D-16 show that the **cost pressure this feature creates pushes toward
changes that silently void its null**. Any future probe-cost optimization should carry a written validity
argument in the PR, the way `promotion_certificate.py:10-16` does.

---

## Section 6: Dogfood reflection

- **Reproducible via the framework: yes, partially.** The refactor-audit recipe dispatched all five lenses,
  the anonymize transform, and this synthesizer. The planner LLM, however, emitted invalid JSON, and the run went
  ahead on mini-ork's **deterministic fallback plan** (plan `assumptions[1]`). The recipe is reproducible; the
  planner step is not.
- **Did a lens get blocked by something this audit found? No, but the meta-loop is closed the wrong way.**
  With `MO_APPLY_CERTIFICATE=1` at shipped settings, *no* recipe can promote, and that includes improvements to
  this recipe's own prompts. Recipes without probes get `no per-task vectors` → quarantine (D-6 receipt). The
  feature under audit would freeze the loop that improves the auditor.
- **Lesson check.** No `[f:…]` ids were supplied in this run's context, so no f-id is repeated. The closest
  supplied lesson is **[t:th-7d2e92b2ce92] (vacuous verifier verdict)**, and the change repeats its shape: the
  commit's green suite (54 passed, per B and C) is *vacuous* for the feature's headline claim. Every test fabricates 12-probe
  vectors (`tests/…:211`, `:336-338`, `:612-614`), and none exercises the shipped cap of 2 (A-4, B-10, D, E-16).
  [t:th-65a39d1b0e3c] applies to this run too: five lens "successes" are not a synthesis, which is why
  §1.1 exists.
- **Grounding ([t:th-acd129ae2eed]).** B and C report running the test file (54 passed). D reports executed
  receipts against a throwaway DB. E shows grep and REPL evidence. A cites greps. Per the input manifest, this
  synthesizer read only the anonymized panel and re-executed nothing. I re-derived the load-bearing arithmetic by hand:
  `1.75² = 3.0625`, `1.75⁷ ≈ 50.3 ≥ 40`, `1.75¹⁴ ≈ 2527 < 20·11·12 = 2640`, so exactly 10 certifiable slots
  at n=14, which agrees with D-4. I resolved factual conflicts from quoted code (§1.1 items 9–11).
- **Scope leaks.** D's cost section (D-3, D-8, D-12, D-16) and E-10 audit code outside the diff and its
  direct callers, against kickoff constraint c:1. They are useful, but they belong in Bundle D, not in this merge's verdict.
  Two of them (D-3, D-16) would have broken the certificate if adopted. That is the cost of a lens without a file allowlist.

---

## Section 7: How to re-run

```bash
# From the main checkout; the review worktree must sit at 4ff3a0aa.
cd /Volumes/docker-ssd/ps/mini-ork
env -u MO_APPLY_CERTIFICATE -u MO_AUTO_APPLY \
  bin/mini-ork run refactor-audit \
  /private/tmp/claude-501/-Volumes-docker-ssd-Migration-Development-researcher/69c08fc2-54d1-4754-ba39-2d8679676baf/scratchpad/review/kickoff-t1-certificate.md
# → .mini-ork/runs/<run_id>/synthesis.md

# Ground truth the panel ran (read-only):
python3 -m pytest -q /Volumes/docker-ssd/ps/mini-ork-worktrees/review-t1-certificate/tests/unit/test_promotion_certificate.py
```

- **No P1 blocks self-dispatch.** The certificate sits only on the apply path and is off by default. Unset
  `MO_APPLY_CERTIFICATE` and `MO_AUTO_APPLY` anyway. Otherwise the post-run `auto_sweep` (`execute.py:2175-2179`)
  spends α slots as a side effect of running the audit, and under the infra-failure finding (Win 3) a busy DB
  turns those into sticky quarantines.
- **Reproducibility gap:** the kickoff lives in a `/private/tmp` scratchpad. Persist it in the repo, or this
  command stops working after the next reboot.
- **Planner fallback:** expect the same "invalid JSON → deterministic recipe plan" path. The output is
  equivalent, but the planner lane's failure is unaddressed.
