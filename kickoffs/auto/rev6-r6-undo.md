# rev6 · R6: `mini-ork undo <commit|run-id>` — a one-step, gated revert that never rewrites main

## Goal

mini-ork's policy is fix-forward: never revert or rewrite `main`. A bad merge
still needs a fast way back. `git revert` adds a new commit and rewrites
nothing, and `mini_ork/rsi/worktree.py::revert(cfg, merged_sha)` already
implements the gated path for RSI cycles: claim a worktree, run
`git revert --no-edit <sha>` in it, merge through
`scripts/mini_ork_worktree.py merge` (green gate, lock, push), and refresh the
primary. Generalize that into a top-level subcommand.

It has to add the safety checks the RSI path lacks:
- refuse a commit that is not on `origin/main`;
- refuse a merge commit unless a parent is named;
- refuse a commit that was already reverted;
- map a run id to the commit it published.

The publisher's commit message carries `[run <run_id>]`
(`mini_ork/cli/publisher.py:438`, `f"mini-ork({recipe}): {node_desc} [run {run_id}]"`).
An RSI cycle's sha is in `rsi_cycles.merged_sha`.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/rsi/worktree.py`

`revert(cfg, merged_sha, *, runner=subprocess_runner, mainline: int | None = None) -> MergeResult`.
When `mainline` is not `None`, the git argv is
`["revert", "--no-edit", "-m", str(mainline), merged_sha]`; otherwise it is
unchanged. Nothing else in this file changes. `mini_ork/cli/rsi_cycle.py` keeps
calling it without `mainline`.

### 2. `mini_ork/cli/undo.py` (new)

```
class UndoError(Exception): ...           # a refusal; message is user-facing

@dataclass(frozen=True)
class UndoPlan:
    ref: str            # what the user typed
    sha: str            # full 40-hex sha being reverted
    subject: str
    mainline: int | None
    files: tuple[str, ...]
    test_cmd: str
    slug: str           # the revert worktree slug revert() will create: "revert-<sha[:12]>"

def resolve_ref(ref: str, *, root: Path, db: Path | None) -> str
def plan_undo(ref: str, *, root: Path, db: Path | None,
              mainline: int | None = None, test_cmd: str | None = None) -> UndoPlan
def record_undo(db: Path | None, plan: UndoPlan, result) -> None
def main(argv: list[str] | None = None) -> int
```

All git calls use `git -C <root>` with a 60 s timeout. `root` is the primary
checkout (`MINI_ORK_ROOT` if set, else `git rev-parse --show-toplevel` of cwd).
`db` is `MINI_ORK_DB` if set, else `<MINI_ORK_HOME or root/.mini-ork>/state.db`,
or `None` if that file does not exist.

1. **`resolve_ref`**, in this order:
   1. `git rev-parse --verify --quiet <ref>^{commit}` succeeds → that sha.
   2. `db` exists and has an `rsi_cycles` row with `id = ref` and a non-empty
      `merged_sha` → that sha. Open it read-only (`file:<db>?mode=ro`, `uri=True`).
      A missing table counts as no row.
   3. `git log origin/main --format=%H --fixed-strings --grep "[run <ref>]"`:
      - one sha → it;
      - several → `UndoError("run <ref> published several commits on origin/main: <shas>; undo one sha at a time")`;
      - none → `UndoError("no commit found for <ref>: not a commit, an RSI cycle id, or a run id with a [run <ref>] commit on origin/main")`.
2. **`plan_undo`** first runs `git fetch origin main`. A failure is an
   `UndoError` naming the stderr tail. It then checks, in order:
   1. `git merge-base --is-ancestor <sha> origin/main` must exit 0, otherwise
      `UndoError("<sha12> is not on origin/main; undo only reverts landed commits")`.
   2. **Parents** come from `git rev-list --parents -n 1 <sha>`.
      - more than one parent and `mainline is None` → `UndoError("<sha12> is a merge commit; pass --mainline N (1..<parents>) to choose the parent to keep")`;
      - `mainline` given on a single-parent commit → `UndoError("--mainline only applies to merge commits")`;
      - `mainline` outside `1..parents` → `UndoError`.
   3. **Already reverted.** `git log origin/main --format=%H --fixed-strings --grep "This reverts commit <full sha>"` non-empty → `UndoError("<sha12> was already reverted by <sha12 of the first match>")`.
   4. **`files`** is `git diff --name-only --no-renames <sha>^<mainline or 1> <sha>`, sorted. Empty → `UndoError("<sha12> changes no files")`.
   5. **`test_cmd`.** If the caller passed one, use it. Otherwise take the paths in `files` that are test files:
      - under `tests/`, with basename `test_*.py`;
      - not added by the commit (`git diff --diff-filter=A --name-only <sha>^N <sha>` excludes them).
      If any remain, the command is `python3.11 -m pytest -q -p no:asyncio <those paths>`. If none remain → `UndoError("no existing test file is touched by <sha12>; pass --test-cmd to gate the revert")`.
   6. `subject` comes from `git log -1 --format=%s <sha>`, and `slug` is `f"revert-{sha[:12]}"`.
3. **`record_undo`**: if `db` is not `None`, insert into `audit_log`:
   - `event_type = 'undo'`
   - `actor = 'human:' + getpass.getuser()`
   - `target = 'commit:' + plan.sha`
   - `payload` = JSON `{ref, sha, subject, mainline, files, test_cmd, slug, status, revert_sha, detail}`
     (`status`/`revert_sha`/`detail` from the `MergeResult`)
   Any `sqlite3.Error` → one stderr warning line, never raise.
