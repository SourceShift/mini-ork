# Findings validation synthesis — GEPA + gradient pipeline

Source lens reports:
- Codex: `.mini-ork/runs/run-1783687254-45663/lens-codex.md`
- Kimi: `.mini-ork/runs/run-1783687254-45663/lens-kimi.md`
- Opus: `.mini-ork/runs/run-1783687254-45663/lens-opus.md`
- MiniMax: `.mini-ork/runs/run-1783687254-45663/lens-minimax.md`

All four expected lens reports were present and non-stub. No lens column is ABSENT.

## 1. Consensus table

| Finding | stated verdict | votes (codex/kimi/opus/minimax) | consensus | confidence |
|---|---|---|---|---|
| G1 — GEPA never enabled | CONFIRMED | AGREE (`lens-codex.md:3`; `lens-kimi.md:8`; `lens-opus.md:4`; `lens-minimax.md:40`) | CONFIRMED, but qualified as intentional gate-off rather than standalone defect. | HIGHEST |
| G2 — offline hash-scoring makes acceptance impossible | CONFIRMED | AGREE (`lens-codex.md:3`; `lens-kimi.md:9`, `lens-kimi.md:40-64`; `lens-opus.md:5`; `lens-minimax.md:41`) | CONFIRMED. The strict-improvement gate is incompatible with unscored/hashless mutations. | HIGHEST |
| G3 — mutation model is `stub` / no real rewrite | CONFIRMED | AGREE (`lens-codex.md:3`; `lens-kimi.md:10`, `lens-kimi.md:33-38`; `lens-opus.md:6`; `lens-minimax.md:42`) | CONFIRMED, with mechanism correction: `stub` is an unknown lane that yields `_NoProposal`, not a real stub rewrite. | HIGHEST |
| G4 — suggest-only; no apply/promotion path | CONFIRMED | AGREE (`lens-codex.md:3`; `lens-kimi.md:11`; `lens-opus.md:7`; `lens-minimax.md:43`) | CONFIRMED, but qualified as deliberate R4b suggest-only scope plus missing R5 apply path. | HIGHEST |
| Gr1 — runaway re-extraction | CONFIRMED | AGREE (`lens-codex.md:3`; `lens-kimi.md:12`; `lens-opus.md:8`; `lens-minimax.md:44`) | CONFIRMED. 9,777 gradients from 1,603 traces and no already-mined filter/watermark. | HIGHEST |
| Gr2 — dedup keyed on trace-specific signal | CONFIRMED | AGREE / PARTIAL / AGREE / AGREE (`lens-codex.md:4`; `lens-kimi.md:13`, `lens-kimi.md:20-28`; `lens-opus.md:9`, `lens-opus.md:20-21`; `lens-minimax.md:45`) | CONFIRMED in outcome, QUALIFIED in mechanism and counts. Same-target dedup is the deeper failure; one pass uses `signal+suggested_change`, not signal alone. | HIGH |
| Gr3 — no apply path for emergent patterns | CONFIRMED | AGREE (`lens-codex.md:3`; `lens-kimi.md:14`; `lens-opus.md:10`; `lens-minimax.md:46`) | CONFIRMED. All 39 emergent patterns remain `proposed`; no approved/rejected/apply consumer exists. | HIGHEST |
| X1 — diagnose-never-act loop | CONFIRMED | AGREE (`lens-codex.md:3`; `lens-kimi.md:15`; `lens-opus.md:11`, `lens-opus.md:22-27`; `lens-minimax.md:47`) | CONFIRMED. This is the load-bearing root cause: Score/Diagnose/Cluster exists; Apply/Online-eval/Gate/Promote does not. | HIGHEST |
| X2 — diagnosis quality high but wasted | CONFIRMED | PARTIAL / PARTIAL / AGREE / AGREE (`lens-codex.md:5`; `lens-kimi.md:16`; `lens-opus.md:12`; `lens-minimax.md:48`) | QUALIFIED. The diagnosis signal is real, but the numeric framing is loose and confidence is not calibrated by applied outcomes. | CONTESTED |

### Contested-finding proof quotes

- X2, Codex partial: `lens-codex.md:5` says `PARTIAL on X2 (real signal, but the "13" headline understates it).`
- X2, Kimi partial: `lens-kimi.md:16` says reviewer confidence mode is `0.88` and only about 16 rows are `>=0.90`, so the `0.92-0.95` dominant-band framing is loose.
- X2, Opus qualification: `lens-opus.md:12` agrees the diagnosis quality is real, but warns that confidence is model self-consistency, not apply-side value.

## 2. Verdict changes

No finding is overturned outright. The panel qualifies five findings and corrects two mechanisms/numeric claims.

### G1 qualified: intentional off-state, not standalone defect

The stated fact is true: GEPA is off unless `MO_OPTIMIZER=gepa`. Opus explicitly classifies this as deliberate pre-launch opt-in: `bin/mini-ork-reflect:226-232` says the default path leaves the block skipped (`lens-opus.md:4`). MiniMax adds that even `MO_OPTIMIZER=gepa` is insufficient without `_GEPA_TASK_CLASS` or `MO_OPTIMIZER_ALLOW_DEFAULT=1` (`lens-minimax.md:99-100`).

