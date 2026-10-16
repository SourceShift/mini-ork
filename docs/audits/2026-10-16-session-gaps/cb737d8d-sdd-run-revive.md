# Session gaps — `cb737d8d` (SDD run-initiation failure, revive monitor)

**Session:** `cb737d8d-b58f-4c61-97a2-24b432c9e14d` — the session that triaged the failed
mini-ork runs in both homes, root-caused the `2026-10-16-run-initiation` spec-driven-delivery
failures, and landed the engine-side fixes for them.

**Audit window:** 2026-10-16, late evening. **Method:** every claimed status re-checked against
git, the engine source, the fork's crate source, and the live corpora. Nothing below is
inherited — every line marked *receipt* is a command this audit ran, whose output is quoted.

**Scope note.** This report covers *this session's* work only. Sibling reports
`86880f3c-revive-monitor.md` and `83e57620-failed-run-triage.md` cover the same problem space
from other sessions; where they overlap, this report's receipts are additive, not a restatement.

---

## 1. Task table

Live state at audit time: engine `origin/main` head → `60c00154` + this session's `e0171807`;
fork `mini_ork_ui` → `cargo check` clean (see R4).

| id | description | recorded | VERIFIED | evidence |
|---|---|---|---|---|
| R1 | Root-cause why the `2026-10-16-run-initiation` SDD runs died | "in progress" | **DONE — root-caused** | 5 specs / 36 ACs carry **zero** executable fences → `gates-materialize` emits one `NO_EXECUTABLE_FENCE` per AC → `test-validity` fails → 8 failed nodes. `run-1792151693-87094/verifier_gates-materialize.json` vs `verifier_spec-lint.json` (same run, lint said clean) |
| R2 | Stop the runaway recovery loop instead of burning to the iteration cap | "in progress" | **DONE — stopped** | loop is non-convergent by construction (§2 G2); run stopped at round 2 of `max_iterations: 6`; DB now `status='failed'`, `ended_at` 17:35:01 |
| R3 | Lint must flag a spec that can never be probed | "pending" | **LANDED** | `e13b67f2` — `NO_EXECUTABLE_FENCE` (WARNING) in `mini_ork/specdir/lint.py:52`, `_has_executable_fence` at `:115`, firing at `:148` |
| R4 | Crate (`mini_ork_ui`) does not build | "blocked" | **RESOLVED — not a defect of this session** | `cargo check -p mini_ork_ui --all-targets` → ``Finished `dev` profile [unoptimized + debuginfo] target(s) in 13.88s``, 6 warnings, **0 errors**. The 7 errors seen earlier were a peer session's mid-refactor state, since finished |
| R5 | Every diagnostic gate the fix touches must still pass | "pending" | **LANDED + VERIFIED** | `e13b67f2` added `test_lint_flags_a_spec_whose_acceptance_can_never_be_probed`; `tests/test_specdir_ingest.py tests/test_sdd_gates_materialize.py` → **79 passed** |
| R6 | The documented `# ACn precondition` marker path in `gates-materialize` could never fire | "pending" | **LANDED + VERIFIED** | `a401f596` — `_marker_tags_precondition` at `gates-materialize.py`; the injected `set -euo pipefail` header meant the author's marker was never on line 0. 2 new tests, proven to bite against the reverted source |
| R7 | The `specs --help` Codes line was a literal that had already gone stale | "pending" | **LANDED** | `e0171807` — `_codes_line()` derives it from `SEVERITY`; the literal omitted `NO_EXECUTABLE_FENCE` the moment `e13b67f2` added it. `specs --help` now prints 3 error + 6 warning codes |
| R8 | Decision record asserted a lint guarantee that did not exist | "pending" | **LANDED** | `76d91e55` — addendum to `docs/decisions/20261004-sdd-gates-deterministic.md` quoting the measured evidence |
| R9 | Author executable fences into the 5 unprobeable specs | "pending" | **NOT STARTED** | §2 G4 — corpora re-read this audit: all 5 still have 0 shell-fence headers |
| R10 | Fix the two ACs tagged `precondition` against a nonexistent test file | "pending" | **NOT STARTED** | §2 G5 — `tests/unit/test_classify.py` still absent |

**Ancestry receipt** — all four commits are on `origin/main`:

```
$ for c in a401f596 e13b67f2 76d91e55 e0171807; do git merge-base --is-ancestor $c origin/main; done
a401f596  YES
e13b67f2  YES
76d91e55  YES
e0171807  YES
```

