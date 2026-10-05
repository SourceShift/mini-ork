# VT3 Declared equivalence operator for the behavioral verifier (G10-T07)

## Goal

Any behavioral verdict that compares two values depends on a comparison relation. Today that relation is hard-coded and nobody declares it. `idempotent_repeat` in `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt3-equivalence-operator/mini_ork/verify/behavioral.py:468-480` compares probe bodies by `_canonical` (sorted-key JSON) equality, so it REFUTES a correct endpoint whose body carries a volatile `uptime` or timestamp. The `api` surface also has no way to compare an observed body with a declared expected value. This task makes the relation an explicit operator declared per observable, and records on every verdict which operator produced it.
Evidence: G10-T07 is consensus #3 of `verify-review-20261005-122958`, the predicted signature of the measured 3-of-6 correct-fixes-rejected gap. arxiv:2609.34785 shows latency-insensitive equivalence accepting valid designs that cycle-accurate matching rejects. arxiv:2609.34967 shows that the choice of equivalence relation, separate from aggregation, is an error source that fails silently.
Doctrine: a verdict is anchored on what the code DID, and there is no LLM in the comparison path. Verdicts are PROVEN / REFUTED / UNVERIFIED, and anything not established is UNVERIFIED, never a pass.

## Mechanism (exact spec)

1. NEW `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt3-equivalence-operator/mini_ork/verify/equivalence.py`. It is pure stdlib: no I/O, no `mini_ork.dispatch`, httpx or yaml import, and its only import-time effect is dict inserts.
   - `DEFAULT_OPERATOR = "exact"`.
   - `@dataclass(frozen=True) EquivalenceSpec(operator: str = "exact", rules: Mapping[str, Any] = {})` with `classmethod from_raw(raw)`. `None` gives exact, and a `str` gives that operator with no rules. A mapping may only have the keys `operator` (str, required) and `rules` (mapping). Anything else raises `ValueError`. It never consults the registry.
   - `@dataclass(frozen=True) EquivalenceResult(equal: bool | None, operator: str, detail: str = "", path: str = "")`. On `False`, `detail` is `"first diff at <path>: observed <repr> != expected <repr>"`. Reprs are truncated to 120 chars, and paths use the forms `$`, `$.k`, `$[i]` over the operator-normalized values. On `None` (could not evaluate), `detail` holds the reason.
   - `OperatorFn = Callable[[Any, Any, Mapping[str, Any]], EquivalenceResult]`, called as `(observed, expected, rules)`.
   - `register_operator(name, fn, *, validate: Callable[[Mapping], str] | None = None)` is the OCP seam, and the last write wins. `name` must match `^[a-z][a-z0-9_-]*$` and `fn` must be callable, otherwise it raises `ValueError`. `validate(rules)` returns `""` or a reason. `None` means rules must be empty.
   - `get_operator(name)` returns `OperatorFn | None`, and `known_operators()` returns a sorted tuple.
   - `spec_error(spec) -> str` returns `""` when valid. Otherwise it returns `"unknown equivalence operator 'X'; registered: canonical, exact, set, tolerant"` or the validator's reason.
   - `compare(observed, expected, spec=None) -> EquivalenceResult` never raises. It returns `equal=None` on a spec_error, when the operator raises, or when the operator returns something other than an `EquivalenceResult`. The result's `operator` is always `spec.operator`.
