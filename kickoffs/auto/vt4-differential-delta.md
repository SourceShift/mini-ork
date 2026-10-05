# VT4 differential behavioural-equivalence term in the Assay oracle (G10-T02)

## Goal

`oracle.judge` proves a patch on ONE reproduction pair (PoC+ red→green) plus invariants/relations inside the bug's domain;
nothing executes inputs OUTSIDE it, so a patch that fixes the bug and breaks neighbouring behaviour in the same function is
PROVEN today (demo: a `median` that always averages the two middle values fixes `[1, 2, 3, 4]`, returns 1.5 for `[1, 2, 3]`).
Add a differential term: the LLM proposes ONE input suite partitioned `bug_domain` / `preserve`; the runner executes the SAME
file on base and on the patch; code compares canonical observations; a confirmed `preserve` divergence (collateral damage) →
REFUTED with the input and both outputs. Evidence: arXiv 2610.00182 (13 unique faults from 18 designed fixtures), 2602.15761
(functional-equivalence failures predefined tests miss); priority #4 of the 2026-10 verification review. Post-merge measure:
correct-fix rejections on the replay set must not rise from 3/6 (veto-only ⇒ precision is the cost); faults caught that
today's gate passes. Doctrine: verdicts anchor on what code DID; the LLM proposes, execution decides, no LLM approves; PROVEN |
REFUTED | UNVERIFIED; an input not observed on BOTH sides is excluded and never counts as agreement.
Settled from the code: `Crucible.run_test` runs `pytest -q` (stdout captured), keeps `output[-800:]` ⇒ the harness emits via
`atexit` + `os.write(1, …)`, landing after pytest's summary (verified locally), one file per case. Equality = sha256 of
in-sandbox canonical JSON; `obs` (≤300 chars) is evidence only. fail@base ∧ pass@head already holds for the PoC (hook runs only
on a would-be PROVEN) and is re-established ON THE SUITE as the ANCHOR (≥1 `bug_domain` case diverges), else no veto. No
rescue, no strict mode (abstention never changes the verdict), no tolerance rules (the equivalence-operator feature owns them).

## Mechanism (exact spec)

