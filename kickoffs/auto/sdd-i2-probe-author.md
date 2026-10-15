# I2 — authorship separation enforced, held-out probe split, acceptance-probes.json

Epic `sdd-i2-probe-author` (`kickoffs/sdd-mechanisms/roadmap.md:318`).

## Correction to the roadmap — read this first

The roadmap block was written 2026-10-07 and says "add a `probe_author` step". **That
step already exists.** `recipes/framework-edit/workflow.yaml` ships a `spec_test_author`
node (`type: researcher`, `model_lane: glm_lens`, `prompt_ref: prompts/spec-test-author.md`,
`dispatch_mode: serial`) wired `planner → spec_test_author → implementer` by `depends_on`,
plus `verifiers/spec-tests.py` (892 lines) which runs the authored tests with tamper
detection. `prompts/spec-test-author.md` already requires the author to work from the
kickoff alone and to quote the clause each test checks.

So this kickoff **does not add a second author node** — a parallel `probe_author` would
duplicate a mature one. It enforces the *general* contract that the existing node
satisfies only by hardcoding. Three gaps, each verified against the tree at `027ac5c0`
(the verification method is given with each, because "X is missing" is a claim with a
timestamp):

1. **`heterogeneity.authorship_separation` is declared and read by nothing.**
   `grep -rn "implementer_lanes\|contract_and_test_lanes" --include=*.py --include=*.ts`
   → no hits. The declaration lives at
   `recipes/spec-driven-delivery/task_class.yaml:38-48`
   (`contract_and_test_lanes: [planner, reviewer]`, `implementer_lanes: [implementer, worker]`).
   framework-edit enforces separation only through a bespoke verbatim lane map
   (`recipes/framework-edit/verifiers/framework-edit-shape.py:129 _lane_bindings_verbatim`,
   which pins `spec_test_author: glm_lens` by hand and comments that it must differ from
   the implementer). Every other recipe is unguarded.
2. **There is no held-out split.** `verifiers/spec-tests.py:329-330` says of its failure
   output: *"This is the evidence the implementer's revise round reads: it names the
   kickoff clause that failed"*. Every failing clause is therefore fed back to the
   implementer, which can iterate until it passes all of them. Nothing is withheld, so
   nothing measures the difference between "fits the visible probes" and "meets the spec".
3. **No `acceptance-probes.json`.** `grep -rn "acceptance-probes"` → only prose in
   `kickoffs/sdd-mechanisms/roadmap.md:328` and
   `docs/plans/2026-10-07-sdd-mechanisms-for-mini-ork.md:137`. Probes live as
   `run_dir/spec-tests/test_spec_*.py`, in a shape I1's `load_probes` does not read.

## Mechanism

The exact spec of the change, for the blind test author:

A new engine module `mini_ork/verify/authorship_separation.py` mirroring
`mini_ork/verify/probe_validity.py` exactly in its flag idiom:
`FLAG = "MO_AUTHORSHIP_SEPARATION"`, `mode()` returning `off` (default) | `shadow` |
`enforce` (`=1`). It exposes
`check_separation(workflow: dict, task_class: dict) -> tuple[bool, str, dict]`.

- The probe-author node is **identified structurally**, never by name: any node that
  writes acceptance probes — i.e. whose `prompt_ref` resolves to `spec-test-author.md`,
  or whose `type` is `probe_author`. Matching on the literal name `spec_test_author` is
  forbidden: the check must survive a recipe renaming its node.
- Its lane is the node's `model_lane`. The implementer lanes are
  `task_class["heterogeneity"]["authorship_separation"]["implementer_lanes"]`.
- If the probe-author lane is in `implementer_lanes`, the run is **refused** under
  `enforce`; under `shadow` the same finding is written to
  `run_dir/authorship-separation-gate.json`
  (`{mode, would_block, reason, probe_author_lane, implementer_lanes}`) plus a
  `task_runs.notes` line `[shadow] authorship separation would block: <reason>`, and the
  run proceeds. Under `off` nothing is written and behaviour is byte-identical to today.
- Reason strings: `author_lane_is_implementer: <lane>`, `no_probe_author_node` (a
  declaration exists but the workflow has no probe-author node),
  `no_separation_declared` (task_class declares none — never a failure).

`task_class` must reach the check: `mini_ork/planning/recipe_plan.py` reads
`artifact_contract.yaml` today and does not carry `heterogeneity`; thread it through so
the plan (and a shadow re-evaluation) can see the declaration.

**The held-out split.** The probe author marks each probe `held_out: true|false`. A
held-out probe is still *run* and still *counts in the verdict*, but its failing clause
is excluded from the evidence the implementer's revise round reads. The split is by
clause, not by file, and the marking is the author's, bounded: at least one probe must be
held out when the author writes two or more, at most half.

**`acceptance-probes.json`.** The probe author persists the probe set as
`run_dir/gates/acceptance-probes.json`:
`{"spec_id": <epic id>, "probes": [{"id", "probe" (the pytest nodeid or command),
"acceptance_ref", "clause" (the verbatim kickoff quote), "held_out", "expect"}]}`.
That is exactly the shape `mini_ork.verify.probe_validity.load_probes` already reads for
`gates/*.json` — so I1's validity gate binds to it with no new plumbing, which is what
makes AC3 checkable rather than aspirational.

