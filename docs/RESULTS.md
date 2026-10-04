# Results — what has been measured, and how

Every number in the README links here. Each section says what was run, on what, with
which models, and what the number does **not** show. Small samples are called small.

## 1. `mini-ork certify` on the demo repository

`examples/certify-demo/demo.sh` builds a one-file repository whose `median()` returns the
upper middle value for even-length input, plus two changes that both make the example in
the bug report pass:

- `fix-correct` averages the two middle values;
- `fix-cheat` returns `2.5` when the input is exactly `[1, 2, 3, 4]` and changes nothing else.

Result (2026-10-01, `MO_CERTIFY_MODEL=minimax`, 4 invariants):

| change | verdict | exit | what decided it |
|---|---|---|---|
| fix-correct | PROVEN | 0 | the reproduction test passes, and 4 of 4 generated invariants hold |
| fix-cheat | REFUTED | 1 | the reproduction test passes, but 0 of 4 invariants hold |

Cost per certificate at list price: about $0.03 (MiniMax-M3), 1.5–2 minutes.

The reproduction test alone (`assert median([1, 2, 3, 4]) == 2.5`) passes on **both**
changes. The invariants are what separate them: they are generated to vary the input the
patch may have special-cased, and each one is kept only if it fails on the unpatched
code. The same pair has returned the same verdicts on every run since 2026-09-29.

## 2. `mini-ork certify` on real repositories

<!-- RESULTS:REAL-REPOS -->
Three external Python libraries, each with a real upstream bug fix (merged between
October 2025 and August 2026, so less likely to be memorized) and, on a second branch, a plausible but wrong fix that makes the
issue's own example pass. Ground truth is the upstream test that ships with the fix,
run separately; certify never sees it. Fixtures were built by a separate agent and
checked five ways (install, test fails on base, passes on the fix, fails on the wrong
fix, issue example passes on the wrong fix). Git history after the fix was removed from
each clone so the answer could not be looked up.

