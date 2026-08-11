"""
A minimal Result type (railway-oriented programming) used to compose the
ingestion pipeline's stages declaratively via `functools.reduce`, instead
of an imperative try/except-per-stage or an if/elif ladder.

    result = reduce(bind, stages, Ok(initial_context))
    match result:
        case Ok(value): ...
        case Err(reason, detail): ...
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")
U = TypeVar("U")


@dataclass(frozen=True, slots=True)
class Ok:
    value: object


@dataclass(frozen=True, slots=True)
class Err:
    reason: str
    detail: str | None = None


Result = Ok | Err


def bind(result: Result, step: Callable[[object], Result]) -> Result:
    """Apply `step` only if `result` is Ok; short-circuit on Err."""
    match result:
        case Ok(value):
            return step(value)
        case Err():
            return result


def map_ok(result: Result, fn: Callable[[object], object]) -> Result:
    """Lift a plain value-transforming function into the Result world."""
    return bind(result, lambda value: Ok(fn(value)))


def run_pipeline(initial: object, stages: tuple[Callable[[object], Result], ...]) -> Result:
    """Fold every stage over the initial value, stopping at the first Err."""
    from functools import reduce

    return reduce(bind, stages, Ok(initial))
