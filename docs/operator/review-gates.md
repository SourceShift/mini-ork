# Human review gates, node checkpoints and follow-the-agent

mini-ork can require a human decision at two points: before a run's change
merges into its target branch, and after chosen DAG nodes before the run
continues. Every DAG node also leaves a git checkpoint of the run's worktree
that you can restore. In Zed, the agent panel's follow-the-agent crosshair
jumps to the files a run reads and edits.

Everything below is **off by default**. Turning the gates on holds the epic
scheduler's and the goal loop's auto-merges until a human approves, so switch
them on deliberately.

---

## 1. Turn it on

The policy comes from the environment first, then from
`$MINI_ORK_HOME/config/review.yaml`, then from the defaults (everything off).
An unknown value is an error, never a silent "off".

```bash
cp config/review.example.yaml "$MINI_ORK_HOME/config/review.yaml"
mini-ork approvals policy            # show what is in force
```

```yaml
review:
  before_merge: required      # off | required
  after_nodes: [implementer]  # node types or node ids; "*" = every node
  tier: simple                # simple | secure
```

For a single invocation you can set the same keys as env vars:
`MO_REVIEW_BEFORE_MERGE`, `MO_REVIEW_AFTER_NODES` (comma list, or `all`) and
`MO_REVIEW_TIER`.

### Two tiers

| | simple (a laptop) | secure (shared or cloud) |
|---|---|---|
| Who approves | anyone with the CLI, the IDE or Zed. The approver name is recorded. | the same, but every approval is HMAC-signed |
| Key | none | `MO_REVIEW_SIGNING_KEY`, read through the vault chain. Use a non-`env` backend (sops, openbao, command) so run environments never hold it. |
| Missing key | — | approvals fail closed |
| A run approving itself | refused: `approve` errors when `MINI_ORK_RUN_ID` or `MINI_ORK_RUN_DIR` is set | refused, and an approval with a bad signature never counts |

Approvals live in the existing oversight channel (`mo_inbox_gates`), so
`mini-ork oversight --calibrate` also measures review fatigue.

---

## 2. Before merge (`before_merge: required`)

- **Every CLI run gets a worktree.** `mini-ork run` mints
  `mini-ork/<run_id>` before the profile is generated, so the run never edits
  your checkout. You can also force this per run with
  `--workspace worktree|in-place` or `MO_WORKSPACE_MODE`. A non-git target with
  review required stops the run with rc 2.
- **An approval is bound to the exact content.** It is tied to the base sha
  plus the **tree sha** of the worktree. If the content changes after the
  approval, the approval no longer counts.
- **Every merge path checks for an approval:**
  - `workspaces.merge` (IDE **Merge**, Zed review card, race keep, `/merge`);
  - the epic `auto_merge`;
  - `auto_merge_pr`;
  - the cleaner's squash merges (exit 8, `HELD_FOR_REVIEW`);
  - `self-improve --auto-merge`.
  Automated paths **hold**; the human surfaces treat the click itself as the
  approval (`ide:<user>`, `zed:<user>`).

```bash
mini-ork approvals list                  # pending requests + published workspaces awaiting a decision
mini-ork approvals approve <run>         # approve the current worktree content
mini-ork approvals reject <run> --note "…"
mini-ork board merge <run>               # human merge = approve + merge
```

---

## 3. After a node (`after_nodes: [...]`)

Before each dispatch, the executor asks the gate whether a finished node still
needs a human:

- **park:** the node is not dispatched and the run ends with status `reviewing`
  and exit code **9**. It writes `<run_dir>/.review-pause`. No rollback, no
  verdict, and the edit under review stays in the worktree.
- **block:** a reviewer asked for changes, so the node is blocked.
- **proceed:** nothing is waiting.

```bash
mini-ork approvals list
mini-ork approvals approve <run> --node implementer       # last pending one → resumes automatically
mini-ork approvals request-changes <run> --node implementer --note "add a test for zero"
#   → the note goes to operator steering, the node re-runs (recover --from-node … --strategy retry)
#     and the run parks again with a fresh review row
mini-ork approvals approve <run> --node implementer --no-resume   # only print the recover command
```

- The resume log is written to `<run_dir>/review-resume.log`.
- `recover` refuses a `reviewing` run while reviews are pending, unless you pass `--force`.

---

## 4. Node checkpoints and restore

After every node that succeeds, and once at run start as `_start`, the
executor snapshots the run's target tree into
`refs/mini-ork/ckpt/<run>/<node>`:

- untracked files are included;
- HEAD, the index and your files are never touched;
- set `MO_NODE_GIT_CKPT=0` to turn it off.

```bash
mini-ork board checkpoints <run> [--json]
mini-ork board restore <run> <node>      # takes a _pre-restore-<ts> safety ref first, so it can be undone
```

A restore refuses a live run or a worktree that isn't the run's own; pass
`--force` to override.

Zed's own **Restore Checkpoint** button cannot appear for an external agent,
because Zed never gives external agents `truncate`. Use these commands, or the
IDE and Zed surfaces below, instead.

---

## 5. Where you see it

| Surface | What it offers |
|---|---|
| mini-ork IDE, node page | **Restore to after this node**; **Approve** / **Request changes…** when the node is waiting |
| mini-ork IDE, run page | a **Waiting for your review** callout; a **Merge review** chip with **Approve merge** |
| mini-ork IDE, inbox | an **Awaiting review** section |
| Zed thread (ACP) | a review card on a parked run (Approve and resume / Request changes / Later); `/approve`, `/request-changes`, `/checkpoints`, `/restore` |
| Zed follow-the-agent | the crosshair follows a run's Read/Edit/Write tool calls, at absolute paths in the run's worktree |
| CLI | `mini-ork approvals …`, `mini-ork board checkpoints|restore|merge` |

---

## 6. GUI verification (Agent-S)

`mini_ork/gates/agent_s_smoke.py` (gate type `agent_s_gui`) verifies a
human-visible state by driving Agent-S on the real screen. It runs
**observe-only** by default and is advisory.

```bash
MO_AGENT_S_ALLOW=1 AGENT_S_PYTHON=<venv with gui-agents==0.3.2>/bin/python \
AGENT_S_PROVIDER=open_router AGENT_S_MODEL=openai/gpt-4.1 \
AGENT_S_GROUND_PROVIDER=open_router AGENT_S_GROUND_URL=https://openrouter.ai/api/v1 \
AGENT_S_GROUND_MODEL=bytedance/ui-tars-1.5-7b MINI_ORK_RUN_DIR=<run_dir> \
  python -m mini_ork.gates.agent_s_smoke <run_dir>/agent_s_task.yaml
```

The evidence (`verdict.json` and step screenshots) lands in
`<run_dir>/artifacts/agent_s/`.
