# recipe: refactor-audit

A multi-model audit recipe that dogfoods mini-ork to audit a codebase
(including mini-ork itself) for scalability, security, performance,
or architectural shape — composed via 4 model-lens stances + 1
synthesis pass.

## What this recipe does

Given a kickoff describing **what to audit** and **what dimensions to
audit on**, the recipe:

1. **Classify** — routes to `refactor_audit` task class
2. **Plan** — generates an audit plan with 4 lens nodes + 1 synthesis
   node (the verifier_contract is "every lens produced a report at
   `${MINI_ORK_HOME}/runs/<id>/lens-<name>.md`")
3. **Execute** — dispatches the 4 lenses in **parallel** (each runs
   under a different model lane per workflow.yaml node.model_lane):
   - **glm-lens** → tactical bottleneck scan (fast + broad)
   - **kimi-lens** → code-level refactor diffs (long-context)
   - **codex-lens** → LLM-dispatch / cost optimization (deep
     code-intelligence)
   - **opus-lens** → architectural shape + synthesis perspective
4. **Verify** — checks all 4 lens reports exist + non-empty + cite
   file:line anchors
5. (Out-of-band) **Reflect** — gradients written from each lens's
   trace; pattern emergence detects recurring findings across past
   audits

## Output artifacts

- `${MINI_ORK_HOME}/runs/<run_id>/lens-glm.md`
- `${MINI_ORK_HOME}/runs/<run_id>/lens-kimi.md`
- `${MINI_ORK_HOME}/runs/<run_id>/lens-codex.md`
- `${MINI_ORK_HOME}/runs/<run_id>/lens-opus.md`
- `${MINI_ORK_HOME}/runs/<run_id>/synthesis.md` — composed final audit
- (Optional) `docs/refactor/<slug>-AUDIT.md` published by the
  publisher node

## Tracker de-duplication (before publishing)

A lens panel reads code, and code cannot tell it that the repo already has an
issue for this defect — or that a maintainer already declined the change in a
closed thread. The `tracker_dedupe` node runs after synthesis and before the
completeness verifier: it searches the target repo's issues and PRs (open and
closed) for each finding's primary symbol, and when a tracker **title** matches
that symbol it moves the finding out of `synthesis.md` into
`already-reported.md` (restored verbatim, with the issue/PR links). A weaker
overlap is listed as "possible" and left in the report — dropping a real
finding costs more than shipping one duplicate.

| Knob | Default | Effect |
|---|---|---|
| `MO_AUDIT_TARGET_REPO` | git `origin` of the cwd | `owner/name` the de-dupe searches |
| `MO_AUDIT_DEDUPE_OFF` | unset | `1` skips the check (`status: "skipped"`) |

The node abstains — `pass: true`, `status: "abstained"`, with a reason — when
`gh` is missing or the repo cannot be resolved; it never reports a check it did
not run. `pass: false` (and therefore a publisher refusal, since the envelope
is gating) means duplicates **were** found and the rewrite could not be written.

## When to use

- Before a major release — audit your own framework for what to fix in v0.x+1
- When user-reported issues cluster on a single subsystem — audit that subsystem
- Quarterly check-up — schedule via `make audit-mini-ork` cron

## When NOT to use

- For specific bug fixes — use `recipes/code-fix/` (audit is overkill)
- For shipping a feature — use `recipes/bdd-first-delivery/` (audit
  produces analysis, not patches)
- Sub-1K LOC codebases — manual audit is faster

## Cost expectation

| Scale | Estimated cost | Wall-clock |
|---|---|---|
| 1K LOC, single concern | $1-3 | 3-5 min |
| 50K LOC, full framework | $20-40 | 30-60 min |
| 500K LOC, multi-service | $200-400 | 2-4 hours |

Cost dominated by Opus synthesis (long context). GLM/Kimi/Codex are
cheap-or-free lanes. Configure via `MO_REFACTOR_AUDIT_BUDGET_USD`.

## How to run

```bash
# 1. Write a kickoff describing the audit
cp ~/ps/mini-ork/recipes/refactor-audit/example-kickoff.md ./my-audit.md
# Edit my-audit.md — name the target dir, the dimensions, the depth.

# 2. Dispatch
mini-ork run refactor-audit ./my-audit.md

# 3. The synthesis lands under .mini-ork/runs/<run_id>/synthesis.md
```

## Dogfood note

This recipe is what produced
[`docs/refactor/SCALABILITY-AUDIT.md`](../../docs/refactor/SCALABILITY-AUDIT.md).
The first run was actually composed via the Agent tool (not via this
recipe) because v0.1.1's `llm_dispatch` bare-name issue (audit finding
D-007) blocked real-LLM dispatch. Once v0.2 lands D-007's one-line fix,
this recipe will run end-to-end via mini-ork itself.

## Customization

| Knob | Where | Effect |
|---|---|---|
| Lens count | `workflow.yaml` nodes | Add a 5th lens (e.g. "security-lens") by adding a node + prompt |
| Models per lens | `workflow.yaml` node.model_lane | Swap glm→haiku, opus→sonnet, etc. |
| Synthesis depth | `prompts/synthesis.md` | Customize the cross-lens ranking matrix |
| Output target | `verifiers/audit-published.sh` | Publish to docs/, GitHub Issues, Slack, etc. |

## Closing the loop: a finding is not a contribution

An audit ends with findings, and a finding on a tracker is the most attractive
work on it: on 2026-11-24 a confirmed p3 was reproduced and merged by an
outside contributor within three hours of being filed. Filing without patching
hands the commit away. The published findings therefore feed a `code-fix` run,
which now ends in a PR by itself.

Per finding, write a kickoff that names the issue, the fix shape, and numbered
Done-When criteria, then run the fix recipe with the outbound contribution
switched on:

```bash
MINI_ORK_TARGET_CWD=/path/to/clone \
MO_CONTRIBUTE_SLUG=owner/repo \
MO_CONTRIBUTE_TITLE="fix(scope): one line, conventional commit style" \
MO_CONTRIBUTE_BASE=main \
MO_CONTRIBUTE_REMOTE=fork \
MO_CONTRIBUTE_HEAD_OWNER=<your-github-handle> \
MO_CONTRIBUTE_BRANCH=fix/<slug> \
MO_CONTRIBUTE_ISSUE=<number> \
  mini-ork run code-fix ./finding-03-pr-body-input.md
```

The `contributor` node runs after the publisher, because publishing is what
commits the patch. It pushes the branch and opens the PR through
`mini_ork.cli.contribute` (`gh auth login` for the simple tier, `GH_TOKEN` for
the secure one; both fail closed). It writes what it did to
`<run>/contribution.json`, and — unless `MO_CONTRIBUTE_STRICT=1` — a failed
push or PR records the failure without failing the run, so a network error
cannot trigger a rollback of an already-published patch.

Two guards the loop depends on:

- **The body.** Write `<run>/pr-body.md` (or set `MO_CONTRIBUTE_BODY_FILE`).
  Without it the PR body degrades to `Fixes #N` plus one line, which reads as
  machine output to every maintainer who sees it.
- **Security findings never take this path.** A run marked `MO_SECURITY_FIX`
  is refused outright by the contributor: the finding goes to the channel named
  in the target's `SECURITY.md`, and a public PR discloses it.

## See also

- `docs/SCALABILITY-AUDIT.md` — example output produced by this recipe
- `docs/EXTENSION.md` — adding new lenses
- `docs/SAFETY.md` — bounded-autonomy ladder (audits are rung-1: tune
  prompt wording based on findings; promotion to rung-7 not allowed)
