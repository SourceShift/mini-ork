# RSI Tier-1 #12c: an anti-bloat budget on prompt mutation (GEPA G02-T06)

## Goal

The R2K review (G02-T06, `internal-docs/research/2026-09-29-rsi-technique-review-2000.md`)
documents a failure: evolutionary optimizers "such as GEPA append rules until
prompts are up to 3x longer with no accuracy gain" (arXiv 2609.04197). The
technique therefore includes "anti-bloat selection".

The status doc (`docs/plans/2026-10-24-rsi-technique-status.md`, survey
cautions) answers "GEPA needs an anti-bloat guard" with **No: append mode grows
prompts**. `mini_ork/cli/apply.py` `apply_mutation` in `MO_APPLY_MODE=append`
(the default) adds one `<!-- applied:… -->` block per promotion. It has no
size, count or novelty limit. The only dedupe is an exact `source_ref` match,
so the same directive arriving from two sources lands twice.

`apply_mutation` is the only live writer: GEPA (`mini_ork/gepa/run_gepa.py`)
writes candidates to an `--out` directory and never to the live recipe.

Today's state:

- 0 applied blocks are on disk across 290 `recipes/*/prompts/*.md` files. They
  were reverted under the 2026-10-07 only-verified rule.
- Prompt sizes (stripped): median 2,135 chars, p90 4,507, max 7,457.
- `apply_attempts` holds 43 rows and no measured promote. Apply is OFF by
  default (`MO_APPLY_ENABLED=0`).

The guard is preventive. It must be in place before the apply loop is switched
on.

**Default behaviour.**

- **On by default.** Opt out with `MO_APPLY_PROMPT_BUDGET=0`. It has no effect
  until apply writes, because `MO_APPLY_ENABLED=0` remains the master gate.
- **The budget.** A mutation is refused when any of these holds:
  - its directive is a near-duplicate (token Jaccard ≥ 0.8) of a directive
    already applied to the file;
  - the file would carry more than 5 applied blocks;
  - the file would exceed `max(1.5 × base, base + 1,200)` characters, where
    *base* is the prompt with every applied block removed.

  At the median prompt that ceiling is 3,335 characters (1.56×), well under
  GEPA's 3×.
- **When it is checked.** In `apply_run` before scoring, so a refused candidate
  spends no probe budget. Again in `apply_mutation`, for direct callers.

