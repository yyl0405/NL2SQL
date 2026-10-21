"""SQL validation, read-only execution and deterministic repair tools."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ..guard import SQLGuard
from ..repair import SQLRepairer
from ..text import to_jsonable
from .base import Tool, ToolSpec, string_param
from .db import connect_readonly


def run_readonly_query(
    db_path: str | Path,
    sql: str,
    *,
    max_rows: int = 200,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    max_rows = max(1, min(1000, int(max_rows)))
    timeout_ms = max(100, int(timeout_ms))
    started = time.monotonic()
    deadline = started + timeout_ms / 1000.0
    conn = None
    try:
        conn = connect_readonly(db_path)

        def _progress() -> int:
            return int(time.monotonic() > deadline)

        conn.set_progress_handler(_progress, 1000)
        cursor = conn.execute(sql)
        columns = [desc[0] for desc in (cursor.description or [])]
        fetched = cursor.fetchmany(max_rows)
        truncated = cursor.fetchone() is not None
        rows = [list(row) for row in fetched]
        return {
            "success": True,
            "columns": columns,
            "rows": to_jsonable(rows),
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except FileNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_code": "db_not_found",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except sqlite3.OperationalError as exc:
        error = str(exc)
        code = "timeout" if "interrupted" in error.lower() else "sql_error"
        if code == "timeout":
            error = f"查询超过 {timeout_ms}ms 被中止，请缩小扫描范围或加过滤条件"
        return {
            "success": False,
            "error": error,
            "error_code": code,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except sqlite3.Error as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_code": "sql_error",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
    finally:
        if conn is not None:
            conn.close()


def build_sql_tools(
    guard: SQLGuard,
    repairer: SQLRepairer,
    db_path: str | Path,
    *,
    max_rows: int = 200,
    timeout_ms: int = 5000,
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
        result = run_readonly_query(
            db_path,
            check.sql,
            max_rows=limit,
            timeout_ms=timeout_ms,
        )
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
