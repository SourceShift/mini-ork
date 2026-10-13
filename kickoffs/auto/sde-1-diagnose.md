# SDE-1 typed run diagnosis (self-driving epic, step 1)

## Goal

A failed run gets ONE typed, evidence-cited diagnosis that says which layer owns
the failure, so the repair loop can pick the right fix instead of handing every
unrecognised failure to a human.

Evidence it is missing today: `run-1791914481-48337` finished every node, its
`verdict.json` is `pass` with `levels_ok: true`, and its publisher abstained
`publish-no-authored-patch` (`publish-abstain.json`). `auto_repair.decide` had no
rule for it and fell through to `human`; `retry_hint._case_unknown`
(`mini_ork/recovery/retry_hint.py`, the case-5 branch) used the
`_latest_impl_log_node` fallback and named `implementer` — a node whose latest
`node_end` finished `done`. The real owner was the engine (delivery stage), fixed
later in `88a3f99e`.

All paths below are relative to the repository root (the worktree you run in).

## Mechanism (exact spec)

1. New module `mini_ork/recovery/diagnose.py` exposing
   `diagnose(home: Path, run_id: str, *, write: bool = True) -> dict`.
   It returns and (when `write`) writes `<run_dir>/diagnosis.json`:

   ```json
   {"version": 1, "run_id": "...", "layer": "engine", "stage": "deliver",
    "node": "publisher", "rule_id": "R1-publish-withheld-levels-proven",
    "source": "rule", "confidence": 1.0,
    "evidence": [{"kind": "file", "ref": "publish-abstain.json", "excerpt": "publish-no-authored-patch"}],
    "routes_to": "fix_epic", "summary": "...", "computed_at": "..."}
   ```

   - `layer` ∈ `change | model | provider | environment | engine | kickoff | user | unclassified`.
   - `stage` ∈ `setup | plan | implement | verify | review | deliver | unknown`.
   - `routes_to` ∈ `revise | swap_lane | retry | owner | fix_epic | kickoff_repair | none | human`.
   - `source` is always `"rule"` in this step (a model diagnoser is a later step).

2. Rules, first match wins. Each rule MUST attach at least one evidence item
   (a file in the run dir, a `run_events` event id, or a log excerpt). A rule that
   cannot cite evidence does not match.
   - **R1** publisher's latest `node_end` has `finish_reason == "publish_abstain"`
     AND the run-level report (`mini_ork.acp.task_state.run_level_report`) has
     `levels_ok` true → `engine / deliver / publisher`, `routes_to: fix_epic`.
   - **R2** `mini_ork.triage.blame.attribute` returns `mini_ork` for the failing
     node → `engine`, `routes_to: fix_epic` (reuse the function; do not copy it).
   - **R3** infra/provider signals already recognised by `auto_repair._is_infra`
     or `failure_classifier` (`infra_interrupt`, `provider_limit`, finish reasons in
     `auto_repair._INFRA_FINISH`) → `provider`, `routes_to: retry`.
   - **R4** retry hint `needs_change.kind == "lane"` → `model`, `routes_to: swap_lane`.
   - **R5** retry hint kind in `environment | credentials | budget` →
     `environment`, `routes_to: owner`.
   - **R6** retry hint kind `interrupted` with a `killed-by-user` note, or kind
     `stopped` → `user`, `routes_to: none`.
   - **R7** failing node type is `reviewer | verifier | eval`, or retry hint kind
     `code` → `change`, `routes_to: revise`.
   - Otherwise → `unclassified`, `routes_to: human`, `confidence: 0.0`.

3. Invariant: `diagnosis.node` never names a node whose LATEST `node_end` finished
   `done`. Apply the same invariant to `retry_hint._case_unknown`: the
   `fallback_name` / `_latest_impl_log_node` fallback must skip a node whose latest
   `node_end` finished `done`; when the publisher abstained, the failed node is
   `publisher`.

4. `auto_repair.decide` calls `diagnose(home, run_id, write=True)` once and adds an
   additive `"diagnosis": {"layer", "stage", "node", "rule_id"}` key to every
   decision it returns. In the final fall-through (rule 8) the `reason` becomes the
   diagnosis summary when the layer is not `unclassified` (e.g. `engine fault at
   deliver/publisher: publish-no-authored-patch`). Do NOT change which action any
   rule returns in this step — routing on the diagnosis is the next step.
   `apply` copies the diagnosis into the attempt it appends to `repair.json`.

5. `python3 -m mini_ork.recovery.diagnose <run_id> [--json] [--no-write]` and
   `--replay [--limit N]` (diagnose the N most recent `failed` runs in
   `task_runs`, never writing to the DB, printing a layer histogram). Guard with
   `if __name__ == "__main__":`. Do not register a new `mini-ork` subcommand.

## Files in scope

- mini_ork/recovery/diagnose.py (new)
- mini_ork/recovery/retry_hint.py
- mini_ork/recovery/auto_repair.py
- tests/unit/test_diagnose.py (new)

## Tests

`tests/unit/test_diagnose.py`, hermetic (temp home + temp sqlite with the
`task_runs` / `run_events` tables the code reads; no LLM, no network):
- the `run-1791914481-48337` shape (all nodes `done`, publisher `publish_abstain`,
  `verdict.json` levels_ok true, `publish-abstain.json` present) diagnoses
  `engine / deliver / publisher` with R1 and cites `publish-abstain.json`;
- for the same shape, `retry_hint.load_or_compute(..., write=False)` no longer
  names `implementer`;
- a reviewer `needs_revision` failure diagnoses `change / revise`;
- a `timeout` finish reason diagnoses `provider / retry`;
- a run with no evidence for any rule is `unclassified` with `confidence 0.0`;
- the `done`-node invariant holds for every case above;
- `auto_repair.decide` returns the same `action` as before for each case and now
  carries the `diagnosis` key.

Run the existing suites too:
`tests/unit/test_auto_repair*.py tests/unit/test_retry_hint*.py` (whatever exists).

## Self-application measurement

Against the live state, read-only:
`MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork python3 -m mini_ork.recovery.diagnose --replay --limit 50 --json --no-write`.
Accepted iff `run-1791914481-48337` comes out `engine/deliver/publisher`, no
diagnosis names a `done` node, and the `unclassified` share is reported.

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/diagnosis-replay.json` — the replay output above.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_diagnose.py` passes, and the existing
  auto-repair / retry-hint unit tests still pass.
- No new required env var; no auto-repair action changes in this step.

## Rules

Edit files directly; do not emit unified diffs. Do not touch execute.py,
publisher*.py, triage/, or any recipe.
