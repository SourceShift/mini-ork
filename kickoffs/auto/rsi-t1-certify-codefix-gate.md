# RSI Tier-1 #8b: let the level vector honour a recorded certify veto (MO_CODEFIX_CERTIFY=gate)

## Goal

`rsi-t1-certify-codefix` (merged first) records the relations and differential
vetoes in the code-fix test payload, under key `certify`. It never gates: the
verifier's `pass` and exit code are unchanged in every mode, and
`MO_CODEFIX_CERTIFY=gate` is only stamped as `certify.mode == "gate"`.

This kickoff adds the consumer at the publisher's level vector
(`mini_ork/verify/levels.py:derive_levels`). The level vector is the gate that
**withholds a commit without rolling back the fix** (`publish_decision` →
`"abstain"`). That is the right first gate for LLM-proposed evidence whose
precision has not yet been measured on live runs.

`derive_levels` keeps its contract: it reads evidence only and never reads the
environment for this. The mode that was recorded at verify time decides.

The mapping is chosen per term:

- A **relations** veto ("the patch special-cases the reported input") is a
  `target` failure.
- A **differential** veto ("collateral divergence on preserve inputs") is a
  `preserve` failure.

Both downgrade PROVEN to **UNVERIFIED**, never REFUTED, so `publish_decision`
abstains and no rollback fires.

With the default `shadow` mode, nothing changes.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/verify/levels.py`

- **New helper `certify_vetoes(T: dict) -> list[str]`.**
  - Let `C = T.get("certify")`.
  - Return `[]` unless all three hold:
    - `C` is a dict;
    - `C.get("mode") == "gate"`;
    - `C.get("verdict") == REFUTED`.
  - Otherwise return the entries of `C.get("vetoed_by")` that are
    `"relations"` or `"differential"`, in their original order. A non-list
    `vetoed_by` gives `[]`.
  - Never raises.
- **`derive_levels`.** After the existing `target` block and after the existing
  `preserve` block, and before `contract`, compute
  `vetoes = certify_vetoes(T)`. Then:
  - When `"relations" in vetoes` and `vector["target"] == PROVEN`:
    - set `vector["target"] = UNVERIFIED`;
    - set `reasons["target"] = f"verifier_test.json: certify veto (relations): {_veto_reason(T, 'relations')}"`.
  - When `"differential" in vetoes` and `vector["preserve"] == PROVEN`:
    - set `vector["preserve"] = UNVERIFIED`;
    - set `reasons["preserve"] = f"verifier_test.json: certify veto (differential): {_veto_reason(T, 'differential')}"`.
  - A veto never changes a level that is not PROVEN: n/a, REFUTED and
    UNVERIFIED stay exactly as computed.
- **New helper `_veto_reason(T, term) -> str`.** Returns the first one of these
  that holds:
  - `T["certify"][term]["reason"]` when it is a non-empty str;
  - `str(T["certify"].get("reason") or "")`;
  - `""` on any error.
- **Docstring.** Add the `certify` gate to the module docstring's level list.
  Nothing else in `levels.py` changes: not `LEVELS`, not `REQUIRED_LEVELS`,
  not `publish_decision`, not `level_report`'s keys.

### 2. `docs/architecture/verification-stack.md`

In the `⑮ Certify terms in code-fix` section, replace "gate is recorded but
inert" with a description of the consumer. Name:

- the two mappings: relations → `target`, differential → `preserve`;
- that both are UNVERIFIED, so the publish is withheld and the fix stays in the
  tree;
- that the switch is `MO_CODEFIX_CERTIFY=gate`.

Add one sentence: flip the default only after shadow evidence shows no veto on
a run whose fix was confirmed correct.

## Files in scope (touch ONLY these)

- `mini_ork/verify/levels.py`
- `docs/architecture/verification-stack.md`
- `tests/unit/test_levels_certify_gate.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_levels_certify_gate.py`: pure files in `tmp_path`, no subprocess)

Copy `_run_dir` and `_test_evidence` from `tests/unit/test_verify_levels.py`.

Fixtures:

- **Base payload `P`:**
  `{"verifier": "test", "pass": True, "post_rc": 0, "base_rc": "", "replay": {"overlap": ["test_mod.py::test_a"]}}`.
  With no certify block it derives `target == PROVEN` and
  `preserve == PROVEN`.
- **Summary:** `{"status": "implemented", "files_changed": ["mod.py"]}`.
- **`cert(mode, verdict, vetoed_by)`** builds:
  `{"mode": mode, "status": "ran", "reason": "r0", "poc_file": "test_mod.py", "relations": {"verdict": verdict, "reason": "rel-reason"}, "differential": {"verdict": None, "reason": "diff-reason"}, "verdict": verdict, "vetoed_by": vetoed_by, "would_block": verdict == "REFUTED"}`.

Tests:

- **Shadow never moves a level.**
  `cert("shadow", "REFUTED", ["relations", "differential"])` gives a vector and
  reasons equal to those of `P` with no `certify` key.
- **Gate + relations.**
  - `target == UNVERIFIED`;
  - `reasons["target"] == "verifier_test.json: certify veto (relations): rel-reason"`;
  - `preserve == PROVEN`.
- **Gate + differential.**
  - `preserve == UNVERIFIED`;
  - its reason starts with `"verifier_test.json: certify veto (differential)"`;
  - `target == PROVEN`.
- **Gate without a veto.** `cert("gate", None, [])` leaves the vector
  unchanged.
- **Malformed blocks are ignored.** Each of these leaves the vector unchanged:
  - `certify: "REFUTED"`;
  - `certify: {"mode": "gate", "verdict": "REFUTED", "vetoed_by": "relations"}`;
  - `vetoed_by: ["bogus"]`.
- **A veto never touches a non-PROVEN level.** A payload with `pass: False`
  and `replay.overlap: []` (target REFUTED today), plus a gate relations veto,
  keeps `target == REFUTED`.
- **`level_report` abstains.** With the summary and a gate relations veto,
  `level_report(str(rd), "code_fix")` has `levels_decision == "abstain"` and
  `levels_ok is False`. Without the veto it is `"publish"`.
- **`certify_vetoes`.** It returns `["relations", "differential"]` for a gate
  REFUTED block that lists both, and `[]` for `{}`.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_levels_certify_gate.py tests/unit/test_verify_levels.py tests/unit/test_levels_framework_edit.py`
- `pytest -q -p no:asyncio tests/unit/test_replay_early_applicability.py tests/unit/test_withheld_publish_outcome.py tests/unit/test_gates_settle.py tests/unit/test_recover_carry_publish.py`
- `pytest -q -p no:asyncio tests/unit/test_codefix_certify_terms.py tests/unit/test_codefix_certify_verifier.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_levels_certify_gate.py --junitxml="${MINI_ORK_RUN_DIR}/certify-gate-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/certify-gate-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- Any `verifier_test.json` without a `certify` block, or with a shadow one,
  derives a byte-identical `(vector, reasons)`.
- `ruff check --select F,E9 mini_ork/verify/levels.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
