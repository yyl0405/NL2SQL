"""Schema-aware tools: table recall, schema inspection and value linking."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from ..catalog import Catalog
from .base import Tool, ToolError, ToolSpec, string_param
from .db import connect_readonly, quote_ident


def build_schema_tools(catalog: Catalog, db_path: str | Path) -> list[Tool]:
    def search_tables(query: str, top_k: int = 5) -> dict[str, Any]:
        top_k = max(1, min(20, int(top_k)))
        hits = catalog.search_tables(str(query or ""), top_k=top_k)
        return {
            "query": query,
            "count": len(hits),
            "tables": [hit.to_dict() for hit in hits],
        }

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
            raise ToolError(f"字段不存在或已禁用: {table}.{column}")
        if column_obj.sensitive:
            raise ToolError(f"敏感字段不允许做值检索: {table}.{column}")

        limit = max(1, min(50, int(limit)))
        query = str(query or "").strip()
        sql = (
            f"SELECT DISTINCT CAST({quote_ident(column_obj.name)} AS TEXT) AS value "
            f"FROM {quote_ident(table_obj.name)} "
            f"WHERE {quote_ident(column_obj.name)} IS NOT NULL"
        )
        params: list[Any] = []
        if query:
            sql += f" AND CAST({quote_ident(column_obj.name)} AS TEXT) LIKE ?"
            params.append(f"%{query}%")
        sql += " ORDER BY value LIMIT 200"
        conn = connect_readonly(db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        values = [str(row["value"]) for row in rows]
        if query:
            values.sort(
                key=lambda value: (
                    -_match_score(query, value),
                    value,
                )
            )
        values = values[:limit]
        return {
            "table": table_obj.name,
            "column": column_obj.name,
            "query": query,
            "count": len(values),
            "values": values,
        }

    return [
        Tool(
            ToolSpec(
                name="search_tables",
                description=(
                    "Retrieve relevant tables from the schema catalog using lexical schema linking. "
                    "Call this first. It returns table names, descriptions and column names only."
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
                    "name, status, region). Use it before filtering on a Chinese value that is not in the schema."
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
