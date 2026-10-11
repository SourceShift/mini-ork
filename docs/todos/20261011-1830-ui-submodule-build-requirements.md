# UI submodule: can a user build the Rust IDE from the mini-ork repo?

**Asked:** 2026-10-11 18:30
**Status:** closed — all five requirements satisfied (R1/R2 `6ba54378`, R3
`03ae5fe4`, R4 `882cc8cc`, R5 below). The "build the IDE from mini-ork" part
cannot be met as stated, for the reason above; R1 and R2 instead document and
script the path that does work.

## The question

"Users can build the Rust version using the `ui/` submodule in the mini-ork repo —
does it satisfy all requirements?"

## Answer up front

**No — and one part cannot be met as stated.** `mini-ork` has no cargo workspace.
The crate declares `edition.workspace = true` and inherits *every* dependency as
`dep.workspace = true` (`ui/Cargo.toml:4,5,9,16-18`), 14 of them Zed-internal
path crates (`gpui`, `ui`, `workspace`, `agent_ui`, …). Nothing in mini-ork can
resolve them. The `ui/` submodule is a *source* checkout, not a build input.

The IDE builds only in a `zed-mini-ork` checkout. That was true before this change
and both `ui/README.md` and the fork's `MINI-ORK.md` say so.

## Requirements, and where each stands

### R1 — A clone of mini-ork should give a usable tree
**Status:** SATISFIED (`6ba54378`).
`README.md:218` clones with `--recurse-submodules`, and `:226` says the Python
runtime works without `ui/` filled. `install.sh:81` runs `git submodule update
--init --recursive --depth 1` best-effort and prints the retry command when it
fails, so an install never leaves the crate empty and silent about it.

### R2 — A user should be able to build the Rust IDE, documented from mini-ork
**Status:** SATISFIED (`6ba54378`).
`docs/UI.md:17` is a "Building the IDE" section: clone
`SourceShift/zed-mini-ork --recurse-submodules`, `script/mini-ork-build`, then
`open -a "mini-ork IDE"`, pointing at the fork's `MINI-ORK.md` for the rest.

### R3 — Worktrees should carry the crate
**Status:** SATISFIED (`03ae5fe4`).
`git worktree add` leaves `ui/` **empty** (verified). Both call sites that create
worktrees now materialise the submodule right after the add:
- `mini_ork/workspaces.py:344-346` — `git worktree add -b <branch> <path> <base>`
- `scripts/mini-ork-worktree.sh:161` — same

Both `git submodule update --init --recursive` and **warn on failure** rather than
raise: the crate is a build input for the IDE, not for the runtime, and dying
would break worktree creation for every offline run. The original note here said
"a worktree shares the superproject's `modules/`, so the fetch is cheap" — that is
**wrong on git 2.39** (verified): a linked worktree gets its *own* module dir
(`.git/worktrees/<name>/modules/ui`), so the update clones from the recorded URL
and needs the network. Offline is exactly the case the warning covers.

### R4 — `git archive` should carry the crate — the live regression
**Status:** SATISFIED (`882cc8cc`).
`git archive HEAD` emits exactly one entry under `ui/` — the empty directory, zero
files (verified). Both framework-edit verifiers archive and extract the base:

    recipes/framework-edit/verifiers/static-check.py:210  git -C $REPO_ROOT archive <base> | extractall(WORKTREE)
    recipes/framework-edit/verifiers/test.py:158          (same)

`tests/unit/test_framework_edit_verifier_target_root.py` was written for exactly
this failure — its docstring records that a submodule gitlink archives as an empty
directory, so `ui/src/page.rs` was absent, the diff could not apply, and every
framework-edit round failed. **That fix — "archive the TARGET, not the engine" —
did not cover it:** the target is a submodule now too. Worse, the test's fixture
created `ui/src/page.rs` as a **real directory** in a scratch repo, so the suite
stayed green while production failed.

Both verifiers now re-archive each submodule at the commit the superproject records
and untar it over the same path. They read the target's own checkout rather than
fetching, so a verifier stays reproducible and does not depend on the remote still
serving that commit. The fixture now registers a real submodule via
`_add_submodule`, and `test_archive_alone_is_what_loses_the_submodule` pins the
hole itself: with the helper disabled, exactly the two new cases fail.

### R5 — Remote nodes should carry the crate
**Status:** SATISFIED.
`materialize()` in `mini_ork/remote/tree_sync.py` restored a gitlink the same way
`read-tree` always does — the pointer, not the tree — so a remote spawn saw an
empty `ui/`. It now runs `_materialise_submodules()` between `reset -q` and the
tree verification, which checks each submodule out from the URL the superproject
records. The verification is unaffected: `submodule update` checks out exactly the
commit the index already records, and `git add -A` hashes a gitlink as that commit
whether or not its content is present — which is also why the hole went unnoticed
for so long.

Unlike R3/R4 this one can genuinely need the network *per sync*, and a node may not
reach the URL, so it **warns** (`mini-ork: submodules not materialised in <path>`)
instead of raising: the tracked tree still matches the snapshot, only the vendored
content is missing. `docs/operator/remote-nodes.md` now says so in place of the old
"not supported yet" line; Git LFS pointers remain unsupported.

The two new tests in `tests/unit/test_tree_sync.py` run the production flow
(snapshot → bundle → fetch → materialize) over a repo that vendors `ui/` as a real
submodule. `test_the_bundle_alone_leaves_the_submodule_empty` pins the pre-fix
behaviour, so the hole cannot silently return.

## Suggested order (kept for the record)

R1 and R2 went first. **R4 was the live one** — a shipped test was silently blind
to it, so the suite reported green over a broken path. R3 and R5 were tooling
changes that wanted a decision first (materialise, or document); both are now
materialised, with warnings at the two call sites where failure is survivable.
