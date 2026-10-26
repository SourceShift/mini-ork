# rev6 · R3: risk tiers from the change — sensitive paths need a person's approval before they reach main

## Goal

Every change reaches `main` through `make worktree-merge`
(`scripts/mini_ork_worktree.py` `merge_worktree`), and under standing
authorization the agent that wrote a change is usually the one merging it.
- `task_class.risk_class` (`schemas/task_class.schema.json:72-76`: "high
  (human gate required)") is declared on 8 recipes as `high`, and nothing
  reads it.
- The content-bound approval ledger exists (`mini_ork/gates/merge_approval.py`:
  `request`, `approve`, `is_approved`; rows in `mo_inbox_gates`; HMAC in the
  secure tier), but the merge driver never consults it.
- K10's and K12's test-gaming code would have merged if a person had not read
  the diff.

Add a merge-time risk check, decided by WHAT changed, not who changed it:

- **High tier:** a branch that touches a high-tier path, or that carries a
  `[run <id>]` commit from a recipe whose `risk_class` is high or critical.
  It merges only with an approval bound to the branch's patch
  (`git patch-id --stable` of `origin/main...HEAD`, which is stable across a
  clean rebase), given by a person at a terminal.
- **Low tier:** merges exactly as today.
- **Test-gaming scan:** the driver also runs `mini_ork.verify.gaming_scan`
  (R1) over the branch and refuses production test-gaming patterns.

The policy and the scanner always run from the engine checkout that runs the
merge, and the rules are read from `origin/main`. A branch therefore cannot
weaken the check it is being checked by, and editing the rules file is itself
high tier.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `config/risk-tiers.yaml` (new)

```yaml
# Changes that need a person's approval before they merge to main.
# Read from origin/main at merge time (a branch cannot loosen its own check).
# Docs: docs/operator/review-gates.md#risk-tiers
risk_tiers:
  enabled: true
  include_frozen_kernel: true        # every path repo_integrity_guard.enforced_frozen_paths returns
  high_risk_classes: [high, critical]
  high:
    - config/risk-tiers.yaml
    - config/kernel-frozen.yaml
    - .github/workflows/**
    - .githooks/**
    - db/migrations/**
    - install.sh
    - scripts/install-system-deps.sh
    - scripts/full_install.py
    - scripts/mini_ork_worktree.py
    - mini_ork/cli/install_command.py
    - mini_ork/dispatch/vault.py
    - mini_ork/dispatch/vault_write.py
    - mini_ork/dispatch/secrets.py
    - mini_ork/cli/vault_cmd.py
    - mini_ork/remote/secrets_scope.py
    - config/vault.example.yaml
    - config/secrets.example.sh
    - .mini-ork/config/**
    - mini_ork/gates/merge_risk.py
    - mini_ork/gates/merge_approval.py
    - mini_ork/cli/approvals_cmd.py
    - mini_ork/verify/gaming_scan.py
```

### 2. `mini_ork/gates/merge_risk.py` (new)

```
DEFAULT_RULES = {"enabled": True, "include_frozen_kernel": True,
                 "high_risk_classes": ["high", "critical"], "high": [<the list above>]}

@dataclass(frozen=True)
class RiskVerdict:
    tier: str                    # "low" | "high"
    reasons: tuple[str, ...]     # e.g. "db/migrations/0085_x.sql matches db/migrations/**"
    patch_id: str
    files: tuple[str, ...]

def load_rules(wt: str, env: Mapping[str, str]) -> dict
def changed_files(wt: str) -> list[str]
def branch_patch_id(wt: str) -> str
def run_ids_in_range(wt: str) -> list[str]
def classify(wt: str, *, engine_root: str, db: str, env: Mapping[str, str]) -> RiskVerdict
def check_merge(wt: str, slug: str, *, engine_root: str, db: str,
                env: Mapping[str, str]) -> tuple[bool, str]
def main(argv: list[str] | None = None) -> int
```

1. **`load_rules`** tries these sources in order:
   1. `env["MO_RISK_TIERS_FILE"]` if set, a path to a YAML file;
   2. `git -C <wt> show origin/main:config/risk-tiers.yaml`;
   3. `<wt>/config/risk-tiers.yaml`;
   4. `DEFAULT_RULES`.
   The first source that exists is parsed with `yaml.safe_load`. Its
   `risk_tiers:` mapping overrides keys of `DEFAULT_RULES`. A file that does
   not parse, or whose `high` is not a list of strings, raises `ValueError`
   naming the source.
2. **`changed_files`**: `git -C <wt> diff --name-only --no-renames origin/main...HEAD`.
   **`branch_patch_id`**: the same range's `git diff --no-color --no-renames`
   piped to `git patch-id --stable`, first token (`""` for an empty diff).
3. **`run_ids_in_range`**: the `\[run ([A-Za-z0-9._:-]+)\]` matches in
   `git -C <wt> log --format=%B origin/main..HEAD`, de-duplicated in order.
4. **`classify`**:
   - **Path rules.** For each changed file, `fnmatch.fnmatch(path, pattern)`
     against `high`. Each `dir/**` pattern also matches anything under `dir/`.
     When `include_frozen_kernel` is set, the patterns also include
     `mini_ork.vcs.repo_integrity_guard.enforced_frozen_paths(engine_root)`.
     Each hit adds the reason `<path> matches <pattern>`.
   - **Risk class.** For each run id, read `task_runs.recipe` from `db`
     (read-only URI; a missing DB, table or row is skipped). Read that recipe's
     `risk_class` from `git -C <wt> show origin/main:recipes/<recipe>/task_class.yaml`
     (fallback: `<wt>/recipes/<recipe>/task_class.yaml`; unreadable → skip).
     When it is in `high_risk_classes`, add the reason
     `run <id> is recipe <recipe> (risk_class <c>)`.
   - The tier is `high` if any reason exists. When `enabled` is false, return
     tier `low` with the reason `risk tiers disabled`.
5. **`check_merge`**:
   - **Low tier:** `(True, "risk tier: low")`.
   - **High tier:**
     - `run_id = f"wt:{slug}"`;
     - `policy = mini_ork.gates.review_policy.load_policy(home=<dirname of db>)`;
     - if `merge_approval.is_approved(run_id, v.patch_id, db_path=db, env=env, policy=policy)`
       → `(True, "risk tier: high — approved for patch <pid[:12]>")`;
     - otherwise call
       `merge_approval.request(run_id, "", v.patch_id, branch=f"wt/{slug}", base_branch="main", db_path=db)`
       and return `False` with a message: one line per reason, then
       `this change needs a person's approval. At a terminal, run:` and
       `  mini-ork approvals approve wt:<slug> --head <patch_id>`.
   - Any exception → `(False, "risk check could not run: <exc>")`. It fails
     closed.
6. **`main`**: `check --worktree DIR --slug SLUG` (argparse).
   - `db` = `mini_ork.review.gate.resolve_db(wt, os.environ)`;
   - `engine_root` = the repo root that holds this module;
   - print the message and return 0 when ok, 1 when not; usage errors rc 2.

### 3. `scripts/mini_ork_worktree.py`

1. **`ENGINE`** = `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`.
   **`_engine_python()`** returns `<ENGINE>/.venv/bin/python` if executable,
   else `sys.executable`. Reuse it for the existing `_prereview` interpreter
   choice when that helper is equivalent.
2. **`_gaming_guard(wt)`**:
   - skip when `<ENGINE>/mini_ork/verify/gaming_scan.py` is missing, or
     `MO_MERGE_ALLOW_TEST_PROBE == "1"` (print that the override is in use);
   - otherwise run
     `[_engine_python(), "-m", "mini_ork.verify.gaming_scan", "--git-range", "origin/main...HEAD", "--repo", wt]`
     with `cwd=ENGINE` and `PYTHONPATH=ENGINE` prepended;
   - rc 1 → `die("test-gaming patterns in production code (above); fix them. A person who has read them may set MO_MERGE_ALLOW_TEST_PROBE=1")`;
   - any other non-zero rc → `die("test-gaming scan failed to run (rc=<rc>); refusing to merge unscanned")`.
3. **`_risk_guard(wt, slug) -> bool`**:
   - skip (return False) when `<ENGINE>/mini_ork/gates/merge_risk.py` is missing;
   - otherwise run
     `[_engine_python(), "-m", "mini_ork.gates.merge_risk", "check", "--worktree", wt, "--slug", slug]`
     the same way;
   - rc 0 → return whether its stdout contains `risk tier: high`;
   - rc 1 → `die("merge refused: high-risk change without a person's approval (see above)")`;
   - any other rc → `die("risk check failed to run (rc=<rc>); refusing to merge unchecked")`.
4. **In `merge_worktree`:**
   - right after `_oss_guard(wt, slug)`, call `_gaming_guard(wt)` and then
     `high = _risk_guard(wt, slug)`;
   - inside the merge lock, after the attempt's `rebase origin/main` and before
     `push`, call `_risk_guard(wt, slug)` again when `high` is true. The
     approval is bound to the patch, so a rebase that changed the patch must
     be approved again.
   - `die` raises `SystemExit`, so the lock's context manager releases the
     lock. Keep that.

### 4. `mini_ork/cli/approvals_cmd.py` — `_cmd_approve`

For a `run_id` starting with `wt:`, before calling `approve`:
1. **Require a TTY.** If `sys.stdin.isatty()` is false →
   `approvals approve: a merge-risk approval (wt:…) must be typed by a person at a terminal`,
   rc 1.
2. **Default `--head`.** When `--head` is absent, head =
   `merge_risk.branch_patch_id(<worktrees dir>/<slug>)`, where the worktrees
   dir is `mini_ork.rsi.cycle._worktrees_dir(<repo root>)`, and base `""`.
   An empty head → rc 1 with a message.
3. **Confirm.** Print the branch's changed files (at most 40) and
   `merge_risk.classify(...).reasons`, then
   `input(f"type {head[:8]} to approve: ")`. Anything else → rc 1,
   "not approved".

Every other `run_id` behaves exactly as today.

### 5. `docs/operator/review-gates.md`

Add a `## Risk tiers` section covering:
- what is high tier (the file + frozen kernel + risk_class);
- that the rules are read from `origin/main`;
- the approval command and that it needs a terminal;
- that approval is bound to `git patch-id`;
- the overrides `MO_RISK_TIERS_FILE` (tests/CI), `MO_MERGE_ALLOW_TEST_PROBE`;
- the two tiers: **simple** (approval recorded in the local DB after a typed
  confirmation) and **secure** (`config/review.yaml` `tier: secure`: the
  approval must carry the HMAC from `MO_REVIEW_SIGNING_KEY` through the vault
  chain, so the agent's environment alone cannot mint one).

## Files in scope (touch ONLY these)

- `config/risk-tiers.yaml` (new)
- `mini_ork/gates/merge_risk.py` (new)
- `scripts/mini_ork_worktree.py`
- `mini_ork/cli/approvals_cmd.py`
- `docs/operator/review-gates.md`
- `tests/unit/test_merge_risk.py` (new)

Do NOT modify `mini_ork/gates/merge_approval.py`, `mini_ork/gates/review_policy.py`,
`mini_ork/verify/gaming_scan.py`, the publisher, any migration, or any path in
`config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (`tests/unit/test_merge_risk.py`, hermetic)

Reuse the `repo` fixture shape of `tests/unit/test_merge_oss_guard.py:51-78`:
- a bare origin plus a clone, with seed pushed;
- `MINI_ORK_ROOT`, `MINI_ORK_WORKTREES_DIR`, `MINI_ORK_OWNERSHIP_FILE`, `MO_CONCORD=0` and `MINI_ORK_HOME` set, and `_lane_env()`;
- the gate stubbed with `MINI_ORK_TEST_CMD=true` and `MO_REVIEW_SKIP=1`;
- a DB at `$MINI_ORK_HOME/state.db` from `mini_ork.stores.migrate.init_db(db=..., root=REPO)` (assert rc 0);
- `MO_RISK_TIERS_FILE` pointing at a tmp YAML with `risk_tiers: {high: ["danger/**"], include_frozen_kernel: false}`.

Run the driver with `[sys.executable, REPO/scripts/mini_ork_worktree.py, ...]`
(so `ENGINE` is the real repo).

1. `classify`: a branch touching only `ok.txt` → low; `danger/x.txt` → high, with the reason naming `danger/**`.
2. Merging a high-tier branch with no approval:
   - rc ≠ 0 and `origin/main` unchanged;
   - one pending `mo_inbox_gates` row with `gate_id='merge:wt:<slug>'` and head equal to `branch_patch_id`;
   - stdout/stderr contain `mini-ork approvals approve wt:<slug> --head`.
3. After `merge_approval.approve("wt:<slug>", <pid>, approver="human:test", db_path=<db>, env={})`, the merge succeeds.
4. An approval for a different patch id → still refused.
5. Pushing an unrelated upstream commit to origin/main after approval (patch-id unchanged) → the merge still succeeds.
6. A low-tier branch merges with no approval row created.
7. A commit message containing `[run r-hi]`, a `task_runs` row `('r-hi', recipe='demo')`, and `recipes/demo/task_class.yaml` with `risk_class: high` committed to origin/main → high, with the reason naming the run.
8. A branch adding `x = os.environ.get("PYTEST_CURRENT_TEST")` to `mod.py` → refused by the gaming guard; with `MO_MERGE_ALLOW_TEST_PROBE=1` the gaming guard passes.
9. `MO_RISK_TIERS_FILE` naming an unparsable file → the merge is refused ("risk check failed" or a non-zero risk rc), never merged.
10. `load_rules` prefers `origin/main:config/risk-tiers.yaml` over a weakened copy on the branch (unset `MO_RISK_TIERS_FILE` for this test).
11. `approvals approve wt:<slug>` with a non-TTY stdin → rc 1 and the terminal message. Call `approvals_cmd.main([...])` with `sys.stdin` replaced by `io.StringIO`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_merge_risk.py tests/unit/test_merge_oss_guard.py tests/unit/test_worktree_script_py.py tests/unit/test_merge_approval.py tests/unit/test_node_review.py tests/unit/test_gaming_scan.py`

(Drop any listed test file that does not exist; do not create it.)

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_merge_risk.py --junitxml="${MINI_ORK_RUN_DIR}/rev6-r3-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rev6-r3-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/gates/merge_risk.py mini_ork/cli/approvals_cmd.py scripts/mini_ork_worktree.py` is clean.
- No code path in the driver merges a high-tier branch without a matching approval. Every failure of the risk check or the scanner refuses the merge.
- The driver still imports nothing from `mini_ork` at module top level.
