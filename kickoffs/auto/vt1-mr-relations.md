# VT1 metamorphic relations as an execution-anchored check in the Assay oracle (G10-T01)

## Goal

`oracle.judge` proves patches with perturbed REPRODUCTION tests (`invariants.build`, each must FAIL ON BASE), never a relation
BETWEEN two executions, and abstains when it cannot build ≥2: a main recall-hole source (SWE-bench v2 recall 21/25 = 84%,
docs/RESULTS.md §3). Add metamorphic relations: the LLM proposes k spec-derived (SOURCE, transform T, FOLLOWUP = T(SOURCE),
relation R over the two outputs); a deterministic runner executes BOTH inputs and evaluates R. A patch special-casing the
reported input fixes SOURCE but not FOLLOWUP → R breaks → REFUTED with the pair. Evidence: arXiv 2602.10522, 2603.24774 (+6).
Doctrine: verdicts anchor on what code DID; the LLM proposes, execution decides, no LLM approves; PROVEN | REFUTED |
UNVERIFIED; a relation that cannot execute ABSTAINS and never counts as holding.

## Mechanism (exact spec)

1. New module `relations.py`, styled like invariants.py (law in the docstring, `DispatchFn`, lazy `_default_dispatch()`,
   injectable `dispatch`). Env read at CALL time: `enabled()` ⇔ `MO_ASSAY_RELATIONS` ∈ {1,true,yes,on} (case-insensitive;
   DEFAULT OFF = kill switch engaged); `rescue_enabled()` ⇔ same parse of `MO_ASSAY_RELATIONS_RESCUE` (default off; ignored
   unless `enabled()`); `k_from_env()` = int `MO_ASSAY_RELATIONS_K`, default 3, clamped [1, 5], non-int → 3;
   `veto_min_from_env()` = int `MO_ASSAY_RELATIONS_VETO_MIN`, default 1, clamped ≥ 1.