| repository | bug (upstream issue) | wrong fix | verdict on correct fix | verdict on wrong fix |
|---|---|---|---|---|
| marshmallow | `fields.Constant(x, required=True)` raises at declaration (#2900) | sets the default after `__init__`, so declaring works but loading a missing field fails | PROVEN ✓ | REFUTED ✓ |
| toolz | `partition_all` leaks its padding token when `__len__` is wrong (#602) | raises only when `__len__` over-reports, not when it under-reports | PROVEN ✓ | REFUTED ✓ |
| pyparsing | `QuotedString` strips whitespace from a delimiter like `"\n;"` (#492) | strips only spaces and tabs, so whitespace-only delimiters are accepted again | **REFUTED ✗** | **PROVEN ✗** |

**First pass: 4 of 6 verdicts correct** (2026-10-01, `MO_CERTIFY_MODEL=minimax`, 4
invariants, one run each). Both misses were on pyparsing:

- The correct fix was rejected because 3 of the 4 generated invariants were wrong: two
  put a tab inside the delimiter and assumed pyparsing keeps tabs in the input (it
  expands them by default), one assumed a multi-character end delimiter behaves in a way
  it does not. They fail on *any* correct fix. Finding this needed the invariant source
  in the certificate, which certificates now carry (`evidence.invariants[].src`).
- The wrong fix was called PROVEN on the first run and REFUTED on a second run with the
  same inputs. The only thing that separates it from the correct fix is a sentence in
  the issue ("whitespace-only delimiters are rejected today, which seems right"), and the
  generated invariants did not always test that sentence.

**The change.** An invariant that fails on the patch now counts against it only if the
patched code fails *the same way the buggy base did* (same first pytest `E` line) — the
signature of a fix that did not generalise. If the patch changed the behaviour on that
input, the invariant is *inconclusive*. PROVEN still needs two thirds of **all**
invariants to hold, so the change cannot create a PROVEN; it can only turn a REFUTED into
an UNVERIFIED.

**Second pass, same 8 branches** (demo pair included, one run each):

| | correct fix | wrong fix |
|---|---|---|
| demo (`stats.median`) | PROVEN ✓ | REFUTED ✓ |
| marshmallow | PROVEN ✓ | REFUTED ✓ |
| toolz | PROVEN ✓ | REFUTED ✓ |
| pyparsing | UNVERIFIED (2 hold, 2 inconclusive) | REFUTED ✓ |

On the three external repositories: **5 of 6 correct, 1 abstention, 0 wrong.** One run
per branch is a small sample, and the pyparsing wrong fix has already shown it can flip
between runs; treat this as "no wrong verdict seen", not as a rate.

Fixtures and per-branch certificates: kept outside this repository (third-party code).
<!-- RESULTS:REAL-REPOS-END -->

## 3. The oracle on an adversarial SWE-bench corpus (July 2026)

`certify` is a port of an oracle first measured on SWE-bench. The corpus was 120
SWE-bench instances across 9 repositories. For each instance it was given the gold fix
and, where one could be built, a **hard negative**: a patch that passes the issue's own
reproduction but fails SWE-bench's official `FAIL_TO_PASS` tests (so it is verifiably
wrong). Precision is the share of PROVEN verdicts that were correct fixes; a *false
completion* is a wrong patch called PROVEN.

| version | PROVEN that were correct | false completions | recall (correct fixes proven) |
|---|---|---|---|
| v1 (all 120 instances) | 28 of 29 (97%, Wilson 95% lower bound 83%) | 1 | 28 of 37 (76%) |
| v2 (first 86 instances of the re-run) | 21 of 21 (100%, Wilson 95% lower bound 85%) | 0 | 21 of 25 (84%) |

The v1 false completion was `matplotlib__matplotlib-23299`: a patch that special-cased the
reported input passed the reproduction and all four generated invariants, because the
invariants never varied the dimension it special-cased. v2 changed two things: the
invariant generator sees the candidate patch and is asked for inputs that avoid whatever
it special-cases, and every invariant must fail on the unpatched base before it counts.
The PROVEN bar is two thirds of the invariants. On v2 the 12 known cheats were all
stopped, and the old miss now comes back UNVERIFIED (the oracle abstains) instead of
PROVEN.

Limits: the samples are small (a 21-of-21 result still has an 85% lower bound), the
SWE-bench evaluation harness is not part of this repository, and SWE-bench repositories
are well known to the models — which is exactly what hid the "the model tests its own
copy of the code" failure that `certify` had to fix for unseen repositories (see
`mini_ork/certify/context.py`).

## 4. Held-out tasks mined from this repository's own history

`scripts/mine_heldout_tasks.py` mines SWE-bench-style tasks from this repository's `fix:`
commits: each task is the parent commit, the commit message as the task statement, and
the commit's test changes as **hidden** tests that fail before the fix and pass after it.
92 tasks, split dev 40 / test 52 (`evals/heldout/mined/manifest.json`). The test split has
never been run, so nothing has been tuned on it.

`scripts/run_heldout.py` solves each task with `mini-ork run code-fix` in a scratch
checkout, then restores the hidden tests and grades `FAIL_TO_PASS` + `PASS_TO_PASS`. The
solver never sees the hidden tests.

Baseline, dev split, 2026-09-29 (MiniMax-M3 implementer, GLM-5.3 reviewer):

| difficulty | resolved |
|---|---|
| easy | 10 of 14 |
| medium | 7 of 14 |
| hard | 5 of 12 |
| **total** | **22 of 40 (55%)** |

Per-task results: `evals/heldout/results/2026-09-29-baseline-dev.json` (its `cost_usd`
fields are the old meter's figures, which sum to $95.92; the list-price total below is
recomputed from token counts).

**Cost, at the providers' list prices:** $16.83 for all 40 tasks — **$0.42 per task, $0.77
per resolved task** — recomputed from the token counts recorded in `llm_calls`
(uncached input, cache reads, output) at MiniMax-M3 $0.30 / $0.06 / $1.20 and GLM-5.3
$1.40 / $0.26 / $4.40 per million tokens. At the time, the cost meter reported $123.70 for
the same run, because it took the `claude` CLI's own cost figure, which prices models it
does not know at Anthropic rates.
The meter now prices each model at its list price (commits `46f32302`, `13ba1c53`).

What this does not show: 40 tasks from one repository, one configuration, one run. The
task statement is a commit message, which often names the fix, so these tasks are easier
than real bug reports; 24 of the 92 tasks are also flagged `weak_signal` (the hidden test
largely restates the change). It is a baseline for measuring changes, not a leaderboard
number.

Reproduce:

```bash
python3 scripts/run_heldout.py --split dev --out results.json   # needs working model lanes
```

## 5. Does learned routing lower cost? Not yet — the reward it learns from is too noisy

The claim: as verified outcomes accumulate, the router moves work to cheaper models where
they verify just as well. We tested it on the held-out tasks of §4 (October 2026), with the
implementer's learning reward set to "the run passed its own checks, minus a cost penalty"
(`MO_IMPL_REWARD=run_verified`, `MO_ROUTER_COST_LAMBDA=0.1`) and spend at list prices from
`llm_calls`.

Arms: two fixed implementer lanes — **S-ds** (deepseek-v4-pro) and **S-mm** (MiniMax-M3) —
on the dev split D1 (14 tasks); then the learner **L** (`learning_governed`, ε = 0.10),
seeded with the implementer traces of both fixed arms on D1, against S-ds on a second
split D2 (15 tasks). Every arm ran the same engine commit and the same recipe; "solved"
means the task's hidden tests pass on the run's patch.

| arm | split | solved | spend | $ per solved | implementer lanes |
|---|---|---|---|---|---|
| S-ds | D1 | 8 / 14 | $8.68 | $1.09 | deepseek 14 |
| S-mm | D1 | 3 / 14 | $1.54 | $0.51 | MiniMax 8 (6 runs stopped before the implementer) |
| S-ds | D2 | 8 / 15 | $11.60 | $1.45 | deepseek 15 |
| L | D2 | 5 / 15 | $6.54 | $1.31 | MiniMax 13 (learned), deepseek 2 (exploration) |

**Result.** On D2 the learner spent 44% less than S-ds and solved 3 fewer tasks. Paired by
task, it solved a strict subset of S-ds's tasks (5 both, 3 only S-ds, 0 only L; exact
McNemar p = 0.25 at n = 15 — the direction is consistent, the sample is small). Lower cost
at the *same* correctness — the claim — is not shown.

