# Wire the orphaned function-level metamorphic engine into the live behavioral verifier as `surface="function"`

## Goal

`mini_ork/learning/metamorphic.py` + `mini_ork/learning/metamorphic_proposer.py`
are a complete, tested, *pure* metamorphic engine (relation library + safe
LLM-proposal boundary) with **zero production callers** — verified: nothing under
`mini_ork/` imports them; only `tests/unit/test_metamorphic*.py` do. Give that
engine a single live entry point by registering it as a new behavioral
**`surface="function"`** handler in `mini_ork/verify/behavioral.py`, so a
function-level metamorphic check fires through the already-wired
`BehavioralVerdict → verdict_reward → committee/catalog` path.

This is purely ADDITIVE wiring: turn dead-but-tested code into a live verifier
surface. It does NOT delete or move the engine, and it does NOT touch the
separate Layer-0 aggregator path (see the hard invariant on the two reward
consumers — that path is load-bearing and stays exactly as-is).

## Scope Allow (files the implementer MAY edit — these are in scope)

- `mini_ork/verify/behavioral.py`
- `mini_ork/verify/catalog.py`
- `tests/unit/test_behavioral_function_surface.py` (NEW)

## Grounding (verified in the live tree)

- `behavioral.py`: `_SURFACES = ("api", "ui", "journey")` (line 75);
  `register_surface_handler(surface, handler)` (line 834) is the OCP seam;
  `run()` dispatches via handler-signature inspection, injecting only
  `requester`/`driver` (lines 858-867); `BehavioralVerdict.to_json()` emits
  `{"verifier":"behavioral","surface",...,"status","pass": status==PROVEN,...}`
  (line 245); `_resolve(checks)` → REFUTED if any `ok is False`, else UNVERIFIED
  if any `ok is None`, else PROVEN (line 794); `observable_from_env` builds the
  Observable from `MO_OBSERVABLE_SPEC` (file) or `MO_BEHAV_*` vars (line 891);
  module docstring's hard rule: **import-time is pure stdlib** — httpx/yaml are
  imported lazily inside functions (lines 34-37).
- `metamorphic.py` (pure engine, no IO/dispatch): `check(fn, seed_inputs,
  relations, *, check_immutability=True, max_counterexamples=50) ->
  MetamorphicResult` (line 119); `MetamorphicResult` has `.checks`,
  `.violations`, `.per_relation` (`{name: {checks, violations}}`),
  `.counterexamples`, `.passed`, `.pass_fraction`; `RELATION_LIBRARY`
  (name→factory, the safe whitelist, line 245); `resolve_relations(names)` drops
  unknown names — **the boundary that keeps LLM-proposed relations to audited
  code** (line 253); `UNIVERSAL_RELATIONS = (determinism(),)` (line 236).
- `metamorphic_proposer.py`: `build_proposer_prompt(function_name,
  function_source)` (line 24), `parse_proposal(text) -> {relations, seed_inputs}`
  (line 77, filters relation names to `RELATION_LIBRARY`, seeds to plain JSON
  only via `_is_json_data`). Pure — the LLM dispatch happens in the caller.
- `catalog.py`: `_SURFACE_ENUM = ("api", "ui", "journey", "")` (line 38);
  `VerifierCard.surface` must be one of it or `__post_init__` raises (lines 93-96).
- `reward.py`: `verdict_reward(status)` maps PROVEN→1.0, REFUTED→0.0,
  UNVERIFIED→None — **surface-agnostic** (keys on status only). This is the
  behavioral verifier's native, three-valued reward path. DO NOT edit it.

## The deliverable (exact target shape)

### 1. `behavioral.py` — add the `function` surface

- Add `"function"` to `_SURFACES`.
- Extend the `Observable` dataclass with function-surface fields:
  `module: str = ""`, `function: str = ""`,
  `seed_inputs: list = field(default_factory=list)`,
  `relations: list[str] = field(default_factory=list)`.
