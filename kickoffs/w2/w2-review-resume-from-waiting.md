# W2-review-resume: approving a parked node resumes from the node that was waiting, not from scratch

## Goal

A live spec-driven-delivery run (`sdd-smoke-20261031-001808`, 2026-10-31) proved the SDD
ratification pause: the run parked after `contract_compiler`, and `.review-pause` held
`{"waiting_node": "ratification_check", "awaiting": ["contract_compiler"], "inbox_ids": [929], …}`.

`mini-ork approvals approve <run> --node contract_compiler` then spawned
`mini-ork recover <run> --strategy resume` **without an entry point**. The recovery planner chose
its own: the first node with no checkpoint row. That is `specdir_ingest`, a verifier, and
verifier nodes do not write `node_checkpoints`. So it re-ran 14 of 15 nodes:
`recover --status` printed `reuse (0 nodes)` and `rerun (14 nodes): [first] specdir_ingest (no_row)`.

That re-dispatched the just-reviewed `contract_compiler`. Every re-dispatch appended to its
checkpointed artifact, so the checkpoint hash never matched again, and it burned $4.29 on 7
failed LLM calls in a loop. The approval did not continue the run; it restarted it.

`recover --status --from-node ratification_check` on the same run shows the right plan:
`reuse (2 nodes): contract_compiler, replanner` and `rerun (10 nodes)` starting at
`ratification_check`.

Fix: when a node approval resumes a parked run, resume **from the sentinel's `waiting_node`**.

Read first:
- `mini_ork/cli/approvals_cmd.py`: `_review_pause_sentinel`, `_maybe_resume_parked_run`
  (it renames the sentinel to `.review-pause.resolved`), `_spawn_recover`, the node-approve
  branch that calls `_maybe_resume_parked_run(..., extra_args=["--strategy", "resume"], ...)`,
  and the request-changes branch that already passes `--from-node`.
- `mini_ork/cli/execute_handlers.py`, where the executor writes `.review-pause`, including the
  `waiting_node` key.
- `tests/unit/test_review_resume.py`: `_stub_spawn` and
  `test_approve_last_pending_spawns_resume`.

## Mechanism (exact spec)

### `mini_ork/cli/approvals_cmd.py`

1. A helper:

```python
def _waiting_node(home: Path, run_id: str) -> str | None:
    """The ``waiting_node`` the executor recorded in ``.review-pause``, or ``None``.

    ``None`` when the sentinel is missing, empty, not JSON, or has no non-empty string
    ``waiting_node`` (sentinels written by an older engine).
    """
```

2. In the node-**approve** branch, read the sentinel **before** calling
   `_maybe_resume_parked_run`, which renames it. When `_waiting_node(...)` returns a node, pass
   `extra_args=["--from-node", node, "--strategy", "resume"]`. Otherwise keep
   `["--strategy", "resume"]` and print to stderr:
   `approvals approve: .review-pause has no waiting_node; resuming without an entry point (the
   recovery planner picks the first node it cannot reuse)`.
3. Update the manual-resume hint printed when nothing was spawned, so it includes
   `--from-node <node>` when one is known.
4. Update the module docstring's resume paragraph: approve resumes `--from-node <waiting_node>
   --strategy resume` when the sentinel names it.

The request-changes and reject paths are unchanged.

## Files in scope (touch ONLY these)

- `mini_ork/cli/approvals_cmd.py`
- `tests/unit/test_approvals_resume_waiting_node_py.py` (new)

Do NOT modify any other file. `tests/unit/test_review_resume.py` must keep passing unchanged.
Its sentinels carry no `waiting_node`, so they take the documented no-entry-point path.

## Tests (`tests/unit/test_approvals_resume_waiting_node_py.py`)

Reuse `test_review_resume`'s fixtures and helpers (import them; `_stub_spawn` records
`extra_args`):
- A sentinel with `{"waiting_node": "ratification_check", "awaiting": ["contract_compiler"],
  "inbox_ids": [<id>]}`, approving the last pending node row, spawns exactly once with
  `extra_args == ["--from-node", "ratification_check", "--strategy", "resume"]`. The sentinel is
  renamed to `.review-pause.resolved`, which still holds the payload.
- An empty sentinel file spawns with `["--strategy", "resume"]`, and stderr mentions
  `no waiting_node`.
- A sentinel holding `"not json"` behaves like the empty one.
- `waiting_node: ""` behaves like the empty one.
- `--no-resume` with a waiting node spawns nothing, and the printed manual hint contains
  `--from-node ratification_check`.
- `_waiting_node` unit cases: missing file, valid payload, non-dict JSON.

## Verification commands

- `python3 -m pytest -q -p no:asyncio tests/unit/test_approvals_resume_waiting_node_py.py tests/unit/test_review_resume.py`
- `python3 -m pytest -q -p no:asyncio -k "approvals or node_review or review_resume or intervention" tests/unit`
- `ruff check --select F,E9 mini_ork/cli/approvals_cmd.py tests/unit/test_approvals_resume_waiting_node_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_approvals_resume_waiting_node_py.py --junitxml="${MINI_ORK_RUN_DIR}/w2-review-resume-junit.xml"
```

## Required artifacts: declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/w2-review-resume-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- An approval whose sentinel names the waiting node resumes there. Older sentinels behave as
  today, with an explicit stderr note.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