**Correction to an earlier claim of mine.** I had recorded R1's adjacent rule
(`mini_ork/recovery/diagnose.py`, `_rule_publish_abstain`) as routing `publish_abstain` to
`fix_epic` *unconditionally*, and flagged it for the user's ruling. Re-reading the source, it
does not: `diagnose.py:521` requires `levels_ok is True` before it returns
`routes_to=ROUTE_FIX_EPIC`, and a withheld publish whose levels are *unproven* falls through to
R1b. The rule is correctly guarded. **Not a defect**; no ruling needed.

---

## 2. Missing features & gaps

**G1 — the recovery loop cannot converge on an unprobeable spec.** (open, by design)
Workflow edge `gates_materialize → reflector (escalates_to, condition: fail)`. The replanner
writes SpecCards (`revise/round-2.md`, `written: 3`) mutating `acceptance[ACn].gate.probe`
— which `gates-materialize` never reads: it rebuilds every probe from the spec's own bash fences,
which the replanner does not touch. Round 2 of a 6-round budget reproduced the identical
`NO_EXECUTABLE_FENCE` verdict. The loop is a cost amplifier on this failure mode, not a
repair path.

**G2 — `NO_EXECUTABLE_FENCE` is a warning, so the corpus still lints "clean".** (accepted)
`r3` requires warning-not-error so no existing corpus changes its `has_errors` result. The
consequence is the exact trap that killed both runs: a spec with acceptance criteria, one
inline `` `command args` `` span, and no fence reports `0 warning(s)` and then dies at
`gates_materialize`. Deliberate, but it means lint is not a sufficient pre-flight for SDD.

**G3 — per-AC fence coverage is still unenforced.** (open)
`e13b67f2` enforces ≥1 fence per *spec*. It does not require one per *AC*. A spec with one
fence and nine ACs still yields nine identical whole-spec probes, so per-AC resolution is
nominal. This is the gap the `20261004` record's Maintainer note believed lint already closed —
the addendum records that it did not. The referenced follow-up
`docs/todos/20261004-specdir-lint-ui-fence-warning.md` **is not present under `docs/todos/`**.

**G4 — all five `2026-10-16-run-initiation` specs remain unprobeable.** (open)
Re-read this audit — every one still has zero shell-fence headers:

| spec | shell-fence headers |
|---|---|
| `crate/pick-files-directories.md` | 0 |
| `engine-phase2/dispatch-preflight-sheet.md` | 0 |
| `engine/board-preflight.md` | 0 |
| `engine/codefix-classifier-coverage.md` | 0 |
| `engine/specdir-kickoff-precedence.md` | 0 |

36 ACs total. Until fences exist, every revive of these runs re-enters the same wall G1 describes.
Writing them is the actual unblock; the lint fix only tells you sooner.

**G5 — two ACs point at a test file that does not exist.** (open)
`codefix-classifier-coverage.md` AC8 and `specdir-kickoff-precedence.md` AC7 both cite
`tests/unit/test_classify.py` and are tagged `precondition`. The engine repo has no such file —
the real classify tests are `tests/unit/test_mini_ork_classify_py.py` and
`tests/unit/test_ide_pages_orch_classify.py`, both present. A `precondition` probe is required
to *pass on the untouched tree*; these cannot, so they fail the gate by construction. Also
`codefix-classifier-coverage.md` **AC1 duplicates AC8 verbatim**.

**G6 — the `specs lint` pre-flight needs `--recursive`.** (resolved in use, undocumented)
A spec dir holding only subdirectories reports `0 finding(s)` without it, which reads as
"clean". With `--recursive` the same corpus reports `5 finding(s): 0 error(s), 5 warning(s)`, all
`NO_EXECUTABLE_FENCE`. Not a defect of the tool (`--recursive` is documented) but it is a
foot-gun for any pre-flight script that omits the flag.

---

## 3. Blocked on

- **The user**, for one decision only: which existing test proves which AC in the five specs.
  Authoring fences (R9) is mechanical once that mapping is settled; guessing it would write
  `precondition` tags that fail the gate, which is exactly G5.
- **Not blocked on** the crate (R4 resolved) or on any missing engine capability. R3–R8 are all
  landed and verified; the remaining work is corpus authoring in the fork, not engine code.
- **Pending, not blocking:** `run-1792151796-92942` (failed 17:21:20, auto-repair gave up —
  `retry-hint.json` present) still awaits a revive decision, per the standing instruction to
  revive at most a few runs per firing, cheapest first.