Change: do not fix G1 by simply removing the gate. Fix the production wiring and only then enable the optimizer path.

### G3 mechanism corrected: unknown lane, not a working stub provider

The finding remains fatal, but the mechanism changes. Kimi shows `"stub"` is not in `KNOWN_MODELS`, so dispatch fails before a rewrite is proposed (`lens-kimi.md:33-38`). Opus confirms the same call chain through `providers.py:29-31`, `providers.py:193-197`, and `_NoProposal` (`lens-opus.md:6`).

Change: fix G3 and G2 together. Passing a real model only exposes G2's acceptance-gate failure.

### G4 and Gr3 qualified: deliberate suggest/proposed stages, missing upper apply path

Opus says `mini-ork-reflect:229` explicitly describes GEPA as suggest-only and no consumer promotes `prompt_change` rows (`lens-opus.md:7`). For Gr3, Opus notes `reflection_persist_suggestions` intentionally writes `status='proposed'`, but no consumer moves rows to approved/rejected/apply (`lens-opus.md:10`).

Change: the defect is not that the lower stages write `suggested`/`proposed`; the defect is the missing Apply → Online-eval → Gate → Promote stage.

### Gr2 mechanism and count corrected

The outcome stands: semantic duplicates survive. But Kimi shows the original mechanism is imprecise: pass 1 uses `signal`, while the extractor containment pass uses combined `signal + suggested_change` (`lens-kimi.md:20-28`). Opus adds the deeper failure: the cross-target pass skips same-target rows, so the 172 `agent.reviewer.prompt` rows are not compared by that pass at all (`lens-opus.md:20-21`). Kimi also rejects the original `13` count and finds 57-58 evidence/verdict-like reviewer rows (`lens-kimi.md:29-32`).

Change: rewrite Gr2 as "dedup keys on lexical form and skips the concentrated same-target duplicate case," not "both passes key only on signal."

### X2 numeric/value framing qualified

MiniMax and Opus agree the diagnosis asset is real (`lens-minimax.md:48`; `lens-opus.md:12`). Codex and Kimi partially agree but reject the exact headline numbers (`lens-codex.md:5`; `lens-kimi.md:16`). Opus further notes confidence is not calibrated by apply-side outcomes (`lens-opus.md:48-49`).

Change: keep X2 as a real wasted-asset finding, but do not sell it as proven by a dominant `0.92-0.95` band. The stronger metric is 39 proposed patterns and 0 applied changes.

### X1 promoted to top priority

Opus explicitly says X1 is the load-bearing structural defect and that G2/G4/Gr1/Gr2/Gr3 are symptoms (`lens-opus.md:22-27`). MiniMax similarly says the central claim is verified end-to-end and the two highest-leverage fixes are apply-path/dedup and reflect-trace exclusion (`lens-minimax.md:106-108`).

Change: rank X1 first. Bookkeeping fixes help cost, but X1 is what unlocks value.

## 3. Implementation-ready set

### HIGHEST-confidence, safe to implement with qualifications

1. **X1 — build Apply → Online-eval → Gate → Promote.**
   - Proof: all apply-side tables are empty while diagnose-side artifacts exist (`lens-kimi.md:15`; `lens-opus.md:11`; `lens-minimax.md:47`).
   - Implementation constraint: online-eval must be included; applying without reward comparison recreates theater (`lens-opus.md:63-64`).

2. **G2 + G3 — fix as one paired GEPA execution path.**
   - Proof: G3 currently short-circuits before G2 (`lens-opus.md:18`), and G2 still rejects even if a real model is wired (`lens-kimi.md:40-64`).
   - Implementation constraint: pass a real model lane and replace hash-only offline scoring with online or held-out scoring of mutated prompt text.

3. **G4 + Gr3 — add consumers for `prompt_change` and `emergent_patterns.status='proposed'`.**
   - Proof: `workflow_candidates`, `promotion_records`, `version_registry`, and `textual_gradients` are all empty (`lens-opus.md:7`; `lens-opus.md:10`; `lens-minimax.md:43`, `lens-minimax.md:46`).
   - Implementation constraint: do not merely flip statuses; route proposed changes through the X1 gate.

4. **Gr1 — make reflection extraction idempotent and bounded.**
   - Proof: 9,777 gradient rows from 1,603 traces, worst trace re-mined 29 times (`lens-kimi.md:12`; `lens-opus.md:8`; `lens-minimax.md:44`).
   - Implementation constraint: use per-trace watermark or slice-claim semantics; exclude `__reflect__` traces because they account for 926 gradient rows (`lens-minimax.md:60-67`).

5. **G1 — keep the gate, but make enabling meaningful.**
   - Proof: off-state is deliberate (`lens-opus.md:4`), and secondary task-class/default gates also apply (`lens-minimax.md:99-100`).
   - Implementation constraint: enabling GEPA should be the final switch after G2/G3/X1 wiring, not the first patch.

### HIGH but not implementation-ready as originally worded

