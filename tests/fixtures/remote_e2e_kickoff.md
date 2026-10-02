# Fix the off-by-one in total_upto

`calc_pkg.calc.total_upto(n)` should return the sum of 1..n inclusive, but it
stops at n - 1: `total_upto(3)` returns 3 instead of 6, and
`tests/test_calc.py` fails.

## Files in scope

- `calc_pkg/calc.py`

Do not change any other file, including the test.

## Definition of done

- `tests/test_calc.py` passes.
- The only change is in `calc_pkg/calc.py`.

## Verification command

```bash
python3 -m pytest -q tests/test_calc.py
```
