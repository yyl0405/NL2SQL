"""Schema-aware tools: table recall, schema inspection and value linking."""

from __future__ import annotations

import difflib
import re
from typing import Any

from ..catalog import Catalog
from .base import Tool, ToolError, ToolSpec, string_param
from .db import Target, dialect_sql
from .db import query as run_query


def build_schema_tools(catalog: Catalog, target: Target) -> list[Tool]:
    def search_tables(query: str, top_k: int = 5) -> dict[str, Any]:
        top_k = max(1, min(20, int(top_k)))
        result = catalog.search(str(query or ""), top_k=top_k).to_dict()
        return {"query": query, **result}

    def get_table_schema(tables: list[str] | str | None = None) -> dict[str, Any]:
        if tables is None:
            names = catalog.enabled_names()
        elif isinstance(tables, str):
            names = [part for part in re.split(r"[,，\s]+", tables) if part]
        else:
            names = [str(name) for name in tables]
        names = list(dict.fromkeys(names))[:8]
        if not names:
            raise ToolError("至少提供一张表名")
        data = catalog.describe_tables(names)
        data["returned_tables"] = len(data["tables"])
        return data

    def search_values(
        table: str,
        column: str,
        query: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        table_obj = catalog.table(str(table))
        if table_obj is None or table_obj.disabled:
            raise ToolError(f"表不存在或已禁用: {table}")
        column_obj = table_obj.column(str(column))
        if column_obj is None or column_obj.disabled:
            available = [
                item.name
                for item in table_obj.columns
                if not item.disabled and not item.sensitive
            ][:20]
            raise ToolError(
                f"字段不存在或已禁用: {table}.{column}。可用字段: {', '.join(available)}"
            )
        if column_obj.sensitive:
            raise ToolError(f"敏感字段不允许做值检索: {table}.{column}")

        limit = max(1, min(50, int(limit)))
        query_text = str(query or "").strip()
        sql, params = dialect_sql(
            target,
            table=table_obj.name,
            column=column_obj.name,
            limit=200,
            pattern=query_text or None,
        )
        result = run_query(target, sql, params, max_rows=200, timeout_ms=30000)
        if not result.get("success"):
            raise ToolError(f"值检索失败: {result.get('error')}")
        values = [str(row[0]) for row in result["rows"]]
        if query_text:
            values.sort(
                key=lambda value: (
                    -_match_score(query_text, value),
                    value,
                )
            )
        values = values[:limit]
        payload = {
            "table": table_obj.name,
            "column": column_obj.name,
            "query": query_text,
            "count": len(values),
            "values": values,
        }
        if not values:
            payload["hint"] = "该列没有匹配值；不要继续对同一列反复试值，改用已确认字段或写入 assumptions。"
        return payload

    return [
        Tool(
            ToolSpec(
                name="search_tables",
                description=(
                    "Retrieve relevant tables from the schema catalog using schema linking "
                    "(BM25, optionally fused with embedding recall). Call this first. It returns "
                    "table names, descriptions, column names and the retrieval source per table."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": string_param("用户问题或其中的业务关键词"),
                        "top_k": {
                            "type": "integer",
                            "description": "返回候选表数量，1-20",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            ),
            search_tables,
        ),
        Tool(
            ToolSpec(
                name="get_table_schema",
                description=(
                    "Get full column definitions, data types, disabled/sensitive markers, required join "
                    "filters and relationships for up to 8 tables."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "需要查看的表名列表",
                        }
                    },
                    "required": ["tables"],
                },
            ),
            get_table_schema,
        ),
        Tool(
            ToolSpec(
                name="search_values",
                description=(
                    "Look up real values of a low-cardinality dimension column (customer name, material "
                    "name, status, region). The column must be an exact name from get_table_schema; unknown "
                    "columns return the available column list. Use it before filtering on a Chinese value "
                    "that is not in the schema."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "table": string_param("表名"),
                        "column": string_param("字段名"),
                        "query": string_param("待查找的值或值的一部分，可为空"),
                        "limit": {"type": "integer", "default": 10, "description": "最多返回值数量"},
                    },
                    "required": ["table", "column"],
                },
            ),
            search_values,
        ),
    ]


def _match_score(query: str, value: str) -> float:
    q, v = query.lower(), value.lower()
    if q == v:
        return 4.0
    if v.startswith(q) or q.startswith(v):
        return 3.0
    if q in v or v in q:
        return 2.0
    return difflib.SequenceMatcher(None, q, v).ratio()