2. `REL_PROMPT` (+ `REL_CONTEXT_SUFFIX`, shaped like `MR_CONTEXT_SUFFIX`) gets issue[:1800], the validated PoC[:1400], the
   patch[:2500] (read adversarially, as MR_PROMPT does) and asks for exactly k SEPARATE ```python blocks, each a standalone
   pytest file importing the code under test with module-level `SOURCE = <input where the bug manifests>`,
   `FOLLOWUP = <T(SOURCE), may be an expression of SOURCE>`, `TRANSFORM = "<one line>"`, `RELATION = "<one line>"` and ONE
   `def test_relation():` that calls the code on SOURCE and on FOLLOWUP and `assert`s R between the two outputs. R must follow
   from the CORRECT behaviour the issue states (never today's output); FOLLOWUP stays in the bug's domain but off whatever
   the patch special-cases. The prompt MUST contain `METAMORPHIC RELATIONS` and MUST NOT contain `states_expected_behaviour`,
   `Write ONE pytest test`, or match `Write\s+\d+\s+pytest tests` (existing test stubs key on those).
3. Static law `admissible(src: str, context: CodeContext | None = None) -> tuple[bool, str]`, else `(False, why)`: parses;
   exactly one top-level `def test_*`; exactly one module-level assignment each to `SOURCE` and `FOLLOWUP`; their value
   `ast.dump`s differ and FOLLOWUP is not bare `SOURCE` (two DISTINCT inputs); the test loads BOTH names and has ≥1
   `ast.Assert`; neither name is rebound elsewhere (incl. `global`); `TRANSFORM`/`RELATION` are non-empty module-level str
   constants; if `context.modules`, `context.imports_code_under_test(src, context.modules)` holds (import it, never copy it).
   `describe(src) -> dict` → str `transform`, `relation` (≤200 chars), `source`, `followup` (`ast.unparse` of the assigned
   expressions, ≤300 chars): the pair comes from the code that RAN, never from model prose.
4. `build(poc, issue, patch="", *, k=3, tries=2, dispatch=None, context=None) -> tuple[list[tuple[str, str]], list[dict]]`:
   extract EVERY fenced python block, name them `rel_1, rel_2, …` in emission order across tries, split into admitted
   `(name, src)` and rejected `{"name","status":"inadmissible","why","src"}`; retry (feedback = the whys) only while
   admitted < k; return ≤ k admitted. Empty model output → `([], [])`.
5. `classify(on_patch: ExecOutcome, on_base: ExecOutcome | None) -> tuple[str, bool]` → `(status, repaired)`. Per admitted
   relation, in order: `on_patch = runner.run_test(src, patch=patch)`. Unless `passed`, or `failed` with `exc` in
   (`AssertionError`, `Failed`): `abstained`, base NOT run (test_defect / error / no_run / TypeError; a raising `run_test`
   also abstains, `why` names it). Else `on_base = runner.run_test(src)`: patch passed → `held` (`repaired` iff base
   assertion-failed); patch assertion-failed and base `passed` → `violated` (the patch BROKE a relation the unpatched code
   kept); otherwise `violated_unattributed`. Base only ATTRIBUTES, never admits: relations that hold on base are exactly
   the ones a special-cased patch breaks.
6. `decide(records, *, veto_min: int, rescue: bool) -> tuple[str | None, str]`: count(`violated`) ≥ veto_min → `(REFUTED,
   reason naming the first violated relation, RELATION, TRANSFORM, source/follow-up pair)`; elif `rescue` and held ≥ 2 and
   repaired ≥ 1 and violated == violated_unattributed == 0 → `(PROVEN, "PROVEN relative to <held> relations: …")`; else `(None, "")`.
7. `check(poc, issue, patch, *, runner, dispatch=None, context=None, k=None, rescue=False) -> dict` = build + execute + decide,
   returning exactly `{"mode": "veto"|"rescue", "k", "veto_min", "records", "counts": {"held", "repaired", "violated",
   "violated_unattributed", "abstained", "inadmissible"}, "verdict": "REFUTED"|"PROVEN"|None, "reason"}`. Record keys:
   `name, status, why, transform, relation, source, followup, on_patch, on_base, repaired, src`, plus `pair: {"source",
   "followup"}` and `detail` (tail ≤600 of exc+output on the patch) ONLY for `violated*`. Print ONE stderr line
   `[assay-relations] ` + `json.dumps(..., sort_keys=True)` of the dict minus `src`/`detail` (live-run observability).
8. Hook in `oracle.judge` (the only oracle.py change besides `from mini_ork.certify import relations`): (a) after the C6
   block, if `verdict == PROVEN and relations.enabled()`: `rec = relations.check(poc_src, issue, patch, runner=runner,
   dispatch=d_fn, context=context, k=relations.k_from_env())`; `detail["relations"] = rec`; if `rec["verdict"] == REFUTED`
   → `verdict, reason = REFUTED, rec["reason"]` (`mr_pass_rate`, `mr_n`, invariants untouched). Never runs on a would-be
   REFUTED/UNVERIFIED; cannot create PROVEN. (b) In the `if not cands:` branch, only if `enabled() and rescue_enabled()`:
   `rec = check(..., rescue=True)`; return `Verdict(rec["verdict"] or UNVERIFIED, rec["reason"] or <the existing reason
   verbatim>, poc_plus=poc_src, detail={"relations": rec})`. `gold` never reaches relations. Knobs off ⇒ no extra
   dispatch or run_test and no `relations` key: byte-identical Verdicts.

## Files in scope (touch ONLY these)

- /Volumes/docker-ssd/ps/mini-ork-worktrees/vt1-mr-relations/mini_ork/certify/relations.py (new): generator, law, executor, decision.
- /Volumes/docker-ssd/ps/mini-ork-worktrees/vt1-mr-relations/mini_ork/certify/oracle.py: the import + the two guarded hooks (step 8).
- /Volumes/docker-ssd/ps/mini-ork-worktrees/vt1-mr-relations/tests/unit/test_certify_relations.py (new): the tests below.

Read-only references (do NOT edit), under /Volumes/docker-ssd/ps/mini-ork-worktrees/vt1-mr-relations/: mini_ork/certify/
{invariants.py (style), context.py (`CodeContext`, `imports_code_under_test`), probe.py, verdict.py}, mini_ork/runtime/engine.py
(`ExecOutcome`, `Crucible.run_test`), mini_ork/cli/certify.py (production caller), tests/unit/test_certify_oracle.py
(FakeRunner / make_dispatch: copy, never import). Do NOT modify any other file.

## Tests

Hermetic: local FakeRunner (scripted ExecOutcome per call, records `(src, patch)`), stub dispatch keyed on prompt shape (ground
JSON / PoC / `test_mr_*` / `METAMORPHIC RELATIONS` → canned relations), `monkeypatch.setenv`; no LLM, docker or network.
1. Static law (parametrized): a well-formed relation is admitted; rejected with a why: missing FOLLOWUP, identical inputs,
   `FOLLOWUP = SOURCE`, FOLLOWUP never loaded, no assert, two tests, SOURCE rebound in the test, no RELATION; with
   `CodeContext("", ("stats",), ())`: no `stats` import, and `from stats import median` + `def median`.
2. Special-cased patch via `oracle.judge`, knobs on: 3 invariants hold (would-be PROVEN); rel_1 assertion-fails on the patch
   and passes on base → REFUTED, reason names rel_1, record `violated`, `pair` == the unparsed SOURCE/FOLLOWUP, `detail`
   non-empty; `mr_pass_rate`, `mr_n`, `detail["invariants"]` equal the knobs-off run. 3. Correct patch via `judge`: all k
   relations hold → verdict AND reason identical to knobs-off; held == k, violated == 0.
4. Unexecutable relation: patch `test_defect`, `error`, `failed`/exc `TypeError`, raising `run_test` → `abstained`, base NOT
   run (assert `runner.calls`), never `held`; abstentions alone leave PROVEN unchanged. 5. Patch and base both
   assertion-fail → `violated_unattributed`; verdict stays PROVEN.
6. Knobs off ⇒ byte-identical: PROVEN, REFUTED (1/4), mid-band UNVERIFIED and no-invariant scenarios with env unset vs
   `MO_ASSAY_RELATIONS=0` vs `MO_ASSAY_RELATIONS_RESCUE=1` alone → identical `json.dumps(dataclasses.asdict(v), sort_keys=True)`,
   no `relations` key, no relations prompt dispatched, identical `runner.calls`.
7. Knobs on, invariants REFUTE (1/4) → no relations prompt, no `relations` key; no-invariant branch, rescue off → unchanged.
8. Rescue bar via `judge`, no-invariant branch, both knobs on: 3 held incl. 1 repaired → PROVEN, "relative to" in reason;
   3 held, 0 repaired → UNVERIFIED, original reason; 2 held + 1 violated → REFUTED; 1 held + 2 abstained → UNVERIFIED.
9. `MO_ASSAY_RELATIONS_K=2` → ≤2 relations executed; `99` → 5; `abc` → 3. 10. `capsys`: exactly one `[assay-relations] `
   stderr line, valid JSON with `counts` and `records`, no `src`.

## Success criteria

- The verification command passes; tests/unit/test_certify_oracle.py passes UNCHANGED (171 certify tests green at base).
- No knob set ⇒ byte-identical Verdicts, no new required env var. Knobs on: relations only flip a would-be PROVEN to REFUTED
  (≥ veto_min attributed violations, pair recorded); a relations PROVEN exists only under RESCUE=1. `ruff check` clean.

## Verification command

cd /Volumes/docker-ssd/ps/mini-ork-worktrees/vt1-mr-relations && python3.11 -m pytest -q /Volumes/docker-ssd/ps/mini-ork-worktrees/vt1-mr-relations/tests/unit/test_certify_relations.py /Volumes/docker-ssd/ps/mini-ork-worktrees/vt1-mr-relations/tests/unit/test_certify_oracle.py /Volumes/docker-ssd/ps/mini-ork-worktrees/vt1-mr-relations/tests/unit/test_certify_context.py /Volumes/docker-ssd/ps/mini-ork-worktrees/vt1-mr-relations/tests/unit/test_certify_cli.py

## Review bar

Every new input has a production producer: `poc_src` ← `probe.build` inside `judge` (or the caller's validated `poc`);
`patch` ← `cli/certify.py:_read_patch`; `runner` ← `Crucible(RuntimeSpec(...))` in cli/certify.py; `dispatch` ← `judge`'s
`d_fn` (`certify.llm.default_dispatch`, lane `MO_CERTIFY_MODEL`); `context` ← `code_context` in cli/certify.py; knobs ←
operator env. Relation sources come ONLY from `relations.build` via `dispatch` (no seeded-only wiring); tests 2, 3, 8 drive
`oracle.judge`. Reject: an LLM call that approves; an abstained or non-assertion outcome counted as held or violated; base
used to admit or drop a relation; any edit to `mr.score` or the C6 block.

## Rules

Edit files directly; do not emit unified diffs. Do NOT touch invariants.py, probe.py, verdict.py, context.py, certificate.py,
cli/certify.py, any recipe, or any existing test. No new `mini-ork` subcommand, no new dependency.
