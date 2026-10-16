# Session gaps — 732bb4cc (author-style-imitation-review)

Session: `732bb4cc-0c80-474c-82c1-f3c225843d09`
Audited: 2026-10-16
Method: every status below was re-derived from git and the filesystem; recorded
status was not trusted. Commands actually run are quoted inline.

## (a) Task table

| id | description | recorded | VERIFIED | evidence |
|---|---|---|---|---|
| `author-style-assign` | Deterministic technique→family assignment: clusterer emits family definitions only; `assign` sorts all techniques | completed | **SHIPPED, but never exercised end-to-end** | `0190e322` is an ancestor of `main` HEAD (`git merge-base --is-ancestor 0190e322 HEAD`). Symbols present: `_STOPWORDS` `lib/style_pipeline.py:558`, `_words` `:565`, `assign` `:578`. Contract wired: `workflow.yaml:222` (`path: families.json`), `artifact_contract.yaml:27/30/33`, `verifiers/split.py:19-34`. |
| recipe `author-style-imitation-review` | 37-node recipe: 1000-paper lens review of writing-style imitation | completed | **VERIFIED complete** | `0918cf4f` (25 files tracked under `recipes/author-style-imitation-review/`, `git ls-files \| wc -l` → 25); version fix `f950a3b3`. |
| version normalization | Versioned arXiv ids must resolve against an unversioned corpus | completed | **VERIFIED complete** | `_unversioned` defined `lib/style_pipeline.py:123`, used at `:117`, `:285`, `:408`. |
| live run `style-review-20261016-160813` | Execute the recipe on the real corpus | completed | **VERIFIED green** | `verdict.json` → `{"verdict":"pass","failed_nodes":0,"levels_ok":true}`; `rubric.json` → `{"pass":true,"score":8}`; `verifier_verify.json` → `{"groups":8,"techniques":97,"cited_papers":454,"report_ids":54,"corpus_papers":1000}`. |
| `wu-2b7d40af` | Reformat USB stick (WIN11 `/dev/disk8`) so a Samsung TV plays an MKV over USB | completed | **VERIFIED** | `diskutil eraseDisk ExFAT SAMSUNG MBR /dev/disk8` → "Finished erase"; `mount` → `/dev/disk8s1 on /Volumes/SAMSUNG (exfat,…,fskit)`; `ffprobe` → `codec_name=h264 profile=High` + `codec_name=ac3 channels=2`; `diskutil eject /dev/disk8` → "Disk /dev/disk8 ejected". |

Note on SHAs: `9a58fed8` and `c388d68a` are the pre-rebase worktree commits and
are **not** ancestors of `main` — that is expected, not a loss. The merge path
(`push HEAD:main` after rebase) rewrote them to `0918cf4f` and `f950a3b3`, whose
contents are present.

ContextNest: `cn_retrieve` scoped to this session returns its atoms
(e.g. `cn-6b2c5f592f6c045ac536f9424a972cb4`). `cn_inbox` (500 items) contains
**no** item attributed to this session — every inbox row belongs to
`da0b8c69-…` or `86880f3c-…`. So there are no pending substrate todos for this
session.

## (b) Missing features & gaps

**G1 — The merged fix has never run end-to-end. (highest severity)**
The only successful execution of the recipe **predates** the assignment change, so
its artifact set does not match the contract now declared on `main`. In
`.mini-ork/runs/style-review-20261016-160813/`:
`families.json` **ABSENT**, `cluster-assignment.json` **ABSENT**, `clusters.json`
present (12026 bytes, 17:29) with `top-level keys: ['groups']` and
`group0 keys: ['name','scope','technique_keys','technique_count','total_supporting_papers']`
— i.e. the *old* model-authored mapping, not the deterministic output.
`artifact_contract.yaml:27/30/33` declares three cluster artifacts; no run has
ever produced all three. The deterministic logic is covered by tests, but the
model-facing half — the clusterer emitting `families.json` under the rewritten
`prompts/cluster-techniques.md` — is unproven. Closing this requires one real run.

**G2 — The deliverable is not versioned.**
`author-style-imitation-review.md` (244 KB, 718 lines) plus `source-corpus.json`
(1.2 MB) and `technique-catalog.json` (156 KB) exist only under the gitignored
`runs/` directory. A `runs/` cleanup destroys them. I offered to copy the report
into the repo; **the user has not answered**, so this remains open.

**G3 — The tests live in `/tmp`.**
`/tmp/test_assign.py` (assign logic, empty-family repair, 7-family rejection) and
`/tmp/smoke_style_wt.py` (full deterministic chain) are outside the repo and will
not survive a reboot. `assign()` has no CI coverage; a future refactor can break
it silently.

**G4 — The worktree was never torn down.**
`/Volumes/docker-ssd/ps/mini-ork-worktrees/author-style-review` still exists on
disk, and branch `wt/author-style-review` with it. This was deliberate — a
post-run `reflect` child was live and cleaning would have orphaned it. That child
has now **exited** (`pgrep -fl mini_ork.cli.reflect` → not running; `reflect.log`
mtime 18:32), so cleanup is unblocked and simply not yet done.

**G5 — `make worktree-merge` fails on interpreter choice, not on code.**
The bare gate (`python3 -m pytest -q`) resolves to micromamba 3.9 and dies at
collection via `_require_supported_python` exit 2 in `bin/mini-ork`;
`python3.12` dies with `ModuleNotFoundError: No module named 'fastapi'` (7
collection errors). Only `python3.11` works. Recorded in
`memory/feedback_worktree_gate_python311.md`, but the script still needs the
explicit pin.

**Not attributable to this session:** `mini_ork/run_status.py` is untracked in
the primary checkout; it was present at session start and was not created here.

## (c) Blocked on

- **User decision — copy the report into the repo?** Pending since it was raised;
  the report stays unversioned until answered. (G2)
- **User go-ahead for one live run** against the new contract. This is the honest
  way to close G1; the recipe is a real-cost LLM run, so it is not started
  unilaterally.
- **kimi lane credential (401)** in `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/config/secrets.local.sh`.
  Pre-existing and unrelated to this work, but it makes `bin/mini-ork validate`
  exit non-zero, so a clean validate gate is unavailable until it is fixed or the
  lane is removed from `providers.yaml`.
- **Nothing blocks G3–G5** — they are pick-up-able work, not waiting on anyone.