1. New `differential.py`, styled like relations.py (law in docstring, `DispatchFn`, lazy `_default_dispatch()`, injectable
   `dispatch`; copy `_truthy`, never import relations). Env read at CALL time: `enabled()` ⇔ `MO_ASSAY_DIFFERENTIAL` ∈
   {1,true,yes,on} (case-insensitive; DEFAULT OFF = kill switch engaged); `n_from_env()` = int `MO_ASSAY_DIFFERENTIAL_N`,
   default 6, clamped [2, 8], non-int → 6; `veto_min_from_env()` = int `MO_ASSAY_DIFFERENTIAL_VETO_MIN`, default 2, clamped
   ≥ 1, non-int → 2 (ONE mislabelled `preserve` input must never sink a correct fix, the invariants' supermajority rule). `split(n) -> (b, p)`: `b = max(1, n // 3)`, `p = n - b`.
2. `DIFF_PROMPT` (+ `DIFF_CONTEXT_SUFFIX` shaped like `REL_CONTEXT_SUFFIX`) gets issue[:1800], PoC[:1400], patch[:2500] (read
   adversarially); asks for ONE ```python block: a plain module (NOT a test) importing the code under test, `CASES =
   [("bug_domain", <input>), …, ("preserve", <input>), …]` (b `bug_domain`, the FIRST = the issue's reported input, then p
   `preserve`) and ONE `def observe(x):` calling the code on x and RETURNING JSON-serialisable plain data; no asserts,
   randomness, time or IO; exceptions propagate. `preserve` = the issue's wrong behaviour CANNOT occur, so a correct fix
   returns EXACTLY the buggy output (same type and representation); prefer inputs through the lines the patch changes. MUST
   contain `DIFFERENTIAL INPUT SUITE`; MUST NOT contain `METAMORPHIC RELATIONS`, `states_expected_behaviour`, `Write ONE
   pytest test`, `MO_DIFF`, or match `Write\s+\d+\s+pytest tests` (existing test stubs key on those).
3. `admissible(src, context: CodeContext | None = None) -> tuple[bool, str]`: parses; no `MO_DIFF` substring; exactly one
   module-level `CASES = <ast.List|ast.Tuple>` of ≥ 2 2-tuples whose first is a str Constant in {"bug_domain","preserve"};
   ≥ 1 of each; no two inputs with equal `ast.dump`; exactly one top-level `def observe` with one positional arg (no
   *args/**kwargs); no top-level `def test*`/`class Test*`; `CASES`/`observe` bound nowhere else (incl. `global`, args); if
   `context.modules`, `imports_code_under_test(src, context.modules)`. `cases(src)` → `[(partition, ast.unparse(input)[:300])]`
   in CASES order: inputs come from the code that RAN, never model prose.
4. `build(poc, issue, patch="", *, n=4, tries=2, dispatch=None, context=None) -> tuple[str | None, str | None, list[dict]]` =
   `(suite_name, suite_src, rejected)`: blocks named `suite_1, suite_2, …` across tries; FIRST admissible wins; rejected
   `{"name","status":"inadmissible","why","src"}`; retry (feedback = whys) only while none admitted; empty output or a raising
   dispatch = no blocks for that try.
5. `harness(src, case: int) -> str` = `src.rstrip() + "\n\n\n"` + exactly this block, `{case}` substituted via `str.replace`
   (NOT `str.format`: the block has dict braces):
   ```python
   # -- mini-ork differential harness (appended by differential.py; not model-authored) --
   MO_DIFF_CASE = {case}

   def test_mo_differential_observe():
       import atexit, hashlib, json, os
       value = observe(CASES[MO_DIFF_CASE][1])
       canon = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
       line = json.dumps({"case": MO_DIFF_CASE, "obs": canon[:300],
                          "sha256": hashlib.sha256(canon.encode()).hexdigest()}, sort_keys=True)
       atexit.register(os.write, 1, ("MO_DIFF_OBS " + line + "\n").encode())
   ```
6. `parse_obs(o: ExecOutcome, case) -> tuple[dict | None, str]`: status ≠ `passed` → `(None, "<status> (<exc>)")`; lines
   `^MO_DIFF_OBS (\{.*\})\s*$` (re.M): none → "no observation line", > 1 → "ambiguous observation"; bad JSON, wrong `case`,
   `sha256` not 64 hex, `obs` not str → "malformed observation"; else `({"obs","sha256"}, "")`.
7. `select(cases, n)` = first b `bug_domain` + first p `preserve` indices, in index order. Per case i, `h = harness(src, i)`:
   base `run_test(h)`; raise/unparsed → `excluded` (why `base: …`), head NOT run. Head `run_test(h, patch=patch)`;
   raise/unparsed → `excluded` (`head: …`). Equal sha256 → `agree`. Unequal: `bug_domain` → `diverged`; `preserve` → CONFIRM
   (re-run base once, then head once; both reproduce their first sha256 → `diverged`, else `excluded`, `nondeterministic
   observation (base|head)`).
8. `decide(records, *, veto_min) -> (verdict, state, reason)`: no `bug_domain` `diverged` → `(None, "unverified", "no
   bug_domain input diverged: the suite does not observe what the patch changed")`; preserve `diverged` ≥ veto_min →
   `(REFUTED, "refuted", "differential: collateral divergence on preserve input <name>: input=<input> base=<base_obs>
   head=<head_obs> (<d> of <m> observed preserve inputs diverged)")`; any other preserve divergence or zero preserve `agree` →
   `(None, "unverified", <why>)`; else `(None, "equivalent", "<a> preserve inputs agree; <k> bug_domain inputs diverge")`.
9. `check(poc, issue, patch, *, runner, dispatch=None, context=None, n=None) -> dict` returns exactly `{"n", "veto_min",
   "suite", "src", "anchored", "records", "counts", "state", "verdict", "reason"}` (no suite → `records == []`, `unverified`,
   "no admissible input suite"). Record keys: `name` (`case_<i>`), `case`, `partition`, `input`, `status`
   (agree|diverged|excluded), `why`, `on_base`, `on_head` (status|None), `base_obs`, `head_obs`, `base_sha256`, `head_sha256`.
   `counts` = `{"bug_diverged","bug_agree","preserve_agree","preserve_diverged","excluded","inadmissible"}`. Print ONE stderr
   line `[assay-differential] ` + `json.dumps(<result minus "src">, sort_keys=True)`.
10. Hook in `oracle.judge` (only oracle.py change besides `from mini_ork.certify import differential`): (a) immediately before
   `if verdict == PROVEN and relations.enabled():` add `proven_before_vetoes = verdict == PROVEN`; (b) after the relations
   block, before `return Verdict(...)`: `if proven_before_vetoes and differential.enabled(): drec = differential.check(poc_src,
   issue, patch, runner=runner, dispatch=d_fn, context=context, n=differential.n_from_env())`; `detail["differential"] = drec`;
   `if drec["verdict"] == REFUTED and verdict == PROVEN: verdict, reason = REFUTED, drec["reason"]`. Both vetoes run and are
   recorded; relations' reason wins if both veto. Never on a would-be REFUTED/UNVERIFIED nor in `if not cands:` (incl.
   relations rescue); cannot create PROVEN; `gold` never reaches it. Knobs off ⇒ no dispatch/run_test/key: byte-identical.

## Files in scope (touch ONLY these)

- /Volumes/docker-ssd/ps/mini-ork-worktrees/vt4-differential-delta/mini_ork/certify/differential.py (new): prompt, law, harness, runner, decision.
- /Volumes/docker-ssd/ps/mini-ork-worktrees/vt4-differential-delta/mini_ork/certify/oracle.py: the import + the hook (step 10).
- /Volumes/docker-ssd/ps/mini-ork-worktrees/vt4-differential-delta/tests/unit/test_certify_differential.py (new): the tests below.

Read-only references (do NOT edit), under /Volumes/docker-ssd/ps/mini-ork-worktrees/vt4-differential-delta/: mini_ork/certify/
{relations.py (style + hook precedent), invariants.py, probe.py, context.py, verdict.py}, mini_ork/runtime/engine.py
(`ExecOutcome`, `Crucible.run_test`), mini_ork/cli/certify.py (production caller), tests/unit/test_certify_relations.py
(make_dispatch, FakeRunner: copy, never import), kickoffs/auto/vt1-mr-relations.md. Do NOT modify any other file.

## Tests

Hermetic (no LLM, docker, network). Copy `make_dispatch` from test_certify_relations.py; add, after its keys and before `""`,
`"DIFFERENTIAL INPUT SUITE" in prompt` → fenced suite. `DiffRunner(ordered, cases)` records `(src, patch)` in `calls`; src
matching `MO_DIFF_CASE = (\d+)` is served from `cases[(i, "head" if patch else "base")]` (ExecOutcome or list served in order,
last repeated); other src pops `ordered` (`error` when empty). `obs_outcome(i, value)` = `passed`, output `"1 passed\nMO_DIFF_OBS
" + <line built as the harness does> + "\nCRUCIBLE_RC=0\n"`. Suite: `from stats import median`, CASES `[("bug_domain", [1, 2, 3,
4]), ("preserve", [1, 2, 3]), ("preserve", [5]), ("preserve", [3, 1, 2])]`, `observe` returns `median(x)`. Clear `MO_ASSAY_*`.
1. Static law (parametrized): suite admitted; rejected with a why: no CASES, non-literal CASES, bad label, no preserve, no
   bug_domain, duplicate inputs, no observe, 2-arg observe, top-level `def test_x`, `MO_DIFF` in source, CASES rebound, and
   (with `CodeContext("", ("stats",), ())`) no `stats` import. Built prompt carries the marker and none of the forbidden ones.
2. Transport (real pytest subprocess in `tmp_path`, `PYTEST_ADDOPTS` unset): `sys.executable -m pytest crucible_probe.py -q -p
   no:cacheprovider` on `harness(pure_suite, 1)`; `parse_obs(ExecOutcome("passed", output=(stdout+stderr)[-800:]), 1)`
   returns the canonical obs and its sha256.
3. Collateral via `oracle.judge`, knobs on: 3 invariants hold; case_0 base 3 / head 2.5; case_1 AND case_3 base 2 / head 1.5 (both runs)
   → REFUTED, reason starts `differential:`, names case_1, `[1, 2, 3]`, `2`, `1.5`; `mr_pass_rate`, `mr_n`,
   `detail["invariants"]` equal the knobs-off run. 4. Correct fix via `judge`: case_0 diverges, preserve agree → verdict AND
   reason identical to knobs-off; state `equivalent`.
5. Excluded (parametrized, `check`): base `test_defect`, base `failed`, base passed without marker, raising base `run_test` →
   `excluded`, head NOT run (assert `calls`); head `error`, malformed marker, two markers → `excluded`; never `agree`; all
   preserve excluded → `unverified`, and via `judge` PROVEN stays PROVEN.
6. Unanchored: case_0 agrees + case_1 diverges → no veto, `unverified`, `judge` stays PROVEN; base `no_run` on every case
   (unavailable base) → all excluded, `unverified`. 7. Nondeterministic: case_1 base re-run sha256 differs → `excluded`, no
   veto, exactly 4 `calls` for case_1.
8. Knobs off ⇒ byte-identical: PROVEN, REFUTED (1/4), mid-band UNVERIFIED and no-invariant scenarios, env unset vs
   `MO_ASSAY_DIFFERENTIAL=0` vs `MO_ASSAY_DIFFERENTIAL_N=6` alone → identical `json.dumps(dataclasses.asdict(v),
   sort_keys=True)`, no `differential` key, no differential prompt, identical `runner.calls`.
9. Knobs on, would-be REFUTED / UNVERIFIED / no-invariant branch (also relations+rescue on) → no differential prompt or key.
10. Relations + differential on, via `judge`: both veto → relations reason, both keys, differential verdict REFUTED; relations
   hold + collateral → differential reason; relations violated + differential equivalent → relations reason, key present.
11. `MO_ASSAY_DIFFERENTIAL_N=2` → 2 cases (1 + 1); `99` → 8; `abc` → 6; `1` → 2. Default VETO_MIN (2) + ONE confirmed preserve divergence → no veto (state `unverified`), PROVEN stays;
   `..._VETO_MIN=1` + one divergence → REFUTED.
12. `capsys`: exactly one `[assay-differential] ` stderr line, valid JSON with `counts`, `records`, `state`, `anchored`, no `src`.

## Success criteria

- Verification command passes; the 199 existing certify tests (oracle, relations, context, cli) pass UNCHANGED.
- No knob set ⇒ byte-identical Verdicts, no new required env var. Knobs on: differential only flips a would-be PROVEN to
  REFUTED (anchored suite, ≥ veto_min confirmed preserve divergences, input + both observations recorded). `ruff check` clean.

## Verification command

cd /Volumes/docker-ssd/ps/mini-ork-worktrees/vt4-differential-delta && python3.11 -m pytest -q -p no:cacheprovider /Volumes/docker-ssd/ps/mini-ork-worktrees/vt4-differential-delta/tests/unit/test_certify_differential.py /Volumes/docker-ssd/ps/mini-ork-worktrees/vt4-differential-delta/tests/unit/test_certify_oracle.py /Volumes/docker-ssd/ps/mini-ork-worktrees/vt4-differential-delta/tests/unit/test_certify_relations.py /Volumes/docker-ssd/ps/mini-ork-worktrees/vt4-differential-delta/tests/unit/test_certify_context.py /Volumes/docker-ssd/ps/mini-ork-worktrees/vt4-differential-delta/tests/unit/test_certify_cli.py

## Review bar

Producers: `poc_src` ← `probe.build` inside `judge` (or the caller's validated `poc`); `issue` ← `cli/certify.py:_read_issue`;
`patch` ← `_read_patch`; `runner` ← `Crucible(RuntimeSpec(image=image_tag, workdir=args.workdir))` in `cli/certify.py:main`;
`dispatch` ← `judge`'s `d_fn` (`certify.llm.default_dispatch`, lane `MO_CERTIFY_MODEL`); `context` ← `code_context(repo,
base_sha, patch_text)`; knobs ← operator env; suite ← `differential.build` via `dispatch` only; observations ← the appended
harness run by `Crucible.run_test` (`MO_DIFF_OBS` in `ExecOutcome.output`), never model prose. Tests 3, 4, 6, 10 drive
`oracle.judge`. Reject: an LLM call that approves; equality other than sha256 of the harness's canonical JSON; any
tolerance/normalisation; an excluded input counted as agree; a veto without the anchor or the confirm re-run; edits to
`replay_check`, `mr.score` or the C6 block; the hook in the `if not cands:` branch.

## Rules

Edit files directly; do not emit unified diffs. Do NOT touch relations.py, invariants.py, probe.py, verdict.py, context.py,
certificate.py, cli/certify.py, `replay_check`, any recipe, or any existing test. No new `mini-ork` subcommand, no new dependency.