**Why.** The router learned that MiniMax was the better implementer (relative advantage
0.025 over 8 runs vs deepseek's 0.020 over 14), the opposite of what the hidden tests say.
Its reward is the run's own verdict, and that verdict disagreed with the hidden tests on
40% of the 52 runs that executed:

| run's own verdict | hidden tests pass | hidden tests fail |
|---|---|---|
| passed | 16 | 13 |
| failed | 8 | 15 |

On D1, five of S-ds's eight correct fixes were failed by the run's LLM reviewer node (four
"revise", one "fail"); on D2, seven S-ds runs passed their own checks with a patch the
hidden tests reject. A learner fed that signal follows it. The six S-mm runs that stopped
early were blocked by the run-profile gate ("what command should prove this run
succeeded?") before any implementer ran, so S-mm's D1 row understates the MiniMax
implementer (3 solved of the 8 runs that reached it).

**What this means.** The routing mechanics work — the learner did move nearly all work to
the lane its reward preferred, at roughly half the cost — but the in-run verdict is not a
good enough label to learn correctness from. Before learned routing can claim "cheaper at
equal quality", its reward has to be grounded in test outcomes rather than the reviewer's
judgement, or the reviewer has to be calibrated against hidden-test truth. The raw outputs
(results JSON, patches, state databases) are kept with the experiment scripts outside the
repository.
