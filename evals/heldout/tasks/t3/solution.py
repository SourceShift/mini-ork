"""FizzBuzz helper shipped with a known bug for held-out eval.

The shipped implementation returns only "Fizz" for multiples of 15.
The gold test asserts the canonical "FizzBuzz" label for n in {15, 30};
the obvious fix checks the joint divisibility first.
"""


def fizzbuzz(n: int) -> str:
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    return str(n)
