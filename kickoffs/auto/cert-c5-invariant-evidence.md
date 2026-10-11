# C5 — certify: put each invariant's source and failure detail in the certificate

## Goal

A certificate's `evidence.invariants` lists `{"mr", "on_patch", "holds"}` per invariant
but not the test code. When certify REFUTES a change ("only 25% of 4 invariants hold"),
nobody can audit whether the invariants were right or the patch was wrong — measured on a
real repository (pyparsing, upstream fix for issue #492 came back REFUTED) where that
question could not be answered from the certificate. A rejection must carry the evidence
that produced it.

## Files in scope

- `mini_ork/certify/oracle.py` — the per-invariant record in `judge`.
- `tests/unit/test_certify_oracle.py` — extend.

No other file changes. The certificate builder (`mini_ork/certify/certificate.py`) and the
CLI already pass `detail["invariants"]` through unchanged, so the new keys flow into the
certificate on their own — verify that with a CLI-level assertion only if an existing CLI
test already inspects `evidence.invariants` (do not add files).

## Mechanism (exact spec)

In `judge`, where each kept invariant is evaluated:

```python
rec = {"mr": name, "on_patch": res.status, "holds": hit}
```

becomes

```python
rec = {"mr": name, "on_patch": res.status, "holds": hit, "src": src}
if not hit:
    rec["detail"] = _tail(<the runner result's failure text>, 600)
```

- `src` is the invariant's full test source exactly as run.
- `detail` only on a failed invariant: the last 600 characters of the most specific failure
  text the runner result exposes (`res.exc` if it is a non-empty string, else `""`).
  Add a tiny private helper `_tail(text, n)` returning `text[-n:]` for strings, `""` otherwise.
- Records built with `gold` (measurement only) keep their extra keys unchanged.
- Nothing else in the verdict logic changes.

## Tests (extend `tests/unit/test_certify_oracle.py`, reuse its fake runner/dispatch)

- A judged run with one invariant that passes and one that fails on the patch: the kept
  records carry `src` equal to the candidate sources the fake dispatch produced, the
  passing record has no `detail`, the failing record's `detail` ends with the fake
  runner's failure text and is at most 600 characters.
- A very long failure text (2000 chars) is truncated to its last 600 characters.
- Every existing test in the file passes unchanged.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_certify_oracle.py tests/unit/test_certify_cli.py tests/unit/test_certify_context.py` passes.
- `ruff check mini_ork/certify/oracle.py tests/unit/test_certify_oracle.py` clean.
- Diff touches only the two files in scope.