**`risk_class` gating.** The probe author runs when the recipe's `risk_class` is `high`
or `critical`, or when the kickoff is an epic. It is skipped for `trivial` and `low` on a
code-fix, where a blind pytest author costs more than the change. `framework_edit` is
`risk_class: medium` (`recipes/framework-edit/task_class.yaml:32`) and must keep its
author — gate on `trivial`/`low` explicitly, never on "not high".

## Acceptance

- **AC1** — a workflow whose probe-author lane equals an implementer lane is refused under
  `MO_AUTHORSHIP_SEPARATION=1`, with reason `author_lane_is_implementer: <lane>`, and
  proceeds under `shadow` with `would_block: true` in `authorship-separation-gate.json`
  and the task_runs note. `off` writes nothing. The node is matched structurally: a
  fixture that renames the node to `probe_author` and one that keeps `spec_test_author`
  both refuse; a fixture whose author lane is `glm_lens` against `implementer_lanes:
  [implementer, worker]` (today's framework-edit) passes.
- **AC2** — a held-out probe's failing clause never appears in the evidence the
  implementer's revise round reads; assert it on a fixture (author a two-probe set, make
  one `held_out`, fail both, and assert the revise-facing output names only the visible
  one while `verdict.json` counts both).
- **AC3** — every probe in `run_dir/gates/acceptance-probes.json` is readable by
  `probe_validity.load_probes(run_dir, plan)` and every one carries a non-empty `clause`;
  assert on a fixture run. A probe whose `clause` is empty is not a probe of a criterion
  and `load_probes`' own rule (checks with no acceptance mapping are not judged) must not
  be bypassed to admit it.
- **AC4** — with `risk_class: trivial` or `low`, no probe-author node runs; with `high`,
  `critical`, or an epic kickoff, it does. Assert both.

## Files in scope

- `mini_ork/verify/authorship_separation.py` (new) — FLAG, mode, check_separation.
- `mini_ork/planning/recipe_plan.py` — carry `heterogeneity` from the task_class into the
  plan (`overlay_plan` and/or `recipe_fallback_plan`).
- `mini_ork/cli/execute_handlers.py` — call `check_separation` at node dispatch, so a
  refusal happens before the author node is dispatched; mirror the probe-validity wiring.
- `recipes/framework-edit/prompts/spec-test-author.md` — emit the manifest with a
  `held_out` mark and the verbatim clause per probe.
- `recipes/framework-edit/verifiers/spec-tests.py` — write
  `run_dir/gates/acceptance-probes.json`; exclude held-out clauses from the revise-round
  evidence.
- `docs/operator/feature-flags.md` — a `MO_AUTHORSHIP_SEPARATION` row next to
  `MO_PROBE_VALIDITY`.
- `tests/unit/test_probe_author.py` (new).

Do NOT modify any other file. In particular do not touch
`recipes/framework-edit/verifiers/framework-edit-shape.py`: its `exact_node_count == 12`
check and `_lane_bindings_verbatim` map are load-bearing and this change adds no node.

## Out of scope

- Changing the default lanes in `config/agents.yaml` (explicitly out, per the roadmap).
- Adding a second `probe_author` node, or renaming `spec_test_author`.
- Flipping the default. `MO_AUTHORSHIP_SEPARATION` stays `off`; enabling shadow in a home
  is a separate step.
- I1's `verify_proven` / `probe_validity.py` internals, and the reviewer prompt (I7).
- The §"do not expect more visible tests" lesson: this change *hides* probes, it does not
  add any. A probe that would introduce a requirement the kickoff never stated is a defect
  of the author prompt, caught by AC3's `clause` requirement, not by this code.

## Tests (`tests/unit/test_probe_author.py`)

- `check_separation`, pure:
  - author lane in `implementer_lanes` → `(False, "author_lane_is_implementer: ...")`;
  - author lane outside → `(True, "")`;
  - node identified by `prompt_ref` tail `spec-test-author.md` AND by `type:
    probe_author`, under a renamed node id;
  - no probe-author node + a declaration → `no_probe_author_node`;
  - no declaration → passes, never a failure.
- Flag arms: `off` writes nothing; `shadow` writes the gate json and the note and returns
  ok; `1` refuses.
- AC3: driving the writer with a two-probe fixture, then
  `load_probes(run_dir, plan)` returns both, with `clause` non-empty; a probe with an
  empty clause is not admitted.
- AC4: `risk_class` gating for `trivial` / `low` / `high` / epic.
- AC2: the held-out fixture above.
- Falsify each gate once (disable the check → the test fails). State it in
  `implementer-summary.json` as `gates_falsified`, with the executed test output.

## Verification command

```bash
env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_probe_author.py tests/unit/test_probe_validity.py tests/unit/test_evidence_ledger.py tests/unit/test_kickoff_guard.py tests/test_sdd_verifiers.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/verify/authorship_separation.py mini_ork/planning/recipe_plan.py mini_ork/cli/execute_handlers.py recipes/framework-edit/verifiers/spec-tests.py tests/unit/test_probe_author.py` → clean.
- `git diff --stat` touches only the files in scope. Do NOT edit this kickoff file.
- Never commit, rebase, reset, pull or check out another ref: leave the change
  uncommitted on the starting commit.
