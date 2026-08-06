"""Sample module exercising many Python constructs."""

from abc import ABC, abstractmethod
import math as m
from typing import Optional


CONSTANT = 42


# A comment before the class.
class Animal(ABC):
    """Base class for animals."""

    def __init__(self, name: str, age: int = 0):
        self.name = name
        self.age = age

    @abstractmethod
    def speak(self) -> str:
        """Produce a sound."""
        pass

    def describe(self) -> str:
        return f"{self.name} is {self.age} years old"


class Dog(Animal):
    def speak(self) -> str:
        return "Woof"

    def run(self, distance: float) -> float:
        if distance > 10:
            result = distance * 2
        else:
            result = distance
        while result > 100:
            result /= 2
        for i in range(3):
            result += i
        return result


def compute(values: list[int], factor: float = 1.0) -> float:
    total = 0.0
    with open("out.txt", "w") as fh:
        for value in values:
            total += value * factor
    try:
        total = sum(values) / len(values)
    except ZeroDivisionError as err:
        raise ValueError("empty") from err
    finally:
        print("done")
    return total


async def fetch_all(items):
    results = []
    for item in items:
        results.append(await fetch(item))
    return results


def lambdas():
    square = lambda x: x * x
    return square(4)


def comprehensions():
    squares = [n * n for n in range(10) if n % 2 == 0]
    pairs = {k: v for k, v in zip("ab", "12")}
    return squares, pairs


def use_walrus(n):
    if (s := n * 2) > 10:
        print(s)
    assert n >= 0, "must be non-negative"
    return s


def use_match(value):
    match value:
        case 0:
            return "zero"
        case int() as i if i > 0:
            return "positive"
        case _:
            return "other"


def imports():
    print(m.sqrt(16))
