# RSI Tier-0 #7 (loop defect D3): a negative clause inside an allow section denies its paths

## Goal

`mini_ork/kickoff_scope.py:kickoff_scope` puts every path found under an
allow-kind heading (any heading containing "scope") into `allow`.

**The failure.** The recursive-self-improve kickoff's `## Scope` section says:

> Improvable surface: the `mini_ork/` Python package, `recipes/`, and
> `schemas/`. Leave `.mini-ork/` (run state) untouched; …

So `.mini-ork/` is parsed as **allowed**. `context_v2.parse_contract` then
injects it as a hard constraint, "Touch only these files: …, .mini-ork/". That
is the inverse of the kickoff's intent, as observed in the 2026-10-23 cycle-3
synthesis.

**The fix.** Make polarity clause-aware inside allow sections, without changing
how paths are found.

All paths are relative to the repository root.

## Mechanism (exact spec)

All in `mini_ork/kickoff_scope.py` (the module stays pure: no I/O, no env, no
`mini_ork` imports).

### 1. New private helper `_negative_clause(clause: str) -> bool`

It strips, in order:
1. a leading list marker (`_LIST_MARKER_RE`);
2. leading markup (`_START_MARKUP_RE`);
3. `**` / `__` / backticks (`_strip_markup`).

Then it lowercases the text and returns `True` when the text:

- starts with one of `_TRIGGERS` (`do not`, `don't`, `never`, `must not`); or
- starts with `except`; or
- matches `^(leave|keep)\b.*\b(untouched|unchanged|alone|as[- ]is)\b`.

Otherwise it returns `False`.

### 2. `kickoff_scope` (physical lines, as today)

- Heading and fence handling are unchanged.
- For a line in a **deny** section, behaviour is unchanged (all its paths are
  denied).
- For a line in an **allow** section:
  - Split the line into clauses with `_SENTENCE_SPLIT_RE`. The first clause
    keeps the line's list marker, so a bare bullet head (`- mini_ork/foo.py`)
    is still found by `paths_in_line`.
  - For each clause, take `paths_in_line(clause)`. Those paths go to `deny`
    when `_negative_clause(clause)`, else to `allow`.
- The existing rule stays: a path in both lists is kept only in `deny`.
- The return shape and document order are unchanged.

### 3. `do_not_sentences`

A sentence is also kept when it matches the `leave|keep … untouched|unchanged|alone|as-is`
pattern of `_negative_clause`, so "Leave .mini-ork/ (run state) untouched"
becomes a do-not rule. Everything else in that function is unchanged.

### 4. Module docstring

Add one sentence recording the polarity rule and its known limit: a negative
clause wrapped across two physical lines only denies the paths on its first
line.

## Files in scope (touch ONLY these)

- `mini_ork/kickoff_scope.py`
- `tests/unit/test_kickoff_scope_polarity.py` (new)

Do NOT modify any other file. Every existing test in
`tests/unit/test_kickoff_scope.py` must pass unchanged.

## Tests (`tests/unit/test_kickoff_scope_polarity.py`: pure, no I/O)

Use this text, exactly as written, with the wrap on the third bullet line:

```
## Scope
- Target repo: this mini-ork checkout at `/tmp/wt` — an isolated git worktree
  on branch self-improve/iter-1-x.
- Improvable surface: the `mini_ork/` Python package, `recipes/`, and
  `schemas/`. Leave `.mini-ork/` (run state) untouched; tests are read-only
  context unless the patch adds a regression test.
- Exactly one patch this iteration; keep the diff minimal and reviewable.
```

Tests:

- **The template.** `kickoff_scope(text)`:
  - `".mini-ork/"` is in `deny` and not in `allow`;
  - `"mini_ork/"`, `"recipes/"` and `"schemas/"` are all in `allow`.
- **The rule is emitted.** `do_not_sentences(text)` contains
  `"Leave .mini-ork/ (run state) untouched"`.
- **Mixed clauses on one line.** Under `## Files in scope`, the line
  ``- Leave `a/` untouched; edit `b/foo.py`.`` → `a/` denied, `b/foo.py`
  allowed.
- **A trigger clause.** Under `## Files in scope`, the line
  ``- Do not touch `x/y.py`.`` → `x/y.py` denied.
- **Keep … unchanged.** Under `## Scope`, the line
  ``Keep `config/agents.yaml` unchanged.`` → denied.
- **"keep" without the qualifier stays allowed.** Under `## Scope`, the line
  ``Keep the tests in `tests/unit/test_a.py` passing.`` → allowed (no
  untouched/unchanged/alone/as-is).
- **Bare bullet heads still parse.** Under `## Files in scope`, the line
  `- mini_ork/foo.py — the parser` → `mini_ork/foo.py` allowed.
- **Deny sections are unchanged.** Under `## Out of scope`, the line
  ``- `z/` (keep it)`` → `z/` denied.

## Verification commands

- `pytest -q tests/unit/test_kickoff_scope_polarity.py tests/unit/test_kickoff_scope.py`
- `pytest -q tests/unit/test_context_v2.py tests/unit/test_context_v2_wiring.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_kickoff_scope_polarity.py --junitxml="${MINI_ORK_RUN_DIR}/scope-polarity-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/scope-polarity-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/kickoff_scope.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
