"""Build a schema catalog from a live SQL Server database (read-only introspection).

The generated catalog has the same shape as ``data/demo_schema.json`` so the
whole agent — BM25 table recall, guard, evaluator — runs unchanged against a
real database. Relationships come from two deterministic sources: SQL Server
foreign keys and join hints embedded in comments (``MS_Description``); neither
is model-generated. Join hints look like::

    客户ID[关联表:Customer,关联字段:ID]
    行业[关联表:BasicData,关联字段:ID,前置条件:DictType=3]

Nothing here touches the repository: the output path is chosen by the caller and
the CLI defaults to ``var/``, which is gitignored on purpose. A real schema is
not portfolio material — only the numbers measured against it are.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

TABLE_SQL = """
SELECT o.name AS table_name, o.type AS object_type,
       CAST(ep.value AS NVARCHAR(500)) AS comment
FROM sys.objects o
LEFT JOIN sys.extended_properties ep
       ON ep.major_id = o.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
WHERE o.is_ms_shipped = 0 AND o.type IN ('U', 'V')
ORDER BY o.name
"""

COLUMN_SQL = """
SELECT o.name AS table_name, c.name AS column_name, ty.name AS type_name,
       c.max_length, c.precision, c.scale, c.is_nullable,
       CAST(ep.value AS NVARCHAR(500)) AS comment
FROM sys.columns c
JOIN sys.objects o ON o.object_id = c.object_id AND o.is_ms_shipped = 0 AND o.type IN ('U', 'V')
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.extended_properties ep
       ON ep.major_id = c.object_id AND ep.minor_id = c.column_id AND ep.name = 'MS_Description'
ORDER BY o.name, c.column_id
"""

PRIMARY_KEY_SQL = """
SELECT kc.table_name, kc.column_name
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kc
  ON kc.constraint_name = tc.constraint_name AND kc.constraint_schema = tc.constraint_schema
WHERE tc.constraint_type = 'PRIMARY KEY'
ORDER BY kc.table_name, kc.ordinal_position
"""

FOREIGN_KEY_SQL = """
SELECT tp.name AS parent_table, cp.name AS parent_column,
       tc.name AS child_table, cc.name AS child_column
