"""Read-only SQL guard: permissions, SELECT *, required filters, columns and safety."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog

_BLOCKED_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|attach|detach|pragma|vacuum|"
    r"grant|revoke|exec|execute|merge|load_extension)\b",
    re.IGNORECASE,
)
_LEADING_KEYWORD = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FROM_OR_JOIN = re.compile(
    r"\b(?:from|join)\s+(?!\()\s*(?:[A-Za-z_]\w*\s*\.\s*)?([A-Za-z_]\w*)",
    re.IGNORECASE,
)
_FROM_CLAUSE = re.compile(
    r"\bfrom\b(.*?)(?=\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|"
    r"\bunion\b|\bwindow\b|\bqualify\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_CTE = re.compile(
    r"(?:\bwith|,)\s+(?:recursive\s+)?([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s+as\s*\(",
    re.IGNORECASE,
)
_AGGREGATE_STAR = re.compile(
    r"(?is)\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(?:DISTINCT\s+)?\*\s*\)"
)
_SELECT_BARE_STAR = re.compile(
    r"(?is)\bselect\b\s+(?:top\s*(?:\(\s*\d+\s*\)|\d+)\s*(?:percent\s+)?)?"
    r"(?:distinct\s+)?\*"
)
_QUALIFIED_STAR = re.compile(
    r"(?is)(?:\bselect\b\s+(?:top\s*(?:\(\s*\d+\s*\)|\d+)\s*(?:percent\s+)?)?"
    r"(?:distinct\s+)?|,)\s*([A-Za-z_]\w*)\s*\.\s*\*"
)

_KEYWORDS = {
    "select", "from", "where", "group", "by", "order", "having", "limit", "offset",
    "join", "left", "right", "inner", "outer", "full", "cross", "on", "as", "and",
    "or", "not", "in", "is", "null", "like", "between", "case", "when", "then",
    "else", "end", "union", "all", "distinct", "asc", "desc", "with", "recursive",
    "using", "window", "partition", "over", "qualify", "lateral", "exists", "any",
    "some", "into", "values", "set", "top", "percent",
}


@dataclass
class GuardIssue:
    level: str
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass
class GuardResult:
    valid: bool
    sql: str
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    issues: list[GuardIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[GuardIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[GuardIssue]:
        return [i for i in self.issues if i.level == "warning"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "sql": self.sql,
            "tables": self.tables,
            "columns": self.columns,
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings],
        }


class SQLGuard:
    """Validates generated SQL before it reaches a read-only connection.

    The guard is intentionally conservative. It is not a full SQL parser; it
    blocks the failure modes that caused production incidents: writing data,
    querying disabled tables, SELECT *, disabled/sensitive fields, missing
    dimension filters and mistyped qualified columns.
    """

    def __init__(self, catalog: Catalog, allow_sensitive: bool = False) -> None:
        self.catalog = catalog
        self.allow_sensitive = allow_sensitive

    def check(self, sql: str) -> GuardResult:
        cleaned = self._clean_input(sql)
        issues: list[GuardIssue] = []
        used_tables: list[str] = []
        used_columns: list[str] = []

        if not cleaned:
            return GuardResult(False, cleaned, issues=[GuardIssue("error", "empty_sql", "SQL 不能为空")])
        if len(cleaned) > 20000:
            return GuardResult(False, cleaned, issues=[GuardIssue("error", "sql_too_long", "SQL 超过 20000 字符")])

        masked = mask_sql(cleaned)
        masked_no_semicolon = masked.rstrip().rstrip(";").rstrip()

        if not _LEADING_KEYWORD.match(masked):
            issues.append(GuardIssue("error", "not_select", "只允许 SELECT 或 WITH 查询"))
        if ";" in masked_no_semicolon:
            issues.append(GuardIssue("error", "multi_statement", "只允许单条语句，禁止分号拼接"))
        blocked = sorted({m.group(1).lower() for m in _BLOCKED_KEYWORDS.finditer(masked)})
        if blocked:
            issues.append(
                GuardIssue("error", "write_keyword", f"检测到只读查询不允许的关键字: {', '.join(blocked)}")
            )

        ctes = _extract_ctes(masked)
        table_tokens = _extract_tables(masked, ctes)
        table_objects = []
        for token in sorted(table_tokens):
            table = self.catalog.table(token)
            if table is None:
                issues.append(
                    GuardIssue("error", "unknown_table", f"表 '{token}' 不在 schema 目录中")
                )
                continue
            if table.disabled:
                issues.append(
                    GuardIssue("error", "disabled_table", f"表 '{table.name}' 已被禁用，不能查询")
                )
                continue
            table_objects.append(table)
            used_tables.append(table.name)
            for required in table.required_filters:
                column = str(required.get("column", ""))
                value = str(required.get("value", ""))
                pattern = rf"\b{re.escape(column)}\s*=\s*['\"]?{re.escape(value)}['\"]?"
                if not re.search(pattern, cleaned, re.IGNORECASE):
                    reason = str(required.get("reason", ""))
                    issues.append(
                        GuardIssue(
                            "error",
                            "required_filter",
                            f"查询 {table.name} 必须带上 {column} = '{value}'"
                            + (f"（{reason}）" if reason else ""),
                        )
                    )

        if sql_has_select_star(masked):
            issues.append(
                GuardIssue(
                    "error",
                    "select_star",
                    "禁止 SELECT *，必须显式列出最小必要字段",
                )
            )

        alias_map = _extract_aliases(masked)
        old_columns = set(used_columns)
        for prefix, column_name in _qualified_columns(masked):
            key = prefix.lower()
            if key in {"main", "temp", "sqlite_master"}:
                continue
            table_name = alias_map.get(key)
            if not table_name or table_name.lower() in ctes:
                continue
            table = self.catalog.table(table_name)
            if table is None:
                continue
            column = table.column(column_name)
            if column is None:
                candidates = [c.name for c in table.columns if not c.disabled]
                close = difflib.get_close_matches(column_name, candidates, n=3, cutoff=0.55)
                hint = f"；相近字段: {', '.join(close)}" if close else ""
                issues.append(
                    GuardIssue(
                        "error",
                        "unknown_column",
                        f"表 {table.name} 中不存在字段 {column_name}{hint}",
                    )
                )
                continue
            if column.disabled:
                issues.append(
                    GuardIssue("error", "disabled_column", f"字段 {table.name}.{column.name} 已被禁用")
                )
                continue
            if column.sensitive and not self.allow_sensitive:
                issues.append(
                    GuardIssue(
                        "error",
                        "sensitive_column",
                        f"字段 {table.name}.{column.name} 属于敏感字段，默认禁止查询",
                    )
                )
                continue
            old_columns.add(f"{table.name}.{column.name}")

        for table in table_objects:
            for column in table.columns:
                if column.disabled:
                    if re.search(
                        rf"(?<![\w]){re.escape(column.name)}(?![\w])", masked, re.IGNORECASE
                    ):
                        issues.append(
                            GuardIssue(
                                "error", "disabled_column", f"字段 {table.name}.{column.name} 已被禁用"
                            )
                        )
                elif column.sensitive and not self.allow_sensitive:
                    if re.search(
                        rf"(?<![\w]){re.escape(column.name)}(?![\w])", masked, re.IGNORECASE
                    ):
                        issues.append(
                            GuardIssue(
                                "error",
                                "sensitive_column",
                                f"字段 {table.name}.{column.name} 属于敏感字段，默认禁止查询",
                            )
                        )

        if len(table_objects) > 5:
            issues.append(
                GuardIssue(
                    "warning",
                    "wide_join",
                    f"本次查询涉及 {len(table_objects)} 张表，注意 JOIN 扇出导致指标重复计算",
                )
            )

        used_columns = sorted(old_columns)
        valid = not any(issue.level == "error" for issue in issues)
        return GuardResult(
            valid=valid,
            sql=cleaned.rstrip().rstrip(";").rstrip(),
            tables=sorted(set(used_tables)),
            columns=used_columns,
            issues=issues,
        )

    @staticmethod
    def _clean_input(sql: str) -> str:
        text = (sql or "").strip()
        fence = re.match(r"^```(?:sql)?\s*(.*?)```$", text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()
        return text


def mask_sql(sql: str) -> str:
    """Blank string literals and comments while preserving SQL structure."""
    out: list[str] = []
    i = 0
    length = len(sql)
    while i < length:
        ch = sql[i]
        if ch == "'":
            out.append(" ")
            i += 1
            while i < length:
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        if ch == "-" and i + 1 < length and sql[i + 1] == "-":
            while i < length and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < length and sql[i + 1] == "*":
            i += 2
            while i + 1 < length and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i = min(length, i + 2)
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    masked = "".join(out)
    masked = re.sub(r"\[([^\]\[]+)\]", r"\1", masked)
    masked = re.sub(r'"([^"]+)"', r"\1", masked)
    return masked


def _extract_ctes(masked: str) -> set[str]:
    return {m.group(1).lower() for m in _CTE.finditer(masked)}


def _split_top_level(expr: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(ch)
    if buffer:
        parts.append("".join(buffer))
    return parts


def _extract_tables(masked: str, ctes: set[str]) -> set[str]:
    names: set[str] = set()
    for match in _FROM_OR_JOIN.finditer(masked):
        name = match.group(1).lower()
        if name not in ctes and name not in _KEYWORDS:
            names.add(name)

    from_match = _FROM_CLAUSE.search(masked)
    if from_match:
        for part in _split_top_level(from_match.group(1)):
            cleaned = part.strip()
            if not cleaned or cleaned.startswith("("):
                continue
            match = re.match(r"(?:[A-Za-z_]\w*\s*\.\s*)?([A-Za-z_]\w*)", cleaned)
            if not match:
                continue
            name = match.group(1).lower()
            if name not in ctes and name not in _KEYWORDS:
                names.add(name)
    return names


def _extract_aliases(masked: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    pattern = re.compile(
        r"\b(?:from|join)\s+(?!\()\s*(?:[A-Za-z_]\w*\s*\.\s*)?([A-Za-z_]\w*)"
        r"(?:\s+(?:as\s+)?([A-Za-z_]\w*))?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(masked):
        table = match.group(1).lower()
        alias = (match.group(2) or "").lower()
        aliases.setdefault(table, table)
        if alias and alias not in _KEYWORDS:
            aliases[alias] = table
    return aliases


def _qualified_columns(masked: str) -> list[tuple[str, str]]:
    return [
        (m.group(1), m.group(2))
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\b", masked)
    ]


def sql_has_select_star(masked_sql: str) -> bool:
    text = _AGGREGATE_STAR.sub(" ", masked_sql or "")
    return bool(_SELECT_BARE_STAR.search(text) or _QUALIFIED_STAR.search(text))


def validate_column_reference(catalog: Catalog, table_name: str, column_name: str) -> str | None:
    table = catalog.table(table_name)
    if table is None:
        return None
    column = table.column(column_name)
    return column.name if column else None
