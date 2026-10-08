# I5 (revision 2, fresh run) — evidence ledger bound to code state (verdicts are void once the tree moves)

Epic `sdd-i5-evidence-ledger` (kickoffs/sdd-mechanisms/roadmap.md). Today a run's approval
rests on claims that are not bound to code. `implementer-summary.json` says "implemented,
files_changed=5" and the level vector marks `applies` PROVEN from it. Verifier evidence
(`run_dir/verifier_<stem>.json`) carries no record of which tree it judged, so a verdict
from before a revise round still counts after the tree changed.

## Goal

A run-level `run_dir/evidence-ledger.jsonl`. Each row records which acceptance item was
checked, by what, with what verdict, against which exact code state:
`{ts, row_id, ac_id, probe, verdict, tree, log}`. The publisher backs an approval only with
ledger rows whose `tree` equals the tree being published. `implementer-summary.json`
stays advisory text: it never proves anything on its own.

Behaviour-tightening rule (user decision, 2026-10-07): this ships behind a NEW flag
`MO_EVIDENCE_LEDGER` = `0` (default, legacy behaviour byte-identical) | `shadow` (write
the ledger, evaluate the gate, record would-blocks, never block) | `1` (enforce). Mirror
`mini_ork/verify/probe_validity.py` `FLAG` / `mode()` and its publisher wiring exactly.

## Acceptance

- **AC1** — the tree hash is computed from the working tree, not the index.
  `git write-tree` alone hashes the index, which misses in-place edits. Use a temporary
  index:
  1. `GIT_INDEX_FILE=<tmp> git -C <target> add -A`
  2. `GIT_INDEX_FILE=<tmp> git -C <target> write-tree`
  3. delete the tmp index.

  The real index and the working tree must be unchanged afterwards. A row whose `tree`
  differs from the current tree is void: `valid_rows()` excludes it.
- **AC2** — with `MO_EVIDENCE_LEDGER=1`, the publisher refuses to publish
  (`[BLOCK] evidence-ledger: <reason>`, `return 1, "verdict_fail"`) unless the ledger
  holds at least one valid (current-tree) row with `verdict: "pass"`, and no valid row
  with `verdict: "fail"` for the same `ac_id`.
  - Reasons: `no_ledger`, `no_valid_rows` (all rows void), `failing_rows: <ac_ids>`.
  - With `shadow` it writes `run_dir/evidence-ledger-gate.json`
    `{mode, would_block, reasons, rows_total, rows_valid, tree}` and a task_runs note
    `[shadow] evidence ledger would block: <reason>`. It never blocks and prints nothing.
  - With `0` nothing is written and the publisher is byte-identical to today.
  - The reviewer prompt is NOT changed (that is I7): "cites a ledger row" means the
    publisher's lookup, not reviewer text.
- **AC3** — a fixture run whose `implementer-summary.json` claims implemented
  (`status: implemented` or `files_changed > 0`) while the target tree equals the tree at
  `run_dir/pre-implementer-ref` (nothing changed) cannot publish under `1`. Reason
  `implementer_claim_unbacked`. Shadow records it.
- Every `git` subprocess in tree_hash and publish_gate passes `timeout=` (publish_gate
  runs on the publisher path; a slow repo must not hang publication). A timeout counts
  as "cannot evaluate": blocked under `1` with reason `tree_hash_unavailable`.
- **Writers** (only when the flag is not `0`):
  - (a) the generic verifier handler `mini_ork/cli/execute_handlers.py`
    `_handle_verifier`, right after `_run_verifier_ref` returns. It appends one row per
    verifier run: `ac_id` = the verifier JSON's `ac_id` if present, else
    `verifier:<stem>`; `probe` = the script path; `verdict` = `pass` iff rc == 0;
    `log` = the evidence path.
  - (b) `recipes/spec-driven-delivery/verifiers/ledger-writer.py` additionally appends
    one evidence row per smoke gate × clause it already emits to `ledger.jsonl`
    (`ac_id` = clause_id).
  - Ledger writes are enrichment: an exception warns once and never fails the node.
  - Rows are appended, never rewritten.

## Files in scope

- `mini_ork/verify/evidence_ledger.py` (new). Functions: FLAG, mode, tree_hash,
  append_row, load_rows, valid_rows, publish_gate (gate_mode = shadow | enforce).
- `mini_ork/cli/publisher.py`: one gate block right after the probe-validity block
  (~:259-285), same shadow/enforce shape
- `mini_ork/cli/execute_handlers.py`: writer (a) only, in `_handle_verifier`
- `recipes/spec-driven-delivery/verifiers/ledger-writer.py`: writer (b)
- `docs/operator/feature-flags.md`: a `MO_EVIDENCE_LEDGER` row next to `MO_PROBE_VALIDITY`
- `tests/unit/test_evidence_ledger.py` (new)
- `tests/test_sdd_verifiers.py`: only if writer (b) needs a fixture update

Do NOT modify any other file. Leave the level vector (`applies` from
implementer-summary) alone; that is a follow-up.

## Out of scope

- Reviewer prompt changes (I7).
- Flipping the default. `MO_EVIDENCE_LEDGER` stays `0`; enabling shadow in a home is a
  separate step.
- The level vector, `verify_proven` (I1) and any `task_runs` CHECK change.

## Tests (`tests/unit/test_evidence_ledger.py`)

- `tree_hash`:
  - matches a manual temp-index hash;
  - changes when a tracked file is edited in place and when an untracked file is added;
  - leaves `git status --porcelain` and the real index byte-identical.
- AC1: append a pass row, edit a file, then `valid_rows()` is empty.
- AC2 enforce:
  - no ledger → blocked `no_ledger`;
  - void rows only → `no_valid_rows`;
  - a valid pass + a valid fail for the same `ac_id` → `failing_rows`;
  - a valid pass → ok.
- Shadow: same inputs never block; the gate json and note are written.
- `0`: no ledger file written by the handler, and the publisher path is unchanged.
- AC3: implementer-summary claims implemented, tree == `pre-implementer-ref` tree →
  blocked `implementer_claim_unbacked` under `1`.
- Writer (a): driving `_handle_verifier` with a stub verifier under `shadow` appends
  exactly one row with the current tree.
- Falsify each gate once (disable the check → the test fails). State it in
  `implementer-summary.json` as `gates_falsified`, with the executed test output.

## Verification command

```bash
env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_evidence_ledger.py tests/test_sdd_verifiers.py tests/unit/test_probe_validity.py tests/unit/test_run_finalization.py tests/unit/test_verdict_hygiene.py tests/unit/test_kickoff_guard.py tests/unit/test_verifier_verdict_guard.py tests/unit/test_verifier_nodes_run.py tests/unit/test_verify_levels.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/verify/evidence_ledger.py mini_ork/cli/publisher.py mini_ork/cli/execute_handlers.py recipes/spec-driven-delivery/verifiers/ledger-writer.py tests/unit/test_evidence_ledger.py` → clean.
- `git diff --stat` touches only the files in scope. Do NOT edit this kickoff file.
- Never commit, rebase, reset, pull or check out another ref: leave the change
  uncommitted on the starting commit. (Revision 1 rebased onto origin/main mid-run and
  its diff swept in 48 unrelated files; the engine now fails that as `impl_moved_base`.)
