# RSI Tier-1 #4a: every frozen-path publish refusal is a safety event, and never opens a fix epic

## Goal

`config/kernel-frozen.yaml` is enforced at the publish choke point
(`mini_ork/cli/publisher_authored_patch.land_patch`, the `frozen = frozen_hits(...)`
block): a patch touching a frozen path is refused with abstain reason
`publish-frozen-path`. Two defects:

- **The refusal is silent.** The only `safety_events` writer for the frozen
  kernel is the framework-edit verifier (`recipes/framework-edit/verifiers/static-check.py`
  `_emit_frozen_kernel_event`), and it fires only for auto-triage kickoffs. A
  `land_patch` refusal, and an operator override through `MO_ALLOW_FROZEN_EDIT=1`,
  leave no row. `safety_events` has 0 rows ever.
- **The refusal escalates into a kernel edit.** `mini_ork/recovery/diagnose.py`
  rule R1 (`_rule_publish_abstain`) classifies EVERY `publish_abstain` with a
  proven level vector as `layer=engine, routes_to=fix_epic`. A frozen-path refusal
  has that shape, so the self-driving loop would open a framework-edit fix epic
  to "fix" the engine that refused a frozen edit.

Record each refusal and override in `safety_events`, and diagnose a frozen-path
refusal as an owner decision, not an engine fault.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/cli/publisher_authored_patch.py`

- **New helper** `_record_frozen_event(tripwire_id: str, severity: str, paths: list[str], *, run_id: str = "") -> bool`.
  - Imports `mini_ork.stores.safety_events` locally inside the helper.
  - Calls `safety_events.emit(tripwire_id, severity, json.dumps({"run_id": run_id, "paths": list(paths), "boundary": "publish"}), run_id=run_id)`.
    Do not pass `db=`: `emit` resolves `MINI_ORK_DB`, then `MINI_ORK_HOME/state.db`.
  - Returns `True` when the returned dict's `"id"` is a non-empty string, else `False`.
  - Returns `False` on any exception. It never raises.
- **`land_patch` signature** becomes
  `land_patch(repo: str, patch_text: str, branch: str, *, message: str = "", run_id: str = "") -> str | Abstain`.
  Existing callers that do not pass `run_id` keep working unchanged.
- **The refusal branch** (`if frozen and not frozen_edit_allowed():`): call
  `_record_frozen_event("frozen-kernel-edit", "high", frozen, run_id=run_id)`
  before the `return Abstain("publish-frozen-path", ...)`. The returned Abstain
  (reason and detail text) is byte-for-byte unchanged, and it is returned
  whatever `_record_frozen_event` returns.
- **The override branch.** Immediately after that `if` block, add: when `frozen`
  is non-empty (so `frozen_edit_allowed()` was true), call
  `_record_frozen_event("frozen-kernel-override", "medium", frozen, run_id=run_id)`,
  then continue to the normal landing path unchanged.
- The tripwire id `frozen-kernel-edit` is deliberately the same one the verifier
  writes, so one query finds every frozen refusal. `boundary: "publish"` tells
  them apart (verifier rows carry no `boundary` key).

### 2. `mini_ork/cli/publisher.py`

- In `_publisher_try_commit_files`, the one call
  `landed = _pap.land_patch(target_repo, authored.patch_text, "", message=msg)`
  becomes `landed = _pap.land_patch(target_repo, authored.patch_text, "", message=msg, run_id=run_id)`.
  Nothing else in this file changes.

### 3. `mini_ork/recovery/diagnose.py`

- **New rule** `_rule_frozen_refusal(run_dir: Path, run_id: str, ends: dict[str, dict[str, Any]]) -> dict[str, Any] | None`, rule id `R1f`:
  - `got = _publisher_withheld(run_dir, ends)`; return `None` when `got is None`.
  - `pub, reason, abstain = got`. Return `None` unless `abstain` is a dict and
    `str(abstain.get("reason") or "") == "publish-frozen-path"`.
  - Matches **whatever** `_levels_ok(run_dir)` says (proven, unproven or absent).
  - Evidence: `_abstain_evidence(pub, reason, abstain, _levels_ok(run_dir), str((ends.get(pub) or {}).get("event_id") or ""))`.
  - Returns `_result(run_id, rule_id="R1f", layer=LAYER_KICKOFF, stage="deliver", node=pub or "publisher", confidence=0.95, evidence=evidence, routes_to=ROUTE_OWNER, summary="the publisher refused a frozen-path edit (config/kernel-frozen.yaml) — only the owner can authorise it (MO_ALLOW_FROZEN_EDIT=1) or re-scope the change", ends=ends)`.
- **Rule order in `_diagnose`:** add `lambda: _rule_frozen_refusal(run_dir, run_id, ends),`
  as the FIRST entry of the rule tuple, before `_rule_publish_abstain`.
- Add one line to the module docstring rule list:
  `* ``R1f`` publisher refused a frozen-path edit → kickoff / deliver / owner`.
- Nothing else changes: R1, R1b and every other rule are untouched.

## Files in scope (touch ONLY these)

- `mini_ork/cli/publisher_authored_patch.py`
- `mini_ork/cli/publisher.py`
- `mini_ork/recovery/diagnose.py`
- `tests/unit/test_frozen_refusal_events.py` (new)

Do NOT modify any other file. In particular do not modify
`mini_ork/stores/safety_events.py`, `config/kernel-frozen.yaml`,
`mini_ork/vcs/repo_integrity_guard.py` or `recipes/framework-edit/verifiers/static-check.py`
(all frozen).

## Tests (`tests/unit/test_frozen_refusal_events.py`: hermetic temp git repo + temp sqlite, no network)

**Harness for `land_patch`.** Copy `GIT_IDENTITY`, `_git`, `_write`, `_make_repo`,
`_patch`, `FIXTURE_REL`, `ORDINARY_REL`, `FIXTURE_BASE`/`FIXTURE_EDITED`,
`NOTE_BASE`/`NOTE_EDITED` and `FIXTURE_ONLY_YAML` verbatim from
`tests/unit/test_frozen_paths_enforced.py`. Every test deletes
`MO_ALLOW_FROZEN_EDIT` (monkeypatch) unless it sets it.

**Safety-events DB.** `dbp = tmp_path / "state.db"`; create the table with
`sqlite3.connect(dbp).executescript((REPO / "db/migrations/0036_safety_events.sql").read_text())`
(`REPO = Path(__file__).resolve().parents[2]`); `monkeypatch.setenv("MINI_ORK_DB", str(dbp))`.
Columns: `id, ts, tripwire_id, run_id, recipe, severity, status, evidence_json, ...`;
`status` defaults to `'open'`; an empty `run_id` is stored as NULL.

Tests:

- **A refusal writes one row.** `land_patch(str(repo), <patch editing FIXTURE_REL>, "", run_id="run-x")`
  returns an `Abstain` with reason `publish-frozen-path`; HEAD is unchanged;
  `safety_events` has exactly one row with `tripwire_id='frozen-kernel-edit'`,
  `severity='high'`, `run_id='run-x'`, `status='open'`, and
  `json.loads(evidence_json) == {"run_id": "run-x", "paths": [FIXTURE_REL], "boundary": "publish"}`.
- **An ordinary patch writes nothing.** A patch editing only `ORDINARY_REL`
  returns a 40-hex sha and leaves `safety_events` empty.
- **An override is recorded.** With `MO_ALLOW_FROZEN_EDIT=1`, the frozen patch
  returns a 40-hex sha and writes exactly one row with
  `tripwire_id='frozen-kernel-override'`, `severity='medium'`, evidence
  `boundary == "publish"`.
- **No run id is fine.** `land_patch(str(repo), <frozen patch>, "")` (no `run_id`)
  still refuses; the row's `run_id` is NULL and its evidence `run_id` is `""`.
- **The refusal never depends on the event.**
  - `MINI_ORK_DB` pointing at an empty sqlite file (no `safety_events` table):
    still `Abstain("publish-frozen-path", ...)`, HEAD unchanged.
  - Monkeypatch `mini_ork.stores.safety_events.emit` to raise: still the same
    Abstain; `publisher_authored_patch._record_frozen_event("frozen-kernel-edit", "high", ["a"], run_id="r")` returns `False`.
- **The publisher passes the run id.** Monkeypatch, on the module
  `mini_ork.cli.publisher_authored_patch`: `resolve_authored_patch` to return
  `AuthoredPatch(patch_text="diff --git a/x b/x\n", source="carry-patch")`;
  `report_foreign_hunks` to return `[]`; `land_patch` to a fake that records its
  kwargs and returns `Abstain("publish-frozen-path", "d")`. Write
  `run_dir / "review-verdict.json"` as `{"verdict": "approve"}`. Call
  `mini_ork.cli.publisher._publisher_try_commit_files(str(tmp_path), str(tmp_path), str(run_dir), "", "", "code-fix", "implementer", "run-y")`:
  it returns `False`, the fake saw `run_id == "run-y"`, and
  `run_dir / "publish-abstain.json"` has `reason == "publish-frozen-path"`.
- **R1f.** Copy the `home` fixture, `RUN`, `T0`, `_seed`, `_event`,
  `_seed_abstain` and `_assert_contract` verbatim from `tests/unit/test_diagnose.py`
  (`from mini_ork.recovery import diagnose as dx`). After `_seed_abstain(home)`,
  overwrite `home / "runs" / RUN / "publish-abstain.json"` with
  `{"reason": "publish-frozen-path", "detail": "refusing to publish: mini_ork/recovery/auto_repair.py is frozen"}`.
  `dx.diagnose(home, RUN, write=False)` returns `rule_id == "R1f"`,
  `(layer, stage, node) == ("kickoff", "deliver", "publisher")`,
  `routes_to == "owner"`, `source == "rule"`, `confidence == 0.95`, an evidence
  item with `ref == "publish-abstain.json"`, and passes `_assert_contract`.
  `mini_ork.recovery.auto_repair._escalatable(result)` is `False`.
- **R1f wins over R1b.** The same with `_seed_abstain(home, levels_ok=False)`
  still gives `rule_id == "R1f"`.
- **R1 is unchanged.** `_seed_abstain(home)` with its default abstain artefact
  still gives `rule_id == "R1"`, `layer == "engine"`, `routes_to == "fix_epic"`.

## Verification commands

- `pytest -q tests/unit/test_frozen_refusal_events.py tests/unit/test_frozen_paths_enforced.py tests/unit/test_publisher_authored_patch.py`
- `pytest -q tests/unit/test_diagnose.py tests/unit/test_auto_repair.py tests/unit/test_escalation.py tests/unit/test_safety_events_py.py`
- `pytest -q tests/unit/test_rsi_worktree_py.py tests/unit/test_publisher_delegated_outcome.py tests/unit/test_publisher_optional_outputs.py tests/unit/test_frozen_kernel.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_frozen_refusal_events.py --junitxml="${MINI_ORK_RUN_DIR}/frozen-refusal-events-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/frozen-refusal-events-junit.xml`

## Success criteria

- All verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/cli/publisher_authored_patch.py mini_ork/cli/publisher.py mini_ork/recovery/diagnose.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