2. Built-ins. Each one relaxes only what its declared rules name, rules are never inferred, and a `bool` is never a number.
   - `exact` takes no rules. Two values are equal iff `json.dumps(x, sort_keys=True, default=str)` matches on both sides, falling back to `repr()` on TypeError. This is the SAME relation as `behavioral._canonical` (`:340-344`): key order does not matter, but list order, types (`1`/`1.0`/`true`) and string bytes do. The diff walker compares scalars by this canonical form, never by `==`.
   - `set` takes no rules. It sorts every list at any depth by the canonical form of its elements, then applies `exact`. It is a multiset: `[1,1,2]` != `[1,2,2]`.
   - `canonical` accepts only these rules, then applies `exact` (with no rules it is `exact`):
     - `ignore_keys` (list[str]): drop these keys at any depth, on both sides.
     - `strip_whitespace` (bool): apply `" ".join(s.split())` to every string.
     - `numeric` (bool): an integral float equals the int.
   - `tolerant` accepts only `abs_tol` and `rel_tol`. Each must be finite, >= 0 and not a bool, and at least one must be > 0, otherwise there is a spec_error. It walks both values in parallel, requiring the same dict keys and the same list lengths in order. Two non-bool numbers are equal iff `math.isclose(o, e, rel_tol=, abs_tol=)`, and every other leaf is compared with `exact`.
3. Hook in `behavioral.py`. The production declaration point is the `observable` block of the `MO_OBSERVABLE_SPEC` descriptor.
   - `Observable` (`:155-179`) gains `equivalence: EquivalenceSpec` (default exact), `expect_body: Any = None` and `expect_body_set: bool = False`. `from_mapping` (`:181-276`) parses `equivalence` with `from_raw`, turning a `ValueError` into `ObservableError`. A present `expect_body` key sets `expect_body_set`, and the value must pass `_is_json_data`. Do NOT reject unknown operator names here.
   - `observable_from_env` (`:1123-1153`) reads two new vars in the `MO_BEHAV_*` branch only; a spec file still wins entirely. `MO_BEHAV_EQUIVALENCE` is unset for exact; a value starting with `{` is JSON, and anything else is an operator name. `MO_BEHAV_EXPECT_BODY` is JSON, and unset or empty means no body check. Bad JSON raises `ObservableError`.
   - `Check` (`:133-139`) gains `operator: str = ""`, and `BehavioralVerdict` (`:279-303`) gains `operator: str = "exact"`. `to_json` always emits a top-level `"operator"` right after `"pass"`, and a per-check `"operator"` only when non-empty.
   - `run()` (`:1074-1090`): if `spec_error(obs.equivalence)` is non-empty, return UNVERIFIED with `Check("equivalence", None, reason, operator=name)` for EVERY surface: never fall back to exact and never pass. Otherwise, stamp `verdict.operator = obs.equivalence.operator`. Journey steps do not inherit the parent's operator.
   - `run_api_check` (`:731-782`) applies the same guard, because callers invoke it directly, and stamps `operator` on every verdict it returns. After the status and schema checks, if `expect_body_set`:
     - observed is `primary.body`, or `primary.text` when the body is None.
     - It appends `Check("expect_body", r.equal, d, operator=op)` with `r = compare(observed, obs.expect_body, obs.equivalence)`.
     - `d` is `f"body equivalent under operator={op}"` when equal, else `f"{r.detail} [operator={op}]"`.
   - `_amplify` `idempotent_repeat` (`:468-480`) compares probes 1..n-1 with probe 0 using `compare`, and sets `Check.operator=op`.
     - Any `None`: ok=None, with the reason as detail.
     - Any `False`: ok=False. The detail is today's `f"{n} probes diverged across {k} distinct bodies{cap_note}"` (k still from `_canonical`) plus `f"; {r.detail} [operator={op}]"`.
     - All `True`: under exact, keep `f"{n} probes identical{cap_note}"` exactly. Otherwise use `f"{n} probes equivalent under operator={op}{cap_note}"`.
     - Leave `order_invariant`, `filtered_subset_of_unfiltered` and `_eval_metamorphic` unchanged.
   - `_run_journey_step` (`:913-931`) rebuilds the step's `Observable` field by field. Also copy `equivalence`, `expect_body` and `expect_body_set`, and change nothing else.
4. Schema: `$defs.observable.properties` gains `expect_body` (any JSON) and `equivalence`. `equivalence` is `oneOf` a string, or `{operator: string (required), rules: object}` with additionalProperties false. Leave the `surface` enum alone.

## Files in scope (touch ONLY these)

- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt3-equivalence-operator/mini_ork/verify/equivalence.py` (NEW): the registry and the four built-ins.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt3-equivalence-operator/mini_ork/verify/behavioral.py`: declaration fields, env vars, guard, comparison hooks, operator stamping.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt3-equivalence-operator/schemas/verifier_contract.schema.json`: `$defs.observable` is additionalProperties:false, so without this change the declaration would violate the schema.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt3-equivalence-operator/tests/unit/test_verify_equivalence.py` (NEW): all new tests.

Read-only references (do NOT edit), under the same worktree: `mini_ork/verify/{committee,reward,catalog,__init__}.py`; `mini_ork/cli/verify.py` (spawns the verifier at `:356-373`); `verifiers/api_contract.py` (calls `behavioral.main`) and `verifiers/api_contract.observable.example.yaml`; `tests/unit/test_behavioral_*.py` (`FakeRequester` at `test_behavioral_verifier.py:28-40`). Do NOT modify any other file, and do not edit existing tests.

## Tests

`tests/unit/test_verify_equivalence.py` is hermetic: injected requester, no network, no LLM.

1. `set`: `["b","a"]` vs `["a","b"]` is False under exact and True under set. Through `run_api_check` with `expect_body`, exact gives REFUTED and set gives PROVEN.
2. `canonical`: observed `{"ok":true,"msg":"all  good\n","n":1.0,"ts":"..."}` vs expected `{"n":1,"msg":"all good","ok":true}` with rules `{ignore_keys:[ts],strip_whitespace:true,numeric:true}` is False under exact and True under canonical. A key-order-only difference is already equal under exact. Changing `ok` to `false` gives False with `path == "$.ok"`.
3. `tolerant`:
   - `abs_tol=0.01`: 1.004 vs 1.0 is True, and 1.02 vs 1.0 is False.
   - `rel_tol=0.1`: 105 vs 100 is True, and 120 vs 100 is False.
   - `True` vs `False` with `abs_tol=1.0` is False.
   - With no tolerance declared, `spec_error` is non-empty and `equal is None`.
4. Unknown `fuzzy`, and the typo rule `canonical {ignore_key: [...]}`, give `equal is None`. `run()` and `run_api_check` return UNVERIFIED even though the body matches. A `ui` observable with `fuzzy` also returns UNVERIFIED through `run()`.
5. Default is byte-identical.
   - Use obs `{"surface":"api","staging_url":"https://staging.example","target":"/health","metamorphic":["idempotent_repeat"]}` with a requester that always returns `HttpResult(200, {"status":"ok"}, "", ok_transport=True)`.
   - Pop the top-level `"operator"` (assert `"exact"`) and every check's `"operator"`. The result must equal this pre-change literal:
     `{"verifier":"behavioral","surface":"api","target":"https://staging.example/health","status":"PROVEN","pass":true,"checks":[{"name":"status","ok":true,"detail":"got 200, expected one of [200]"},{"name":"idempotent_repeat","ok":true,"detail":"3 probes identical"}],"evidence":"PROVEN: https://staging.example/health\n  [PASS] status: got 200, expected one of [200]\n  [PASS] idempotent_repeat: 3 probes identical"}`
   - Queue `(a, a, b)` (repeat-last) with `a={"uptime":1.0}` and `b={"uptime":2.0}`. The result is REFUTED with names and ok values unchanged. The idempotent detail starts with `"3 probes diverged across 2 distinct bodies"`, contains `$.uptime`, and ends with `[operator=exact]`.
