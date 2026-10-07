# Only verified learnings in prompt files — revert the 18 unverified directives, guard against more

## The rule (user decision, 2026-10-07)

"Only verified learnings should be added to the prompts." A learned directive may sit in a recipe
prompt file only if the apply loop MEASURED it and promoted it: probe/code scorer, decision
`promoted`, rationale with neither `UNVETTED` nor `scorer=mock`.

## Why (live evidence)

Commit 13b2f210 (2026-09-12, "F3 first live gradient sweep") appended 18 directive blocks to 10
recipe prompt files. Every block has this shape:

```

<!-- applied:gradient_records:<source_id> -->
- Observation: <text>
- Directive: <text>
```

All 18 were promoted on the mock scorer via `MO_APPLY_UNVETTED`. Their rationale starts
"UNVETTED promote (scorer=mock fabricates utility; operator-enabled via MO_APPLY_UNVETTED)". A
scan of `recipes/*/prompts/*.md` against `apply_attempts ⋈ promotion_records` finds 18 live
markers: 18 unverified, 0 verified. Source ids and files:

- `gr-093a20cdc2ac` framework-edit/prior-art-lens.md
- `gr-104ae59b748b` code-fix/reviewer.md (measured 2026-10-05: no gain over control)
- `gr-11f4aba904d9` recursive-validate-impl/planner.md
- `gr-12809214f154` researcher-qdrant-contract/reviewer.md
- `gr-1c9d2fad50a1` framework-edit/implementer.md
- `gr-273f14dc50f3` research-synthesis/synthesis.md
- `gr-34c3297700fe` research-synthesis/planner.md
- `gr-354ca01c7d27` recursive-validate-impl/implementer.md
- `gr-388d3a5e7b10` refactor-audit/planner.md
- `gr-5655b20811e1` framework-edit/planner.md
- `gr-59a2f4c2cbf5` blog-post/planner.md
- `gr-7b8e0368cc24` framework-edit/code-impact-lens.md
- `gr-9687377cb10a` obs-smoke/tiny-reviewer.md
- `gr-a3d73ad9b318` researcher-qdrant-contract/implementer.md
- `gr-becc5b498213` framework-edit/reviewer.md
- `gr-d29a38fd0963` researcher-qdrant-contract/planner.md
- `gr-d8d6a0740ee5` code-fix/implementer.md
- `gr-e0e0beeecda2` blog-cohesion/arbiter.md

New promotions are already safe: mock/gepa "can never promote, regardless of env"
(`mini_ork/cli/apply.py` docstring), and `MO_APPLY_UNVETTED` is no longer read. Only the legacy
has to go, plus a guard so it can't come back silently.

`version_registry` holds one `kind='agent'` row per promoted prompt, with `name` = an absolute
path in an old worktree (e.g. `/…/mini-ork-worktrees/f3-apply-enable/recipes/blog-post/prompts/planner.md`).
Match those rows by the `recipes/<recipe>/prompts/<file>` suffix.

## Files in scope (touch ONLY these)

- `mini_ork/learning/prompt_directives.py` (new)
- `mini_ork/cli/apply.py`: ONLY a new `--revert-unverified` branch in `main`, and a sidecar write
  inside `apply_mutation` on a real promote
- the 10 prompt files listed above: block removal only (done by running the new command)
- `recipes/framework-edit/prompts/reviewer.md`: block removal + the authored output contract
  (fix 5)
- `tests/unit/test_prompts_only_verified.py` (new)

Do NOT modify any other file.

## Changes (exact)

1. **`prompt_directives.scan(repo_root) -> list[dict]`**: every `<!-- applied:gradient_records:<id> -->`
   block in `recipes/*/prompts/*.md`: `{source_id, file (repo-relative), start_line, end_line, text}`.
   A block = the marker line + the following `- Observation:` / `- Directive:` lines (stop at
   the first line that starts with neither) + exactly one preceding blank line if present.
