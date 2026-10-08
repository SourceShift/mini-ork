# Run flow map data: a run as Ticket → Design → Build → Integrate → Review → (Merge), for the IDE's new Graph view

## Why

The user wants every run's graph drawn as a flow map, not the column DAG:

- **Ticket in:** the left "Ticket in" bar.
- **Design:** Architect → Critic, an orange "revise" arc with an "approved in round N" pill, and
  a score ("9/10").
- **Build:** a fan-out into PACKAGE boxes with Implementer/Tester lanes, each ending in a
  "critic".
- **Integrate:** a pill node listing its test tiers (unit / integration / end-to-end).
- **Review:** a round hub with radial spokes (Alignment, a second AI model, Correctness, Testing,
  Production, Architecture, Security), plus an orange "Fix — then review again" spoke.
- **Merge:** a bar with a checklist (PR ready, CI green) and "Human — makes the final merge".
  It appears **only when the run merged code**.
- **Edge colours:** in progress = blue, sent back = orange, approved = green.
- **Bottom stage chips:** 1 Intake … 6 Merge, click to jump.

This run builds that MODEL in Python. The IDE (Rust) only draws it. The plan is
`/Users/admin/.claude/plans/twinkling-churning-feather.md`.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/run_flow.py` (new)
- `mini_ork/ide_pages/run.py`: ONLY `build`. At `ide_level() >= 2`, add a top-level `"flow":
  run_flow.build_flow(run)` next to `"graph"`, built with a lazy import, fail-soft: on error,
  `"flow": null` plus `errors["flow"]`.
- `tests/unit/test_ide_pages_run_flow.py` (new)

## Data available (reuse; read-only)

- **The run:** `run.py` `_load(home, run_id)` → `Run` with `nodes` (`Node`: id, type,
  role_lane, family = lane, state, start, end, finish, cost, calls), `cols` (DAG layers; retries
  edges already ignored), `card` (status, title, task_state), `run_dir`, `workspace`.
- **The workflow:** `recipes/<recipe>/workflow.yaml` nodes have `type` in {planner, researcher,
  implementer, verifier, reviewer, eval, publisher, rollback} and edges `depends_on` /
  `verifies` / `retries` / `supplies_context_to` / `escalates_to`.
- **Revise rounds:** `<run_dir>/revise/round-<n>.md`. Each starts with `## <node> (<type>)`
  sections naming who sent it back. `revise/current.json` has `{"round", "max_rounds"}`.
- **Checks:** `verifier_<stem>.json` (pass, status, error_summary, checks[]) and
  `*.checks.tsv`. Reuse `run_story`'s check-row helpers; do not re-implement.
- **Review:** `review-<node>.json` (verdict, findings, reasons); `rubric.json` (`items[{label,
  verdict PASS/FAIL/SKIP}]`, `score`, scale 0-8); `verdict.json` / `run-verdict.json` (`levels`
  applies/executes/target/preserve/contract, `levels_decision`); `eval` scores in `run_events`
  / the eval node output (axes correctness/completeness/groundedness/safety) when present.
- **Merge evidence:**
  - the publisher's `[publish] committed N file(s): <sha>` line in `<run_dir>/execute.log` (or
    the live run log);
  - a worktree merge (`run.workspace` merged: use `workspaces.status` / `task_state` rule 3);
  - `rolled-back.json` (rollback ran).
- **Outcome:** `outcome.resolve(run)` (state, tone, text).

## The model (exact JSON)

```
flow = {
  "ticket": {"id": run.id, "title": <kickoff title>, "state": "done"},
  "stages": [ {"key": "intake"|"design"|"build"|"integrate"|"review"|"merge",
               "label": "Intake"|"Design"|"Build"|"Integrate"|"Review"|"Merge",
               "n": 1..6, "state": <stage state>} ],     # merge only when present
  "design":    {"nodes": [step], "revise": [loop]},
  "build":     {"packages": [{"id", "label", "lanes": [{"role", "node": step, "critic": step|null}]}],
                "revise": [loop]},
  "integrate": {"node": step|null, "tiers": [{"label", "state", "passed", "total"}]},
  "review":    {"hub": step|null, "verdict": str, "score": "7/8"|"",
                "spokes": [{"label", "sub", "state", "kind": "rubric"|"level"|"model"|"axis"}],
                "fix": {"rounds": int, "state": "sent_back"|"approved"|"none"}},
  "merge":     null | {"state", "commit": sha|"", "checks": [{"label", "ok": bool}],
                       "human": null | {"label": "Human", "sub": "makes the final merge", "state"}},
  "rollback":  null | {"state": "done", "paths": int},
  "timeline":  [{"t": epoch, "node": id, "event": "start"|"end", "state"}],   # replay
  "legend":    [{"key": "in_progress", "label": "in progress"},
                {"key": "sent_back", "label": "sent back"},
                {"key": "approved", "label": "approved"}]
}
step = {"id", "label", "sub", "lane", "type", "state", "score": str, "do": S.page_link(...)|null}
loop = {"from": id, "to": id, "rounds": int, "approved_round": int|null, "state": "sent_back"|"approved"}
```