6. Mutant set: at least 7 pairs where a value really differs, none mutating `ts`. Cover a scalar (Δ > 0.01), a string, a bool flip, a dropped list element, an extra key, `"1"` vs `1`, and `[1,1,2]` vs `[1,2,2]`. Every pair gives `equal is False` under EACH of exact, set, `canonical{ignore_keys:[ts],strip_whitespace:true,numeric:true}` and `tolerant{abs_tol:0.01}`.
7. Flip measurement over 4 fitted fixtures: reordered list (set), volatile ts (canonical), in-tolerance float (tolerant), whitespace (canonical). Exact REFUTES all 4 and the fitted operator PROVES all 4. Assert `flips == 4`, and `false_approvals == 0` over test 6.
8. `idempotent_repeat` over probes `{"status":"ok","uptime":1.001}`, then `1.002`, then `1.003`, via `run_api_check`. Exact gives REFUTED; `canonical{ignore_keys:[uptime]}` and `tolerant{abs_tol:5}` give PROVEN. A probe with `"status":"down"` stays REFUTED under both.
9. Every REFUTED in tests 1, 2, 5 and 8 carries the declared payload `operator`. Every failing check that compares values has an `"operator"` key and a detail ending in `[operator=<op>]`.
10. PRODUCTION ENTRYPOINT. Write a yaml spec to `tmp_path` (`surface: api`, `expect_body: [a, b]`, `equivalence: set`) and set `MO_OBSERVABLE_SPEC` to it. Call `behavioral.main([], requester=FakeRequester(HttpResult(200, ["b","a"], "", ok_transport=True)))`, which is what `verifiers/api_contract.py` runs.
    - `set` gives rc 0, PROVEN and `"operator":"set"`.
    - `exact` gives rc 1 and REFUTED.
    - `fuzzy` gives rc 1 and UNVERIFIED.
    - `MO_BEHAV_EQUIVALENCE='{"operator":"canonical","rules":{"ignore_keys":["uptime"]}}'` with `MO_BEHAV_METAMORPHIC=idempotent_repeat` over volatile uptime gives PROVEN.
11. OCP and safety:
    - A registered `casefold` operator can be used by name.
    - An operator that raises gives UNVERIFIED.
    - `register_operator("Bad Name", f)` raises `ValueError`. A fixture restores the registry.
    - A journey step declaring `set` with a reordered `expect_body` gives PROVEN.
    - The schema has both new properties, and a canonical declaration validates with `jsonschema.Draft202012Validator`.

## Success criteria

- The verification command passes, including the 122 existing tests, unchanged.
- `python3.11 -c "import mini_ork.verify.equivalence as e; print(e.known_operators())"` prints `('canonical', 'exact', 'set', 'tolerant')`.
- `grep -nE "dispatch|httpx|yaml|requests" <equivalence.py>` prints nothing.

## Verification command

```bash
cd /Volumes/docker-ssd/ps/mini-ork-worktrees/vt3-equivalence-operator && python3.11 -m pytest -q tests/unit/test_verify_equivalence.py tests/unit/test_behavioral_verifier.py tests/unit/test_behavioral_oracle.py tests/unit/test_behavioral_ui.py tests/unit/test_behavioral_function_surface.py tests/unit/test_verifier_committee.py tests/unit/test_verifier_catalog.py tests/unit/test_verifier_dispatch_py.py tests/unit/test_mini_ork_verify_py.py && PYENV_VERSION=3.10.15 ruff check mini_ork/verify/equivalence.py mini_ork/verify/behavioral.py tests/unit/test_verify_equivalence.py
```

## Review bar

Each new input needs a named production producer:
- `observable.equivalence` and `observable.expect_body` come from the `MO_OBSERVABLE_SPEC` descriptor. `observable_from_env` (`behavioral.py:1132-1134`) reads it inside the `verifiers/api_contract.py` subprocess, which `mini-ork verify` spawns for `success_verifiers: [api_contract]` (`cli/verify.py:356-373`).
- The `MO_BEHAV_*` vars come from the `os.environ` forwarded at `cli/verify.py:365`.
- The `operator` stamps land in the stdout JSON, which the dispatcher writes to the evidence log.

REJECT the change if: an unknown or invalid operator yields PROVEN or silently falls back to exact; a bool is compared as a number; an operator relaxes something its rules do not name; there is an LLM or dispatch import; an env var or config changes the default operator globally; or files outside the four are edited.

## Rules

Edit files directly; do not emit unified diffs. This is ONE deliverable: no new `mini-ork` subcommand, no recipe wiring, no LLM call, no edits to existing tests.
