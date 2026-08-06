# Synthesis — Recursive Self-Improvement, iter 1 (20260806123316)

All five lenses (bottleneck / perf / correctness / arch / arxiv) converge on a
single load-bearing defect: **telemetry/provenance collapse at the
dispatch→trace boundary**. 112/126 `recursive_self_improve` traces carry empty
`tool_calls`/`files_read`/`files_written`; **126/126 carry empty
`prompt_version_hash`/`context_bundle_hash`**. The second half of that is not
merely a logging gap — it makes the recipe's own learning loop *structurally
inert*: `optimize/gepa.py` and `learning/rho_aggregator.py` filter their input
with `WHERE prompt_version_hash <> '' AND prompt_version_hash IS NOT NULL`
(per lens-correctness §A4), so every self-improve row is discarded before it
can teach anything. A recursive-self-improvement recipe that cannot feed its
own GRPO loop is the highest-leverage fix on the board.

**Grounding note (synthesizer correction).** The three code lenses name the
trace-writer `build_trace_payload()`; the live function is
`trace_write_node` (`mini_ork/trace_store.py:140`). Line ranges the lenses
cite are correct; the symbol name is not. Patch 1 below uses the verified
name. The write-site gap is confirmed by direct read: `_tf`
(`mini_ork/cli/execute.py:1662-1672`) builds `extra` with `trace_id`,
`run_id`, `objective_domain`, `verifier_output`, `agent_version_id` — and
never sets either provenance hash; `trace_write_node`'s payload dict
(`:184-188`) has no slot for them either.

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | 126/126 self-improve traces have empty `prompt_version_hash`/`context_bundle_hash` → GEPA/rho_aggregator filter out 100% of rows → learning loop inert | correctness | In `_tf` (`execute.py:1662-1672`), stamp `prompt_version_hash` + `context_bundle_hash` into `extra`, computed **at the write site** from the node's recipe prompt file and the run's `context-pack.json` (materials provably present), so downstream `WHERE prompt_version_hash <> ''` filters return rows | `execute.py:1662-1672` (gap), `trace_store.py:184-191` (pass-through), lens-correctness §A4, lens-perf H2/F2, lens-arch RC1; arXiv 2605.17169, 2607.00941, 2606.07889 | 0.88 |
| 2 | Verifier persists `{"verdict":"fail"}` only — 7 of 33 failures undiagnosable | correctness | At the verdict-write site (`verify.py:295-313`), carry the `results` array already built at `:295-303` plus a `failure_detail` list (`{rule_id, expected, observed, missing_inputs}`) into `verifier_output` | `verify.py:295-313`, `_safe_trace_write` `:307-313`, lens-correctness §A2, lens-bottleneck row 2; arXiv 2607.28871, 2412.13114, 2608.02011 | 0.82 |
| 3 | `duration_ms`/`cost_usd` re-coerced to 0 when sidecar absent — 112/126 rows are 0, hung≡fast≡unmeasured | perf+correctness | In `trace_write_node` (`trace_store.py:169-182`), add `cost_observed`/`duration_observed` bools reflecting whether the sidecar was actually read; guard the 5 arithmetic consumers so unobserved-zero ≠ real-zero | `trace_store.py:169-188`, `context_assembler.py:332-333`, `otel_export.py:96,101,239,258,266`, lens-perf F1, lens-correctness §A1; arXiv 2605.08747, 2604.05119 | 0.80 |
| 4 | Implementer no-op with `files_written=[101 paths]`, `tool_calls=[]`, accepted as success — audit cannot answer "what did it do?" | correctness | Implementer prompt mandates a per-file provenance block; `trace_write_node` downgrades `status` to `needs_provenance` when `cost_usd > floor AND tool_calls=[] AND files_read=[] AND files_written non-empty` | `trace_store.py:169-191`, `execute.py:1683`, `recipes/recursive-self-improve/prompts/implementer.md`, lens-correctness §A3, lens-bottleneck row 3; arXiv 2604.04035, 2603.14688, 2604.17557 | 0.74 |
| 5 | Dual verifier filenames `verifier_*.json` (legacy) + `verifier-result-*.json` (current) both written every run | arch | Drop `"verifier_"` from the mirror tuple at `execute.py:1385`; canonicalize legacy source read-only | `execute.py:1385`, readers `self_improve.py:91`/`web/why.py:91`/`run_detail.py:169`, lens-arch RC2, lens-correctness §A5 | 0.70 |

