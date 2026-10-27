from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from nl2sql_agent.memory import ExperienceStore
from nl2sql_agent.repair import SQLRepairer
from nl2sql_agent.tools import sql_tools as sql_tools_module
from nl2sql_agent.tools.sql_tools import build_sql_tools


def test_store_opens_in_wal_with_a_busy_timeout(tmp_path: Path) -> None:
    store = ExperienceStore(tmp_path / "experience.db")
    assert store.journal_mode().lower() == "wal"
    with store._session() as conn:
        assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 10000


def test_concurrent_writers_do_not_lose_rows_or_raise(tmp_path: Path) -> None:
    store = ExperienceStore(tmp_path / "experience.db")
    workers = 8
    per_worker = 5
    start = threading.Barrier(workers)
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        try:
            start.wait(timeout=10)
            for item in range(per_worker):
                store.add(
                    f"第 {index} 组第 {item} 个问题销售额",
                    "SELECT 1 AS n",
                    schema_hash="h1",
                )
        except BaseException as exc:  # noqa: BLE001 - the assertion below wants the object
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert store.stats()["total"] == workers * per_worker


def test_readers_keep_working_while_writers_commit(tmp_path: Path) -> None:
    store = ExperienceStore(tmp_path / "experience.db")
    stop = threading.Event()
    errors: list[BaseException] = []
    reads = [0]

    def reader() -> None:
        try:
            while not stop.is_set():
                store.stats()
                store.list(limit=5)
                reads[0] += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(60):
            store.add(f"并发读问题 {index} 销售额", "SELECT 1 AS n", schema_hash="h1")
    finally:
        stop.set()
        thread.join(timeout=10)

    assert errors == []
    assert reads[0] > 0
    assert store.stats()["total"] == 60


def test_a_rollback_journal_database_is_reopened_in_wal(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    store = ExperienceStore(path)
    store.add("旧库问题销售额", "SELECT 1 AS n", schema_hash="h1")
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"

    reopened = ExperienceStore(path)

    assert reopened.journal_mode().lower() == "wal"
    assert reopened.stats()["total"] == 1


def _execute_tool(guard, monkeypatch, outcomes, *, retries: int):
    calls: list[str] = []

    def fake_query(target, sql, max_rows=200, timeout_ms=5000):
        calls.append(sql)
        return dict(outcomes[min(len(calls), len(outcomes)) - 1])

    monkeypatch.setattr(sql_tools_module, "query", fake_query)
    tools = build_sql_tools(
        guard,
        SQLRepairer(),
        "ignored.db",
        retries=retries,
        retry_backoff_sec=0.0,
    )
    execute = {tool.spec.name: tool for tool in tools}["execute_sql"]
    return execute.handler("SELECT COUNT(*) FROM sales_order"), calls


def test_pool_exhaustion_is_retried(guard, monkeypatch) -> None:
    result, calls = _execute_tool(
        guard,
        monkeypatch,
        [
            {"success": False, "error": "pool busy", "error_code": "pool_unavailable"},
            {
                "success": True,
                "columns": ["n"],
                "rows": [[1]],
                "row_count": 1,
                "truncated": False,
                "elapsed_ms": 0.5,
            },
        ],
        retries=1,
    )

    assert result["success"] is True
    assert result["attempts"] == 2
    assert len(calls) == 2


def test_sql_errors_and_timeouts_are_not_retried(guard, monkeypatch) -> None:
    for code in ("sql_error", "timeout"):
        result, calls = _execute_tool(
            guard,
            monkeypatch,
            [{"success": False, "error": "boom", "error_code": code}],
            retries=2,
        )
        assert result["success"] is False
        assert "attempts" not in result
        assert len(calls) == 1, f"{code} 不该被自动重试"