- **Gr2** — implement dedup, but use corrected mechanism: same-target duplicate handling plus semantic/normalized intent over `suggested_change`, not only signal text (`lens-kimi.md:20-28`; `lens-opus.md:20-21`; `lens-minimax.md:72-73`).

### CONTESTED / needs another look

- **X2** — keep as strategic motivation, not a standalone numeric proof. The current signal is real but self-reported confidence is not value-calibrated (`lens-kimi.md:16`; `lens-opus.md:48-49`).

## 4. Missed findings

### Codex additions

- Rank-based overwrite branch in `miniork_adapter.py:153-156` is cache-key-insensitive (`lens-codex.md:8`).
- `model="stub"` defaults exist at both `gepa.py:148` and `miniork_adapter.py:311`, so fixing only one level leaves mutations inert (`lens-codex.md:9`).
- Gr1 is also a cost bomb because `llm_dispatch` runs per trace per window (`lens-codex.md:10`).
- Gr2 dedup is per `(task_class, target)`, so reviewer-evidence insights repeated across task classes do not collapse (`lens-codex.md:11`).
- `validity="insufficient_evidence"` is silently dropped in `bin/mini-ork-reflect:258-260`, making cold DB runs indistinguishable from no run (`lens-codex.md:12`).
- `reflection_pipeline.sh:104-110` `LIMIT 10000` oldest-first can leave latest-window rows lagging in dedup (`lens-codex.md:13`).
- Retro-added `task_class` can group pre-migration rows under empty string and defeat future cross-epic dedup (`lens-codex.md:14`).

### Kimi additions

- `validity:"valid"` conflates "ran" with "improved"; zero accepted mutations can still persist as a valid prompt change (`lens-kimi.md:70-78`).
- The evaluator is blind to prompt text; naive `MO_OPTIMIZER=gepa` fixes still accept nothing if evaluation remains hash-only (`lens-kimi.md:80-88`).
- Dedup can be outrun once the table exceeds bounded batch size because overlapping extraction can grow faster than capped dedup removes (`lens-kimi.md:90-98`).

### MiniMax additions

- Recursive reflect-mining: `__reflect__` traces re-enter extraction and account for 926 gradient rows (`lens-minimax.md:60-67`).
- No rate-limit/change-gate on auto-reflect; every run fires multiple side effects and can spend heavily with no gain (`lens-minimax.md:69-70`).
- Cross-target dedup is structurally blind to within-target duplicates because same-target rows are skipped (`lens-minimax.md:72-73`).
- Reflection extraction batch truncation is silent; visible vs processed trace count is not reported (`lens-minimax.md:75-76`).
- No retention/GC across gradient, pattern, and cross-class tables (`lens-minimax.md:78-79`).
- `evaluate()` rank-based override is order-coupled and brittle (`lens-minimax.md:81-82`).
- `_default_score` is a global mean, biasing GEPA toward no change (`lens-minimax.md:84-85`).
- Cross-class gradient promotion writes `__cross_class__` rows without a bound/upsert (`lens-minimax.md:87-88`).
- GRPO-side overlays populate tables that are not read by dispatch yet (`lens-minimax.md:90-91`).
- Pattern upsert can reset status/resolution state (`lens-minimax.md:93-94`).
- `--task-class` filter is not plumbed through `reflection_run` (`lens-minimax.md:96-97`).
- GEPA block has a secondary `_GEPA_TASK_CLASS` / `MO_OPTIMIZER_ALLOW_DEFAULT` gate (`lens-minimax.md:99-100`).

### Opus additions

- GEPA suggestions written to `pattern_records` can pollute downstream consumers if no apply path reads them (`lens-opus.md:31-32`).
- `--since 24h` remains wrong-by-default even with a watermark; slice-claim semantics are cleaner (`lens-opus.md:34-35`).
- `reflection_detect_stale` computes stale gradients and discards the output (`lens-opus.md:37-38`).
- Silent-failure surfaces compound across gradient extraction, `stub` dispatch failure, and `_NoProposal` handling (`lens-opus.md:40-46`).
- Confidence has no feedback loop from apply-side outcomes (`lens-opus.md:48-49`).
- `reflect_on_component` inherits cwd/framework-tree guards, so self-optimization still needs a safe opt-in path (`lens-opus.md:51-52`).
- Dedup on `suggested_change` needs a normalization step because prescriptions are also trace-specific (`lens-opus.md:54-55`).
- `_score_cache` warmup is currently wasted when `cand_hash == ""` (`lens-opus.md:57-58`).
- `textual_gradients` exists but is empty, confusing source-of-truth boundaries (`lens-opus.md:60-61`).
- X1 fix must include online-eval or it remains theater (`lens-opus.md:63-64`).

## Final synthesis

The panel validates the register's central thesis: mini-ork's learning loop diagnoses repeatedly but does not act. The strongest implementation path is not to flip GEPA on directly; it is to build the missing Apply → Online-eval → Gate → Promote loop, then wire GEPA with a real model lane and prompt-text-aware evaluation, then turn on gated production use.

The only contested item is X2's numeric framing. The diagnosis asset is real, but its value should be measured by applied, reward-improving changes rather than self-reported confidence bands.
