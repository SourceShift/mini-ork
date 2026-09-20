# E2a pre-state capture — make the pre-implementer baseline durable and vendorable

## Goal

Make `python3.11 -m pytest tests/unit/test_pre_impl_fixture_py.py -q` pass, by adding a
durable, minimal pre-state capture for every code run.

Two defects in `_capture_pre_impl_baseline` (mini_ork/cli/execute.py:1474) make a
later probe harvest impossible today, both measured against the live state.db
(2026-09-20):

1. **The ref decays.** It writes `git stash create` output — a commit with no ref,
   i.e. a dangling object. `git gc` reaps it. Of 30 failing code runs that did
   record a ref, **22 refs are already gone** (`git cat-file -t` → "could not get
   object info"); 8 survive.
2. **The shape is not vendorable.** Even a surviving ref points at a whole target
   repo (the mini-ork framework itself, 107 of 159 failing runs; a 230 MB astropy
   checkout for the rest). A probe needs a small, self-contained fixture directory
   with a fast frozen test — the shape `recipes/code-fix/probes/fixtures/<stem>/`
   already uses. Nothing produces that shape.

This epic is the capture half only. The harvest half (a `harvest_probes` that reads
this shape) is deliberately out of scope: with zero existing fixtures there would be
nothing to harvest, so building the harvester now is unverifiable. Capture first.

## Mechanisms (exact spec)

### 1. Make the baseline reachable

In `_capture_pre_impl_baseline`, after computing the ref, pin it to a real ref in
the target repo so gc cannot reap it:

```python
run_id = os.path.basename(run_dir.rstrip(os.sep))
subprocess.run(["git", "-C", cwd, "update-ref", f"refs/mo/pre-impl/{run_id}", ref], ...)
```

Best-effort, inside the existing `try` (a target that rejects refs must not fail the
run). Keep the existing `<run_dir>/pre-implementer-ref` file — it is read by
`_assemble_reviewer_inputs`, `_write_implementer_summary` and
`_write_self_migrate_implementer_summary`; do not change its format.

### 2. Snapshot a minimal fixture

Add `_capture_pre_impl_fixture(run_dir, target)` to `mini_ork/cli/execute.py`, and
call it from `mini_ork/cli/execute_handlers.py` **immediately after the first
implementer edit returns**, next to the existing `_write_implementer_summary` call.
It must:

- read `<run_dir>/pre-implementer-ref`; no ref → return, write nothing;
- compute the changed files as
  `git -C target diff --name-only <ref>` plus
  `git -C target ls-files --others --exclude-standard`
  (the same scope `_write_implementer_summary` already uses);
- write `<run_dir>/pre-impl-fixture/MANIFEST.json`:
  `{"run_id", "task_class", "target_repo", "baseline_ref", "changed_files",
    "verification_command", "kickoff_path", "created_at"}` —
  `task_class` / `verification_command` / `kickoff_path` read from
  `<run_dir>/run_profile.json` when present, else empty;
- for **every changed file that still exists at `<ref>`**, write its pre-edit
  content to `<run_dir>/pre-impl-fixture/files/<relpath>` using
  `git -C target show <ref>:<relpath>` — bytes preserved
  (`capture_output=True`, no `text=True` on this call);
- a changed file that does NOT exist at `<ref>` (newly created by the implementer)
  gets **no** fixture entry and must NOT appear as a zero-byte file. Record it in
  the manifest under `created_by_run`;
- be **idempotent**: an existing `MANIFEST.json` means return immediately;
- never write model output. Every byte comes from git or from an existing file
  under `run_dir`. A model-authored fixture is a model-authored test, which is the
  failure this epic exists to prevent.

Define the directory and file names as module-level named constants (e.g.
`_PRE_IMPL_FIXTURE_DIR`, `_PRE_IMPL_MANIFEST`) rather than inline literals.

## Tests

New `tests/unit/test_pre_impl_fixture_py.py`, over a throwaway git repo built in
`tmp_path` (one committed file, then a modified and a newly created file). Assert:

1. the manifest exists with the expected `changed_files`, `baseline_ref`,
   `created_by_run`;
2. the modified file's pre-edit content is byte-identical to its committed version
   at the baseline (not the working-tree version) — this is the whole point;
3. **the ref survives `git gc --prune=now`**: after
   `_capture_pre_impl_baseline`, run `git gc --prune=now` in the target, then assert
   `git cat-file -t refs/mo/pre-impl/<run_id>` exits 0. This is the load-bearing
   test — it is the defect that lost 22 of 30 refs;
4. a second call is a no-op (manifest unchanged, mtime or content);
5. no changed files → manifest with `changed_files == []` and still a valid ref.

## Files in scope

- mini_ork/cli/execute.py
- mini_ork/cli/execute_handlers.py
- tests/unit/test_pre_impl_fixture_py.py

Do not touch `mini_ork/learning/probe_scorer.py`, `benchmark_suite.py`,
`recipes/code-fix/probes/`, or any existing probe. Do not modify the format of
`pre-implementer-ref`.

## Self-application measurement

The improvement is capture durability, and it is directly measurable. Baseline is
today's rate: 8 of 30 failing code runs with a recorded ref still resolvable.

Instrument — a live smoke, run from the worktree root with a scratch target:

```bash
python3.11 -c "
import subprocess, os, tempfile, shutil
from mini_ork.cli import execute as ex
d = tempfile.mkdtemp(); shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
subprocess.run(['git','-C',d,'init','-q'],check=True)
subprocess.run(['git','-C',d,'config','user.email','t@e.invalid'],check=True)
subprocess.run(['git','-C',d,'config','user.name','T'],check=True)
open(os.path.join(d,'a.py'),'w').write('V = 1\n')
subprocess.run(['git','-C',d,'add','-A'],check=True)
subprocess.run(['git','-C',d,'commit','-qm','init'],check=True)
open(os.path.join(d,'a.py'),'w').write('V = 2\n')
rd = tempfile.mkdtemp(); rid = os.path.basename(rd.rstrip(os.sep))
os.environ['MO_TARGET_CWD'] = d
ex._capture_pre_impl_baseline(rd)
ex._capture_pre_impl_fixture(rd, d)
subprocess.run(['git','-C',d,'gc','--prune=now'],check=True)
t = subprocess.run(['git','-C',d,'cat-file','-t','refs/mo/pre-impl/'+rid],capture_output=True,text=True)
assert t.returncode == 0, 'FAIL: ref did not survive gc'
body = open(os.path.join(rd,'pre-impl-fixture','files','a.py')).read()
assert body == 'V = 1\n', 'FAIL: fixture holds the post-edit body: %r' % body
print('capture survives gc and holds the PRE-edit body')
print(open(os.path.join(rd,'pre-impl-fixture','MANIFEST.json')).read())
"
```

Accepted iff that prints both lines and `changed_files` contains `a.py`.

Evidence artifact: `${MINI_ORK_RUN_DIR}/pre-state-capture.json` with
`{"run_id", "ref_reachable_after_gc": <bool>, "fixture_files": [<paths>],
"pre_edit_body_matches": <bool>}`.

## Verification commands

Run each from the worktree root. Use `python3.11` explicitly — the ambient
`python3` here is 3.9 and dies at collection.

```bash
# 1. the new unit layer
python3.11 -m pytest tests/unit/test_pre_impl_fixture_py.py -q

# 2. the existing baseline-capture tests are unbroken
python3.11 -m pytest tests/unit/test_framework_edit_capture_baseline_py.py \
  tests/unit/test_reviewer_diff_scope_py.py -q

# 3. everything still compiles
python3.11 -m py_compile mini_ork/cli/execute.py \
  mini_ork/cli/execute_handlers.py tests/unit/test_pre_impl_fixture_py.py

# 4. the live smoke above, which is the actual claim
```

## Done When

- `python3.11 -m pytest tests/unit/test_pre_impl_fixture_py.py -q` is green, including
  the survives-`git gc` assertion.
- `python3.11 -m pytest tests/unit/test_framework_edit_capture_baseline_py.py tests/unit/test_reviewer_diff_scope_py.py -q` is green.
- The live smoke prints `capture survives gc and holds the PRE-edit body`.
- `${MINI_ORK_RUN_DIR}/pre-state-capture.json` exists and shows
  `ref_reachable_after_gc: true` and `pre_edit_body_matches: true`.
- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"`.