FROM sys.foreign_key_columns fkc
JOIN sys.tables tp ON tp.object_id = fkc.referenced_object_id
JOIN sys.columns cp ON cp.object_id = fkc.referenced_object_id AND cp.column_id = fkc.referenced_column_id
JOIN sys.tables tc ON tc.object_id = fkc.parent_object_id
JOIN sys.columns cc ON cc.object_id = fkc.parent_object_id AND cc.column_id = fkc.parent_column_id
WHERE tp.is_ms_shipped = 0 AND tc.is_ms_shipped = 0
ORDER BY tp.name, cp.name, tc.name, cc.name
"""

_JOIN_RE = re.compile(
    r"\[\s*关联表\s*[:：]\s*([^,，\]\s]+)\s*[,，]\s*关联字段\s*[:：]\s*([^\]\[]+?)\s*(?:[,，]\s*前置条件\s*[:：]\s*([^\]]+))?\s*\]"
)
_SENSITIVE_NAME_RE = re.compile(
    r"(phone|mobile|tel_|email|mail|id_card|identity|身份证|手机号|电话|邮箱|bank|银行|账号|账户|"
    r"password|passwd|secret|token|身份证|银行卡|salary|工资|薪资)",
    re.IGNORECASE,
)
_JUNK_NAME_RE = re.compile(
    r"(^tmp|^temp|^table_|_bak$|^bak_|_copy|副本|测试|~|_log$|operationlog|errorlog|_oplog$|operatelog)",
    re.IGNORECASE,
)

_STRING_TYPES = {"varchar", "nvarchar", "char", "nchar", "text", "ntext", "sysname"}


@dataclass
class IntrospectOptions:
    domain: str = "SQL Server ERP"
    as_of_date: str = ""
    include: Sequence[str] = field(default_factory=tuple)
    exclude: Sequence[str] = field(default_factory=tuple)
    include_views: bool = False
    mark_sensitive: bool = True
    mark_disabled: bool = True
    max_columns_per_table: int = 120


def _type_name(raw: str, length: int, precision: int, scale: int) -> str:
    name = (raw or "").strip().lower()
    if name in {"varchar", "char", "varbinary"}:
        return f"{name}({length})" if 0 < length < 8000 else name
    if name in {"nvarchar", "nchar"}:
        half = length // 2 if length and length > 0 else length
        return f"{name}({half})" if 0 < half < 4000 else name
    if name in {"decimal", "numeric"} and precision:
        return f"{name}({precision},{scale})"
    return name


def _matches(name: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, name, re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in name.lower():
                return True
    return False


def parse_join_hints(comment: str) -> list[dict[str, str]]:
    """Extract ``[关联表:X,关联字段:Y(,前置条件:Z)]`` hints embedded in comments."""
    hints: list[dict[str, str]] = []
    for match in _JOIN_RE.finditer(comment or ""):
        target, column, condition = match.group(1), match.group(2), match.group(3)
        hints.append(
            {
                "table": target.strip(),
                "column": column.strip(),
                "condition": (condition or "").strip(),
            }
        )
    return hints


def _fetch(cursor, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    cursor.execute(sql, *params) if params else cursor.execute(sql)
    return [tuple(row) for row in cursor.fetchall()]


def introspect(connection: Any, options: IntrospectOptions | None = None) -> dict[str, Any]:
    """Read tables, columns, primary keys and comments; return a catalog payload."""
    options = options or IntrospectOptions()
    cursor = connection.cursor()

    rows = _fetch(cursor, TABLE_SQL)
    # sys.objects.type is CHAR(2): pyodbc returns 'U ' and a plain == would drop every table
    tables = [r for r in rows if options.include_views or str(r[1]).strip().upper() == "U"]
    columns = _fetch(cursor, COLUMN_SQL)
    pks = _fetch(cursor, PRIMARY_KEY_SQL)
    fks = _fetch(cursor, FOREIGN_KEY_SQL)

    pk_by_table: dict[str, list[str]] = {}
    for table_name, column_name in pks:
        pk_by_table.setdefault(str(table_name), []).append(str(column_name))

    columns_by_table: dict[str, list[tuple[Any, ...]]] = {}
    for row in columns:
        columns_by_table.setdefault(str(row[0]), []).append(row)

    selected: list[tuple[str, str, str]] = []
    for table_name, _object_type, comment in tables:
        name = str(table_name)
        if options.include and not _matches(name, options.include):
            continue
        if options.exclude and _matches(name, options.exclude):
            continue
        selected.append((name, str(comment or "").strip(), ""))

    known = {entry[0].lower() for entry in selected}
    selected_names = {entry[0] for entry in selected}
    catalog_tables: list[dict[str, Any]] = []
    relationships: list[dict[str, str]] = [
        {
            "left": f"{parent_table}.{parent_column}",
            "right": f"{child_table}.{child_column}",
            "type": "1:N",
        }
        for parent_table, parent_column, child_table, child_column in fks
        if str(parent_table) in selected_names and str(child_table) in selected_names
    ]
    fk_relationship_count = len(relationships)
    filter_suggestions: list[dict[str, str]] = []
    sensitive_count = 0
    hint_count = 0

    for name, comment, _ in selected:
        raw_columns = columns_by_table.get(name, [])
        cols: list[dict[str, Any]] = []
        for _, column_name, type_name, length, precision, scale, _nullable, col_comment in raw_columns[
            : options.max_columns_per_table
        ]:
            entry: dict[str, Any] = {
                "name": str(column_name),
                "type": _type_name(str(type_name), int(length or 0), int(precision or 0), int(scale or 0)),
            }
            text = str(col_comment or "").strip()
            if text:
                entry["description"] = text
            flagged = False
            if options.mark_sensitive:
                flagged = bool(_SENSITIVE_NAME_RE.search(str(column_name))) or bool(
                    text and _SENSITIVE_NAME_RE.search(text)
                )
            if flagged:
                entry["sensitive"] = True
                sensitive_count += 1
            cols.append(entry)

            for hint in parse_join_hints(text):
                hint_count += 1
                if hint["table"].lower() in known:
                    relationships.append(
                        {
                            "left": f"{hint['table']}.{hint['column']}",
                            "right": f"{name}.{entry['name']}",
                            "type": "1:N",
                        }
                    )
                if hint["condition"]:
                    filter_suggestions.append(
                        {"table": name, "column": entry["name"], "condition": hint["condition"]}
                    )

        table: dict[str, Any] = {
            "name": name,
            "description": comment or name,
            "primary_key": ", ".join(pk_by_table.get(name, [])),
            "columns": cols,
        }
        if options.mark_disabled and _JUNK_NAME_RE.search(name):
            table["disabled"] = True
        catalog_tables.append(table)

    unique_relationships: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rel in relationships:
        key = (rel["left"], rel["right"])
        if key in seen:
            continue
        seen.add(key)
        unique_relationships.append(rel)

    unique_suggestions: list[dict[str, str]] = []
    seen_conditions: set[tuple[str, str, str]] = set()
    for item in filter_suggestions:
        key = (item["table"], item["column"], item["condition"])
        if key in seen_conditions:
            continue
        seen_conditions.add(key)
        unique_suggestions.append(item)

    return {
        "domain": options.domain,
        "dialect": "mssql",
        "as_of_date": options.as_of_date,
        "business_rules": [],
        "tables": catalog_tables,
        "relationships": unique_relationships,
        # Review list only: a 前置条件 comment becomes a guard-enforced required_filters
        # entry only after a human confirms the column really is a type discriminator.
        "filter_suggestions": unique_suggestions,
        "_stats": {
            "tables": len(catalog_tables),
            "columns": sum(len(t["columns"]) for t in catalog_tables),
            "join_hints": hint_count,
            "foreign_keys": len(fks),
            "fk_relationships": fk_relationship_count,
            "relationships": len(unique_relationships),
            "filter_suggestions": len(unique_suggestions),
            "sensitive_columns": sensitive_count,
            "disabled_tables": sum(1 for t in catalog_tables if t.get("disabled")),
        },
    }
