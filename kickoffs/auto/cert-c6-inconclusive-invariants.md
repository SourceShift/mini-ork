# C6 — certify: an invariant failure counts against the patch only if the patch left the failure unchanged

Depends on C5 (each kept invariant record carries `src` and `detail`), merged.

## Goal

Measured on a real repository (pyparsing, upstream fix for issue #492): the CORRECT fix
was REFUTED because 3 of 4 generated invariants were themselves wrong — they assumed
pyparsing keeps tab characters in the input string (it expands them by default), so they
fail on any correct fix. Today every failed invariant counts as evidence that the patch
"does not generalise".

What distinguishes the two cases:

- a special-cased **cheat** leaves the general bug in place, so on an invariant's input
  the patched code behaves **exactly as the buggy base did** (same assertion failure,
  e.g. `assert 4 == 3.5` on both);
- an **invalid invariant** on a correct fix fails **differently** from the base: the
  patch changed the behaviour on that input, just not into what the invariant guessed
  (pyparsing: base returns a wrong match, patch returns `[]`).

Only the first is evidence against the patch. The second is "inconclusive".

## Files in scope

- `mini_ork/certify/oracle.py` — the invariant loop and verdict at the end of `judge`.
- `tests/unit/test_certify_oracle.py` — extend.

No other file changes. `mini_ork/certify/invariants.py::score` is reused unchanged.

## Mechanism (exact spec)

1. New pure helper in `oracle.py`: `_failure_signature(output: str) -> str | None` —
   the first line of `output` that starts with `E ` (pytest's assertion/exception line),
   with leading `E` and whitespace stripped and internal runs of whitespace collapsed to
   one space; `None` when there is no such line.
2. In `judge`, for each kept invariant whose run on the patch did **not** pass:
   - run the same source on the base: `base = runner.run_test(src)` (no patch);
   - `sig_patch = _failure_signature(res.output)`, `sig_base = _failure_signature(base.output)`;
   - classify:
     - `"broken"` when `base.status == "failed"` and `res.status == "failed"` and
       `sig_patch is not None` and `sig_patch == sig_base`
       (the patch did not change the failing behaviour);
     - `"broken"` also when either signature is `None` (cannot tell → keep today's
       conservative behaviour);
     - otherwise `"inconclusive"`.
   - record `rec["outcome"] = "broken" | "inconclusive"` and `rec["base_detail"]` =
     the same 600-char tail format C5 uses for `detail`, built from `base`.
   - passing invariants get `rec["outcome"] = "holds"` (no base re-run).
3. Verdict — asymmetric, so precision cannot move:
   `holds`, `broken`, `inconclusive` = counts by outcome; `n` = all kept; `n_eff = holds + broken`.
   - **PROVEN** exactly as today: `holds / n >= 2/3` (inconclusive invariants count as
     not holding). Use `mr.score(holds / n, n)` and keep its PROVEN result unchanged.
   - Otherwise, **REFUTED** only when `n_eff >= 2` and `holds / n_eff < 0.5` (the
     conclusive evidence says the bug survives); reason as `mr.score` words it for
     `rate = holds / n_eff`, plus `f" ({inconclusive} inconclusive invariant(s) excluded)"`
     when `inconclusive > 0`.
   - Otherwise **UNVERIFIED**. When `inconclusive > 0` the reason is
     `f"{inconclusive} of {n} invariants failed in a way the buggy code did not — the patch changed the behaviour they test, so they cannot judge it; {holds} of {n} hold"`;
     else keep `mr.score`'s UNVERIFIED reason.
   - `mr_pass_rate` stays `holds / n`; `mr_n` stays `n`.
   - Add `"n_effective": n_eff, "inconclusive": inconclusive` to `detail`.
4. Gold measurement (`gold is not None`) is unchanged and still measurement-only.
5. Cost note in a comment: one extra sandbox test run per failed invariant, no extra
   model calls.

Safety argument (put it in the docstring): the PROVEN rule is byte-identical to today's,
so no change can create a PROVEN that today's rule would not. The change can only turn a
REFUTED into an UNVERIFIED — a false reject becomes an abstention.

## Tests (extend `tests/unit/test_certify_oracle.py`, reuse FakeRunner / make_dispatch)

Use `ExecOutcome(status="failed", output="...\nE       assert 4 == 3.5\n...")` style
outputs. The fake runner must now also serve the extra base re-runs (they come right
after each failed patch run, in order).

- cheat shape: 4 invariants all fail on the patch with the same `E` line as their base
  re-run → `REFUTED`, every record `outcome == "broken"`.
- invalid-invariant shape: 1 holds, 3 fail on the patch with `E assert [] == [['x']]` while
  the base re-run fails with `E assert [['y']] == [['x']]` → `UNVERIFIED`, reason mentions
  `cannot judge`, `detail["inconclusive"] == 3`.
- mixed: 3 hold, 1 inconclusive → `PROVEN` (3/4 ≥ 2/3, same as today).
- precision guard: 2 hold, 1 broken, 1 inconclusive → NOT PROVEN (2/4 < 2/3); it is
  `UNVERIFIED` (n_eff=3, 2/3 ≥ 0.5 so not REFUTED).
- refute with exclusion: 0 hold, 3 broken, 1 inconclusive → `REFUTED`, reason ends with
  `(1 inconclusive invariant(s) excluded)`.
- property test: for every (holds, broken, inconclusive) with total 1..6, the new verdict
  is PROVEN iff today's rule (`mr.score(holds/n, n)`) is PROVEN.
- missing signature (no `E ` line in either output) → counts as broken (conservative).
- `_failure_signature`: picks the first `E ` line, collapses whitespace, returns None when absent.
- every existing test in the file still passes (update only fixtures whose runner
  sequences now need the extra base outcomes, and say so in a comment).

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_certify_oracle.py tests/unit/test_certify_cli.py tests/unit/test_certify_context.py` passes.
- `ruff check mini_ork/certify/oracle.py tests/unit/test_certify_oracle.py` clean.
- Diff touches only the two files in scope.
