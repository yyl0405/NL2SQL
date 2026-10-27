"""Deterministic, low-risk SQL repair after database errors.

This is deliberately not an LLM retry. Each rule is reversible, only touches
the projection list where possible, and never introduces new tables/columns.
If no safe rule matches, the caller is expected to ask the model to regenerate
or hand the query to a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_TSQL_ERROR = re.compile(r"\((\d{3,5})\)")
_TSQL_ERROR_KINDS = {
    207: ("invalid_column", "列名不存在"),
    8120: ("not_in_group_by", "选择列未参与聚合或 GROUP BY"),
    102: ("syntax", "语法错误"),
}


@dataclass
class RepairResult:
    sql: str
    applied: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error_code: int | None = None
    error_kind: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "changed": self.changed,
            "applied": self.applied,
            "notes": self.notes,
            "error_code": self.error_code,
            "error_kind": self.error_kind,
        }


class SQLRepairer:
    def repair(self, sql: str, error: str = "") -> RepairResult:
        original = (sql or "").strip()
        result = RepairResult(original)
        if not original:
            result.notes.append("SQL 为空，无法修复")
            return result

        error_info = classify_tsql_error(error)
        if error_info:
            code, kind = error_info
            result.error_code = code
            result.error_kind = kind
            result.notes.append(
                f"SQL Server 错误 {code}（{_TSQL_ERROR_KINDS[code][1]}），按 {kind} 选择修复规则"
            )

        cleaned = self._clean_syntax(original)
        if cleaned != result.sql:
            result.applied.append("clean_syntax")
            result.sql = cleaned
            if result.error_kind == "syntax":
                result.notes.append("已按 102 语法错误清理尾逗号/分号")

        if error:
            if error_info is None or result.error_kind == "invalid_column":
                repaired = self._remove_invalid_projection(result.sql, error)
                if repaired != result.sql:
                    result.applied.append("remove_invalid_projection")
                    result.sql = repaired
                    result.notes.append("已从 SELECT 中移除报错字段；WHERE/JOIN/GROUP BY 保持不变")

            if error_info is None or result.error_kind == "not_in_group_by":
                repaired = self._wrap_group_by_expression(result.sql, error, result.error_code)
                if repaired != result.sql:
                    result.applied.append("wrap_group_by_expression")
                    result.sql = repaired
                    result.notes.append("已用 SUM() 包裹 GROUP BY 查询中的二元算术表达式")

        if not result.applied:
            result.notes.append("没有命中确定性修复规则")
        return result

    @staticmethod
    def _clean_syntax(sql: str) -> str:
        text = sql.strip()
        text = re.sub(r";\s*$", "", text)
        text = re.sub(
            r",\s*(from|where|group\s+by|order\s+by|having|limit)\b",
            r" \1",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r",\s*\)", ")", text)
        return text.strip()

    def _remove_invalid_projection(self, sql: str, error: str) -> str:
        column = self._extract_invalid_column(error)
        if not column:
            return sql
        select_match = re.search(
            r"\bselect\b\s+(?P<body>.+?)\s+\bfrom\b", sql, re.IGNORECASE | re.DOTALL
        )
        if not select_match:
            return sql
        items = _split_select_items(select_match.group("body"))
        if len(items) <= 1:
            return sql
        column_pattern = re.compile(
            rf"(?<![A-Za-z0-9_])(?:[A-Za-z_]\w*\s*\.\s*)?\[?{re.escape(column)}\]?"
            rf"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        kept = [item for item in items if not column_pattern.search(item)]
        removed = [item for item in items if column_pattern.search(item)]
        if not removed or not kept:
            return sql
        rebuilt = ", ".join(kept)
        return (
            sql[: select_match.start("body")]
            + rebuilt
            + sql[select_match.end("body") :]
        )

    @staticmethod
    def _extract_invalid_column(error: str) -> str | None:
        patterns = (
            r"no such column:\s*([A-Za-z0-9_.\[\]\"`]+)",
            r"列名\s+'([^']+)'\s+无效",
            r"Invalid\s+column\s+name\s+'([^']+)'",
        )
        for pattern in patterns:
            match = re.search(pattern, error or "", re.IGNORECASE)
            if match:
                raw = match.group(1).strip()
                raw = raw.split(".")[-1]
                return raw.strip('[]"`')
        return None

    @staticmethod
    def _wrap_group_by_expression(sql: str, error: str, code: int | None = None) -> str:
        text = error or ""
        if "GROUP BY" not in sql.upper():
            return sql
        if code != 8120 and "8120" not in text and "选择列表中的列" not in text:
            return sql
        select_match = re.search(
            r"\bselect\b\s+(?P<body>.+?)\s+\bfrom\b", sql, re.IGNORECASE | re.DOTALL
        )
        if not select_match:
            return sql
        items = _split_select_items(select_match.group("body"))
        if not items:
            return sql
        ident = r"(?:\[[^\]]+\]|[A-Za-z_]\w*)"
        column_ref = rf"{ident}\s*\.\s*{ident}"
        binary_expr = re.compile(
            rf"(?P<expr>{column_ref}\s*(?:\+|-|\*|/)\s*{column_ref})", re.IGNORECASE
        )
        aggregate = re.compile(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", re.IGNORECASE)
        changed = False
        new_items: list[str] = []
        for item in items:
            if aggregate.search(item):
                new_items.append(item)
                continue
            match = binary_expr.search(item)
            if not match:
                new_items.append(item)
                continue
            expr = match.group("expr").strip()
            new_items.append(item[: match.start("expr")] + f"SUM({expr})" + item[match.end("expr") :])
            changed = True
        if not changed:
            return sql
        return sql[: select_match.start("body")] + ", ".join(new_items) + sql[select_match.end("body") :]


def classify_tsql_error(error: str) -> tuple[int, str] | None:
    """Return ``(code, kind)`` for a known SQL Server error, else ``None``.

    pyodbc appends the server error number in parentheses, e.g.
    ``... 列名 'x' 无效。 (207) (SQLExecDirectW)``. Only codes we have a
    deterministic rule for are returned, so unrelated numbers in a message
    cannot steer a repair.
    """
    for match in _TSQL_ERROR.finditer(error or ""):
        code = int(match.group(1))
        if code in _TSQL_ERROR_KINDS:
            return code, _TSQL_ERROR_KINDS[code][0]
    return None


def _split_select_items(select_clause: str) -> list[str]:
    items: list[str] = []
    buffer: list[str] = []
    depth = 0
    for ch in select_clause:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            item = "".join(buffer).strip()
            if item:
                items.append(item)
            buffer = []
        else:
            buffer.append(ch)
    tail = "".join(buffer).strip()
    if tail:
        items.append(tail)
    return items
