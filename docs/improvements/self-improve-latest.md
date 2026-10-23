# Synthesis — Recursive Self-Improvement, iter 1

Inputs: `lens-bottleneck.md` (rev 4), `lens-perf.md`, `lens-correctness.md`,
`lens-arch.md`, `lens-arxiv.md` from this run dir. Every code anchor used for
ranking was re-read in the worktree at HEAD `20b11c68`. The ledger was re-queried
read-only at ~06:25Z (`$MINI_ORK_DB`). `learning_record` = 0 rows and
`pattern_records` = 0 rows, so no prior iteration's findings exist to dedupe
against. The context pack has no `[f:…]` findings (`file_findings: []`), so no
patch below repeats a recorded `[f:…]` problem.

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | Scan #1 / COR-1 / A4: researcher `bottleneck_lens` sits on lane `planner`. The lane name lands in llm_dispatch's `--node-type` slot and arms the planner's JSON **reply** gate, which shape-rejects the markdown scan and re-runs the fallback chain | correctness (critical) | Recipe tier: move `bottleneck_lens` from `model_lane: planner` to `model_lane: opus_lens` (same model, opus; no gate is keyed on that lane). Add a regression test proving that no prose node in this workflow sits on a lane that arms a shape gate | `llm_calls` ids 1–4 ($4.841139, 21.8 min wall); `workflow.yaml:24`; `execute_handlers.py:1277-1278`; `execute.py:2757-2766`; `llm_dispatch.py:791-793, 828-838, 1012, 1121-1125, 1167-1180`; arXiv 2501.10868, 2609.23742, 2603.27905 | 0.90 |
| 2 | Same defect at engine level: the reply-shape gate is keyed on the **lane label**, not on what the node emits. 3 more researcher nodes sit on lane `planner` (`prompt-graph-loop` ×2, `spec-driven-delivery` ×1) | correctness (critical) | Executor seam forwards the node's role and its declared output kind (`--node-role`, `--expect-shape`). `_shape_predicate_for` gates on the declared contract and keeps the `--node-type` lookup only for direct CLI/plan.py calls | `context.py:161-164` (MO_NODE_TYPE published per node), `execute_handlers.py:956-960`; `prompt-graph-loop/workflow.yaml:13,17,42,50` (JSON outputs); `artifacts.py` never validates `kind: json`; arXiv 2501.10868, 2609.23742 | 0.70 |
| 3 | Scan #2 / H1–H2: `self-tests-pass` runs the whole suite in one serial pytest with no `timeout=` inside a 3600 s iteration that wraps the entire DAG | perf (critical) | Give the verifier a hard timeout derived from the iteration deadline and K stdlib-parallel shards (CI's odd/even split). Count per shard with anchored regexes and sum. On timeout, emit a recorded `budget-timeout` verdict instead of dying to the outer rc 124 | `self-tests-pass.py:235, 251-257, 264-269`; `self_improve.py:493, 515-519, 72-73`; `ci.yml:61-70`; collect-only 11 479 tests in 18.09 s (perf lens); arXiv 2105.07311, 2610.04184, 2311.12484 | 0.60 |
| 4 | Scan #3 / COR-2: `self-tests-pass` is an absolute `rc == 0` gate. A base that is already red rejects every patch, and the kickoff's "non-negative quality delta" is computed nowhere | correctness (critical) | Record a per-test baseline at `pre-implementer-ref` and gate on `failures(patch) ⊆ failures(base)`, keeping `rc == 0` as a fast pass. Depends on rank 3's budget | `self-tests-pass.py:272-278`; `self_improve.py:74-81`; audit doc `:25` (11 failed + 8 errors); arXiv 2506.09289, 2410.06992 | 0.55 |
| 5 | Scan #4 / COR-4: `no-regression` checks only `*.sh`/`bin/*` (an empty surface since the bash removal). With 0 benchmark rows it passes with zero evidence | correctness (high) | Add changed and untracked `*.py` to the surface and run `py_compile` on each. When no surface was checked and the benchmark is inconclusive, fail closed (`reason: "vacuous"`) | `no-regression.py:35-36, 103-109, 125-131`; R-5 executed (`pass: true, changed_sh_files=0`); `benchmark_results` = 0 rows; arXiv 2607.24300 | 0.75 |

Ordering notes:

