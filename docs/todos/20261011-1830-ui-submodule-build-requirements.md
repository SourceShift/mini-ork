# UI submodule: can a user build the Rust IDE from the mini-ork repo?

**Asked:** 2026-10-11 18:30
**Status:** open — R1 and R2 done (`6ba54378`); R3, R4, R5 open. The "build the
IDE from mini-ork" part cannot be met as stated, for the reason above.

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
**Status:** NOT SATISFIED.
`git worktree add` leaves `ui/` **empty** (verified). Two call sites create
worktrees and neither materialises the submodule:
- `mini_ork/workspaces.py:344-346` — bare `git worktree add -b <branch> <path> <base>`
- `scripts/mini-ork-worktree.sh:161` — same

**Remaining:** add `git submodule update --init --recursive` after each
`worktree add` (a worktree shares the superproject's `modules/`, so the fetch is
cheap), or document that worktrees do not carry the crate. The former is correct,
the latter cheap. Ignoring it is not an option: mini-ork spawns *runs* into these
worktrees.

### R4 — `git archive` should carry the crate — the live regression
**Status:** NOT SATISFIED.
`git archive HEAD` emits exactly one entry under `ui/` — the empty directory, zero
files (verified). Both framework-edit verifiers archive and extract the base:

    recipes/framework-edit/verifiers/static-check.py:172  git -C $REPO_ROOT archive <base> | extractall(WORKTREE)
    recipes/framework-edit/verifiers/test.py:120          (same)

`tests/unit/test_framework_edit_verifier_target_root.py` was written for exactly
this failure — its docstring records that a submodule gitlink archives as an empty
directory, so `ui/src/page.rs` was absent, the diff could not apply, and every
framework-edit round failed. **That fix — "archive the TARGET, not the engine" —
no longer covers it:** the target is a submodule now too. Worse, the test's fixture
creates `ui/src/page.rs` as a **real directory** in a scratch repo, so the suite
stays green while production fails. The fixture has drifted from reality.
**Remaining:** teach both verifiers to materialise submodule content into the
extracted copy (archive each submodule separately and untar it over the same path),
and make the fixture model a real submodule so the test has teeth again.

### R5 — Remote nodes should carry the crate
**Status:** NOT SATISFIED, and pre-documented.
`docs/operator/remote-nodes.md:274`: "Not supported yet: submodules travel as
gitlinks and Git LFS files as pointers." A remote spawn therefore sees an empty
`ui/`.
**Remaining:** implement submodule materialisation in the mirror, or record the
limitation where a user will find it.

## Suggested order

R1 and R2 are done. **R4 is the live one** — a shipped test is silently blind to
it, so the suite reports green over a broken path. R3 and R5 are tooling changes
and want a decision first (materialise, or document).