**Step state:** one of
- `approved` (node done and passed);
- `in_progress` (running);
- `sent_back` (failed, or sent back by a revise round);
- `failed`;
- `pending`;
- `skipped`.

Each stage state is the worst of its steps (sent_back/failed > in_progress > pending >
approved); an empty stage is `pending`.

## Mapping rules (by workflow node `type`; never by recipe name)

- **design:** `planner`. `researcher` nodes go here too, as parallel readers, when the workflow
  has an implementer.
  - **Label:** planner → "Plan" ("writes the plan"); a researcher → its id humanised ("Code
    impact lens", sub = lane).
  - **Score:** the plan critic score, if a critic/eval node grades the plan; else "".
- **build:**
  - `implementer` nodes. With N implementers, make N packages ("PACKAGE 1…N"); with 1, a single
    unlabeled package.
  - Lane role `"Implementer"`. The critic is a verifier that `verifies` ONLY that implementer in
    a fan-out; otherwise null.
  - With NO implementer (research recipes), the researchers form the build lanes (one package,
    role "Lens").
  - **Revise loops:** `retries` edges whose target is in build, with rounds counted from
    `revise/round-*.md` naming each source. `approved_round` = the round after which the source
    passed.
- **integrate:**
  - `verifier` nodes (and typecheck/test). `node` = a synthetic step labelled "Integrate", sub
    "runs every check tier", whose state is the worst of the verifiers.
  - **Tiers:** one per verifier, label humanised (`static_check_verifier` → "static checks",
    `test` → "tests", `typecheck` → "types"), with passed/total from its check rows.
- **review:**
  - **Hub:** the `reviewer` node (verdict pass → approved, needs_revision/fail → sent_back).
  - **Spokes, in this order:**
    1. every rubric item (label = item label, state pass→approved, FAIL→sent_back,
       SKIP→skipped, kind "rubric");
    2. every level of the level vector except n/a (PROVEN→approved, REFUTED→sent_back,
       UNVERIFIED→pending, kind "level");
    3. a "Second model" spoke when an `eval`/judge node or a second reviewer family ran
       (sub = its lane, kind "model");
    4. eval axes ≥0.7 approved, else sent_back (kind "axis").
  - **Score:** rubric `score/8`.
  - **Fix:** `rounds` = revise rounds whose sections name a reviewer; state sent_back if the
    reviewer's last verdict is not pass, approved if a later round passed, else "none".
- **merge:**
  - ONLY when code merged: a `[publish] committed … <sha>` line exists, or the run's worktree
    was merged. Otherwise `merge: null` and no merge stage.
  - **Checks:** "Review passed" (hub approved), "Checks green" (integrate approved), and
    "Committed <sha7>" when a sha is known.
  - **Human:** present when a worktree exists and is not merged yet (task_state needs_you "merge
    or discard"). Its state is `in_progress`, or `approved` once merged.
- **rollback:** `{state: done, paths: n}` when `rolled-back.json` exists, else null.
- **timeline:** node_start/node_end rows from `run_events` for the run, in order, capped at 400.

## Rules

- Read-only, fail-soft (a broken section yields an empty or null part, never an exception).
- Under 300 ms on a 9-node run.
- Level 1 output is unchanged.
- `do` for a step = `S.page_link("run", "graph", run=run.id, node=<id>)`.

## Tests (`tests/unit/test_ide_pages_run_flow.py`; temp home + workflow fixtures like `tests/unit/test_ide_pages_run_story.py`)

- **framework-edit-shaped run** (planner, 2 researchers, implementer, 2 verifiers, reviewer,
  publisher, rollback; all done; a publish sha in execute.log):
  - design has 3 nodes;
  - build has 1 package with 1 lane;
  - integrate has 2 tiers;
  - review has hub + rubric spokes;
  - merge is present with the sha;
  - stages are exactly 6.
- **Same run with no publish sha and no worktree** → `merge is None`, 5 stages.
- **`revise/round-1.md` and `round-2.md` naming `reviewer`, final reviewer pass** →
  `review.fix == {"rounds": 2, "state": "approved"}`. Naming `test_verifier` → a build revise
  loop with `rounds: 2`.
- **Research recipe** (planner, 4 researchers, reviewer synthesizer, verifier) → the build lanes
  are the 4 researchers.
- **A running node** → its step and stage are `in_progress`.
- **A worktree present and unmerged** → `merge.human.state == "in_progress"`.
- **`MINI_ORK_IDE_SPEC=2`:** `run.build` returns `flow` with these keys. Level 1 has no `flow`.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_ide_pages_run_flow.py tests/unit/test_ide_pages_run.py tests/unit/test_ide_pages_run_story.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/ide_pages/run_flow.py mini_ork/ide_pages/run.py tests/unit/test_ide_pages_run_flow.py` → clean.
- **Live proof (read-only):** for runs `ide-orca-b2b-story-20261008113340` (published code-fix)
  and `sdd-i3-kickoff-contract-20261007181747` (rolled-back framework-edit), print
  `MINI_ORK_IDE_SPEC=2 bin/mini-ork board page run --arg run=<id> --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork`
  `.flow` stages, build packages, integrate tiers, review spokes, merge (null or sha), and
  rollback. Paste them.
- `git diff --stat` touches only the files in scope.