- Rank 3 (perf) sits above rank 4 (correctness) even though both are critical, which departs from the "correctness > perf" tie rule. The reason is a dependency, not a preference. Rank 4 needs a second (baseline) suite run, and the correctness lens itself flags that this "doubles an already ~59-min serial suite inside a 60-min budget". Rank 4 cannot land safely before rank 3.
- Scope polarity + `lstrip("./")` (COR-5/COR-6) is the sixth candidate. The 5-patch cap drops it to the unranked backlog, but it is the smallest backlog diff and the best candidate to bundle next.

## Top patch — detailed plan

### Patch 1: Take the bottleneck scanner off the `planner` lane (same model, no reply gate)

**Problem statement.** `bottleneck_lens` is a `type: researcher` node whose
deliverable is the markdown file `lens-bottleneck.md`, but its `model_lane` is
`planner`. The executor forwards the **lane** in llm_dispatch's `--node-type`
slot, so the planner's JSON reply gate arms on a prose node. Every lane's reply
is shape-rejected, and the whole fallback chain is re-run by the shape-reject
retry. That multiplies cost and wall time for a file that lane 1 had already
written.

**Evidence.**

- Ledger (this run, `llm_calls`, read-only query):
  - id 1 opus: 392 849 ms, $3.184097, failed `dispatch result failed shape check (looks_like_json)`
  - id 2 deepseek_flash: 367 400 ms, $0.126698, same error
  - id 3 minimax: 251 283 ms, $0.084590, same error
  - id 4 opus: success, $1.445754
  - All four rows have `feature_name='mini-ork:planner'`.
  - Node total: **$4.841139 and 21.8 min wall** (05:48:22Z → 06:10:10Z), out of a 60-min iteration.
  - For comparison, all four downstream lenses together (ids 5–8) cost **$0.798776**. The scanner alone cost **6.06×** the rest of the research phase.
- Dispatch transcript `lens-bottleneck.md.stdout.md`:
  - lines 2/4/6: `lane <x> produced wrong shape (rc=65 …); trying next`
  - line 7: `[llm_dispatch RETRY model=opus,deepseek_flash,minimax reason=shape_reject attempt=1/3 sleep=5s]`
  - The scanner then replied in JSON **on purpose** to get past the gate (its own `reply_format_note`). A lane gaming a gate is the signature of a gate on the wrong surface.
- Code path, re-read this pass:
  1. `recipes/recursive-self-improve/workflow.yaml:24` sets `type: researcher, model_lane: planner`.
  2. `mini_ork/cli/execute_handlers.py:1277-1278` calls `self.dispatch_fn(self.task_class, self.lane, prompt)`, putting the lane in the seam's `node_type` slot.
  3. `mini_ork/cli/execute.py:2757-2766` forwards it as `--node-type <lane>`.
  4. `mini_ork/dispatch/llm_dispatch.py:791-793` keys `_NODE_SHAPE_PREDICATES = {"planner": looks_like_json}` on that slot. `:1012` arms `accept_fn`. `:1121-1125` hands it to the chain dispatcher (per-lane shape fallback). `:1167-1180` retries a shape reject up to `MO_DISPATCH_MAX_ATTEMPTS` (default 3, `:1005`), so the bound is 3 lanes × 3 attempts.
  5. The code's own invariant at `llm_dispatch.py:784-786` is broken: "only nodes whose artifact is *known* to be JSON are gated, so prose-emitting nodes (researcher, …) are never false-rejected".
  6. `self_improve.py:505-512` documents that the same failure already killed iterations one layer up; the outer planner was skipped for exactly this reason.
- Correction to the scan's mechanism (it does not change the fix): `execute.py:2759-2764` always passes `--model`, so the `:1029-1035` expansion (`not model_override`) is skipped. The 3-lane chain came from the executor-published `MO_DISPATCH_CHAIN`, and the shape fallback happened inside it via `accept=` (`:1121-1125`).
- Lane facts:
  - `opus_lens` resolves to `opus` in both `config/agents.yaml:44` and the home's `.mini-ork/config/agents.yaml:60`, and in this run's `run_profile.json` (`opus_lens → opus / claude-opus`).
  - `_shape_predicate_for("opus_lens")` is `None` (correctness lens R-1a; the table has only `planner`).
  - `planner`, by contrast, maps to opus, sonnet or glm depending on which config wins (`run_profile.json`, `config/agents.yaml:16`, `.mini-ork/config/agents.yaml:24`).
  - So `opus_lens` also pins the scanner to the model that actually produced this run's scan.
