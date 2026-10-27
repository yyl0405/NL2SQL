"""Result-set comparison shared by the dual-channel vote and the evaluator.

Both the Agent (second opinion must be checked against the first result) and
the evaluator (candidate vs. gold SQL) need exactly the same notion of "these
two result sets are the same answer". Keeping one copy is the point: a vote and
a score that disagree about equality would disagree about nothing else either.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def normalize_row(row: list[Any]) -> tuple[Any, ...]:
    normalized: list[Any] = []
    for value in row:
        if isinstance(value, bool):
            normalized.append(value)
        elif isinstance(value, (int, float, Decimal)):
            # Round first: SUM(qty * price * (1 - discount)) differs in the 5th
            # decimal between two equivalent SQL shapes and is still the same
            # business answer. SQL Server DECIMAL comes back as Decimal, not
            # float, so both types have to take this branch.
            normalized.append(round(float(value), 4))
        elif isinstance(value, str):
            normalized.append(value.strip())
        else:
            normalized.append(value)
    return tuple(normalized)


def rows_equal(
    left: list[list[Any]],
    right: list[list[Any]],
    *,
    order_sensitive: bool = False,
) -> bool:
    normalized_left = [normalize_row(row) for row in left]
    normalized_right = [normalize_row(row) for row in right]
    if len(normalized_left) != len(normalized_right):
        return False
    if order_sensitive:
        return normalized_left == normalized_right
    return sorted(normalized_left, key=repr) == sorted(normalized_right, key=repr)


def describe_diff(
    left: list[list[Any]],
    right: list[list[Any]],
    *,
    order_sensitive: bool = False,
) -> str:
    """A short, human-readable reason two result sets are not the same answer.

    Used in the failure queue and the report, where "the channels disagreed"
    is not actionable but "3 rows vs 5 rows" is.
    """
    if len(left) != len(right):
        return f"行数不同: {len(left)} vs {len(right)}"
    if order_sensitive and left != right:
        return "行顺序不同"
    for index, (row_left, row_right) in enumerate(zip(left, right)):
        if normalize_row(row_left) != normalize_row(row_right):
            return f"第 {index + 1} 行不同: {row_left} vs {row_right}"
    return "结果集不同"
