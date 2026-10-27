"""Read-only scan for near-duplicate tables in a schema catalog.

Every shipped NL2SQL product hides this decision instead of exposing it: Cortex
Analyst answers from a modeled semantic set, QuickSight Q from a topic, Genie
from a space. A raw 576-table ERP catalog does none of that, so the generator has
to guess between ``customer`` and a legacy copy named ``customer_旧表`` - and a
wrong-but-plausible pick costs steps, or worse, returns a confident wrong number.

This module only *finds candidates*. Disabling a table changes what the agent can
see, so it stays a human decision recorded in the overlay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .catalog import Catalog

# Containment, not Jaccard: a legacy copy usually carries a few extra columns, and
# 40-of-42 shared columns is the situation we are looking for. Containment alone is
# not enough though - every ERP table shares ``id`` and ``name`` - so a pair also
# has to overlap on a real number of columns before it counts as a twin.
DEFAULT_MIN_OVERLAP = 0.85
DEFAULT_MIN_COLUMNS = 4
# Display is clustered and capped, so the pair list itself does not need one.
DEFAULT_LIMIT = 0


@dataclass(frozen=True)
class DuplicatePair:
    left: str
    right: str
    overlap: float
    left_columns: int
    right_columns: int
    shared: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "overlap": round(self.overlap, 4),
            "left_columns": self.left_columns,
            "right_columns": self.right_columns,
            "shared": list(self.shared),
        }


@dataclass(frozen=True)
class DuplicateCluster:
    """Tables that are twins of each other, transitively.

    A reviewer decides per cluster: "these are copies, keep one" or "these are
    per-form custom tables, keep all". Reporting pairs instead of clusters makes
    a 9-table template family look like 36 problems and hides the real pair.
    """

    members: tuple[str, ...]
    pairs: int
    min_overlap: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "members": list(self.members),
            "tables": len(self.members),
            "pairs": self.pairs,
            "min_overlap": round(self.min_overlap, 4),
        }


def cluster_duplicates(pairs: Sequence[DuplicatePair]) -> list[DuplicateCluster]:
    if not pairs:
        return []
    neighbours: dict[str, set[str]] = {}
    for pair in pairs:
        neighbours.setdefault(pair.left, set()).add(pair.right)
        neighbours.setdefault(pair.right, set()).add(pair.left)
    claimed: set[str] = set()
    clusters: list[DuplicateCluster] = []
    for start in sorted(neighbours):
        if start in claimed:
            continue
        members = {start}
        frontier = [start]
        while frontier:
            current = frontier.pop()
            for other in neighbours.get(current, ()):  # breadth-first component
                if other not in members:
                    members.add(other)
                    frontier.append(other)
        claimed |= members
        inside = [pair for pair in pairs if pair.left in members and pair.right in members]
        clusters.append(
            DuplicateCluster(
                members=tuple(sorted(members)),
                pairs=len(inside),
                min_overlap=min(pair.overlap for pair in inside),
            )
        )
    # Smallest first: a two-table cluster is the classic "someone copied the table",
    # a wide one is usually a form/template family that deserves a second look.
    clusters.sort(key=lambda item: (len(item.members), -item.min_overlap, item.members[0]))
    return clusters


def _column_names(catalog: Catalog) -> list[tuple[str, frozenset[str]]]:
    return [
        (table.name, frozenset(column.name.lower() for column in table.columns if not column.disabled))
        for table in catalog.enabled_tables()
    ]


def find_duplicates(
    catalog: Catalog,
    *,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
    min_columns: int = DEFAULT_MIN_COLUMNS,
    limit: int = DEFAULT_LIMIT,
    exclude: Sequence[str] = (),
) -> list[DuplicatePair]:
    """Pairs of enabled tables whose column sets contain each other.

    ``min_overlap`` is the share of the *smaller* table that the larger one
    reproduces, so a wide history table and its narrow view still surface.
    ``min_columns`` is what keeps a shared ``id``/``name`` pair out of the list:
    containment is meaningless until the overlap is big enough to be a twin.
    """
    blocked = {str(name).strip().lower() for name in exclude if str(name).strip()}
    tables = [
        (name, columns)
        for name, columns in _column_names(catalog)
        if name.lower() not in blocked
    ]
    pairs: list[DuplicatePair] = []
    for index, (left, left_columns) in enumerate(tables):
        if not left_columns:
            continue
        for right, right_columns in tables[index + 1 :]:
            if not right_columns:
                continue
            shared = left_columns & right_columns
            smaller = min(len(left_columns), len(right_columns))
            if len(shared) < min_columns or smaller < min_columns:
                continue
            overlap = len(shared) / smaller
            if overlap < min_overlap:
                continue
            pairs.append(
                DuplicatePair(
                    left=left,
                    right=right,
                    overlap=overlap,
                    left_columns=len(left_columns),
                    right_columns=len(right_columns),
                    shared=tuple(sorted(shared)),
                )
            )
    pairs.sort(key=lambda item: (-item.overlap, item.left, item.right))
    return pairs[:limit] if limit > 0 else pairs
