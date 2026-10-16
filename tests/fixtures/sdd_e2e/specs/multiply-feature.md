# Multiply two numbers

Depends on: none

Add `multiply(a, b)` to `calc.py`, next to the existing `add(a, b)`.

## Inputs

- `a`, `b`: two numbers, int or float.

## Outputs

- `multiply(a, b)` returns the product `a * b`.

## Edge cases

- A zero operand returns 0.
- Signs: `multiply(-2, 3) == -6` and `multiply(-2, -3) == 6`.
- Floats: `multiply(0.5, 4) == 2.0`.

## Acceptance criteria

- AC1: `python3 -m pytest -q tests/test_multiply.py::test_multiply_integers` reports 1 passed.
- AC2: `python3 -m pytest -q tests/test_multiply.py::test_multiply_edge_cases` reports 1 passed.
- AC3 (precondition, passes before work starts): `python3 -m pytest -q tests/test_add.py` reports 1 passed.

```bash
# AC1
python3 -m pytest -q tests/test_multiply.py::test_multiply_integers
echo "AC1 pass"

# AC2
python3 -m pytest -q tests/test_multiply.py::test_multiply_edge_cases
echo "AC2 pass"

# AC3 (precondition)
python3 -m pytest -q tests/test_add.py
echo "AC3 pass"
```

