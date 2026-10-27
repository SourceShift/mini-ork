# S1: a "do not modify" sentence that wraps keeps its polarity on every line

## Goal

`kickoff_scope()` (`mini_ork/kickoff_scope.py` ~177-223) decides the
framework-edit `scope-respected` check's allow and deny lists
(`recipes/framework-edit/verifiers/static-check.py` ~704-790). In an allow
section it reads the kickoff ONE PHYSICAL LINE at a time and judges each
clause's polarity with `_negative_clause` (a clause is negative when it
STARTS with "do not", "never", "except", …).

A hard-wrapped sentence therefore loses its "do not" after the first line:

```
Do NOT modify `a/y.py`, `a/z.py`,
`recipes/`, `docs/` (note), or any path in
`config/kernel-frozen.yaml`. Every existing test must pass.
```

gives `deny = ["a/y.py", "a/z.py"]` and puts `recipes/`, `docs/` and
`config/kernel-frozen.yaml` in **allow**. Measured 2026-10-27: every bif
kickoff wraps its "Do NOT modify" sentence like this, so each run's scope
check allowed edits to `docs/`, `recipes/` and the kernel-frozen list it was
told to leave alone.

The same module already has `_logical_lines` (~290-330), which joins
hard-wrapped lines into paragraphs and list items for `do_not_sentences`.
Use it for scope polarity too.

All paths are relative to the repository root.

## Mechanism (exact spec)

In `mini_ork/kickoff_scope.py`:

1. `_logical_lines(text: str, *, keep_marker: bool = False) -> list[str]`.
   With `keep_marker=False` the behaviour is byte-for-byte as today. With
   `keep_marker=True`, a list item's logical line starts with the item's raw
   line stripped (`raw.strip()`, marker included) instead of
   `raw[m.end():].strip()`, so `paths_in_line` can still read a bare bullet
   head. Continuation joining, headings, blank lines and fences are unchanged.
2. `kickoff_scope(text)` iterates `_logical_lines(text, keep_marker=True)`
   instead of `text.splitlines()`:
   - a logical line matching `_HEADING_RE` sets `current = heading_kind(...)`
     exactly as today;
   - `current is None` → skip;
   - `current == "deny"` → every path in `paths_in_line(line)` is denied, as
     today;
   - otherwise the existing clause loop (`_SENTENCE_SPLIT_RE`,
     `_negative_clause`) runs on the joined logical line.
   Fences are already dropped by `_logical_lines`; remove the now-redundant
   fence tracking from `kickoff_scope` only if every test still passes.
3. Nothing else changes: `heading_kind`, `paths_in_line`, `_negative_clause`,
   `do_not_sentences` and the return shape stay as they are.

## Files in scope (touch ONLY these)

- `mini_ork/kickoff_scope.py`
- `tests/unit/test_kickoff_scope_polarity.py`

Do not modify `recipes/framework-edit/verifiers/static-check.py`,
`mini_ork/context_v2.py`, `mini_ork/memory/preferences.py`, `docs/`, or any
path in `config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (append to `tests/unit/test_kickoff_scope_polarity.py`, hermetic)

Each kickoff text below sits under a `## Files in scope (touch ONLY these)`
heading.

1. **The wrapped deny sentence above** (with a `- \`a/x.py\`` bullet before
   it) → `allow == ["a/x.py"]` and `deny` contains `a/y.py`, `a/z.py`,
   `recipes/`, `docs/`, `config/kernel-frozen.yaml`.
2. **A new sentence after a terminated deny.** `Do not modify \`a\`.` then, on
   the next line, `Also edit \`b\`.` → `b` in allow, `a` in deny.
3. **A bullet with a wrapped description stays allowed.** `- \`a.py\` (new;` /
   `  do not add tests here)` → `a.py` in allow.
4. **A bare bullet head still parses.** `- mini_ork/foo.py` (no backticks) →
   `mini_ork/foo.py` in allow.
5. **A blank line ends the sentence.** `Do not modify \`a\`,` / blank line /
   `\`b\` is in scope.` → `b` in allow.
6. **A `;` ends the clause.** `Do not modify \`a\`; edit` / `\`b\` instead.`
   → `b` in allow, `a` in deny.
7. `_logical_lines(text)` with the default `keep_marker=False` returns the
   same list as before for a text with list items (strip-marker behaviour
   pinned).

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_kickoff_scope.py tests/unit/test_kickoff_scope_polarity.py tests/unit/test_context_v2.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_kickoff_scope_polarity.py --junitxml="${MINI_ORK_RUN_DIR}/bif-s1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-s1-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/kickoff_scope.py tests/unit/test_kickoff_scope_polarity.py` is clean.
- No path named in a wrapped "do not" sentence ends up in the allow list.
