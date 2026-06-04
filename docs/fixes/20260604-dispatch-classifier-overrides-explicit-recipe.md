# Fix-spec — `bin/mini-ork run <recipe>` ignores the explicit recipe arg

**Discovered:** 2026-06-04, during Wave 1 oracle-hardening dispatch
attempt (kickoffs/oracle-w1-{a,b,c,d}-*.md).

**Symptom**

Invoking `bin/mini-ork run code-fix kickoffs/oracle-w1-c-cw-por.md` results
in the framework writing `task_class=generic` to the run-id log, then
the planner subsequently fails its plan-validation gate because the
`code_fix` task class's verifier contract is not loaded — the run is
dispatched against the `generic` task class instead.

```
[STATUS] dispatch start kickoff=w1-c-cw-por ts=2026-06-04T22:58:14Z
task_class=generic                              ← ignored explicit 'code-fix' arg
workflow_version=latest
kickoff=kickoffs/oracle-w1-c-cw-por.md
run_id=run-1780613894-53465
```

**Root cause hypothesis**

`bin/mini-ork run` accepts `<recipe>` as positional arg #1 but
nonetheless invokes `bin/mini-ork-classify` against the kickoff content
to derive `task_class`. The classifier is keyword-count-based and
routes any kickoff that lacks `code_fix`-vocabulary heavily to
`generic`. The explicit arg is captured but appears to be discarded
between dispatch handoff steps.

**Sister bug (D-015 forensics)**

When the kickoff is genuinely pure-docs (e.g.
`kickoffs/oracle-w1-a-honesty-patch.md`), the planner ALSO rejects the
plan with `verifier_contract.checks is missing or empty`. The
`code_fix` recipe's verifier contract REQUIRES shell-runnable
assertions; pure-docs kickoffs (grep-based `grep -c "term" file.md
returns ≥ 1`) don't fit even though they ARE shell-runnable.

**Proposed fix (two paths, pick one)**

Path A — Honor the explicit recipe arg:
- `bin/mini-ork run` should set `MINI_ORK_RECIPE` from `$1` BEFORE
  invoking the classifier, and the planner should consume
  `MINI_ORK_RECIPE` directly when set rather than re-deriving from
  classifier output.
- Add a `--no-classify` flag (default: on when `$1` is a recipe name)
  so the classifier becomes opt-in.

Path B — Make the classifier rank-by-hits AND honor explicit override:
- The 2026-05-28 cycle already filed `FIX-D-010: Classifier rank-by-hits`
  (state.db task #33). Close that fix-spec by landing the rank-by-hits
  classifier change AND making the explicit arg always win.

**Additional gap — doc-edit task class**

The framework should ship a `docs` task class with a verifier contract
that runs shell-grep / markdown-link / hyperlink-anchor assertions.
`code_fix` is the wrong fit for pure-docs kickoffs. Without this,
contributors are forced to either (a) hand-write doc edits outside the
recipe loop (the W1-A path this session used), or (b) bury grep
assertions in `code_fix` verifier scaffolding that wasn't designed
for them.

**Suggested recipe**

```
recipes/docs/
  workflow.yaml             # nodes: planner, doc-editor (single node),
                            #        grep-verifier, link-verifier
  prompts/planner.md
  prompts/doc-editor.md
  verifiers/grep-assert.sh  # consumes a .grep_assertions section from
                            #   the kickoff and runs each as
                            #   `grep -c <pattern> <file>` with rc=0
                            #   when count >= expected_min.
  verifiers/link-verifier.sh # walks all `[...](...)` links and
                            #   confirms relative paths resolve.
```

**Why this matters now**

Three of the four Wave 1 oracle-hardening sub-epics (W1-B/C/D)
required hand-implementation in this session because the dispatch
path is broken for both pure-docs (W1-A) AND for `code_fix` itself
(W1-B/C/D got `task_class=generic`). The dogfood loop is supposed to
be the framework's own self-improvement substrate — when it's broken
THIS badly, every roadmap epic costs human cycles to ship instead of
mini-ork-cycles.

Fixing this is the highest-ROI mini-ork meta-task remaining: a
working dispatch path multiplies the framework's effective
self-improvement throughput.

**Related**

- Prior fix-spec: `docs/fixes/20260602-preflight-gate-hardening.md`
- Wave 1 commits: 615d899 + 33ba189 + 94d3cfe + f7890a7
- Wave 2 partial: 3dc65ca
- Self-audit synthesis: `docs/refactor/synthesis-latest.md` (esp.
  D-010 + D-008 references)
