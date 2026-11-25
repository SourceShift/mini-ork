# research-synthesis — two defects that cost a finished document

Status: **open**
Opened: 2026-11-25
Found by: `run-1795594516-17762` (cloud-saas-aws-architecture kickoff)

## Why it matters

The run produced four lens reports and a complete 530-line synthesis. The
verifier then failed the run on a citation style, and the recipe's own failure
policy threw the synthesis away. Recovering it required reading the model
session transcript. Two independent defects; either one alone would have
destroyed the output.

## Tasks

### 1. Verifier false-negative on code-anchor citations

Status: **fixed** (commit "fix(research-synthesis): count file:line code anchors
as sources") · Last worked: 2026-11-25 · Remaining: —

- `verifiers/source-completeness.py` counted only URLs, arXiv IDs, GitHub
  links and `(Author Year)` parentheticals. The code lens cites
  `path/to/file.ext:42` anchors, so a 430-line, 73-anchor survey scored
  "2 sources, need ≥5" and the run was failed on citation style, not content.
- Fix: the file:line anchor pattern now counts. Measured on the run:
  lens-codex 2 → 26 → 78, run total 198, `pass: true`.
- Anchor forms that count (`SOURCE_PATTERN`): `path/to/file.ext:42`,
  repo-root `setup.py:12`, and `org/repo:path/file.py:42` (the directory part
  is optional and may contain `:`, which the first version could not match).
  A bare filename without `:42` is a mention, not an anchor, and does not count.
- Regression test: `tests/unit/test_research_synthesis_source_verifier_py.py`
  (commit `1cb3fd85`), four cases — anchors count, an uncited lens fails, a
  line number is required, the synthesis must name every lens.
- Known limits, left on purpose (named by the pre-push review): the extension
  allowlist is fixed and case-sensitive, so a lens citing `.dart`, `.ex` or
  `Dockerfile:12` still scores 0 on those lines; counting is per line, so N
  anchors in one file read as N sources. Both widen what the gate accepts and
  do not reopen this defect; revisit if another lens fails on style.

### 2. `request_changes` + `discard_synthesis` destroys the most expensive artifact

Status: not started · Last worked: — · Remaining: design decision + patch

- `artifact_contract.yaml` has `failure_policy: request_changes` and
  `rollback_strategy: keep_lens_reports_discard_synthesis`. When the verifier
  fails, the rollback node deletes `synthesis.md` and writes
  `NEEDS-CHANGE.md`, and the `retry` recovery refuses to run until
  `--ack-change`.
- The synthesis is the one artifact the run exists to produce, and it is also
  the one the rollback policy deletes. A verifier bug is therefore
  indistinguishable from a bad synthesis, and it is the expensive half that is
  lost.
- Suggested: move a discarded synthesis to `synthesis.rejected.md` (keep it,
  rename it) instead of unlinking it; have `recover --strategy retry` re-run
  the verifier against the existing artifact before honouring
  `NEEDS-CHANGE.md`, so a fixed verifier heals the run without `--ack-change`.
- Second-order effect observed: attempt 2 resumed the same provider session
  (`902a3757-…`) where the model remembered writing the document and emitted a
  1.9 KB status memo instead of the file. A re-dispatch after an
  artifact-discarding rollback must not reuse the prior session — either null
  `node_attempts.provider_session_id` and drop the `node_checkpoints` row, or
  make the runtime do it. `recover --force` alone reuses the session and
  reproduces the memo.
- Confirmed on attempt 3, after nulling the session id and dropping the
  checkpoint row: a fresh session wrote a new 868-line synthesis in one pass.
  The memo was session state, not a prompt or model problem.

### 3. The rolling output path collides across topics

Status: not started · Last worked: — · Remaining: per-kickoff output override

- `artifact_contract.yaml` declares `outputs: [docs/research/synthesis-latest.md]`
  and the kickoff-level override is explicitly "future v0.3". So every topic
  publishes over one path: before this run, that file held a LiveKit
  synthesis from `run-1794659005-72818` (2026-11-14), and anyone reading it as
  "the research synthesis" got the wrong topic with no signal.
- Suggested: honour an `output:` field in the kickoff, or name the default
  path after the kickoff stem (`docs/research/<kickoff-stem>-synthesis.md`)
  and keep `synthesis-latest.md` as a pointer file listing recent runs.

### 4. Rubric judge emits unparseable JSON

Status: not started · Last worked: — · Remaining: reproduce + parse guard

- `rubric.json` for this run has `parse_error: true`, `score: -1`; its own
  diagnostic shows the verdicts it *did* produce are `PASS` ("Quality floor:
  PASS"). The run-level verdict is unaffected (`pass`), but the advisory score
  is noise.
- Suggested: the judge's response must be parsed per item or fail with the raw
  text attached as evidence, not collapse the whole rubric to `-1`.
