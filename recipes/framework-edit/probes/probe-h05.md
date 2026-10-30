---
oracle: git
holdout: true
---
# Frozen probe probe-h05 — framework-edit harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

# Zed (ACP): parked-run review card, /approve, /request-changes, /checkpoints, /restore

Run the framework-edit workflow exactly as written. Do not research, and do not add lens or drafter nodes.

## Goal

mini-ork's ACP agent (`mini_ork/acp/agent.py`, slash commands in `mini_ork/acp/commands.py`, run state in `mini_ork/acp/task_state.py`) streams runs into Zed threads. It already shows a Merge / Discard / Keep permission card when a run is "Ready to review" (`_offer_review` / `_handle_review_decision`, via `self._conn.request_permission`).

The review features are now on main:

- a run parked for node review ends with `task_runs.status = 'reviewing'` and `<run_dir>/.review-pause` (JSON with `waiting_node` and `awaiting`);
- `mini_ork.gates.node_review` has `pending_for_run`, `approve_node` and `request_changes`;
- `mini_ork/cli/approvals_cmd.py` has `_spawn_recover(home, run_id, extra_args)`, which resumes (`["--strategy", "resume"]`) or re-runs a node (`["--from-node", n, "--strategy", "retry"]`);
- `mini_ork.vcs.node_checkpoint` has `list_checkpoints` and `restore`.

Zed's own "Restore Checkpoint" button can never appear for an external agent: Zed never gives external agents `truncate`. So mini-ork offers its own checkpoint commands.

Worktree (ALL edits happen here): `/Volumes/docker-ssd/ps/mini-ork-worktrees/hr-acp-review`

## Behaviour

### 1. `task_state.py`

A run with status `reviewing` and a `.review-pause` file → `TaskState(state="needs_you", detail=f"Waiting for your review: {', '.join(awaiting)}")`. The title mark is the existing `needs_you` mark. Place the rule before the generic terminal rules.

### 2. Review card (`agent.py`)

When a followed run ends `reviewing` (same hook point where "Ready to review" triggers `_offer_review`), send one `request_permission` card:

- `tool_call_id=f"{run_id}:review"`, `kind="other"`, `title=f"Review {node} of run {run_id}"`;
- options: `approve` ("Approve and resume", `allow_once`), `changes` ("Request changes…", `reject_once`), `later` ("Later", `reject_once`).

The handler:
- **approve** → `node_review.approve_node(run, node, approver=f"zed:{getpass.getuser()}")`; when nothing is pending any more, rename `.review-pause` to `.review-pause.resolved` and call `approvals_cmd._spawn_recover(home, run, ["--strategy", "resume"])`; then emit a short agent message. Keep following the run if the existing follow machinery allows it, else just tell the user it resumed.
- **changes** → emit a message telling the user to type `/request-changes <run> <node> <what to change>`.
- **later** → a message with the `/approve` and `/request-changes` commands.

Dedupe: one card per `(run, waiting_node)`, mirroring `_ready_to_review_emitted`.

### 2b. Replayed runs keep their file locations (`agent.py`)

`_drain_live_for` attaches `locations=[ToolCallLocation(...)]` to tool events that carry `LiveEvent.path`. `_replay_live_for` (the finished-run replay on `load_session`) builds the same `ToolCallProgress` without them. A live check confirmed this: replaying a finished run with 91 Read/Edit/Write tool uses put 0 locations on the wire.

- Factor the per-event `ToolCallProgress` construction, including the path → absolute → `ToolCallLocation` logic, into ONE helper that both methods use.
- In replay, attach a location to every replayed tool event that has a path. Do NOT apply or advance the live dedup there: the replay is history, so each file chip in Zed should be clickable.

### 3. Slash commands (`commands.py`, registered like `/merge` and `/discard`, and listed in the available-commands update)

| Command | Behaviour |
|---|---|
| `/approve <run> [node]` | With a node → approve that node (resume as above). Without one → approve the merge: `merge_approval.approve(run, workspaces.content_sha(ws), approver=…, base_sha=ws.base_sha)`. |
| `/request-changes <run> <node> <note…>` | `node_review.request_changes(...)` + `operator_steering.emit(...)` + `_spawn_recover(..., ["--from-node", node, "--strategy", "retry"])`, reusing the approvals_cmd behaviour: call its helper or the same functions. |
| `/checkpoints <run>` | A markdown table: node, short sha, files changed, age. |
| `/restore <run> <node>` | Calls `node_checkpoint.restore(worktree, run, node, home=home)` and reports `restored_to` and the safety ref (`/restore <run> <safety-node>` undoes it). It refuses with the module's reason. |

Every command returns plain text, and errors are user-readable. Never raise.

## Files in scope (touch ONLY these)

- `/Volumes/docker-ssd/ps/mini-ork-worktrees/hr-acp-review/mini_ork/acp/task_state.py`
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/hr-acp-review/mini_ork/acp/agent.py`
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/hr-acp-review/mini_ork/acp/commands.py`
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/hr-acp-review/tests/unit/test_acp_review_flow.py` (NEW)

Do NOT modify any other file.

## Required artifacts

Declare EXACTLY these outputs and nothing else: the four files listed above.

## Definition of Done (`tests/unit/test_acp_review_flow.py`: fake connection as in `tests/unit/test_acp_agent_py.py`, tmp home/DB, `_spawn_recover` monkeypatched)

1. **task_state:** `task_state` for a `reviewing` run with `.review-pause` → `needs_you`, and the detail names the awaiting node.
2. **Card on park:** the agent's end-of-run hook for a `reviewing` run emits exactly one `request_permission` with the three options. Answering `approve` → the node row is approved, `_spawn_recover` is called with `["--strategy", "resume"]`, and the sentinel is renamed. A second trigger → no second card.
3. **`/request-changes`:** `/request-changes <run> implementer add a test` → the row is rejected, a steering row exists, and `_spawn_recover` is called with `--from-node implementer`.
4. **`/checkpoints` and `/restore`:**
   - `/checkpoints <run>` lists a snapshot made with `node_checkpoint.snapshot` in a tmp workspace;
   - `/restore <run> <node>` restores the file content and reports a safety ref;
   - `/restore` of an unknown node returns the refusal reason.
5. **`/approve <run>`** with no node and `MO_REVIEW_BEFORE_MERGE=required` → `merge_approval.status(run)["state"] == "approved"`.
5b. **Replay locations:** `_replay_live_for` over a live file with two `Edit` records → both replayed `ToolCallProgress` updates carry `locations` with the absolute path.
6. The existing `tests/unit/test_acp_agent_py.py` and `tests/unit/test_acp_live_locations.py` pass unchanged.

## Verification command

The command that proves this run succeeded (run in the worktree, must exit 0):

```bash
python3.11 -m pytest -q -p no:cacheprovider -p no:asyncio tests/unit/test_acp_review_flow.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_live_locations.py
```