- No test pins the lane: `tests/test_web_smoke.py:1148` asserts only the node **name**.
- arXiv (all from `lens-arxiv.md`):
  - **2501.10868** (JSONSchemaBench): schema enforcement belongs to the declared output contract, not to a routing label.
  - **2609.23742**: a structural (shape) check is necessary-not-sufficient and must not stand in for the artifact verifier. Here it ran on the reply instead of the artifact.
  - **2603.27905** (ATLAS-RTC): prevent the violation instead of paying for retries. Removing a deterministic mis-gate is the degenerate case of that, since retrying a deterministic reject is the same experiment run again.
- Not new infra: one YAML value changes and one test file is added.

**Proposed change.** Two files.

1. `recipes/recursive-self-improve/workflow.yaml`, line 24: change
   `model_lane: planner,` → `model_lane: opus_lens,` on the `bottleneck_lens`
   entry. Change nothing else on that line. Extend the comment block above it
   (`:20-23`) with:
   ```yaml
   # Its lane is opus_lens, NOT planner: the executor passes the lane in
   # llm_dispatch's --node-type slot, and lane `planner` arms the planner's
   # JSON reply gate (_NODE_SHAPE_PREDICATES), which shape-rejected every
   # markdown scan and re-ran the fallback chain (iter-1-20261023054815:
   # 4 lanes, $4.84, 21.8 min). Same model (opus), no reply gate.
   ```
2. New file `tests/unit/test_rsi_workflow_shape_gate.py`. The kickoff permits
   adding a regression test, and the test is the whole guard.

Do **not** touch `mini_ork/`, `.mini-ork/`, any verifier, or any other recipe.
`.mini-ork/` is listed as allowed in constraint `c:0` only because of the
negation-parsing defect (COR-5). The kickoff says "Leave `.mini-ork/` (run
state) untouched", and that wins.

**Regression test.** `tests/unit/test_rsi_workflow_shape_gate.py`. It must fail
on the base (lane `planner`) and pass after the change:

```python
"""recursive-self-improve: no prose node may sit on a lane that arms a reply-shape gate.

2026-10-23 (self-improve-iter-1-20261023054815): bottleneck_lens is a researcher
that writes lens-bottleneck.md, but its model_lane was `planner`. The executor
passes the lane in llm_dispatch's --node-type slot, so the planner JSON gate
rejected every markdown reply and re-ran the chain: 4 lanes, $4.84, 21.8 min.
"""
from pathlib import Path

import pytest
import yaml

from mini_ork.dispatch import llm_dispatch as L
from mini_ork.dispatch.predicates import SHAPE_REJECT_RC

WORKFLOW = (Path(__file__).resolve().parents[2]
            / "recipes" / "recursive-self-improve" / "workflow.yaml")


def _nodes():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["nodes"]


def _prose_nodes():
    return [n for n in _nodes()
            if n.get("model_lane") and n["type"] not in L._NODE_SHAPE_PREDICATES]


def test_workflow_has_laned_prose_nodes():
    assert {"bottleneck_lens", "perf_lens", "opus_synthesizer"} <= {
        n["name"] for n in _prose_nodes()}


@pytest.mark.parametrize("node", _prose_nodes(), ids=lambda n: n["name"])
def test_prose_node_lane_does_not_arm_shape_gate(node, monkeypatch):
    monkeypatch.delenv("MO_SHAPE_CHECK", raising=False)
    monkeypatch.delenv("MO_PLAN_ARTIFACT", raising=False)
    assert L._shape_predicate_for(node["model_lane"]) is None, (
        f"{node['name']} ({node['type']}) is on lane {node['model_lane']!r}, "
        "which arms the planner's JSON reply gate")


def test_bottleneck_scan_is_dispatched_once_on_a_markdown_reply(monkeypatch, tmp_path):
    # Same isolation as tests/unit/test_llm_dispatch_shape_fallback.py::_isolate.
    monkeypatch.setattr(L, "cost_circuit_open", lambda *a, **k: False)
    monkeypatch.setattr(L, "write_llm_calls_row", lambda *a, **k: None)
    monkeypatch.setattr(L, "backoff_seconds", lambda n: 0)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("MO_DISPATCH_MAX_ATTEMPTS", "3")
    for key in ("MO_SHAPE_CHECK", "MO_FUSE_ENABLED", "MO_PLAN_ARTIFACT"):
        monkeypatch.delenv(key, raising=False)
    lane = next(n["model_lane"] for n in _nodes() if n["name"] == "bottleneck_lens")
    calls = []

    def lane_writes_markdown(model, prompt, out_file, timeout_s, max_turns):
        calls.append(model)
        Path(out_file).write_text("# Bottleneck Scan\n\nmarkdown, not json\n")
        return SHAPE_REJECT_RC

    out = str(tmp_path / "out.txt")
    L.llm_dispatch(["--node-type", lane, "--model", lane, "--prompt-text", "x",
                    "--out", out], dispatch_fn=lane_writes_markdown)
    # One lane run. On lane `planner` the shape-reject retry re-runs it 3x.
    assert calls == [lane]
```

