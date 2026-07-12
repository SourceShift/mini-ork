from solution import fizzbuzz


def test_multiples_of_three():
    assert fizzbuzz(3) == "Fizz"
    assert fizzbuzz(9) == "Fizz"


def test_multiples_of_five():
    assert fizzbuzz(5) == "Buzz"
    assert fizzbuzz(25) == "Buzz"


def test_multiples_of_fifteen_are_fizzbuzz():
    assert fizzbuzz(15) == "FizzBuzz"
    assert fizzbuzz(30) == "FizzBuzz"


def test_other_numbers_pass_through():
    assert fizzbuzz(1) == "1"
    assert fizzbuzz(7) == "7"
