# Synthesis — warp practices ranked for mini-ork

Inputs: `lens-conventions.md`, `lens-pipeline.md`, `lens-architecture.md`,
`lens-agent-ux.md`. Every `path:line` below was opened somewhere in this
benchmark — by me in this node, or by the lens report that first cited it —
before it was used here. Relative paths resolve against the reference root
`/Volumes/docker-ssd/ps/refs/warp-ref`; mini-ork's copies of `AGENTS.md`,
`CONTRIBUTING.md` and `.github/workflows/ci.yml` collide with warp's, so every
mini-ork citation of an ambiguous filename is written as an absolute path under
`/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench`. Nothing here is cited
from memory.

---

## 1. Verdict

Warp's distinguishing practice is not its 80-crate workspace or its 39 KB CI
matrix; it is that **a rule is given a machine before it is given a paragraph**.
Concretely: `.clippy.toml:1-8` turns four "don't use the obvious thing" rules
into compile errors whose `reason` string *is the instruction*, and
`script/presubmit:9-29` orders the local checks so formatting and the
inexpensive shape check fail before the expensive test compile. Mini-ork is not
behind on writing rules down — it is often ahead, and its hooks
(`.githooks/reference-transaction:5-17`) record the actual failure story that
motivated each rule, which warp never does. What mini-ork is behind on is the
last mile: it writes the check and then does not run it. `mini-ork features
check-skill` exists, is documented as "what makes … a *guarantee* rather than a
habit" (`mini_ork/cli/features.py:10-12`), and is invoked by no CI job, no hook
and no Makefile target; `mini-ork validate` and `mini-ork garden` are listed as
pre-commit quality gates (mini-ork's `AGENTS.md`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/AGENTS.md:87-88`) and appear in none of the five CI
jobs (`/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:17-25`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:27`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:58`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:142`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:192`). The
single highest-value practice to import is therefore not a warp artefact at all
— it is warp's *habit* of wiring the check it already wrote, applied to the
three checks mini-ork has already written.

---

## 2. Ranked improvements

Ranked by value ÷ effort, not by how impressive the reference looks.

### 1. Wire the three already-written gates into CI

- **Evidence** (reference): `.github/workflows/feature_flag_cleanup.yml:42-52`
  — a lifecycle rule that a scheduled job actually executes, rather than a TODO
  in a doc; and `script/presubmit:21` + `script/check_no_inline_test_modules:19-41`
  — the inline-test rule is enforced by a script that prints the fix and
  `exit 1`, wired into the one local presubmit entry point.
- **Current state** (mini-ork): three checks exist and nothing runs them.
  `mini_ork/cli/features.py:1-12` documents `check-skill` as the thing that
  makes registry→skill sync "a *guarantee* rather than a habit";
  `mini_ork/cli/features.py:192-199` implements it and returns 1 on staleness;
  `docs/operator/feature-flags.md:324-331` and `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/AGENTS.md:87-88` advertise
  `validate`/`garden` as gates. `grep` over the subject tree finds `check-skill`
  only in the CLI, its unit test, the operator doc and the skill; the five CI
  jobs (`/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:17-25`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:27`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:58`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:142`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:192`) run
  none of the three.
- **Change**: add two steps to the existing `python-lint` job — `mini-ork
  features check-skill` and `mini-ork validate` (and `mini-ork garden` once its
  exit code is confirmed stable) — after the ruff step at
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:44`. No new job, no new workflow file. Delete the
  claim from `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/AGENTS.md:87-88` if the step is not added: an advertised gate that
  runs nowhere trains readers to ignore the whole list.
- **Why it pays**: the specific failure is a feature registered in the registry
  but not re-rendered into `skills/wizard/SKILL.md`, so the wizard silently
  omits a knob that the CLI exposes. The check that detects it costs
  milliseconds and already passes — it just never runs. Same class as warp's
  documented-but-unrun `--all-features` clippy claim
  (`CONTRIBUTING.md:194` vs `.github/workflows/ci.yml:667`), which the pipeline
  lens found in warp and which mini-ork should not reproduce.
- **Effort**: S — a dozen lines in one workflow file.
- **Risk**: low. Wiring an unrun check can go red on the current tree, which is
  the point; if `validate` is noisy, gate it on `--strict` off first and promote
  later. Two-tier: this is CI only (shared state for the team, no secrets, no
  network); the local tier is unchanged and still fails closed via the pre-push
  hook.
- **Acceptance**: append a junk feature to `mini_ork/features/registry.py`,
  push, and assert the `python-lint` job goes red naming the stale skill block;
  revert and assert green. Then
  `python3 -c 'import yaml;d=yaml.safe_load(open(".github/workflows/ci.yml"));print(d["jobs"]["python-lint"]["steps"])' | grep -c "check-skill"` returns ≥1.

### 2. Make the "no silent fallback" rule a deterministic check with the fix in the message

- **Evidence** (reference): `.clippy.toml:1-3` — `disallowed-macros` entry
  `{ path = "std::dbg", reason = "dbg!() is only for use in local testing, not
  submitted code" }`, and `.clippy.toml:6-8` — `std::time::Instant` disallowed
  with the replacement named in the `reason`. The agent receives the rule at the
  exact moment it would violate it, as a compile error, not a paragraph.
  `script/check_no_inline_test_modules:19-26` prints the literal fix block
  before `exit 1` at `:41`.
- **Current state** (mini-ork): the rule is prose plus an LLM reviewer.
  `recipes/code-fix/prompts/implementer.md:59-61` bans "silent exception
  swallowing" and "returning default on error"; it is injected as
  `forbidden_fallbacks` via `mini_ork/context_assembler.py:323-329`; the only
  check is a reviewer instruction at `recipes/code-fix/prompts/reviewer.md:57-58`.
  The injector itself violates the rule: `mini_ork/context_assembler.py:320-321`
  and `:330-331` are both bare `except Exception: pass`, swallowing a failed
  config read and falling back to empty lists silently.
- **Change**: add a deterministic AST pass — in `mini_ork/cli/validate.py`
  beside `_check_kickoff` (`mini_ork/cli/validate.py:110-126`), or as a
  `code-fix` verifier beside `recipes/code-fix/verifiers/test.py` — that flags
  the narrow parseable shapes (`except: pass`, `except Exception: pass`,
  `except Exception: return None`) and emits each finding with a `Fix:` hint,
  matching the existing findings contract (`mini_ork/cli/validate.py:44-46`).
  Run it in the `code-fix` verifier node, not only on demand. Then fix or
  explicitly annotate the two instances in `context_assembler.py` — a rule that
  its own enforcement layer breaks is not a rule.
- **Why it pays**: a reviewer-only rule is enforced at the strength of the
  weakest lane. A free-tier reviewer that approves a swallowed exception turns
  a loud failure into a wrong-but-green run, and the diff looks minimal, which
  is exactly what the implementer prompt optimises for
  (`recipes/code-fix/prompts/implementer.md:66-68`).
- **Effort**: S — one AST walk, ~60 lines, plus the two-line cleanup.
- **Risk**: medium-low. The pattern is narrow on purpose; widening it to "any
  `except`" would fail the whole tree. Two-tier: purely local/static — no
  secrets, no network, no shared state, and it fails closed (a parse error is a
  finding, not a pass).
- **Acceptance**: `python3 -m pytest tests/unit/test_<new_check>.py -q` passes
  (file path, never a `-k` name filter). Then run the check against
  `mini_ork/context_assembler.py` — it must exit non-zero naming
  `mini_ork/context_assembler.py:320` and `:330` with a `Fix:` line; clean those
  two sites and it must exit 0.

### 3. Replace the unordered quality-gate list with an ordered validation policy

- **Evidence** (reference): `AGENTS.md:41-50` — a five-step order with a stated
  objective ("Optimize for fast delivery and let CI catch uncommon failures"),
  a while-editing rule (smallest targeted check only), a stopping rule ("open or
  update the PR without rerunning tests or lint after formatting and without
  adding a full `./script/presubmit` run"), and an explicit audience override at
  `AGENTS.md:50`. `script/presubmit:9-29` is the executable twin: format →
  inline-test shape check → clippy → … → tests.
- **Current state** (mini-ork): `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/AGENTS.md:82-89` is a flat four-command block
  ("make test / make lint / mini-ork validate / mini-ork garden") with no order,
  no while-editing rule, and no stopping rule. `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/AGENTS.md:91-94` claims CI
  mirrors them and names a "UI typecheck" that no CI job runs
  (`/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:17-25`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:27`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:58`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:142`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:192`).
- **Change**: rewrite `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/AGENTS.md:82-94` as an ordered sequence — while editing,
  run the single changed test file; when done, `make lint` → `mini-ork validate`
  → targeted pytest; the full suite only before push; CI catches the rest. Keep
  `mini-ork garden` out until it is in CI (item 1). Point
  `recipes/code-fix/prompts/implementer.md:70` at this section instead of
  restating the test-invocation rule a second time.
- **Why it pays**: this cost is measured in-repo, not hypothetical.
  `recipes/code-fix/prompts/implementer.md:76-79` records a bare `-k` filter
  spawn 11 workers × ~900 MB on an 11-core box, load average 124–138 for 8+
  minutes. The benchmark run that produced this synthesis carries the same
  guard as a hard constraint — the failure reproduced. An unordered gate list is
  an invitation to run the most expensive command first.
- **Effort**: S — edit one section, delete one duplicated paragraph.
- **Risk**: low. Prose only; no behaviour changes. The one real risk is
  documenting an order CI does not honour, which item 1 and item 5 fix.
- **Acceptance**: `grep -c '^1\.' AGENTS.md` finds the numbered policy; a
  reviewer reading only `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/AGENTS.md:82-94` can state what to run while editing
  and what CI is expected to catch.

### 4. Delete (or mechanically reconcile) the stale contributor docs

- **Evidence** (reference): warp keeps two manuals and *declares* the
  divergence in writing — `AGENTS.md:50` says the agent section "replaces the
  broader pre-push presubmit guidance in `CONTRIBUTING.md`". It is not immune
  itself: `AGENTS.md:221` tells contributors to add a flag variant to
  `warp_core/src/features.rs`, which is a one-line re-export
  (`crates/warp_core/src/features.rs:1`), while the enum lives elsewhere — a
  stale-path drift of exactly the class described here.
- **Current state** (mini-ork): four verified stale surfaces.
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/CONTRIBUTING.md:21` instructs `git checkout -b fix/...` from main, which
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/AGENTS.md:29-31` says a `reference-transaction` guard *blocks*.
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/CONTRIBUTING.md:24-25` and `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/CONTRIBUTING.md:60` mandate `bash tests/smoke.sh` and
  `shellcheck lib/*.sh` as the primary harness; `tests/smoke.sh` does not exist
  and the test harness is pytest (`Makefile:74-75`).
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/PULL_REQUEST_TEMPLATE.md:14-16` and `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/PULL_REQUEST_TEMPLATE.md:26` repeat both, plus a
  nonexistent `lib/config.sh`. `tests/README.md:19-20` lists `tests/e2e/` and
  `tests/optimize/` — `ls tests/` shows `unit/ integration/ live/
  langgraph_parity/ fixtures/ _apply_loop_fixtures/` and neither of those.
  `.pre-commit-config.yaml:23-30` scopes shellcheck to `(bin/|lib/|hooks/|tests/|…)*.sh`,
  which matches nothing in a Python repo.
- **Change**: prefer deletion. Rewrite `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/CONTRIBUTING.md:20-40` and `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/CONTRIBUTING.md:58-67` to
  the pytest/worktree reality or delete the sections; replace
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/PULL_REQUEST_TEMPLATE.md:12-30` with the four checks that exist;
  fix the `tests/README.md:16-25` table; delete the shellcheck and bash-syntax
  hooks at `.pre-commit-config.yaml:23-38` and keep `schema-validate-yaml`
  (`.pre-commit-config.yaml:40-42`). Then extend the **existing**
  `scripts/readme_claim_check.py` (`scripts/readme_claim_check.py:1-27`, already
  a blocking CI job at `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:17-25`) to probe every
  backticked script/path in `CONTRIBUTING.md`, the PR template and
  `tests/README.md` for existence, the way it already probes README claims.
  Do not add a new checker; the mechanism and its CI job already exist.
- **Why it pays**: a coding agent that reads `CONTRIBUTING.md` first — a
  plausible default, since it is the file named "contributing" — runs
  `bash tests/smoke.sh`, gets "No such file", and either reports a bug against
  the harness or improvises. The improvised run is unreproducible across lanes.
  Warp has the same class of bug and the same lack of a machine; mini-ork has
  more of them and already owns the checker that would catch all four.
- **Effort**: M — four doc files plus ~40 lines of probe extension.
- **Risk**: low for deletions; medium for the probe (a false positive on a
  deliberately illustrative path will annoy). Use an explicit skip list rather
  than loosening the match. Two-tier: the checker is CI (shared state) and runs
  locally via the pre-push hook (`.githooks/pre-push:8-12`), which hard-blocks;
  both tiers read the repo only, no secrets, no network.
- **Acceptance**: add `` `tests/e2e/` `` to the README table, run
  `python3 scripts/readme_claim_check.py`, assert non-zero and a message naming
  `tests/e2e/`; revert and assert zero. Then `grep -n 'smoke.sh' CONTRIBUTING.md
  .github/PULL_REQUEST_TEMPLATE.md` returns nothing.

### 5. Add one aggregate `ci-result` job

- **Evidence** (reference): `.github/workflows/ci.yml:881-910` — a final job
  whose own comment says it exists so "the only required check is this 'gather'
  job, simplifying the process of adding and removing jobs"; it `needs` every
  quality job (`:893-901`), runs under `if: !cancelled()`, and emits
  `::error::Job <name> had unexpected status` for any result other than the
  expected success/skipped (`:907-910`).
- **Current state** (mini-ork): five jobs and **zero** `needs:` relations
  (`grep -n 'needs:' .github/workflows/ci.yml` returns nothing; jobs at
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:17`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:27`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:58`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:142`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:192`). Branch
  protection would have to enumerate all five, and adding a sixth silently
  leaves it unrequired.
- **Change**: add a `ci-result` job with `needs: [readme-claim-check,
  python-lint, python-tests, verifiers-pin, web-smoke]`, `if: always()`, and a
  short step that fails unless every dependency is `success` (or deliberately
  skipped). Do not touch the existing jobs.
- **Why it pays**: a renamed, added or conditionally skipped job currently
  leaves the workflow green with no signal. The `python-tests` matrix already
  carries per-shard `if: matrix.shard == …` guards
  (`/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:109`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:123`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:129`); a typo in one of those
  strings skips a shard and the run still reports success. I found no recorded
  mini-ork incident of this, so treat the value as structural, not remedial.
- **Effort**: S — ~20 lines in one file.
- **Risk**: low, but note the pipeline lens's caveat: branch-protection
  settings are not visible from either checkout, so this only helps if
  `ci-result` is then made the single required check. Two-tier: CI only; the
  local tier is unaffected.
- **Acceptance**:
  `python3 -c 'import yaml;d=yaml.safe_load(open(".github/workflows/ci.yml"));j=d["jobs"];assert set(j["ci-result"]["needs"])==set(j)-{"ci-result"}'`.
  Then open a PR that deliberately skips one shard and confirm `ci-result` reds.

### 6. Require numbered testable invariants in the kickoff, not just a heading

- **Evidence** (reference): `CONTRIBUTING.md:92-95` defines the product-spec
  core as "a numbered list of **testable behavior invariants**" and the tech
  spec as required Context/Proposed-changes/Testing sections;
  `specs/GH408/product.md:53-61` is the worked example — seven numbered,
  individually observable criteria.
- **Current state** (mini-ork): `mini_ork/cli/validate.py:107-126` checks only
  that the kickoff has a recognizable Goal/Done-When heading and a size cap.
  Any prose under "Done When" passes. The runtime's own design already records
  why this is too weak: `docs/plans/2026-10-03-spec-driven-delivery.md:13-18`
  requires acceptance gates derived before implementation and cites shipped
  vacuous specs (20/32 in one audited wave) and rubric self-reports mistaken for
  verdicts.
- **Change**: extend `_check_kickoff` to warn (then error) when the Done-When
  section contains no numbered list, and require each item to name an
  observable. Point the planner at the numbers — `recipes/code-fix/prompts/planner.md`
  already forwards `constraints` (`:28`, `:85`), so the invariants become
  plannable units without a new artefact type. Add one worked example under the
  existing `specs/` directory rather than creating a new docs tree.
- **Why it pays**: an unnumbered "Done When: the feature works" is not
  checkable, so the verifier node falls back to the LLM reviewer's opinion — the
  failure mode the SDD plan documents. Numbering makes each invariant a gate
  the verifier can bind to.
- **Effort**: S–M — a regex and a count in `validate.py`, plus one example;
  medium if existing kickoffs must be migrated.
- **Risk**: medium — 52 recipes ship `example-kickoff.md` files and some
  in-flight kickoffs will fail the new check. Land it as a warning for one
  release, then promote (the same ratchet `pyproject.toml:114-131` already uses
  for ruff).
- **Acceptance**: `mini-ork validate <a kickoff with an unnumbered Done When>`
  exits non-zero with a `Fix:` hint; a numbered kickoff exits 0.

### 7. Give the sharded unit tests a per-test timeout

- **Evidence** (reference): `.config/nextest.toml:1-4` — `slow-timeout =
  { period = "30s", terminate-after = 2 }`, commented with the intent: mark
  slow at 30 s, kill at 60 s. `.config/nextest.toml:6-11` additionally weights
  integration tests at `threads-required = 2`.
- **Current state** (mini-ork): the only bound is the job-level
  `timeout-minutes: 30` at `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:66`, whose comment already
  reasons about hangs ("a shard that needs more is a hang or a regression worth
  seeing", `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:64-65`) but cannot distinguish a hang from a slow suite.
- **Change**: add `pytest-timeout` to the test dependencies, and run the unit
  shards with a generous per-test limit (`--timeout=300`) at
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:113-129`; leave the `rest` shard unlimited, since
  integration-shaped tests legitimately take longer. Start loose and tighten
  only after observing the distribution.
- **Why it pays**: one hung subprocess turns a five-minute fix into a
  30-minute red build and yields "job timed out" instead of a test name. This
  is **preventive**: I found no recorded hang incident in the repo, and the
  architecture lens reached the same conclusion independently. Ranked last for
  that reason.
- **Effort**: S — one dependency and one flag.
- **Risk**: medium-low — a too-tight timeout kills legitimate slow tests, and
  the suite has files near 4% of total runtime
  (`/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:71-73`). Fail closed: a timeout is a failure, never
  a skip.
- **Acceptance**: add a `time.sleep(400)` test in a scratch file under
  `tests/unit/`, run the unit-a shard command from
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:113-116`, and assert the run kills that test in
  ~300 s and names it, rather than hitting the 30-minute job limit.

---

## 3. Practices mini-ork already does as well or better

A benchmark that only lists gaps has not been done. These are not consolation
prizes; in each case mini-ork is ahead of the reference on the same axis.

1. **Failure-story-in-the-guard.** Warp writes rules; mini-ork writes the
   incident. `.githooks/reference-transaction:5-17` names the foreign commit
   that caused the corruption (`3fdeeb4 "Fix DigitalOcean dark mode logo"`),
   the exact symptom ("the 8432 deletions corruption"), and why the fix does not
   depend on the consumer being updated. `.githooks/post-commit:2-15` does the
   same for the concurrent-dispatch file-reversion loop and admits the
   root cause is still unknown. Nothing in warp's `AGENTS.md` or `script/` ties
   a rule to the failure that produced it.
2. **A guard that verifies nothing must fail.** `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:170-190`
   parses its own JUnit XML and fails when the pinned-API test is *absent or
   skipped*, with the comment stating why: "pytest exits 0 having verified
   nothing and this job goes green on a broken pin — the exact failure it exists
   to catch." Warp's closest analogue is a nextest retry count
   (`.config/nextest.toml:13-24`); it has no vacuity gate.
3. **The release gate is the CI matrix, not a narrower copy.**
   `.github/workflows/release.yml:11-23` calls `uses: ./.github/workflows/ci.yml`
   and makes `release` `needs: verify`, with a comment recording that the
   previous inline gate was narrower and blind to the sharded suites. Warp's
   release graph is a separate 142 KB definition
   (`.github/workflows/create_release.yml:1`); pipeline lens confirmed the two
   are not derived from one another.
4. **Docs whose claims are pinned by tests.** `docs/README.md:35-44` states "A
   doc whose claims nothing checks rots silently" and lists four docs pinned to
   named tests; `tests/unit/test_docs_commands_py.py` proves every cited
   `mini-ork <verb>` exists. Warp explicitly *excludes* its hand-written docs
   from build triggering (`.github/workflows/ci.yml:90`, `:97`), so a stale
   `AGENTS.md` is the one file guaranteed never to fail a build.
5. **The empty-shard guard.** `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:108-116` fails the
   shard when its file list is empty, with the reason recorded: `xargs` with no
   input would run bare pytest — the whole suite — green. That is the same
   vacuity class as (2), applied to the *selector* rather than the test.
6. **A two-tier definition of done, written down.** `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/AGENTS.md:96-103` requires
   every feature to ship a secure/cloud tier and a simple/local tier, states
   that the simple tier must fail closed ("a missing value is a clear error,
   never a silent empty string"), and names the reference implementation. Warp
   has no equivalent written policy. Relevant here because the two-tier check
   is a hard constraint on this benchmark's own recommendations.

---

## 4. Rejected candidates

Practices that look good in warp and are wrong for mini-ork. Reasons are scale,
language, or failure-mode mismatch — not taste.

- **An 80-crate workspace split with workspace-level dependency tables**
  (`Cargo.toml:3`, `Cargo.toml:25-31`). The split buys parallel Rust
  compilation, per-crate ownership and a second front-end that links the app as
  a library (`crates/warp_tui/Cargo.toml:73`). mini-ork is one Python
  distribution (`pyproject.toml:7`, `:95`) with no compilation step; splitting
  it would add import ceremony and buy zero build time. The architecture lens
  puts this best: mini-ork "has no crate-level dependency rule to violate, and
  no shape that needs one yet."
- **A nightly agent that opens cleanup PRs for stale flags**
  (`.github/workflows/feature_flag_cleanup.yml:42-52`, `:176-178`). mini-ork's
  `mini_ork/features/` registry holds *user-facing knobs with cost multipliers*,
  not rollout gates; "remove the knob" is a product decision, not a mechanical
  rewrite of call sites. Adopting this would produce plausible PRs that delete
  product surface. The cheap half — running the drift check that already exists
  — is item 1 instead.
- **The three-tree spec corpus** (`specs/`, `agents/specs/`, `.agents/specs/`,
  293 + 19 + 9 files). The agent-ux lens found the same ticket in two trees
  under different filenames, and the conventions lens found the documented
  naming convention describes the minority case (117 `APP-*` vs 25 `GH*`
  directories). mini-ork's kickoff already *is* the product spec; adding
  directories would reproduce the retrieval failure, not the discipline. Item 6
  imports the invariant-numbering rule only.
- **A repo-wide `disallowed-types`/`disallowed-macros` lint table**
  (`.clippy.toml:1-13`). The mechanism depends on a compiler that can name a
  symbol and refuse to build. Python's equivalent is a linter plugin, and
  mini-ork's blocking ruff tier is deliberately minimal (`F` + `E9`,
  `pyproject.toml:114-131`). Item 2 adopts the *shape* — deterministic check,
  fix in the message — for one narrow rule, not a policy table.
- **Scheduled cross-platform test matrices with cache warmers**
  (`.github/workflows/populate_build_cache.yml:1-26`). Pure cost control for a
  Rust workspace whose builds dominate CI time. mini-ork's shards already run
  in 10.8–16.1 minutes (`/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:61-66`); adding pip caching is
  a reasonable small win but not worth a benchmark slot, and a cache-warming
  workflow would be pure ceremony at this scale.
- **`CHANGELOG-*` markers in the PR template**
  (`.github/pull_request_template.md:28-48`) with an agent classifier. mini-ork
  merges via `push HEAD:main` without PRs (`/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/AGENTS.md:47`), so there is no PR
  template to hang the marker on; the analogue would have to be a commit-message
  trailer, and mini-ork's `CHANGELOG.md` is hand-maintained for releases only.
  Different delivery model, not a gap.

---

## 5. Disagreements between lenses

Reported, not adjudicated. Same-family agreement on a stylistic judgement is
not evidence, and the instructions are explicit that two lenses agreeing does
not settle it.

1. **Adopt the two-doc spec genre, or only the invariant numbering?**
   `lens-conventions.md` (candidate 3) recommends adopting "the two-doc spec
   genre with numbered testable invariants and `path:line`-anchored tech specs"
   as a contributor convention with named exemplars. `lens-agent-ux.md` closes
   by explicitly *not* recommending it: "copying warp's three-tier spec tree" is
   rejected, and only the invariant-numbering discipline is recommended. Both
   cite the same reference (`CONTRIBUTING.md:92-95`, `specs/GH408/product.md:53-61`).
   I took the narrower position (item 6), but the wider one is defensible for
   the planner prompt, and the two lenses genuinely disagree about whether a
   new artefact type is warranted.
2. **Is mini-ork's CI immature or ahead?**
   `lens-pipeline.md` treats the five independent jobs with no `needs:` as a
   false-green risk and recommends a gather job. `lens-architecture.md`
   concludes mini-ork is *ahead* of warp on the vacuity axis (the skip-detects
   gate at `/Volumes/docker-ssd/ps/mini-ork-worktrees/warp-bench/.github/workflows/ci.yml:170-190`) and on release gating
   (`.github/workflows/release.yml:11-23`). Both are true on their own axis — job *topology* vs
   guard *quality* — but they support opposite headlines about the same file.
   I kept item 5 because the two claims do not actually conflict; a reader
   should not take "mini-ork's CI is stronger" as covering topology.
3. **Where procedural knowledge should live.**
   `lens-agent-ux.md` (candidate 2) recommends authoring three real project
   skills in `.agents/skills/` — `add-recipe`, `add-gate-evaluator`,
   `worktree-merge` — granted per node, since the registry currently holds one
   demo skill (`.agents/skills/skill-smoke-demo/`, granted at
   `recipes/skill-smoke/workflow.yaml:15-18`). `lens-conventions.md` and
   `lens-architecture.md` both treat `recipes/<name>/prompts/*.md` as
   mini-ork's extension surface and do not flag the single-skill registry as a
   gap. The disagreement is real: a skill is available to any node and any
   interactive session; a recipe prompt is bound to one node and one run. I
   left it out of the ranked list because the failure it prevents is unproven,
   not because the agent-ux position is wrong.
4. **Warp's two manuals: a model or a warning?**
   `lens-conventions.md` cites `AGENTS.md:50` approvingly as "a repo admitting
   it maintains two operating manuals with different thresholds, and naming
   which one wins." `lens-agent-ux.md` counts the same line under "What this
   costs the repo" as one of "two operating manuals that disagree on purpose,"
   because an agent's behaviour then depends on which document it loaded first.
   Same citation, opposite valence. Item 4 resolves the *stale* case only; the
   declared-divergence question is left open.

---

## Lens reports

- `lens-conventions.md`
- `lens-pipeline.md`
- `lens-architecture.md`
- `lens-agent-ux.md` (byte-identical duplicate also present as `lens-agent_ux.md`)