The assertions that must hold, in plain text:

- (a) `_shape_predicate_for(model_lane) is None` for every node of
  `recursive-self-improve` whose `type` is not a key of `_NODE_SHAPE_PREDICATES`.
- (b) Dispatching `bottleneck_lens`'s lane with a markdown reply invokes the
  lane exactly once: `calls == [lane]`. On the base this is
  `["planner", "planner", "planner"]`.

**Verification.**

- New test file: passes after the change. It fails on the base: (a) fails for
  `bottleneck_lens`, and (b) yields 3 calls.
- Existing tests that must stay green, measured green on the base this pass
  (**154 passed in 83.87 s**):
  - `tests/unit/test_llm_dispatch_shape_fallback.py`
  - `tests/unit/test_dispatch_shape_check_py.py`
  - `tests/unit/test_mini_ork_execute_py.py`
  - `tests/unit/test_mini_ork_plan_py.py`
  - `tests/unit/test_remote_unavailable_retry.py`
- Also `tests/test_web_smoke.py` (node-name assertion at `:1148`), and any test
  that loads `recipes/*/workflow.yaml`.
- Run these as explicit file paths (resource guard). Kickoff gate:
  `python3 -m pytest -q`.
- Expected deltas on the **next** iteration's `bottleneck_lens`:
  - `llm_calls` rows for the node: 4 (bound 9) → **1**.
  - Wall time: 1308 s → **~290–395 s**. The range is this run's two single opus lanes (attempt-2 lane ≈ 1308 − 1011.5 − 5 s backoff; attempt-1 lane 392.8 s). That is **−913 to −1018 s (−70 % to −78 %)**, i.e. 15–17 min of the 60-min iteration returned to the verifier stage that rank 3 shows is starved.
  - Cost: $4.84 → **~$1.45–$3.18 (−34 % to −70 %)**.
  - Rows carrying `dispatch result failed shape check`: 3 → **0**.
  - No change to pass/fail counts of the existing suite.
- Side effect to expect, and not to treat as a regression: the node's ledger
  rows change from `feature_name='mini-ork:planner'` to `'mini-ork:opus_lens'`,
  the same label as `opus_synthesizer` and `opus_patch_critic`. Per-node
  attribution still comes from `metadata_json.node_id` (`web/agents.py:514`).
  Fixing lane-keyed attribution is backlog.

**Rollback criteria.** Discard the patch if any of these hold:

1. The new test file fails after the change, or any of the five files above
   (154 tests) or `tests/test_web_smoke.py` regresses.
2. `python3 -m pytest -q` shows **any failure not present on the base**. The
   absolute `rc == 0` gate (rank 4) may reject for pre-existing reds; compare
   failure sets before blaming this patch.
3. The diff touches anything besides `recipes/recursive-self-improve/workflow.yaml`
   (one `model_lane` value plus the comment) and the new test file, or exceeds
   ~80 changed lines.
4. In the next iteration, `opus_lens` fails lane preflight for a researcher
   node, `lens-bottleneck.md` is not written, or any `llm_calls` row for
   `bottleneck_lens` carries a shape-check error.

Restore `model_lane: planner` in each case. The emergency lever, unchanged, is
`MO_SHAPE_CHECK=0` (`llm_dispatch.py:829`).

**Implementer notes.**

- Keep the diff to the two files.
- Run the targeted test files only. The full suite is the verifier's job, and
  this iteration's budget is nearly spent (see Convergence).
