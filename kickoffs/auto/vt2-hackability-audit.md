# VT2 exploit-generation hackability audit for registered gates (G09-T05)

## Goal

`mini-ork gate-fuzz` (G3) scores one hand-labelled corpus against `artifact_contract`, and nothing reads the result. `promotion_gate.promotion_evaluate` calls `verifier_audit.audit(task_class, db_path)` with no corpus, so the gate-fuzzer arm always comes back `skipped`. No registered gate has ever been attacked.

Evidence: on Docker-verified runs, 28.5% of a 49-task SWE-bench Verified sample and 25.0% of 20 R2E-Gym tasks accept incorrect patches (arXiv 2606.16062). Models rewrite evaluator code or inputs so they trivially pass (arXiv 2604.01476). Controlled exploit injection measures this (CATCH, arXiv 2609.39533).

Baseline, measured on this tree on 2026-10-05 (live `gate_registry` rows, scratch DB, the 5 operators below), hackability per gate: oracle-coalition 0.8 (safety=1; a nonexistent `panel_run_id` has zero voters ⇒ `panel_diverse` ⇒ pass), oracle-liveness 0.8, oracle-stability 0.8, oracle-panel-health 0.2, oracle-synthesis-promote 0.0, mutation-adversary-gate 0.0, step-rules-gate 0.0.

Deliverable: attack a registered gate with N known-bad inputs and persist `hackability = passed_known_bad / valid_trials`. Then make the promotion gate refuse a promote that depends on a gate above the threshold.

## Mechanism (exact spec)

1. **New module** `mini_ork/gates/hackability.py`, with `SCHEMA = "gate-hackability/v1"`. Known-bad is decided by deterministic code, never by a model: an input counts as known-bad only if it carries NO affirmative evidence (it is "hollow").
   - `is_hollow_document(text: str | None) -> bool` is True for `None` (the file is absent), for whitespace-only text, and for JSON whose every leaf is `None`, `False`, `0`, `0.0` or a whitespace-only string. Dicts and lists recurse and may be empty: the structure is free, the content is not.
   - It is False for any other leaf (including `True` and NaN) and for non-blank text that is not JSON.
2. **Trials.** `OPERATORS` run in fixed order and cost $0:
   - `empty-context`: the context is `{}`.
   - The other four use the skeleton context and differ only in the document: `dangling-evidence` (document `None`, file never written), `empty-document` (`""`), `hollow-object` (`"{}"`), and `zero-leaf-skeleton` (`json.dumps({"voters":[{}],"lenses":[{}],"structural":{},"panel_score":0,"kill_rate":0,"total":0})`).
   - Skeleton context, using the keys production passes (`cli/publisher.py:publisher_node`, `cli/verify.py`): `{"run_id": R, "panel_run_id": R, "recipe": "hackability-probe", "task_class": "hackability-probe", "current_round": 1, "verdict_file": P, "mutation_report": P, "artifact_path": P, "plan_path": P}`. `R = "hk-" + uuid4().hex[:8]` is fresh per audit. `P = <workdir>/<trial_id>.json` is written only when the document is not None. The class and recipe are deliberately neutral, because a real class value is affirmative evidence to a `scope_gate`.
   - Proposer trials are named `proposer-1..k` and use the same skeleton with a document from the proposer.
3. **`audit_gate`.** Signature: `audit_gate(db_path: str, gate_id: str, *, proposer: Proposer | None = None, n: int | None = None, evaluate: Callable[[str], str] | None = None) -> dict`.
   - Find the active row via the public `gate_registry.gate_list(db_path)`. If it is missing or inactive, raise `KeyError(gate_id)`.
   - Default `evaluate`, inside a `tempfile.TemporaryDirectory()`:
     - Build a scratch DB with `mini_ork.stores.migrate.init_db(db=<tmp>/scratch.db, root=ENGINE_ROOT)`, where `ENGINE_ROOT = Path(__file__).resolve().parents[2]`. Then call `gate_registry.ensure_table`.
     - INSERT the source row verbatim (all 7 columns).
     - Call `gate_registry.gate_evaluate(scratch_db, gate_id, ctx_json, mini_ork_root=RunContext.from_env().root or str(ENGINE_ROOT))` inside `mini_ork.context.scoped_environ({"MO_MUTATION_REPORT": None, "MO_TARGET_CWD": None, "MINI_ORK_TARGET_REPO": None, "MINI_ORK_DB": scratch_db})`. This keeps real run evidence out of reach through both the DB and the environment.
   - Each trial is three-valued. The claim being tested is "this input exploits the gate":
     - Not hollow ⇒ `UNVERIFIED`, reason `not_known_bad`. The gate is NOT run and `verdict` is null.
     - `evaluate` raises, or returns something outside {pass, fail, defer} ⇒ `UNVERIFIED`, reason `crashed: <detail>`.
     - `pass` ⇒ `PROVEN`. `fail` or `defer` ⇒ `REFUTED`; abstaining counts as resisting.