Only **Patch 1** is attempted this iteration (kickoff: "exactly one patch,
minimal and reviewable"). Ranks 2–5 are queued to `learning_record` for
subsequent iters.

## Top patch — detailed plan

### Patch 1: Stamp `prompt_version_hash` + `context_bundle_hash` at the trace-write boundary

**Problem statement.** Every `recursive_self_improve` execution trace persists
an empty `prompt_version_hash` and `context_bundle_hash` (126/126, queried this
run). Both downstream learners — `optimize/gepa.py` and
`learning/rho_aggregator.py` — filter their input on
`prompt_version_hash <> '' AND prompt_version_hash IS NOT NULL`
(lens-correctness §A4), so the recipe's entire output is discarded and the
GRPO learning loop the recipe exists to feed never receives a row.

**Evidence.**
- `mini_ork/cli/execute.py:1662-1672` — verified by direct read: the `_tf`
  closure's `extra` dict is assembled with `trace_id`, `run_id`,
  `objective_domain`, `verifier_output` (and `agent_version_id` at `:1677`);
  **no path sets `prompt_version_hash` or `context_bundle_hash`.** The
  adjacent `.tool-summary` merge (`:1693-1708`) demonstrates the exact,
  in-place, best-effort pattern this patch reuses.
- `mini_ork/trace_store.py:184-191` — the payload dict (`:184-188`) omits both
  hash keys; the `extra`-merge loop (`:189-191`) copies any `extra` key into
  the payload when absent/falsy, so once `_tf` sets the two keys they flow to
  the row without touching `trace_write_node` at all.
- Concrete offenders (from lens-bottleneck, this run's DB): researcher lens
  trace `tr-researcher-arch_lens-4418b76a` (`duration_ms=367788`,
  `cost_usd=1.785704`, final artifact written) with empty hashes;
  `tr-researcher-arxiv_lens-9fee8d64` (`cost_usd=1.3366`) with empty hashes.
- Matches the top 4 known_failure_modes in `context-pack.json` at conf.
  0.96–0.98 (`cross_class:agent.researcher.prompt` — artifact with empty
  prompt/context hashes) and the run's own learned-failure ledger
  (`tr-researcher-arxiv_lens-9fee8d64`: "both prompt_version_hash and
  context_bundle_hash are empty strings").
- arXiv (from `lens-arxiv.md`): **2605.17169** (Hu et al., *Responsible
  Agentic AI Requires Explicit Provenance* — the causal-attribution function
  is precisely a per-trace provenance key; provenance is a structural
  prerequisite, not optional); **2607.00941** (*Evidentiary-Adequacy
  Criterion* — an empty-hash trace fails the completeness property);
  **2606.07889** (*Strained Coherence* — a success trace whose provenance
  fields are empty is the pre-failure signal these hashes make detectable).

**Proposed change.** Compute both hashes from inputs that are **provably
present at write time**, so the patch is not blocked on any upstream producer
(the open question raised by lens-perf Q2 and lens-arch Q3 — "are the hashes
computed upstream or dropped?" — is dissolved by computing them here):

1. Add a small helper in `mini_ork/cli/execute.py`, e.g.
   `_provenance_hashes(node_id, node_type, task_class, run_dir) -> tuple[str, str]`:
   - `context_bundle_hash = "sha256:" + sha256(context_pack_bytes)` where
     `context_pack_bytes` is the byte content of `<run_dir>/context-pack.json`
     (confirmed present in this run dir, 12.4 KB). This is the bundle
     *version* identity: stable across identical bundles, changes when the
     bundle changes.
   - `prompt_version_hash = "sha256:" + sha256(prompt_file_bytes)` where
     `prompt_file_bytes` is the node's recipe prompt under
     `recipes/<recipe>/prompts/<node>.md`, resolved via the existing
     recipe/node→file logic already present in `execute.py` (the same
     resolution `_researcher_output_file` at `:1287-1319` relies on). This is
     the *prompt version*, not the per-run rendered string — matching the
     semantics the perf lens already assumed (`lens-perf-prompt-…-v1` =
     "deterministic copy of the recipe prompt").
   - Best-effort, mirroring `:1707-1708`: any resolution/IO failure returns
     `("", "")` for the affected field and never raises. When a hash can't be
     computed, also set `extra["prompt_hash_source"] = "unavailable"` so an
     empty hash is *explained* rather than silently indistinguishable from a
     bug (satisfies 2607.00941's atomicity/immutability intent without a
     schema change).
2. In `_tf` (`execute.py:1662-1672`), after building `extra`, call the helper
   and set `extra["prompt_version_hash"]` / `extra["context_bundle_hash"]`.
3. **Verify the persistence column-mapping** before claiming success: confirm
   the `INSERT` in `trace_store.py` maps payload keys `prompt_version_hash` /
   `context_bundle_hash` to their columns (the schema columns exist per all
   three lenses). If the INSERT drops unknown keys, add the two columns to the
   mapping — still within the LOC budget. Do **not** ship the `_tf` change
   without confirming the row actually persists the value (otherwise it is the
   no-op the perf lens warned about).

Estimated size: helper + two assignments + INSERT-mapping check + tests ≈
40–70 LOC. Comfortably under the 200-LOC ceiling; additive (new metadata
fields, empty-string default preserved, no existing arithmetic touched).

**Regression test.** New file `tests/test_trace_provenance_hashes.py`:

- `test_tf_stamps_nonempty_provenance_hashes` — dispatch a node through the
  `_tf` path (or the smallest integration that writes a row) with a known
  recipe prompt file and a known `context-pack.json`; assert the persisted
  `execution_traces` row has
  `prompt_version_hash != "" and prompt_version_hash is not None and
  context_bundle_hash != ""`.
- `test_provenance_hashes_are_stable` — two writes with the *same* prompt file
  and *same* context-pack produce **identical** `prompt_version_hash` and
  `context_bundle_hash` (attribution must not drift across identical runs).
- `test_prompt_hash_changes_on_prompt_edit` — mutate one byte of the node's
  prompt file; assert `prompt_version_hash` changes while `context_bundle_hash`
  stays constant (sensitivity + independence of the two axes).
- `test_learning_loop_filter_now_selects_rows` — after a self-improve node
  writes, assert
  `SELECT count(*) FROM execution_traces WHERE task_class='recursive_self_improve'
   AND prompt_version_hash <> '' AND prompt_version_hash IS NOT NULL >= 1`
  (the exact predicate GEPA/rho_aggregator use — proves the loop is no longer
  starved).
- `test_hash_failure_is_best_effort` — point prompt resolution at a missing
  file; assert `_tf` does **not** raise and the row is still written with
  `prompt_hash_source == "unavailable"`.

**Verification.** `python3 -m pytest -q` must stay green (the kickoff's only
verification command). Existing trace/dispatch/learning tests must pass
unchanged — the change is additive metadata; no aggregator reads these fields
for arithmetic, so cost/duration/percentile numbers are byte-identical.
Expected benchmark delta: the count of self-improve rows satisfying the GEPA
filter moves **0 → N**, where N ≈ the number of LLM-backed nodes dispatched
per run (this recipe's plan dispatches 13 nodes; expect a step increase of
that order per iter, sign strictly non-negative). No expected change to
`self-tests-pass` or `no-regression` beyond the new tests passing.

**Rollback criteria.** Discard the patch if any of:
- (a) any existing test regresses, or the new `INSERT` mapping rejects a
  payload (column-type mismatch);
- (b) `test_provenance_hashes_are_stable` fails — non-deterministic hashes
  would *poison* attribution, which is worse than empty hashes;
- (c) hash computation raises on any node despite the best-effort guard
  (must behave like the `:1707-1708` `except: pass`);
- (d) the persisted value is still empty after the change (the no-op failure
  mode — means the source material or column mapping was mis-identified;
  fix-forward rather than ship a cosmetic diff).

## Lower-ranked patches

### Patch 2: Structured `failure_detail` on verifier output (correctness, ~15–25 LOC)
`mini_ork/cli/verify.py:295-303` already builds a `results` array with
`pass_count`/`fail_count`; `_safe_trace_write` (`:307-313`) throws it away and
persists `{"verdict": verdict}` only. Extend the write to carry `results` plus
a `failure_detail` list — one entry per failed check
`{rule_id, expected, observed, missing_inputs, evidence_path}`. Directly fixes
the learned-failure-mode `tr-verify-1785858934-69675`
("verifier_output is only {\"verdict\": \"fail\"}") and its siblings.
**Regression test:** `verify.py --artifact` with one failing check →
`execution_traces.verifier_output` contains `failed_rule_ids: list[str]` and
`results: list[dict]`, not bare verdict. arXiv: 2607.28871 (evidentiary-role
classification is the verbatim `failure_detail` shape), 2412.13114
(assume-guarantee: empty detail is a broken guarantee), 2608.02011 (Read-Gate:
`verdict="fail"` requires non-empty detail). Back-compat: existing consumers
that key on `verifier_output.verdict` are unaffected.

### Patch 3: `cost_observed` / `duration_observed` flags (perf+correctness, ~30 LOC + 5 guards)
`trace_write_node` (`trace_store.py:169-182`) pre-inits `cost=0.0`/
`duration_ms=0` and the sidecar-absent branch silently leaves them at 0 — 3
semantic states (ran in 0ms, sidecar never written, sidecar wrote 0) collapse
to one value. Set `cost_observed = bool(c)` / `duration_observed = bool(d)` and
emit them in the payload; guard the 5 arithmetic consumers
(`context_assembler.py:332-333`, `otel_export.py:96,101,239,258,266`) with
`WHERE cost_observed` (or `cost_observed OR cost_usd=0`) so unobserved-zero is
excluded from sums/percentiles. Prefer the JSON-envelope form if Patch 3 ships
alone; a `0043_*` nullable migration only if it lands with a consumer that
needs `WHERE`. arXiv: 2605.08747 (VIGIL W/B decomposition — "measured" vs
"reported" is exactly `*_observed`), 2604.05119 (typed telemetry beats coerced
zeros). **Regression test:** an aggregator skips `duration_ms=0` rows when
`duration_observed=False`. This is prior-iter (`20260804165336`) rank 2, never
landed — re-ranked here, not a third variant.

### Patch 4: Implementer provenance gate (correctness, dual-surface)
(a) `recipes/recursive-self-improve/prompts/implementer.md` mandates a
`## Provenance` block: one `{path, op: read|set|write, source, justification}`
per `files_written` entry. (b) `trace_write_node` downgrades `status` from
`success` to a new terminal `needs_provenance` when
`cost_usd > MO_PROVENANCE_COST_FLOOR (default 0.05) AND tool_calls=[] AND
files_read=[] AND files_written non-empty`. Fixes
`tr-implementer-implementer-f6b6c79a` (`cost_usd=1.308`, 101 writes, empty
ledger). arXiv: 2604.04035 (causality laundering — the 101-path list *is* the
laundered audit trail), 2603.14688 / 2604.17557 (causal-graph data shape).
Wider blast radius (new status + patch-critic must accept
`status IN ('success','needs_provenance')`) → deferred behind the two narrower
correctness patches.

### Patch 5: Drop the legacy `verifier_` mirror prefix (arch, ~1 LOC + cleanup)
`execute.py:1385` mirrors both `("verifier_", "verifier-")`; both byte-
identical files are written every run (confirmed on disk in
`20260804165336`), while every canonical reader
(`self_improve.py:91`, `web/why.py:91`, `run_detail.py:169`) reads the hyphen
form only. Drop `"verifier_"`; canonicalize any legacy source read-only. No
arXiv required (this is *removal*, not new infra — ranking rule 4 N/A).
**Scope caveat:** `framework_edit` (out of scope per kickoff) shares this
mirror path; the fix is recipe-agnostic at `:1385`, so grep
`recipes/framework-edit/` for a live `verifier_` writer before merging.

## Convergence assessment

**Not converged — continue the loop.** Two signals point in opposite
directions and the net is "more headroom, different surface":

- *Converging on the old surface:* the previously dominant failure pattern
  `pat-c694ab5f46f0` (recursive_self_improve failure) collapsed **freq 28 → 3**
  over the 8 days / 10 follow-up merges between `421a6862` and HEAD
  `83553a58` (verifier-triple reconnection, real pytest, kickoff scope).
- *New load-bearing surface:* telemetry/provenance collapse — dormant while
  the old failures dominated — is now the top defect, and it is *self-
  defeating* (the learning loop that would drive convergence is filtered to
  empty by the very gap Patch 1 fixes). Until Patch 1 lands, the loop cannot
  demonstrate diminishing returns because it receives no rows to learn from.

Recommendation: run at least one more iteration **after** Patch 1 to confirm
the learning loop begins ingesting rows (the `test_learning_loop_filter_now_
selects_rows` predicate turning non-zero in production is the convergence
gate), then re-assess with Patches 2–4 queued. The outer loop should **not**
terminate after this iteration.

## Provenance footer

- Lenses consumed: minimax (perf, arch) / kimi (correctness) / codex
  (bottleneck, arxiv) — all present; none degraded.
- Synthesizer family: opus.
- arXiv papers cited: 11 (2605.17169, 2607.00941, 2606.07889, 2607.28871,
  2412.13114, 2608.02011, 2605.08747, 2604.05119, 2604.04035, 2603.14688,
  2604.17557) — all appear in `lens-arxiv.md`; none invented.
- Cross-iteration learnings applied: 4 — prior synthesis
  `20260804165336` (its unfielded rank-2 duration-coercion re-ranked to Patch
  3, not re-litigated as new); `learning_record` open rows 2/10/11/12
  (consulted, none patchable on the `mini_ork/`+`recipes/`+`schemas/` surface
  per kickoff scope); pattern-store collapse `pat-c694ab5f46f0` 28→3; the
  run's 5 learned failure modes (top 4 map 1:1 to Patch 1's symptom cluster).
- Synthesizer-verified against live source (files read this node):
  `mini_ork/trace_store.py:140-209`, `mini_ork/cli/execute.py:1650-1719` —
  corrected the lens symbol name `build_trace_payload` → `trace_write_node`.
- Scope honored: no patch touches `.mini-ork/` run state or the
  `framework_edit` recipe surface.