- Do not "fix" the bottleneck-scan prompt's JSON-reply workaround. It becomes
  unnecessary but harmless.

## Lower-ranked patches

### Patch 2 — Key the reply-shape gate on the node's declared contract (engine tier)

- **Problem.** Patch 1 removes one instance. The engine trap stays armed for any
  node whose lane happens to be named after a JSON-contract role:
  - `prompt-graph-loop` `prompt_intake` and `recursive_plan_composer`
    (`workflow.yaml:13,42`) are researchers on lane `planner`.
  - `spec-driven-delivery` `contract_compiler` (`workflow.yaml:14`) is a
    researcher on lane `planner`.
  - The two `prompt-graph-loop` nodes declare `kind: json` outputs (`:17,50`),
    and `ArtifactLedger.publish_node_outputs` never validates `kind`. Their only
    JSON check today is this accidental reply gate, which false-rejects an
    agentic lane that writes the file and replies with a summary (the same
    failure as `_plan_artifact_predicate`'s docstring incident, `llm_dispatch.py:799-820`).
  - That is why this patch is not rank 1: a role-only disarm would silently
    remove those nodes' only JSON check.
- **Change.**
  - In `NodeDispatch.dispatch` / `_default_llm_dispatch.d`, forward two values as argv:
    - `--node-role <MO_NODE_TYPE>`, already published per node at `execute_handlers.py:956-960` via `context.py:161-164`;
    - `--expect-shape json|text`: `json` iff the node declares a `kind: json` output, else `text`.
  - In `llm_dispatch.llm_dispatch`, parse both. `_shape_predicate_for(node_type, node_role="", expect_shape="")`:
    - `expect_shape == "text"` → `None`;
    - `expect_shape == "json"` → `looks_like_json`, or the artifact-file predicate when the declared path exists;
    - neither flag → today's `--node-type` lookup, so the `cli/plan.py` path is unchanged.
  - Read the role and shape only from argv. Do not read them from `context_env`
    inside llm_dispatch: `publish_env` leaks `MO_NODE_TYPE` into `os.environ`
    forever (`context.py:268-283`), and a nested `mini-ork run` planner would
    inherit a parent node's role and lose its gate.
- **Regression test.**
  - `_shape_predicate_for("planner", "researcher", "text") is None`
  - `_shape_predicate_for("planner", "researcher", "json") is not None`
  - `_shape_predicate_for("planner") is L._NODE_SHAPE_PREDICATES["planner"]` (back-compat)
  - Seam: with `run_context_scope({"MO_NODE_TYPE": "researcher"})`, argv contains `["--node-role", "researcher"]`.
  - Hazard: `tests/conftest.py` does not pop `MO_NODE_TYPE`, and
    `test_mini_ork_execute_py.py:160-185` asserts an exact argv. Append the new
    flags at the end, only when non-empty, and run tests with `env -u MO_NODE_TYPE`
    when inside a node.
- **Rollback.** Revert if any planner JSON-contract path (epic-runner via
  `MO_PLAN_ARTIFACT`) starts failing shape checks it used to pass, or if
  `prompt-graph-loop` JSON nodes start publishing non-JSON outputs.
- **arXiv:** 2501.10868, 2609.23742. Not new infra.

### Patch 3 — Bound and parallelize `self-tests-pass` (perf)

- **Change** (`recipes/recursive-self-improve/verifiers/self-tests-pass.py`):
  - Export the iteration deadline from `self_improve.py:493` (e.g.
    `MINI_ORK_SELF_IMPROVE_DEADLINE` = epoch of `start + per_iter`). Pass
    `timeout = deadline − now − 120 s` to every `subprocess.run`.
  - On `TimeoutExpired`, write `exc.output` to the evidence log and emit
    `pass: false, reason: "budget-timeout"`.
  - Run K = `min(6, max(2, cpu_count − 2))` shards via `ThreadPoolExecutor`,
    using CI's interleave (`ci.yml:66-70, 96-101`): `tests/unit/*.py` split
    `NR % 2`, plus a `--ignore=tests/unit` rest shard.
  - Add the override `MINI_ORK_SELF_IMPROVE_TEST_JOBS`; `1` gives serial.
  - Count each shard with `re.search(rf"(\d+) {word}\b", …)` and `errors?` as one
    alternation, then **sum**. This also fixes the double count (R-2: reports 27
    vs a true 19).
- **Probe first:** time `python3 -m pytest tests/unit -q` once on this box. The
  3533.93 s audit figure and CI's ~14–17 min comment disagree by ~3.5×. Then time
  the two CI shards concurrently to rule out shared-state collisions (perf lens
  open question 3).
- **Expected:** verifier wall 3534 s → ~700 s at K = 6 (perf lens PC-1
  arithmetic), and a recorded verdict instead of outer rc 124.
- **Rollback:** summed shard counts ≠ serial counts on the same tree, or any
  shard goes red on a lock/fixture error that is absent serially.
- **arXiv:** 2105.07311 (RTS for APR patch validation), 2610.04184 (compact
  evaluator under budget, with truncation reported), 2311.12484 (time-budgeted
  prioritization; needs an explicit inconclusive outcome).

### Patch 4 — Make `self-tests-pass` a delta gate (correctness, after Patch 3)

- **Change.**
  - Run the same sandboxed command once against the worktree at the
    `pre-implementer-ref` sha (already in the run dir), using `--junitxml` for
    per-test ids. Persist `verifier-self-tests-baseline.json`.
  - Pass iff `rc == 0` **or** `failed_ids(patch) ⊆ failed_ids(base)`. Report the
    `new_failures` and `fixed` sets.
  - Cheaper first cut: baseline only the test files that would be selected for
    the diff.
- **Regression test:** for a unit helper `failures_subset(child, base)`, the
  result is `False` iff `child − base` is non-empty. Also, a fixture tail
  `11 failed, 9895 passed, 15 skipped, 8 errors` parses to `failed == 19`.
- **Rollback:** two baseline runs on the same sha disagree on the failure set
  (flakiness). Then fall back to `rc == 0` plus a recorded `baseline_red` marker.
- **arXiv:** 2506.09289 (UTBoost: 345 false "passed" patches), 2410.06992
  (SWE-Bench+: a verified pass must distinguish the patch from the base).

### Patch 5 — Give `no-regression` a Python surface and make it fail closed (correctness)

- **Change** (`recipes/recursive-self-improve/verifiers/no-regression.py:35-36, 125-131`):
  - Build the surface from `git diff --name-only HEAD -- '*.py' '*.sh' 'bin/*'`
    plus `git ls-files --others --exclude-standard -- '*.py'`.
  - Run `python3 -m py_compile` on each `.py` and `bash -n` on each `.sh`.
  - If zero files were checked **and** the benchmark is inconclusive, set
    `pass: false, reason: "vacuous"`.
- **Regression test:** in a scratch git repo, a changed `.py` with a
  `SyntaxError` → `pass: false` naming the file. An untracked `.py` with a
  `SyntaxError` → `pass: false`. An empty diff with no DB →
  `pass: false, reason == "vacuous"`.
- **Rollback:** a legitimate docs-only iteration is blocked. Add that surface
  rather than reverting the fail-closed default.
- **arXiv:** 2607.24300 (SEAL: the audit must be exogenous, diff-based and fail
  closed).

### Unranked backlog (queued for `learning_record`, not this iteration)

- **COR-6 + COR-5, scope prefix and polarity.**
  - `entry.strip().lstrip("./")` → `.removeprefix("./")` at
    `publisher_authored_patch.py:219` and `scheduler.py:950` (R-4:
    `_in_scope('.mini-ork/state.db', ['.mini-ork/'])` is `False`). This is a
    2-line fix.
  - Route paths on negated lines ("leave … untouched", "do not touch",
    "read-only") to `deny` in `kickoff_scope.py:170-208` and
    `preferences.py:237-249`.
  - arXiv 2601.17789, 2603.16417. This was the sixth candidate; bundle the
    `removeprefix` half with the next small patch.
- **COR-7.** One `## Status`-style convergence heading anywhere in the scan
  makes `bottlenecks-found.py:57-61` pass with no synthesis (R-6). Require
  `not missing`, or move the convergence verdict to a runner-owned sidecar.
- **COR-8.** `recipe_plan.py:52` asserts "Planner LLM output was invalid JSON"
  even under `MO_STATIC_RECIPE_PLAN=1`. That fabricated provenance is fed to
  every node.
- **COR-10.** `self_improve.py:558-563` ignores the `git commit` rc before
  `rev-parse HEAD`.
- **PC-4.** A shape reject (deterministic for a given node) is retried like a
  throttle; make shape retries chain-scoped, and keep throttle and fair-usage
  retries intact.
- **COR-1 secondary.** The final `llm_calls.duration_ms` spans the whole retry
  loop (`llm_dispatch.py:1043, 1192-1194`); the column is not summable per
  dispatch.
- **A1.** Node `gates:` are display-only (326 refs, 40 workflows). The two gate
  tables never meet, and `verify.py:567-574` passes `cost_usd: 0.0`.
- **A2.** Outputs are routed by node-id suffix (`execute.py:2906-2940`) although
  the schema has `outputs`.
- **A3 / scan #8.** One static Anthropic rate tuple (`telemetry.py:42-48`): opus
  breakdown 3.91× its envelope (id 1), 2.65× (id 4). Reporting only.
- **Ledger attribution.** `feature_name` is `mini-ork:<lane>`, so `arch_lens`
  and `arxiv_lens` are indistinguishable (ids 5 and 7, both
  `mini-ork:codex_lens`).
- **Test hygiene.** `tests/conftest.py` pops `MINI_ORK_DB`/`MINI_ORK_HOME` but
  not `MO_NODE_TYPE`/`MO_NODE_ID`. This is a prerequisite for Patch 2.

## Convergence assessment

mini-ork is **not** converging. The scan has eight evidence-backed bottlenecks,
four of them critical, and the cross-iteration tables are empty, so there is no
history of resolved items. The loop's liveness is also gated by three of the
ranked patches:

- **Rank 1:** node 1 consumed 36 % of the iteration.
- **Rank 3:** the full suite may not fit the remaining budget.
- **Rank 4:** a red base blocks every promotion.

Until these land, an iteration cannot reliably reach a verdict, let alone show
diminishing returns.

Feasibility warning for **this** iteration:

- The outer `timeout` is `min(3600, time_left)` from ~05:48:16Z, so it expires
  around **06:48Z**.
- At synthesis time (~06:30Z) about 18 minutes remain for the implementer, a
  serial full suite, `no-regression`, and the patch critic.
- If the runner records `timed_out` (rc 124, `self_improve.py:72-73`), that is
  **not** a verdict on Patch 1. Re-queue it as rank 1 rather than recording it
  as failed. Its small diff and targeted tests make it the right first patch for
  the next iteration too.

Following DGM (arXiv 2505.22954), acceptance should rest on a measured delta.
Rank 1's expected delta (−70 to −78 % node-1 wall time) is directly measurable
from `llm_calls` on the next iteration.

## Provenance footer

- **Lenses consumed** (family as actually routed, from `run_profile.json` and `llm_calls`):
  - bottleneck scan: opus via lane `planner`
  - perf: MiniMax-M3 via `minimax_lens` (`llm_calls` id 6)
  - correctness: **GLM-5.3 via `kimi_lens`** (id 8). The lens self-labels "Moonshot Kimi", but the home's policy remaps `kimi_lens → glm`.
  - arch and arXiv: **deepseek-v4-flash via `codex_lens`** (ids 5, 7), not OpenAI Codex
  - The research phase therefore covered four distinct families (anthropic, minimax, zai, deepseek). The kickoff's "kimi"/"codex" families were not the ones that ran.
  - Note: per `run_profile.json`, the implementer lane `codex_lens` is also deepseek_flash on this home.
- **Synthesizer family:** opus (Anthropic).
- **arXiv papers cited:** 12. All appear in `lens-arxiv.md`: 2501.10868, 2609.23742, 2603.27905, 2105.07311, 2610.04184, 2311.12484, 2506.09289, 2410.06992, 2607.24300, 2601.17789, 2603.16417, 2505.22954.
- **Cross-iteration learnings applied:** 0 rows from `learning_record` (table empty, verified by query); `pattern_records` 0 rows.
- **Verification performed by the synthesizer this pass:**
  - re-read every rank-1 code anchor;
  - ledger query (ids 1–8);
  - grep showing no test pins `bottleneck_lens`'s lane;
  - the five existing gate/seam test files run on the base as file paths: 154 passed, 83.87 s;
  - the Patch 1 regression test above, dry-run from a scratch copy (worktree untouched): against the base `workflow.yaml` **2 failed, 8 passed** (`bottleneck_lens` parametrization, and `calls == ['planner', 'planner', 'planner']`); against a copy with `model_lane: opus_lens` **10 passed**.
