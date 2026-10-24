# RSI Tier-0 #3: render ContextNest atom ids so the run-verdict settle can credit them

## Goal

ContextNest's outcome loop (EvoMem) is dead: 0 `cn-outcomes.json` files with
credited atoms in the last 400 runs. The cause is a format mismatch:

- **The settle side.** `mini_ork/cli/execute.py:_collect_cn_atom_ids` sweeps
  `<run_dir>/cn_prefetch/*.md` for atom ids with
  `_CN_ATOM_ID_RE = re.compile(r"cn-[a-f0-9]{8,40}")`, and `_settle_cn_outcomes`
  posts the run's verdict for those ids.
- **The render side.** The prefetch file is rendered by
  `mini_ork/cn_client.py:render_atoms_md`. Each line is
  `- [{kind} sim={sim:.2f} {ts} sess={sid[:8]}] {content}`, where `sid` is the
  hit's `session_id` (else `id`) cut to 8 characters.
- **The mismatch.** ContextNest atom ids look like
  `cn-275a1cef9eb1ba9c328a5fee8230defb` (`cn-` + 32 hex), and the renderer never
  writes them. So the settle finds nothing and credits nothing.

Render the atom id, and change nothing else.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. In `mini_ork/cn_client.py`, `render_atoms_md`, for each hit, let
   `aid = h.get("id")`.
   - When `aid` is a `str` that starts with `"cn-"`, the line becomes
     `- [{kind} sim={sim:.2f} {ts} sess={sid[:8]} id={aid}] {content}`. That is
     ` id=<full atom id>` inserted after the `sess=` token, inside the
     brackets.
   - Otherwise the line is **byte-for-byte unchanged**.
   - The header, the footer, the 280-character content cut and the `limit`
     handling are all unchanged.
2. Nothing in `mini_ork/cli/execute.py` changes: the existing regex already
   matches `cn-` + 32 hex.

## Files in scope (touch ONLY these)

- `mini_ork/cn_client.py`
- `tests/unit/test_cn_atom_id_settle.py` (new)

Do NOT modify any other file. The existing pins in
`tests/unit/test_cn_client_py.py` must pass unchanged: their fixture hits carry
no `cn-` id.

## Tests (`tests/unit/test_cn_atom_id_settle.py`: hermetic, no network)

- **The id is rendered.** `render_atoms_md` on a payload with one hit
  `{"similarity": 0.8, "metadata": {"kind": "decision", "ts": "2026-10-24T00:00:00"}, "session_id": "898364fe-a053", "id": "cn-275a1cef9eb1ba9c328a5fee8230defb", "content": "c"}`
  contains the exact line
  `- [decision sim=0.80 2026-10-24 sess=898364fe id=cn-275a1cef9eb1ba9c328a5fee8230defb] c`.
- **Other lines are unchanged.** A hit whose `id` is `"zz"`, or has no `id`,
  renders with no ` id=` token, and the line equals today's format.
- **The settle finds the id.** Write the rendered markdown to
  `<tmp run_dir>/cn_prefetch/s.md`. Then
  `mini_ork.cli.execute._collect_cn_atom_ids(str(run_dir))` returns
  `["cn-275a1cef9eb1ba9c328a5fee8230defb"]`.
- **Ids are deduplicated.** Two hits with distinct `cn-` ids and a third that
  repeats the first produce both ids once each, in document order.

## Verification commands

- `pytest -q tests/unit/test_cn_atom_id_settle.py tests/unit/test_cn_client_py.py tests/unit/test_context_assembler_py.py tests/unit/test_cn_ui_verdict_outcome.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_cn_atom_id_settle.py --junitxml="${MINI_ORK_RUN_DIR}/cn-atom-id-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/cn-atom-id-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/cn_client.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
