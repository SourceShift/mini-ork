# W2-SDD-ratify-a: `mini-ork specs ratify`, the operator's half of the SDD ratification gate

## Goal

spec-driven-delivery's `contract_compiler` writes one record per spec at
`${MINI_ORK_RUN_DIR}/ratification/<spec_id>.json`. Its `ratification[]` entries are real
uncovered requirements or conflicts, each written with `acknowledged: false`
(`recipes/spec-driven-delivery/prompts/contract-compiler.md`, the "only an operator may set
`true`" rule). `recipes/spec-driven-delivery/verifiers/ratification-check.py`
(`check_ratification`) fails on every unacknowledged entry it cannot reclassify as covered.

**Nothing in mini-ork can set `acknowledged: true`.** A spec with a real gap therefore fails
`ratification_check` forever (`docs/audits/2026-10-16-session-gaps/mini-ork-cb.md`, "operator
accept/adjust step is prompt-specified but unimplemented"). The decision on record is
**option A**: an operator ratification gate between `contract_compiler` and
`ratification_check`.

This kickoff builds the operator command only: list what needs ratifying, and acknowledge it
with an audit trail. A follow-up kickoff (W2-SDD-ratify-b) makes the recipe pause after
`contract_compiler` so the operator gets the chance to run it.

Read first:
- `mini_ork/cli/specs.py` (its `_Parser`, `_parser`, `main`, the `_USAGE` text and the handler
  map)
- `recipes/spec-driven-delivery/verifiers/ratification-check.py` (`check_ratification`)
- `mini_ork/gates/node_review.py` (`_refuse_if_in_run`, `_IN_RUN_KEYS`, `ApprovalError`)
- `mini_ork/cli/approvals_cmd.py` (how it resolves `home / "runs" / run_id`)

## Mechanism (exact spec)

### `mini_ork/cli/specs.py`: a new `ratify` subcommand

```
mini-ork specs ratify <run_id> --list [--json] [--run-dir PATH]
mini-ork specs ratify <run_id> --spec <spec_id> (--all | --entry N [--entry N ...]) --note TEXT [--run-dir PATH]
```

- **Run dir:** `--run-dir` when given. Otherwise `Path(os.environ.get("MINI_ORK_HOME") or ".mini-ork") / "runs" / run_id`.
  Missing dir, or missing `ratification/`: print `specs: no ratification records in <dir>` to
  stderr and exit 2.
- **`--list`:** for every `ratification/*.json`, print one line per entry whose `acknowledged`
  is not `True`: `spec_id<TAB>index<TAB>reason<TAB>source_excerpt` (excerpt truncated to 120
  chars, tabs/newlines replaced by spaces). With `--json`, print a JSON array of
  `{spec_id, index, reason, source_excerpt}` instead. Exit 0. Print nothing when everything is
  acknowledged.
- **Acknowledge** (the second form):
  - Refuse inside a run. Call `node_review._refuse_if_in_run(os.environ)`; on `ApprovalError`
    print `specs: <message>` to stderr and exit 2. A human ratifies, never the agent running
    the spec.
  - `--note` is required and must be non-blank (stripped). Otherwise exit 2.
  - Load `ratification/<spec_id>.json`. When it is missing, or its `spec_id` field differs from
    `--spec`: exit 2.
  - `--all` selects every entry. `--entry N` selects index N; it is repeatable and 0-based.
    Any index out of range: exit 2 **without writing anything**.
  - For each selected entry that is a dict, set:
    - `acknowledged: True`
    - `acknowledged_by: getpass.getuser()`
    - `acknowledged_at`: UTC ISO-8601 with a `Z` suffix
    - `acknowledged_note: <note>`

    Already-acknowledged entries are left untouched. A non-dict entry is an error: exit 2,
    write nothing.
  - Write atomically: `tempfile.NamedTemporaryFile` in the same dir, then `os.replace`. Keep
    every other key and the entry order unchanged, and use `indent=2`.
  - Print `ratified <n> entr(y|ies) in <spec_id> (<remaining> still unacknowledged)` and exit 0.
- **Never** touch `spec-cards/` or any other file.
- Add the subcommand to `_USAGE` and to the module docstring. Add `"ratify": _ratify` to the
  handler map.

## Files in scope (touch ONLY these)

- `mini_ork/cli/specs.py`
- `tests/unit/test_specs_ratify_py.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_specs_ratify_py.py`, hermetic, `tmp_path`)

Build a run dir with `ratification/feat.json` = `{"spec_id": "feat", "source_hash": "sha256:x",
"ratification": [ {source_excerpt: "Support CSV export", reason: "no clause", acknowledged: false},
{source_excerpt: "Must be fast", reason: "conflict with C2", acknowledged: false} ]}`.
Call `specs.main([...])` with `MINI_ORK_RUN_ID`/the other in-run keys cleared via
`monkeypatch.delenv(..., raising=False)`.

- `--list` prints 2 lines; `--list --json` parses to 2 items with the right keys.
- `--spec feat --entry 0 --note "accepted: out of scope v1"` returns 0. Entry 0 now has all
  four ack keys; entry 1 is unchanged; `--list` prints 1 line.
- `--spec feat --all --note n` returns 0 and leaves nothing unacknowledged.
- Re-running `--all` leaves the earlier entry's `acknowledged_at` unchanged.
- `--entry 5` returns 2, and the file bytes are unchanged.
- Blank `--note "  "` returns 2, unchanged.
- `--spec other` returns 2.
- With `MINI_ORK_RUN_ID=r1` set (an in-run key), the call returns 2, the file is unchanged,
  and stderr contains `refuses inside a run`.
- Missing run dir returns 2.
- **Gate integration:** load `ratification-check.py` with `importlib.util.spec_from_file_location`
  (the verifier imports `_sdd_common` from its own dir; insert that dir into `sys.path` first).
  With a minimal card `{"spec_id": "feat", "source_hash": "sha256:x", ...}`, enough for
  `check_ratification`, the function returns an `unacknowledged` violation before `--all` and
  no violations after.

## Verification commands

- `python3 -m pytest -q -p no:asyncio tests/unit/test_specs_ratify_py.py`
- `python3 -m pytest -q -p no:asyncio -k "specs or specdir or ratification or sdd" tests/unit`
- `ruff check --select F,E9 mini_ork/cli/specs.py tests/unit/test_specs_ratify_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_specs_ratify_py.py --junitxml="${MINI_ORK_RUN_DIR}/w2-sdd-ratify-cli-junit.xml"
```

## Required artifacts: declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/w2-sdd-ratify-cli-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- `ingest`, `list` and `lint` behave exactly as before.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