4. **`main(argv)`** uses argparse with a `build_parser()` function:
   `mini-ork undo <ref> [--mainline N] [--test-cmd CMD] [--dry-run] [--json]`.
   - **Exit codes:** `--help` → rc 0; a usage error → rc 2; an `UndoError` → message on stderr, rc 1.
   - **`--dry-run`:** print the plan (`--json`: the `asdict(plan)` object; otherwise a short human summary listing sha, subject, files and the gate command), create nothing, write no audit row, rc 0.
   - **Otherwise:**
     - build `WorktreeConfig(slug=plan.sha[:12], files=plan.files, worktrees_dir=mini_ork.rsi.cycle._worktrees_dir(root), primary=root, test_cmd=plan.test_cmd)`;
     - call `revert(cfg, plan.sha, mainline=plan.mainline)` (`revert` derives the `revert-<sha12>` slug);
     - call `record_undo`;
     - print `undo <sha12>: <status> <revert sha12>` and, if present, the detail on the next line (`--json`: `{"plan": ..., "status", "revert_sha", "detail"}`);
     - rc 0 if `result.landed`, else 1.
   - A conflicted revert keeps its worktree, as `revert` already does; the detail line names it.
   - Keep `if __name__ == "__main__": sys.exit(main())`.

### 3. Registration

- `mini_ork/cli/main.py`: add `"undo": "mini_ork.cli.undo"` to `_NATIVE_MODULE_SUBS`.
- Add this `_HELP` entry in the "Self-improvement:" block, after `rsi-cycle`, with the same column layout:
  `undo <commit|run-id>             Revert a landed commit as a new, gated commit` and a continuation line `                                   (--mainline N, --test-cmd, --dry-run). Never rewrites main.`
- `tests/unit/test_native_dispatch_py.py`: add `"undo"` to the `expected` set in `test_all_former_exec_subs_registered_natively`. Change nothing else there.

## Files in scope (touch ONLY these)

- `mini_ork/cli/undo.py` (new)
- `mini_ork/rsi/worktree.py`
- `mini_ork/cli/main.py`
- `tests/unit/test_native_dispatch_py.py`
- `tests/unit/test_undo_cmd.py` (new)

Do NOT modify `scripts/mini_ork_worktree.py`, `mini_ork/cli/rsi_cycle.py`,
`Makefile`, any migration, or any path in `config/kernel-frozen.yaml`.

## Tests (`tests/unit/test_undo_cmd.py`, hermetic)

Use the bare-remote fixture shape of `tests/unit/test_worktree_script_py.py:30-56`:
- `origin.git` (bare, `-b main`), plus a clone used as the primary;
- a seed commit pushed;
- env `MINI_ORK_ROOT=<clone>`, `MINI_ORK_WORKTREES_DIR`, `MINI_ORK_OWNERSHIP_FILE`, `MO_CONCORD=0`;
- the git identity vars, and `MINI_ORK_DB` pointing at a tmp DB.

Create that DB from `mini_ork.stores.migrate.init_db(db=..., root=REPO)` (assert rc 0). Copy `scripts/mini_ork_worktree.py` into the clone at `scripts/`. Strip `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*` from the env, as `tests/unit/test_merge_oss_guard.py:27` does.

1. `resolve_ref`:
   - a full sha → itself;
   - a short sha → the full sha;
   - a commit message containing `[run run-x1]` on origin/main → that sha for `run-x1`;
   - two such commits → `UndoError` naming both;
   - an `rsi_cycles` row with `merged_sha` → that sha (insert the row directly);
   - unknown → `UndoError`.
2. `plan_undo`:
   - a commit only on a local branch (not pushed) → `UndoError` "not on origin/main";
   - a merge commit without `--mainline` → `UndoError` mentioning `--mainline`; with `mainline=1` → a plan;
   - `mainline=1` on a normal commit → `UndoError`;
   - a commit already reverted on origin/main → `UndoError` "already reverted".
3. `test_cmd` defaulting:
   - a commit that modifies `tests/unit/test_a.py` (existing before) → the default command names it;
   - a commit that only adds `tests/unit/test_new.py` and edits `mod.py` → `UndoError` asking for `--test-cmd`.
4. `main([... "--dry-run", "--json"])` → rc 0, prints the plan, creates no worktree, writes no `audit_log` row.
5. End to end with `--test-cmd true`:
   - origin/main gains a new commit whose message starts with `Revert "` and contains `This reverts commit <sha>`;
   - the reverted file's content is restored;
   - the original commit is still in history (no rewrite);
   - one `audit_log` row with `event_type='undo'` and `target='commit:<sha>'`;
   - rc 0.
6. A gate that fails (`--test-cmd false`) → rc 1 and origin/main unchanged.
7. `mini_ork.rsi.worktree.revert(..., mainline=2)` passes `-m 2`. Use a FakeRunner in the style of `tests/unit/test_rsi_worktree_py.py:324-355` and assert the argv.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_undo_cmd.py tests/unit/test_native_dispatch_py.py tests/unit/test_rsi_worktree_py.py tests/unit/test_mini_ork_cli_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_undo_cmd.py --junitxml="${MINI_ORK_RUN_DIR}/rev6-r6-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rev6-r6-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged except
  the one-word addition to the `expected` set.
- `ruff check --select F,E9 mini_ork/cli/undo.py mini_ork/rsi/worktree.py` is clean.
- `bin/mini-ork undo --help` exits 0, and `mini_ork.cli.main._HELP` lists `undo`.
- `mini_ork/cli/undo.py` itself never runs `git reset`, `git push`, `git rebase`
  or `git commit --amend`; undo only adds a commit, and only through the
  existing `revert()` → merge-script path.