- `Observable.from_mapping`: parse the four new fields. `seed_inputs` must be a
  list; each element must be plain JSON data (reject callables/objects —
  mirror the proposer's `_is_json_data` shape). `relations` must be a list of
  strings; when `surface == "function"`, validate each name against the
  metamorphic library (lazy `from mini_ork.learning import metamorphic as mm`;
  reject unknown names with `ObservableError`, mirroring the api-surface
  `_METAMORPHIC` validation at lines 176-181). The `from mini_ork.learning`
  import MUST be lazy (inside the branch), never at module top — import-time
  purity is a hard invariant.
- Add `run_function_check(obs: Observable) -> BehavioralVerdict`:
  1. If `module`/`function` not declared → UNVERIFIED ("nothing to check").
  2. Import the target lazily: `importlib.import_module(obs.module)` then
     `getattr(mod, obs.function)`. On `ImportError`/`AttributeError`/not-callable
     → **UNVERIFIED** (surface unreachable — cannot exercise; abstain, not fail).
  3. Resolve relations: if `obs.relations` → `mm.resolve_relations(obs.relations)`;
     elif the proposer path is enabled (see §2) → propose; else default to
     `list(mm.UNIVERSAL_RELATIONS)`.
  4. If `not obs.seed_inputs` → **UNVERIFIED** ("no seed inputs — cannot anchor").
  5. `res = mm.check(fn, obs.seed_inputs, relations, check_immutability=True)`.
  6. Map `MetamorphicResult` → `BehavioralVerdict`: build one `Check` per entry in
     `res.per_relation` (including the pseudo-relation `input_immutability`):
     `ok = (slot["violations"] == 0)`; on violation set `ok=False` and put the
     matching `res.counterexamples` into `detail`. If `res.checks == 0` (vacuous:
     no relation ran) → a single `Check(..., ok=None, ...)` so `_resolve` yields
     UNVERIFIED (a check that examined nothing must never PROVEN). Return
     `BehavioralVerdict(_resolve(checks), "function", checks,
     evidence=_summarize(...), target=f"{obs.module}.{obs.function}")`.
- Extend `observable_from_env` so the function surface is reachable via env:
  `MO_BEHAV_MODULE`, `MO_BEHAV_FUNCTION`, `MO_BEHAV_SEED_INPUTS` (JSON array),
  `MO_BEHAV_RELATIONS` (csv). (`MO_OBSERVABLE_SPEC` already supports it via
  `from_mapping`.)
- Register at the bottom next to the others:
  `register_surface_handler("function", run_function_check)`.
- Add `run_function_check` to `__all__`.

### 2. `behavioral.py` — the safe LLM-proposer path (opt-in, injectable)

Add a module-level `_propose_relations(module: str, function: str, fn) ->
list`. Default body (all lazy imports, called only at run time): read source via
`inspect.getsource(fn)`, build the prompt with
`metamorphic_proposer.build_proposer_prompt`, dispatch the LLM via
`mini_ork.dispatch` (lazy import), then
`mm.resolve_relations(metamorphic_proposer.parse_proposal(text)["relations"])`.
`run_function_check` calls it **only when `obs.relations` is empty AND
`os.environ.get("MO_BEHAV_FN_PROPOSE") == "1"`** — default OFF keeps the verifier
deterministic and network-free. `_propose_relations` is module-level so tests
monkeypatch it (no LLM in tests). The safety boundary is unchanged: only
whitelisted relation NAMES survive `parse_proposal`+`resolve_relations`; no
model-authored predicate is ever executed.

### 3. `catalog.py` — admit the new surface

Add `"function"` to `_SURFACE_ENUM` (line 38) and mention it in the surrounding
docstring surface list (lines 72-73). One-line change; no other catalog logic.
(Additive: adding an enum value cannot break an existing card.)

### 4. Tests — `tests/unit/test_behavioral_function_surface.py` (NEW)

Assert:
(a) a pure deterministic fn + `relations=["determinism"]` → PROVEN, and
    `verdict_reward(verdict.status) == 1.0`;
(b) a nondeterministic fn (reads/increments a module global) → REFUTED with a
    counterexample in the failing check detail, `verdict_reward == 0.0`;
(c) an input-mutating fn (mutates a list arg) → REFUTED via `input_immutability`;
(d) empty `seed_inputs` → UNVERIFIED, `verdict_reward is None`; unimportable
    module OR missing function → UNVERIFIED;
(e) **safe-whitelist**: a declared relation name not in `RELATION_LIBRARY` raises
    `ObservableError` at parse; AND via the proposer path (monkeypatched
    `_propose_relations` returning a mix of a valid name and a garbage/`; rm -rf`
    name) only the whitelisted relation ever runs — the garbage name never
    executes;
(f) `run_function_check(obs).to_json()` carries
    `"verifier":"behavioral","surface":"function"` and a `status` field;
(g) `get_surface_handler("function").__name__ == "run_function_check"`.

## Hard invariants (a change that violates ANY of these is WRONG)

1. **Import-time purity preserved.** `import mini_ork.verify.behavioral` must not
   import `mini_ork.learning.metamorphic`, `metamorphic_proposer`, or
   `mini_ork.dispatch` — all three are lazy, inside functions.
2. **Safe-whitelist holds by construction.** No model-authored or free-text
   relation predicate is ever executed. Relations come ONLY from
   `mm.resolve_relations` (name→vetted factory). A test must prove a garbage
   relation name is dropped and never runs.
3. **Three-valued discipline / no vacuous pass.** A function surface that ran no
   relation (no seeds, unimportable target, or all relations skipped) is
   UNVERIFIED — never PROVEN.
4. **Two reward consumers stay distinct — do NOT cross-wire them.** There are two
   deliberately different reward mechanisms; the function surface feeds ONLY the
   first:
   - `verify/reward.py verdict_reward(status)` — three-valued, UNVERIFIED→None
     (correct exclusion). This is where the function `BehavioralVerdict` flows.
   - `learning/eval_judge.py execution_reward(verifier_results)` — a multi-verifier
     aggregator whose `_verifier_passed` (line 221) keys on the `pass` BOOL
     FIRST. `BehavioralVerdict.to_json` ALWAYS emits `pass` (=status==PROVEN), so
     an UNVERIFIED verdict has `pass:false` and would be MISCOUNTED as a 0.0 fail
     if dropped into `execution_reward`. The engine's
     `MetamorphicResult.to_verifier_json` is the vacuous-SAFE envelope for that
     path (it OMITS `pass` on vacuous so `_verifier_passed` returns None →
     excluded). Therefore: DO NOT feed the function `BehavioralVerdict` into
     `execution_reward`, and DO NOT delete/alter `to_verifier_json` or its tests
     (`test_metamorphic_result_flows_into_execution_reward`). Leave the entire
     `metamorphic.py`/`eval_judge.py` Layer-0 path untouched.
5. **api/ui/journey behavior-preserving.** No change to `run_api_check`,
   `run_ui_check`, `run_journey_check`, `_amplify`, `_METAMORPHIC`, or any
   existing verdict.

## Explicitly OUT of scope

- Do NOT edit `mini_ork/verify/reward.py`, `committee.py`, `cli/verify.py`,
  `cli/execute.py`, `learning/eval_judge.py`, `learning/metamorphic.py`, or
  `metamorphic_proposer.py`. (The last three are correct as-is; the function
  surface consumes them by import only.)
- Do NOT relocate/rename `learning/metamorphic.py` or the proposer — wire by
  import, do not move files (keeps churn and test-import breakage to zero).
- Do NOT delete `MetamorphicResult.to_verifier_json` — it is the vacuous-safe
  envelope for the distinct `execution_reward` consumer (invariant 4).
- Do NOT add a new `verifiers/*.py` script — the env seam (`observable_from_env`
  + `main`) is the invocation contract.
- Do NOT touch `.mini-ork/config/**` or any providers/agents yaml.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the unified diff across the
  three in-scope files (behavioral.py, catalog.py, the new test).
- `python3.11 -m pytest tests/unit/test_behavioral_function_surface.py -q` → all green.
- `python3.11 -m pytest tests/unit/test_metamorphic.py tests/unit/test_metamorphic_proposer.py tests/unit/test_behavioral_verifier.py tests/unit/test_behavioral_oracle.py tests/unit/test_verifier_catalog.py tests/unit/test_verifier_committee.py -q`
  → no regressions.
- `python3.11 -c "from mini_ork.verify.behavioral import get_surface_handler; print(get_surface_handler('function').__name__)"`
  → prints `run_function_check`.

## Notes

mini-ork **self-edit** (edits its own verify core); the run sets
`MO_ALLOW_FRAMEWORK_CWD=1`. This is a structure-preserving, additive promotion:
dead, tested code gains its single live caller through the existing
surface-handler + three-valued reward seam. The separate Layer-0 aggregator
(`execution_reward`/`to_verifier_json`) is deliberately left intact because its
vacuous-exclusion semantics are not interchangeable with the behavioral `pass`
bool (invariant 4).