**Merge order.** `rsi-t1-kernel-hole-2-apply-guard`, `rsi-t1-layer-routing-2-apply`
and `rsi-t1-harness-evolution-apply` also edit `mini_ork/cli/apply.py`. Serialize
them through the worktree claim. The insertion points below are anchored so
they hold whichever of those has landed.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/prompt_budget.py` (new)

This module is pure: no DB and no imports from `mini_ork.cli`.

**Constants:**

- `DEFAULT_GROWTH_MAX = 1.5`
- `DEFAULT_MAX_DIRECTIVES = 5`
- `DEFAULT_DUP_SIM = 0.8`
- `MIN_HEADROOM_CHARS = 1200`
- `_MARKER_RE = re.compile(r"^<!--\s*applied(?::(?P<ref>[^\s>]+))?\s*-->\s*$")`
  This matches both `<!-- applied:<ref> -->` and the bare `<!-- applied -->`
  that `_directive_block` renders when `source_ref` is empty.
- `_BODY_RE = re.compile(r"^- (Observation|Directive):")`
- `_DIRECTIVE_RE = re.compile(r"^- Directive:\s?(?P<text>.*)$")`
- `_TOKEN_RE = re.compile(r"[a-z0-9]+")`

**`enabled() -> bool`** returns
`os.environ.get("MO_APPLY_PROMPT_BUDGET", "1") != "0"`.

**`split_applied(text: str) -> tuple[str, list[dict]]`**

- **Walk.** Walk `text.split("\n")`. A line matching `_MARKER_RE` starts a
  block. The block continues over the consecutive following lines that match
  `_BODY_RE`.
- **Preceding blank line.** When a block starts and the last kept line is `""`,
  pop that line, since it belongs to the block (`apply_mutation` writes
  `existing.rstrip("\n") + "\n\n" + block + "\n"`).
- **Block dict:** `{"ref": m.group("ref") or "", "directive": <text>, "text": "\n".join(block lines)}`.
  `<text>` is the `text` groups of the block's `- Directive:` lines, each
  stripped, joined with `" "`.
- **Returns** `("\n".join(kept_lines), blocks)`.
- **Round trip.** `split_applied("ORIGINAL PROMPT\n" + "\n<!-- applied:x -->\n- Directive: d\n")[0] == "ORIGINAL PROMPT\n"`.

**`directive_similarity(a: str, b: str) -> float`**

- Uses token sets `set(_TOKEN_RE.findall(s.lower()))`.
- Returns `0.0` when either set is empty.
- Otherwise returns `round(len(A & B) / len(A | B), 6)`.

**`check(existing: str, after: str, *, directive: str = "", mode: str = "append", growth_max: float | None = None, max_directives: int | None = None, dup_sim: float | None = None) -> dict`**

- **Limits.** Each limit is the kwarg when it is not `None`. Otherwise:
  - `growth_max` is env `MO_APPLY_PROMPT_GROWTH_MAX` when it parses to a float
    > 1.0, else `DEFAULT_GROWTH_MAX`;
  - `max_directives` is env `MO_APPLY_MAX_DIRECTIVES` when it parses to an
    int ≥ 1, else `DEFAULT_MAX_DIRECTIVES`;
  - `dup_sim` is env `MO_APPLY_DIRECTIVE_DUP_SIM` when it parses to a float
    with 0 < x ≤ 1, else `DEFAULT_DUP_SIM`.
- **Return keys, always:** `ok, kind, reason, base_chars, after_chars, allowed_chars, n_directives_after, max_similarity, limits`.
  `limits` is `{"growth_max", "max_directives", "dup_sim", "min_headroom"}`.
- **Measurements:**
  - `after_chars = len(after.strip())`;
  - `n_directives_after = len(split_applied(after)[1])`.
- **No existing prompt.** When `not existing.strip()`, return `ok=True`,
  `kind="ok"`, `reason="no existing prompt"`, `base_chars=0`,
  `allowed_chars=None` and `max_similarity=0.0`.
- **Otherwise:**
  - `base, blocks = split_applied(existing)`;
  - `base_chars = len(base.strip())`;
  - `allowed_chars = max(int(base_chars * growth_max), base_chars + MIN_HEADROOM_CHARS)`;
  - `max_similarity` is the max `directive_similarity(directive, b["directive"])`
    over `blocks`, but only when `mode == "append"` and `directive.strip()`.
    Otherwise, and with no blocks, it is `0.0`.
- **Refusals,** first match wins:

  | Order | Condition | `kind` | `reason` |
  |---|---|---|---|
  | 1 | `base_chars == 0` | `"empty_base"` | `"no base prompt to budget against"` |
  | 2 | `max_similarity >= dup_sim` | `"duplicate"` | `f"duplicate directive: similarity {max_similarity:.2f} >= {dup_sim:.2f} with an applied block"` |
  | 3 | `n_directives_after > max_directives` | `"directive_cap"` | `f"directive cap: {n_directives_after} applied blocks > {max_directives}"` |
  | 4 | `after_chars > allowed_chars` | `"growth_cap"` | `f"growth cap: {after_chars} chars > {allowed_chars} allowed (base {base_chars} chars, x{growth_max:g} or +{MIN_HEADROOM_CHARS})"` |

  Every refusal sets `ok=False`.
- **Otherwise** `ok=True`, `kind="ok"`, `reason="within budget"`.

### 2. `mini_ork/cli/apply.py`

**Docs.** Add these lines to the module docstring's env list:

- `MO_APPLY_PROMPT_BUDGET=0 disables the anti-bloat prompt budget (default ON)`
- `MO_APPLY_PROMPT_GROWTH_MAX (1.5)`, `MO_APPLY_MAX_DIRECTIVES (5)`, `MO_APPLY_DIRECTIVE_DUP_SIM (0.8)`

**New helper `_prompt_budget_verdict(target_file: str, new_prompt: str, *, source_ref: str = "", context: str = "") -> dict | None`.**

- **No verdict (`None`)** when any of these holds:
  - `prompt_budget.enabled()` is false;
  - `target_file` is not an existing file;
  - reading it raises `OSError`;
  - the mode is `append`, `source_ref` is truthy and `source_ref in existing`.
    The mutation would be an idempotent no-op, so nothing grows.
- The mode is `os.environ.get("MO_APPLY_MODE", "append")`.
- **The would-be text,** built exactly as `apply_mutation` builds `out`:
  - `existing.rstrip("\n") + "\n\n" + _directive_block(new_prompt, source_ref=source_ref, context=context) + "\n"`
    when `existing` and the mode is `append`;
  - else `f"{new_prompt}\n"`.
- **Returns** `prompt_budget.check(existing, after, directive=new_prompt, mode=mode)`.
- Import `mini_ork.learning.prompt_budget` locally inside the helper.

**`apply_run`: the pre-score refusal.**

- **Placement.** Insert immediately before the `# 2. Materialize the candidate.`
  comment, so after every early-return block that precedes materialization.
