"""Static JOIN fan-out analysis.

Fan-out is the failure mode that turns a correct-looking query into a wrong
number: joining a parent to its children multiplies the parent's rows, so
``SUM(sales_order.amount)`` silently reports twice the money when an order has
two lines. The SQL is legal, so the regex guard approves it and the database
answers confidently. This module flags it *before* execution — as a warning,
never an error: joining a 1:N key is often exactly what the question asked for,
and a false alarm that blocks execution is worse than a visible caveat.

Three signals, all derived from the catalog rather than guessed from the text:

* a JOIN with **no equality predicate at all** (``CROSS JOIN``, ``JOIN`` without
  ``ON``, or a comma-separated ``FROM``) — the result is a cartesian product;
* a **declared 1:N relationship** that is actually used in this query;
* whether an **aggregate references the "one" side and nothing else** — the
  combination that produces the wrong number. Aggregating the child side
  (``SUM(order_item.qty)``) is correct grain, and mixing both sides inside one
  aggregate (``SUM(inventory.on_hand_qty * material.standard_cost)``) is the
  normal way to roll a child up to its parent, so both stay quiet. Requiring the
  aggregate to be *purely* parent-side is what keeps the warning honest: the
  mixed shape is common enough that flagging it produced noise on 2 of the 37
  gold-SQL cases in the demo corpus. ``MAX``/``MIN`` are excluded too — they
  return the same value however many times a row is duplicated, so the roll-up
  idiom ``MAX(parent.col)`` beside a child-side ``SUM`` cannot be a wrong number,
  and flagging it cost another false positive in the 113-case corpus.

Primary keys are used only to read the relationship direction, not to warn: a
child-grain query that joins on a non-unique parent key is legitimate, so
warning on that alone would fire on most of the demo corpus.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from .catalog import Catalog
    from .guard import GuardIssue

_JOIN_RE = re.compile(
    r"\b(?P<kind>inner|left|right|full|cross)?\s*\bjoin\b\s+(?P<table>[\w\.\[\]]+)"
    r"(?:\s+(?:as\s+)?(?P<alias>[a-zA-Z_]\w*))?"
    r"(?:\s+on\s+(?P<on>.+?))?"
    # The terminator is a keyword *or* end of statement: requiring whitespace
    # before `$` dropped every trailing "… JOIN x", the exact cartesian shape
    # this module exists to catch.
    r"(?=\s+(?:inner|left|right|full|cross|where|group|order|union|having|limit|offset)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_EQ_RE = re.compile(
    r"(?:(?P<left>[a-zA-Z_]\w*)\.)?(?P<lc>\w+)\s*=\s*(?:(?P<right>[a-zA-Z_]\w*)\.)?(?P<rc>\w+)"
)
_COMMA_FROM_RE = re.compile(
    r"\bfrom\s+(?P<tables>[a-z_][\w\.\[\]]*(?:\s+(?:as\s+)?[a-z_][\w]*)?"
    r"(?:\s*,\s*[a-z_][\w\.\[\]]*(?:\s+(?:as\s+)?[a-z_][\w]*)?)+)",
    re.IGNORECASE,
)
_ONE_RE = re.compile(r"\b(?P<func>sum|avg|min|max|count)\s*\(", re.IGNORECASE)
_COLUMN_RE = re.compile(r"(?:(?P<alias>[a-zA-Z_]\w*)\.)?(?P<column>[a-zA-Z_]\w*)")
_REL_TYPES = {"1:N": "one", "ONE:MANY": "one", "N:1": "many", "MANY:ONE": "many"}
_FANOUT_TOLERANT_FUNCS = {"max", "min"}


def _resolve_table(token: str, aliases: dict[str, str], catalog: "Catalog") -> str:
    cleaned = (token or "").strip().strip('"[]`').lower()
    if not cleaned:
        return ""
    mapped = aliases.get(cleaned)
    if mapped:
        return mapped
    table = catalog.table(cleaned)
    return table.name.lower() if table is not None else ""


def _aggregate_groups(masked_sql: str, aliases: dict[str, str], catalog: "Catalog") -> list[set[str]]:
    """One set of referenced tables per aggregate call; unqualified columns are skipped."""
    groups: list[set[str]] = []
    for match in _ONE_RE.finditer(masked_sql):
        # MAX/MIN over duplicated rows return the same value, so they cannot
        # report "twice the money"; warning on them only added noise on the
        # common roll-up shape ``MAX(parent.col)`` next to a child-side SUM.
        if (match.group("func") or "").lower() in _FANOUT_TOLERANT_FUNCS:
            continue
        span = _paren_group(masked_sql, match.end("func"))
        if span is None:
            continue
        argument, _ = span
        if argument.strip() == "*":
            continue
        if re.match(r"^\s*distinct\b", argument, re.IGNORECASE):
            continue
        tables = set()
        for column in _COLUMN_RE.finditer(argument):
            alias = (column.group("alias") or "").lower()
            if not alias:
                continue
            resolved = _resolve_table(alias, aliases, catalog)
            if resolved:
                tables.add(resolved)
        if tables:
            groups.append(tables)
    return groups


def _paren_group(text: str, start: int) -> tuple[str, int] | None:
    """The balanced ``(...)`` body that begins at ``start`` (position after `(`)."""
    if start >= len(text) or text[start] != "(":
        return None
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index
    return None


def analyze_joins(
    catalog: "Catalog",
    masked_sql: str,
    aliases: dict[str, str],
    tables_in_scope: Sequence[str],
) -> list["GuardIssue"]:
    from .guard import GuardIssue

    issues: list[GuardIssue] = []
    scope = {name.lower() for name in tables_in_scope}

    def add(message: str) -> None:
        if any(issue.message == message for issue in issues):
            return
        issues.append(GuardIssue("warning", "join_fanout", message))

    joins = list(_JOIN_RE.finditer(masked_sql))
    for match in joins:
        joined_name = _resolve_table(match.group("table"), aliases, catalog)
        if not joined_name or joined_name not in scope:
            continue
        table = catalog.table(joined_name)
        if table is None:
            continue
        if (match.group("kind") or "").lower() == "cross" or not (match.group("on") or "").strip():
            add(
                f"{table.name} 以 CROSS JOIN 或无 ON 条件参与连接，结果集是两侧行数的笛卡尔积；"
                "请补上等值连接条件，否则任何 SUM/COUNT 都会虚高"
            )

    if _COMMA_FROM_RE.search(masked_sql) and not joins:
        add(
            "FROM 逗号并列多张表且没有任何 JOIN，等价于笛卡尔积；"
            "请改成显式 JOIN 并给出连接条件（带 WHERE 等值也要写成 JOIN，便于复核）"
        )

    aggregated = _aggregate_groups(masked_sql, aliases, catalog)
    if not aggregated:
        return issues

    for rel in catalog.relationships:
        rel_type = str(rel.get("type", "")).upper()
        direction = _REL_TYPES.get(rel_type)
        if direction is None:
            continue
        left_table, _, left_column = str(rel.get("left", "")).partition(".")
        right_table, _, right_column = str(rel.get("right", "")).partition(".")
        one_name, many_name = (
            (left_table, right_table) if direction == "one" else (right_table, left_table)
        )
        one_column, many_column = (
            (left_column, right_column) if direction == "one" else (right_column, left_column)
        )
        if not {one_name.lower(), many_name.lower()} <= scope:
            continue
        if not any(group == {one_name.lower()} for group in aggregated):
            continue
        if not _joined_on(masked_sql, aliases, catalog, one_name, one_column, many_name, many_column):
            continue
        one_table = catalog.table(one_name)
        add(
            f"{one_table.name} 与 {catalog.table(many_name).name} 是 "
            f"1:N 关系（{one_name}.{one_column} = {many_name}.{many_column}），"
            f"本次聚合的列只来自「一」侧 {one_name}：SUM/AVG/COUNT 会按「多」侧行数重复计算 "
            f"{one_name} 的值。请只对 N 侧聚合，或先把子表聚合到父表粒度"
            "（子查询/CTE），或确认该列在每行唯一"
        )

    return issues


def _both_present(masked_sql: str, names: Sequence[str], catalog: "Catalog") -> bool:
    for name in names:
        table = catalog.table(name) if name else None
        if table is None:
            return False
        if not re.search(rf"(?<![\w]){re.escape(table.name)}(?![\w])", masked_sql, re.IGNORECASE):
            return False
    return True


def _joined_on(
    masked_sql: str,
    aliases: dict[str, str],
    catalog: "Catalog",
    one_table: str,
    one_column: str,
    many_table: str,
    many_column: str,
) -> bool:
    expected = {
        (one_table.lower(), one_column.lower(), many_table.lower(), many_column.lower()),
        (many_table.lower(), many_column.lower(), one_table.lower(), one_column.lower()),
    }
    if not _both_present(masked_sql, (one_table, many_table), catalog):
        return False
    for match in _JOIN_RE.finditer(masked_sql):
        for eq in _EQ_RE.finditer(match.group("on") or ""):
            lhs = (_resolve_table(eq.group("left") or "", aliases, catalog), (eq.group("lc") or "").lower())
            rhs = (_resolve_table(eq.group("right") or "", aliases, catalog), (eq.group("rc") or "").lower())
            if (lhs[0], lhs[1], rhs[0], rhs[1]) in expected:
                return True
    return False