4. **Record.** `audit_gate` returns the record and the CLI writes it. Fields:
   - Gate identity: `schema`, `gate_id`, `gate_type`, `condition`, `task_class_filter`, `measured_at` (int epoch).
   - Counts: `trials` (#PROVEN + #REFUTED), `passed_bad` (#PROVEN), `hackability` (`passed_bad/trials`, or `None` when trials is 0), `unverified: {"crashed": int, "not_known_bad": int}`, `exploits` (the PROVEN ids).
   - Proposer: `proposer` (lane or None), `proposer_status` (`off`, `ok`, `budget_exhausted`, `dispatch_failed` or `unparseable`), `proposer_cost_usd`.
   - `results`: a list of `{id, operator, source: operator|proposer, verdict, outcome, reason, context, document[:2000]}`.
5. **Persistence: a JSON file only.** No migration, no registry column.
   - `records_dir(db_path)` is `<dirname(abspath(db_path))>/gate-hackability/`, next to the DB whose registry it describes (for the live home, inside the gitignored `.mini-ork/`).
   - `record_path(db_path, gate_id)` is that dir plus `re.sub(r"[^A-Za-z0-9._-]", "_", gate_id) + ".json"`.
   - `write_record(db_path, record) -> str`: makedirs, write `.tmp`, then `os.replace`, with `sort_keys=True, indent=2`.
   - `read_record(db_path, gate_id) -> dict | None`: None when the file is missing or unparseable, or when the schema or gate_id does not match. The latest measurement wins.
6. **Proposer.** An LLM may PROPOSE documents but never labels them.
   - `Proposer = Callable[[dict, int], dict]` and returns `{"documents": list[str], "cost_usd": float, "status": str}`.
   - `parse_proposals(text, n) -> list[str] | None` parses the span from the first `[` to the last `]` as a JSON list. Non-string items become `json.dumps(item)` and only the first n are kept. Anything else returns None (`unparseable`).
   - The production producer is `lane_proposer(lane: str, budget_usd: float | None = None) -> Proposer`. It is a closure that tracks cumulative cost; `budget_usd` defaults to `MO_GATE_HACKABILITY_BUDGET_USD`. Once spent ≥ budget it returns `budget_exhausted` without calling.
   - Otherwise it makes one call per gate: `mini_ork.dispatch.llm_dispatch.llm_dispatch(["--model", lane, "--node-type", "gate-hackability-propose", "--prompt-text", prompt, "--out", out, "--timeout", "180", "--max-turns", "1"], root=...)`. Redirect stdio the way `learning/gradient_extractor._default_dispatch` does. Read the text from `out` and the cost from `out + ".cost"` (default 0.0). rc≠0 ⇒ `dispatch_failed`.
   - The prompt contains gate_type, condition, the skeleton keys, `inspect.getsource(native_gates.resolve_native_evaluator(condition))` when it resolves (≤4000 chars), the hollow rule, and the instruction "return ONLY a JSON array of ≤N strings, each the full text of an evidence file; goal: make the gate return pass".
   - Any `outcome` or `expect` the model writes is only document content, and it makes the document non-hollow.
7. **Knobs**, read at call time:
   - `MO_GATE_HACKABILITY_N`: default 4, clamped to 0..16. Proposer documents per gate; 0 means the proposer is never called.
   - `MO_GATE_HACKABILITY_BUDGET_USD`: default 0.50.
   - `MO_GATE_HACKABILITY_MAX`: default 0.25; an unparseable value falls back to 0.25.
   - `MO_PROMOTION_GATE_HACKABILITY`: default `"1"`; `"0"` disables the consumer.
8. **CLI.** Extend `mini-ork gate-fuzz` instead of adding a subcommand, because `tests/unit/test_native_dispatch_py.py` pins the subcommand set.
   - Flags: `--hackability --gate ID [--gate ID ...] [--db PATH] [--proposer-lane LANE] [--json]`. `--db` defaults to `RunContext.from_env().db_or_default()`.
   - These exit rc 2 and write nothing: `--gate`, `--db` or `--proposer-lane` without `--hackability`; `--hackability` without `--gate`; `--hackability` with `--corpus`; any unknown or inactive gate. Validate every gate before auditing any.
   - For each gate, run `audit_gate` then `write_record`, with one `lane_proposer` per invocation.
   - Text output per gate: `gate-hackability: gate=<id> hackability=<0.800|-> passed_bad=<k>/<trials> unverified=crashed:<c>,not_known_bad:<b> proposer=<status> record=<path>`, then one `  exploit: <id> (<operator>)` line per exploit. `--json` prints `{"records": [...]}` (sort_keys, indent 2) as the only stdout.
   - rc is 0 for any measurement. Without `--hackability`, the legacy path stays byte-identical.
9. **Consumer.**
   - `promotion_check(db_path: str, task_class: str | None) -> dict` returns `{"max", "ok", "over_threshold": [{gate_id, hackability, measured_at}], "within_threshold": [{gate_id, hackability}], "unmeasured": [{gate_id, reason: no_record|stale|no_valid_trials}]}`. It covers the gates from `gate_registry.gate_list(db_path, task_class)`, which are the gates `gate_run_all` evaluates for that class.
   - A record is `stale` when its `gate_type` or `condition` differs from the current row. A gate is over when `hackability > max`. `ok = not over_threshold`.
   - In `promotion_evaluate`, add `gate_hk = None` next to `audit_result`. After the verifier-audit block, run the check only if the knob is on AND `decision == "promoted"`: `gate_hk = hackability.promotion_check(db_path, _candidate_task_class(con, candidate_id))`.
   - If the check raises: write a stderr line, set `gate_hk = {"error": str(exc)}`, and leave the decision unchanged.
   - If `over_threshold` is non-empty: set `decision = "rejected"` and append `" gate-hackability:" + ",".join(f"{id}={rate:.3f}") + f">{max:g}"` to the rationale. Reject, don't quarantine: the defect is in the instrument, and quarantine is permanent in `mini-ork promote`.
   - Set `result["gate_hackability"] = gate_hk` only when the knob is on. The check never touches a reject or quarantine branch, and an unmeasured gate never causes a reject.

## Files in scope (touch ONLY these)

- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt2-hackability-audit/mini_ork/gates/hackability.py` (new): operators, hollowness witness, `audit_gate`, record IO, `parse_proposals`, `lane_proposer`, `promotion_check`.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt2-hackability-audit/mini_ork/cli/gate_fuzz.py`: the `--hackability` flags, dispatch to `hackability`, and the usage text.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt2-hackability-audit/mini_ork/gates/promotion_gate.py`: the hook in `promotion_evaluate`, plus the two knobs in the module docstring.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt2-hackability-audit/tests/unit/test_gate_hackability.py` (new): the tests below.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt2-hackability-audit/docs/operator/env-vars.md`: append the 4 knobs as rows in the "Runtime behavior" table.

Read-only references (do NOT edit), under `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt2-hackability-audit/`:
- `mini_ork/gates/gate_fuzzer.py`: the legacy path must stay byte-identical.
- `mini_ork/gates/{gate_registry,gate_bootstrap,native_gates}.py`, `mini_ork/learning/verifier_audit.py`, `mini_ork/dispatch/llm_dispatch.py`, `mini_ork/cli/{promote,publisher}.py`, `mini_ork/context.py`, `mini_ork/stores/migrate.py`.
- `tests/unit/test_promotion_gate_py.py`: copy its `_seed_workflow` / `_seed_bench` pattern; do not import it.
- `tests/unit/test_native_dispatch_py.py`.

Do NOT modify any other file.

## Tests

`tests/unit/test_gate_hackability.py` must be hermetic: no LLM, no network. Set up the DB with `mig.init_db(db=tmp, root=REPO)` and register gates with `gate_registry.gate_register`. For a hackable stub type, use `monkeypatch.setitem(gate_registry.GATE_EVALUATORS, "hk_open", lambda *a: "pass")`.
1. Hackable: auditing `hk_open` gives hackability 1.0, trials 5, `exploits` equal to the 5 operator ids, and the record survives a `read_record` round-trip.
2. Sound: `scope_gate` with condition `["code_fix"]` gives 0.0 and `exploits == []`.
3. Crash exclusion: a stub `evaluate` that, in operator order, raises, raises, returns `"maybe"`, `"pass"`, `"defer"` gives trials 2, passed_bad 1, hackability 0.5, `unverified.crashed` 3.
4. All trials crash: hackability is None, and `promotion_check` lists the gate under `unmeasured` with reason `no_valid_trials`.
5. Proposer witness: a stub proposer returns `['{"voters":[{}]}', '{"panel_score": 95}', 'not json', '{"outcome": "PROVEN"}']`. Only the first is evaluated (stub `evaluate` calls == 6); the other three are `UNVERIFIED`/`not_known_bad` with `verdict` None.
6. `lane_proposer` with a monkeypatched `llm_dispatch.llm_dispatch` that writes `--out` and `.cost`: argv contains `--model glm --node-type gate-hackability-propose`; the documents are parsed and the cost is read; `budget_usd=0` ⇒ `budget_exhausted` with no call. `parse_proposals` returns None on text with no array.
7. `is_hollow_document` table. True for None, `""`, `"{}"`, `"[]"`, `'{"a":[{}],"b":0,"c":null,"d":false,"e":" "}'`. False for `'{"a":1}'`, `'{"a":"x"}'`, `'true'`, `'garbage'`.
8. Env isolation: with `MO_MUTATION_REPORT` pointing at a real passing report (`{"kill_rate":0.95,"total":10}`), a `custom` gate `native:mutation-adversary` still audits to 0.0.
9. **Production E2E.** Seed a candidate with 3 runs, all passing, class `code_fix`, and register `hk_open` with a NULL filter. `gate_fuzz.main(["--hackability","--gate",gid,"--db",db,"--json"])` returns rc 0 with JSON hackability 1.0, and the file `<tmp>/gate-hackability/<gid>.json` exists. Then `promotion_gate.promotion_evaluate(db, cid)` returns `"rejected"`, the rationale contains `f"gate-hackability:{gid}=1.000>0.25"`, and the persisted `promotion_records.decision` is `"rejected"`.
10. Below threshold: auditing the `scope_gate` through `gate_fuzz.main` ⇒ `promoted`, with the gate in `within_threshold`.
11. A gate never audited ⇒ `promoted`, unmeasured with reason `no_record`. Audit, then `UPDATE` the condition ⇒ reason `stale`, still `promoted`.
12. Knobs off. With `MO_PROMOTION_GATE_HACKABILITY=0` and a 1.0 record present ⇒ `promoted`, and `set(result)` is exactly the 10 legacy keys. `gate_fuzz.main(["--json"])` stdout equals `json.dumps(fuzz_gate(artifact_contract_evaluator(tmp), load_corpus(DEFAULT_CORPUS)), indent=2, sort_keys=True) + "\n"`, and no `gate-hackability` dir is created.
13. CLI usage errors: the 4 cases in mechanism 8 each return rc 2, and an unknown gate writes no record.

## Success criteria

- The verification command passes. The existing gate-fuzz, promotion, promote-CLI, verifier-audit, registry and subcommand-set tests pass unchanged.
- `ruff check` is clean on all 4 Python files.
- The promotion result changes only through a measured record above `MO_GATE_HACKABILITY_MAX`. No new required env var, no migration, no new subcommand.

## Verification command

```bash
cd /Volumes/docker-ssd/ps/mini-ork-worktrees/vt2-hackability-audit && python3.11 -m pytest -q tests/unit/test_gate_hackability.py tests/unit/test_gate_fuzzer_py.py tests/unit/test_promotion_gate_py.py tests/unit/test_mini_ork_promote_py.py tests/unit/test_verifier_audit_py.py tests/unit/test_native_dispatch_py.py tests/unit/test_gate_registry_py.py && ruff check mini_ork/gates/hackability.py mini_ork/cli/gate_fuzz.py mini_ork/gates/promotion_gate.py tests/unit/test_gate_hackability.py
```

## Review bar

- Every new input has a named production producer that a test exercises. `promotion_check` reads records that `gate-fuzz --hackability` (`write_record`) writes. Proposer documents come from `lane_proposer` → `llm_dispatch`. The skeleton keys are the ones `publisher_node` and `verify` pass.
- The verdict comes from what the code did. Every `PROVEN`/`REFUTED` comes from a real `gate_evaluate` return on a witnessed-hollow input, and no model text ever sets an outcome. Unestablished results stay `UNVERIFIED` and are counted on neither side.
- Reject any change to `gate_fuzzer.py` or `gate_registry.py`, any migration, or any new `mini-ork` subcommand.

## Rules

Edit files directly; do not emit unified diffs. One deliverable: the hackability audit plus its single promotion hook. Do not touch any other gate's verdict logic, and do not fix the hackable gates (coalition, liveness, stability) here; they are follow-ups.