- **Condition.** Only when `target_kind in ("prompt_file", "agent_prompt")`,
  `target_file` and `suggested_change` are all truthy.
- **Verdict.**
  `verdict = _prompt_budget_verdict(target_file, suggested_change, source_ref=f"{source_kind}:{source_id}" if source_id else "", context=parsed.get("signal", ""))`.
- **On refusal** (`verdict is not None and not verdict["ok"]`):
  - `decision = "rejected"` if `verdict["kind"] == "duplicate"`, else
    `"no_candidate"`;
  - call
    `aid = attempt_record(task_class, target_kind, target_name, source_kind, source_id, "", "", "", "", "", "", decision, f"prompt budget (G02-T06 anti-bloat): {verdict['reason']}", dry_flag, apply_enabled, db=db)`;
  - write `aid + "\n"`;
  - write `'{"decision":"<decision>","task_class":"<task_class>","target":"<target_name>","source_id":"<source_id>","reason":"prompt_budget"}\n'`,
    built with the same f-string style as the edit-memory branch;
  - `return 0`.
- **Why two decisions.** A duplicate is `rejected`, which feeds
  `_previously_failed`, so it is never re-proposed. A full budget is
  `no_candidate`, so the source stays eligible once room exists.
- **Nothing else in `apply_run` changes:** scoring, gate,
  `FABRICATING_SCORERS` and `collapse_history` are untouched.

**`apply_mutation`: the defence in depth.**

- **Ordering.** Leave every existing early return (dry-run / not-enabled,
  idempotent skip, any refusal another kickoff added) where it is. Compute
  `out` exactly as today, before the snapshot. The two existing `out`
  assignments move up unchanged.
- **The check,** immediately before the `try:` that opens `target_file` for
  writing:

  ```
  if existing:
      from mini_ork.learning import prompt_budget
      if prompt_budget.enabled():
          verdict = prompt_budget.check(existing, out, directive=new_prompt, mode=mode)
          if not verdict["ok"]:
              sys.stderr.write(f"apply_apply_mutation: prompt budget refused {target_file}: {verdict['reason']}; no file write\n")
              return ""
  ```

- **Unchanged:** the sidecar, version_registry and `replace` semantics, except
  for that refusal.

## Files in scope (touch ONLY these)

- `mini_ork/learning/prompt_budget.py` (new)
- `mini_ork/cli/apply.py`
- `tests/unit/test_prompt_budget.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_prompt_budget.py`: hermetic, tmp_path files, temp sqlite, no network, no probe runs)

**Pure module:**

- **`split_applied`.** It round-trips the example above. It recognises both a
  ref marker and a bare `<!-- applied -->` marker. It extracts `directive` text
  and collects the `- Observation:` lines into `text`. A file with no markers
  returns `(text, [])`.
- **`directive_similarity`.**
  - `("emit an action ledger", "Emit an action-ledger!")` gives `1.0`.
  - `("directive A", "directive B")` gives `0.333333`.
  - An empty string gives `0.0`.
