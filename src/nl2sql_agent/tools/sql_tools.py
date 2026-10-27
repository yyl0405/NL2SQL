"""SQL validation, read-only execution and deterministic repair tools."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..guard import SQLGuard
from ..repair import SQLRepairer
from .base import Tool, ToolSpec, string_param
from .db import Target, query


def run_readonly_query(
    db_path: Target | str | Path,
    sql: str,
    *,
    max_rows: int = 200,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Backend-agnostic read-only execution (see ``tools.db.query``)."""
    return query(db_path, sql, max_rows=max_rows, timeout_ms=timeout_ms)


def build_sql_tools(
    guard: SQLGuard,
    repairer: SQLRepairer,
    target: Target,
    *,
    max_rows: int = 200,
    timeout_ms: int = 5000,
    retries: int = 0,
    retry_backoff_sec: float = 0.05,
) -> list[Tool]:
    def validate_sql(sql: str) -> dict[str, Any]:
        return guard.check(str(sql or "")).to_dict()

    def execute_sql(sql: str, max_rows_limit: int | None = None) -> dict[str, Any]:
        check = guard.check(str(sql or ""))
        if not check.valid:
            return {
                "success": False,
                "stage": "guard",
                "error": "SQL 未通过安全/权限护栏",
                "guard": check.to_dict(),
            }
        limit = max_rows
        if max_rows_limit is not None:
            limit = max(1, min(max_rows, int(max_rows_limit)))
        result = query(
            target,
            check.sql,
            max_rows=limit,
            timeout_ms=timeout_ms,
        )
        attempts = 1
        # Only pool exhaustion is retried: the statement itself never ran, the query
        # is read-only, and the cause is operational. A sql_error is the model's
        # problem and a timeout only gets slower when repeated.
        while (
            attempts <= retries
            and not result.get("success")
            and result.get("error_code") == "pool_unavailable"
        ):
            if retry_backoff_sec > 0:
                time.sleep(retry_backoff_sec)
            result = query(
                target,
                check.sql,
                max_rows=limit,
                timeout_ms=timeout_ms,
            )
            attempts += 1
        if attempts > 1:
            result["attempts"] = attempts
        result["sql"] = check.sql
        return result

    def repair_sql(sql: str, error: str = "") -> dict[str, Any]:
        return repairer.repair(str(sql or ""), str(error or "")).to_dict()

    return [
        Tool(
            ToolSpec(
                name="validate_sql",
                description=(
                    "Validate a candidate SQL before execution: read-only statement, table whitelist, "
                    "no SELECT *, disabled/sensitive fields, required dimension filters and typed columns. "
                    "Always call this before execute_sql."
                ),
                parameters={
                    "type": "object",
                    "properties": {"sql": string_param("待校验的 SQLite SELECT 语句")},
                    "required": ["sql"],
                },
            ),
            validate_sql,
        ),
        Tool(
            ToolSpec(
                name="execute_sql",
                description=(
                    "Execute a validated, read-only SELECT with row/time limits. Returns columns, rows, "
                    "row_count, truncated and elapsed_ms. On error, pass the error to repair_sql."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "sql": string_param("待执行的 SELECT 语句"),
                        "max_rows_limit": {
                            "type": "integer",
                            "description": "返回行数上限，不能超过服务端配置",
                        },
                    },
                    "required": ["sql"],
                },
            ),
            execute_sql,
        ),
        Tool(
            ToolSpec(
                name="repair_sql",
                description=(
                    "Apply deterministic, low-risk repairs after a database error. It can remove an invalid "
                    "projection, clean trailing commas, or wrap GROUP BY arithmetic expressions in SUM(). "
                    "It never adds new tables or fields."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "sql": string_param("执行失败的 SQL"),
                        "error": string_param("数据库返回的原始错误信息"),
                    },
                    "required": ["sql", "error"],
                },
            ),
            repair_sql,
        ),
    ]