2. **`verification(db, source_id) -> dict`**: `{"verified": bool, "candidate_id", "decision",
   "rationale", "decided_at"}`. Verified iff some `promotion_records` row for an
   `apply_attempts.candidate_id` with that `source_id` has `decision='promoted'` and a rationale
   containing neither `UNVETTED` nor `scorer=mock` nor `scorer=gepa`.
3. **`revert_unverified(repo_root, db, *, dry_run=False, files=True, record=True) -> dict`**:
   - for every unverified block, remove it from its file (byte-exact; the file must otherwise be
     unchanged);
   - when `record`: insert one `promotion_records` row per source_id with
     `decision='reverted'`, `decided_by='operator'`, `rationale="unverified: promoted on a
     simulated (mock) score via MO_APPLY_UNVETTED; removed under the rule 'only verified
     learnings in prompts' (2026-10-07)"`, and `candidate_id` = the original candidate;
   - set matching `version_registry` agent rows (suffix match) to `status='quarantined'`,
     `quarantine_reason` = the same text, `quarantined_at` now.
   Returns `{"removed": [...], "kept_verified": [...], "files_changed": [...]}`. Idempotent: a
   second run removes nothing and records nothing.
4. **CLI** `mini-ork apply --revert-unverified [--dry-run] [--files-only | --db-only]` → prints
   that JSON. Exit 0.
5. **Sidecar guard.**
   - On a real promote, `apply_mutation` appends `{source_id, candidate_id, scorer, n, before,
     after, decided_at}` to `recipes/<recipe>/prompts/.verified-directives.json` (a JSON list,
     sorted by source_id).
   - `tests/unit/test_prompts_only_verified.py` scans the REAL repo `recipes/*/prompts/*.md` and
     fails if any applied marker lacks a sidecar entry with `scorer` in `{probe, code}`. The
     failure message lists the offending files.
6. **Keep the reviewer's findings contract, as design rather than a learning.** The directive
   being removed from `framework-edit/prompts/reviewer.md` (`gr-becc5b498213`) is the only place
   that prompt asks for file-level findings, and the IDE's "Your code" view depends on them. Add
   an authored section `## Output contract: findings` to that prompt:
   - the review JSON MUST include `findings: [{"file", "line", "severity": "high|medium|low",
     "snippet", "issue"}]`;
   - a `needs_revision` verdict MUST carry at least one finding;
   - `file` is repo-relative; `line` is the line number in the changed file.
   Mirror the wording style of `recipes/code-fix/prompts/reviewer.md:95-110`, which already
   specifies a `file` field. No `applied:` marker: this is recipe design.
7. Run `python3.11 -m mini_ork.cli.apply --revert-unverified --files-only` in the worktree so the
   18 blocks are removed from the committed prompt files. Do NOT run with record against the live
   DB: the operator runs `--db-only` after merge.

## Tests (`tests/unit/test_prompts_only_verified.py`, temp repo + DB, plus the real-repo guard)

- `scan` finds blocks with exact line ranges on a fixture prompt with 2 blocks and surrounding
  text; `remove` leaves the surrounding bytes identical.
- `verification`: an UNVETTED-mock promote → False; a `probe: n=2 …` promote → True; a
  quarantined-only source → False.
- `revert_unverified` on the temp repo/DB removes only the unverified block, writes one
  `reverted` row and quarantines the suffix-matched version row; the second call is a no-op.
- `apply_mutation` on a real promote writes the sidecar entry.
- The real-repo guard test passes after step 7 (no markers left, or all with sidecar entries).

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_prompts_only_verified.py tests/unit/test_cli_apply_py.py tests/unit/test_apply_significance.py   # must exit 0
```

(These are the existing apply test files; keep them green.)

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/learning/prompt_directives.py mini_ork/cli/apply.py tests/unit/test_prompts_only_verified.py` → clean.
- `grep -rc "applied:gradient_records" recipes/*/prompts/*.md | grep -v ":0"` prints nothing. Paste it.
- `git diff --stat` shows the 10 prompt files with deletions only (plus the reviewer contract
  addition) and the new/edited code files.