- **`check` refusals:**
  - **Duplicate.** The existing file carries a block with directive
    `emit an action ledger`. `check` with `directive="emit an action ledger"`
    (append) gives `ok False` and `kind "duplicate"`. With `mode="replace"` it
    is not a duplicate.
  - **Directive cap.** A 3,000-char base plus 5 blocks, and an `after` with 6,
    gives `kind "directive_cap"`.
  - **Growth cap.**
    - Base `"x" * 1000`: an `after` of 2,201 stripped chars gives
      `kind "growth_cap"` and `allowed_chars == 2200`.
    - Base `"x" * 4000`: `allowed_chars == 6000`.
  - **Empty base.** An existing file of only a block gives `kind "empty_base"`.
- **`check` passes:**
  - An empty existing prompt gives `ok True` and `"no existing prompt"`.
  - `"ORIGINAL PROMPT\n"` plus one small block gives `ok True`. This is the
    shape `tests/unit/test_cli_apply_py.py` relies on.
- **Env overrides.** `MO_APPLY_MAX_DIRECTIVES=1` turns a 2-block `after` into
  `directive_cap`. `MO_APPLY_PROMPT_GROWTH_MAX=0.5` (invalid) falls back to 1.5.

**`apply_mutation`:**

- **Setup.** Use `monkeypatch.setenv("MINI_ORK_DB", db)` with a db from
  `mini_ork.stores.migrate.init_db(db=..., root=<repo root>)`. Monkeypatch-delete
  `MO_APPLY_PROMPT_BUDGET`, `MO_APPLY_PROMPT_GROWTH_MAX`,
  `MO_APPLY_MAX_DIRECTIVES`, `MO_APPLY_DIRECTIVE_DUP_SIM` and `MO_APPLY_MODE`.
  Set `MO_APPLY_ENABLED=1`.
- **Duplicate refusal.** `apply_mutation` appends `"emit an action ledger"`
  with `source_ref="gradient_records:gr-1"`. A second call with the same
  directive and `source_ref="gradient_records:gr-2"` returns `""`, leaves the
  file byte-identical, and stderr contains `prompt budget refused`.
- **Opt-out.** With `MO_APPLY_PROMPT_BUDGET=0` the second call appends.
- **Same source.** Same-source re-apply is still the idempotent skip: the file
  is unchanged and there is no `prompt budget` stderr line.

**`apply_run`:**

- **Setup.** Use the same init_db DB. Seed
  `INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class) VALUES ('gr-new','prompts/reviewer.md','rubric','emit an action ledger','[]',0.9,100,'reviewer')`.
  The target file is `"ORIGINAL PROMPT\n\n<!-- applied:gradient_records:gr-old -->\n- Directive: emit an action ledger\n"`.
  Set `MO_APPLY_ENABLED=1` and `MO_APPLY_SCORER=probe`. Monkeypatch
  `mini_ork.learning.probe_scorer.probe_score` to raise `AssertionError`, which
  proves scoring never ran.
- **Duplicate rejected.**
  `ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md", str(target), db=db)`
  returns 0. The last stdout line parses to `decision == "rejected"` and
  `reason == "prompt_budget"`. The single `apply_attempts` row has decision
  `rejected`, and its rationale starts with
  `"prompt budget (G02-T06 anti-bloat): duplicate directive"`. The target is
  unchanged.
- **Full budget.** With 5 distinct blocks already present and a novel
  directive, the decision is `no_candidate` and the rationale contains
  `directive cap`.
- **Other target kinds.** `target_kind="workflow_node"` skips the pre-check.
  Monkeypatch `ap._prompt_budget_verdict` with a spy that records calls, and
  `probe_score` to return `None`.
  `ap.apply_run("reviewer", "workflow_node", "prompts/reviewer.md", str(target), db=db)`
  never calls the spy, and its stdout has no `"reason":"prompt_budget"`.

## Verification commands

- `pytest -q tests/unit/test_prompt_budget.py tests/unit/test_cli_apply_py.py tests/unit/test_prompts_only_verified.py`
- `pytest -q tests/unit/test_apply_harness_targets.py tests/unit/test_collapse_history_writer.py tests/unit/test_eco6_k7_apply_hygiene.py tests/unit/test_apply_significance.py tests/unit/test_hygiene_c.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_prompt_budget.py --junitxml="${MINI_ORK_RUN_DIR}/prompt-bloat-guard-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/prompt-bloat-guard-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/learning/prompt_budget.py mini_ork/cli/apply.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
