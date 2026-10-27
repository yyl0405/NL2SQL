"""Backend abstraction tests: SQLite stays untouched, the server backends run on fake drivers."""

from __future__ import annotations

import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from nl2sql_agent.config import Settings
from nl2sql_agent.tools import db as db_module
from nl2sql_agent.tools.db import (
    ConnectionPool,
    MssqlTarget,
    PostgresTarget,
    SqliteTarget,
    close_pools,
    configure_pool,
    connect_readonly,
    dialect_sql,
    pool_for,
    query,
)


def mssql_target(**overrides) -> MssqlTarget:
    base = dict(
        server="db.internal",
        port=1433,
        database="erp_test",
        username="reader",
        password="secret",
    )
    base.update(overrides)
    return MssqlTarget(**base)


def pg_target(**overrides) -> PostgresTarget:
    base = dict(
        host="db.internal",
        port=5432,
        database="erp_test",
        username="reader",
        password="secret",
    )
    base.update(overrides)
    return PostgresTarget(**base)


class FakeCursor:
    def __init__(self, rows, description, owner=None):
        self._rows = list(rows)
        self.description = description
        self.owner = owner
        #: (sql, raw params, autocommit at the time) — "execute(sql)" and
        #: "execute(sql, [])" must stay tellable apart, and a SET that ran inside a
        #: transaction that later rolled back is not a session setting.
        self.executed: list[tuple[str, object, bool]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params, bool(getattr(self.owner, "autocommit", False))))
        return self

    def fetchmany(self, size):
        taken, self._rows = self._rows[:size], self._rows[size:]
        return taken

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class FakeConnection:
    def __init__(self, rows, description=(("value",),), error=None):
        self.rows = rows
        self.description = description
        self.error = error
        self.timeout = None
        self.autocommit = False
        self.closed = False
        self.rollbacks = 0
        self.commits = 0
        self.cursor_obj = FakeCursor(rows, description, owner=self)

    def cursor(self):
        if self.error:
            raise self.error
        return self.cursor_obj

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def install_fake_pyodbc(monkeypatch, connection) -> types.ModuleType:
    module = types.ModuleType("pyodbc")
    calls: list[str] = []

    def connect(dsn, timeout=None):
        calls.append(dsn)
        return connection

    module.connect = connect
    monkeypatch.setitem(sys.modules, "pyodbc", module)
    module.connect.calls = calls  # type: ignore[attr-defined]
    return module


def install_fake_psycopg(monkeypatch, connection, *, name: str = "psycopg") -> types.ModuleType:
    module = types.ModuleType(name)
    calls: list[tuple[str, object]] = []

    def connect(dsn, connect_timeout=None):
        calls.append((dsn, connect_timeout))
        return connection

    module.connect = connect
    monkeypatch.setitem(sys.modules, name, module)
    module.connect.calls = calls  # type: ignore[attr-defined]
    return module


def block_pg_drivers(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setitem(sys.modules, "psycopg2", None)


def test_mssql_query_returns_the_shared_contract(monkeypatch) -> None:
    conn = FakeConnection([(1, "100.00"), (2, "200.00")], description=(("id",), ("amount",)))
    install_fake_pyodbc(monkeypatch, conn)
    result = query(mssql_target(), "SELECT id, amount FROM orders", max_rows=200, timeout_ms=5000)
    assert result["success"] is True
    assert result["columns"] == ["id", "amount"]
    assert result["rows"] == [[1, "100.00"], [2, "200.00"]]
    assert result["row_count"] == 2
    assert result["truncated"] is False
    assert conn.closed is True
    assert conn.timeout == 5


def test_mssql_query_marks_truncation_and_closes(monkeypatch) -> None:
    conn = FakeConnection([(i,) for i in range(4)])
    install_fake_pyodbc(monkeypatch, conn)
    result = query(mssql_target(), "SELECT id FROM t", max_rows=2)
    assert result["row_count"] == 2
    assert result["truncated"] is True
    assert conn.closed is True


def test_mssql_timeout_error_is_relabeled(monkeypatch) -> None:
    conn = FakeConnection([], error=Exception("Query timeout expired"))
    install_fake_pyodbc(monkeypatch, conn)
    result = query(mssql_target(), "SELECT 1", timeout_ms=1000)
    assert result["success"] is False
    assert result["error_code"] == "timeout"
    assert "1000ms" in result["error"]
    assert conn.closed is True


def test_missing_driver_is_a_tool_error_not_a_crash(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pyodbc", None)
    result = query(mssql_target(), "SELECT 1")
    assert result["success"] is False
    assert result["error_code"] == "driver_missing"
    assert "pip install" in result["error"]


def test_mssql_dsn_keeps_credentials_out_of_the_label() -> None:
    target = mssql_target()
    assert target.describe() == "mssql:erp_test"
    assert "secret" not in target.describe()
    assert "db.internal" not in target.describe()
    assert "PWD=secret" in target.dsn()


def test_connect_readonly_refuses_an_mssql_target() -> None:
    with pytest.raises(TypeError):
        connect_readonly(mssql_target())


def test_dialect_sql_uses_top_for_mssql_and_limit_for_sqlite() -> None:
    sql, params = dialect_sql(
        mssql_target(), table="Customer", column="Name", limit=200, pattern="华东"
    )
    assert "TOP (200)" in sql
    assert "CAST([Name] AS NVARCHAR(4000)) LIKE ?" in sql
    assert params == ["%华东%"]

    sqlite_sql, sqlite_params = dialect_sql(
        SqliteTarget(Path("demo.db")), table="customer", column="name", limit=200, pattern="华东"
    )
    assert sqlite_sql.endswith("LIMIT 200")
    assert '"name"' in sqlite_sql
    assert sqlite_params == ["%华东%"]


def test_sqlite_path_still_works_through_the_same_entry(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, "甲"), (2, "乙")])
    conn.commit()
    conn.close()

    result = query(path, "SELECT id, name FROM t ORDER BY id", max_rows=1)
    assert result["success"] is True
    assert result["rows"] == [[1, "甲"]]
    assert result["truncated"] is True
    assert "error_code" not in result


def test_runtime_fails_fast_when_the_configured_server_is_unreachable(
    tmp_path: Path, monkeypatch
) -> None:
    """An unreachable host must not degrade into 40 execution errors that still
    print ``valid_sql_rate 1.0``."""
    from nl2sql_agent import runtime as runtime_module

    def boom(target, timeout_ms):
        raise OSError("SSPI credential missing")

    monkeypatch.setattr(runtime_module, "open_connection", boom)
    settings = Settings(
        home=tmp_path,
        db_backend="mssql",
        mssql_server="db.internal",
        mssql_database="erp_test",
        mssql_username="reader",
        mssql_password="secret",
    )
    with pytest.raises(SystemExit) as caught:
        runtime_module.create_runtime(settings)
    message = str(caught.value)
    assert "连不上 mssql:erp_test" in message
    assert "secret" not in message and "db.internal" not in message


def test_runtime_reports_a_missing_driver_as_an_install_hint(
    tmp_path: Path, monkeypatch
) -> None:
    from nl2sql_agent import runtime as runtime_module

    def no_driver(target, timeout_ms):
        raise ModuleNotFoundError("No module named 'pyodbc'")

    monkeypatch.setattr(runtime_module, "open_connection", no_driver)
    settings = Settings(
        home=tmp_path, db_backend="mssql", mssql_server="h", mssql_database="d", mssql_username="u"
    )
    with pytest.raises(SystemExit, match=r"\[mssql\]"):
        runtime_module.create_runtime(settings)


def test_settings_target_and_label_follow_the_backend(tmp_path: Path, monkeypatch) -> None:
    for key in [k for k in os.environ if k.startswith("NL2SQL_")]:
        monkeypatch.delenv(key, raising=False)
    settings = Settings(home=tmp_path, db_backend="mssql", mssql_server="h", mssql_database="d", mssql_username="u")
    assert settings.is_mssql is True
    assert settings.database_label == "mssql:d"
    target = settings.target
    assert isinstance(target, MssqlTarget)

    incomplete = Settings(home=tmp_path, db_backend="mssql")
    with pytest.raises(ValueError, match="NL2SQL_MSSQL_SERVER"):
        _ = incomplete.target

    sqlite_settings = Settings(home=tmp_path)
    assert isinstance(sqlite_settings.target, SqliteTarget)
    assert sqlite_settings.database_label == str(sqlite_settings.db_path)

    pg_settings = Settings(
        home=tmp_path, db_backend="postgres", pg_host="h", pg_database="d", pg_username="u"
    )
    assert pg_settings.is_postgres is True and pg_settings.is_server_backend is True
    assert pg_settings.database_label == "postgres:d"
    assert isinstance(pg_settings.target, PostgresTarget)

    incomplete_pg = Settings(home=tmp_path, db_backend="postgres")
    with pytest.raises(ValueError, match="NL2SQL_PG_HOST"):
        _ = incomplete_pg.target


def test_an_unknown_backend_fails_at_config_not_at_the_first_query(tmp_path: Path) -> None:
    # A typo used to fall through to the bundled demo library: the agent answered a real
    # business question from synthetic rows and reported it as a query result.
    assert Settings(home=tmp_path, db_backend="MSSQL").db_backend == "mssql"
    assert Settings(home=tmp_path, db_backend="PostgreSQL").db_backend == "postgres"
    assert Settings(home=tmp_path, db_backend="pg").db_backend == "postgres"
    with pytest.raises(ValueError, match="未知 NL2SQL_DB_BACKEND"):
        Settings(home=tmp_path, db_backend="postgress")


def test_postgres_query_runs_the_shared_contract_on_a_fake_driver(monkeypatch) -> None:
    conn = FakeConnection([(1, "100.00"), (2, "200.00")], description=(("id",), ("amount",)))
    module = install_fake_psycopg(monkeypatch, conn)
    result = query(pg_target(), "SELECT id, amount FROM orders", max_rows=200, timeout_ms=5000)
    assert result["success"] is True
    assert result["columns"] == ["id", "amount"]
    assert result["rows"] == [[1, "100.00"], [2, "200.00"]]
    assert result["row_count"] == 2
    assert conn.closed is True
    dsn, connect_timeout = module.connect.calls[0]
    assert "host=db.internal" in dsn and "sslmode=prefer" in dsn
    assert connect_timeout == 5


def test_postgres_session_is_read_only_and_bounded_before_any_statement(monkeypatch) -> None:
    conn = FakeConnection([(1,)])
    install_fake_psycopg(monkeypatch, conn)
    query(pg_target(), "SELECT id FROM t", timeout_ms=3000)
    statements = [sql for sql, _, _ in conn.cursor_obj.executed]
    assert statements == [
        "SET default_transaction_read_only = on",
        "SET statement_timeout = 3000",
        "SELECT id FROM t",
    ]
    # A SET inside a transaction that later rolls back does not persist, so the session
    # statements must run under autocommit and the query must not.
    assert [autocommit for _, _, autocommit in conn.cursor_obj.executed] == [True, True, False]


def test_postgres_omits_the_argument_list_on_a_parameterless_statement(monkeypatch) -> None:
    # psycopg treats any non-None arguments as placeholder input: a literal ``%`` in a
    # parameterless statement would then raise. pyodbc does not care, so it always binds.
    conn = FakeConnection([(1,)])
    install_fake_psycopg(monkeypatch, conn)
    query(pg_target(), "SELECT id FROM t")
    assert conn.cursor_obj.executed[-1][1] is None

    mssql_conn = FakeConnection([(1,)])
    install_fake_pyodbc(monkeypatch, mssql_conn)
    query(mssql_target(), "SELECT id FROM t")
    assert mssql_conn.cursor_obj.executed[-1][1] == []


def test_postgres_prefers_psycopg3_and_falls_back_to_psycopg2(monkeypatch) -> None:
    conn = FakeConnection([(1,)])
    block_pg_drivers(monkeypatch)
    install_fake_psycopg(monkeypatch, conn, name="psycopg2")
    result = query(pg_target(), "SELECT 1")
    assert result["success"] is True


def test_postgres_without_any_driver_is_a_tool_error(monkeypatch) -> None:
    block_pg_drivers(monkeypatch)
    result = query(pg_target(), "SELECT 1")
    assert result["success"] is False
    assert result["error_code"] == "driver_missing"
    assert ".[postgres]" in result["error"]


def test_postgres_target_hides_host_and_password() -> None:
    target = pg_target()
    assert target.describe() == "postgres:erp_test"
    for secret in ("secret", "db.internal", "reader"):
        assert secret not in target.describe()
    assert "password=secret" in target.dsn()


def test_connect_readonly_refuses_a_postgres_target() -> None:
    with pytest.raises(TypeError):
        connect_readonly(pg_target())


def test_runtime_fails_fast_for_postgres_too(tmp_path: Path, monkeypatch) -> None:
    from nl2sql_agent import runtime as runtime_module

    def no_driver(target, timeout_ms, **kwargs):
        raise ModuleNotFoundError("No module named 'psycopg'")

    monkeypatch.setattr(runtime_module, "open_connection", no_driver)
    settings = Settings(
        home=tmp_path, db_backend="postgres", pg_host="h", pg_database="d", pg_username="u"
    )
    with pytest.raises(SystemExit, match=r"\[postgres\]"):
        runtime_module.create_runtime(settings)


def test_postgres_probe_names_the_postgres_config_items(tmp_path: Path, monkeypatch) -> None:
    from nl2sql_agent import runtime as runtime_module

    def boom(target, timeout_ms, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(runtime_module, "open_connection", boom)
    settings = Settings(
        home=tmp_path, db_backend="postgres", pg_host="h", pg_database="d", pg_username="u"
    )
    with pytest.raises(SystemExit) as caught:
        runtime_module.create_runtime(settings)
    message = str(caught.value)
    assert "连不上 postgres:d" in message and "NL2SQL_PG_" in message
    assert "secret" not in message


@pytest.fixture
def no_pools():
    close_pools()
    yield
    close_pools()


def _fake_opener(made: list[FakeConnection], *, pooled_flag: list[bool] | None = None):
    def opener(target, timeout_ms, *, pooled=False):
        conn = FakeConnection([(1,)])
        if pooled_flag is not None:
            pooled_flag.append(pooled)
        made.append(conn)
        return conn, "mssql"

    return opener


def test_pool_reuses_one_connection_and_rolls_back_instead_of_closing(
    monkeypatch, no_pools
) -> None:
    made: list[FakeConnection] = []
    pooled_flags: list[bool] = []
    monkeypatch.setattr(db_module, "open_connection", _fake_opener(made, pooled_flag=pooled_flags))
    target = mssql_target()
    configure_pool(target, max_size=2)
    assert query(target, "SELECT 1")["success"] is True
    assert query(target, "SELECT 1")["success"] is True
    stats = pool_for(target).stats()
    assert len(made) == 1, "两条查询建了两条物理连接：池没有复用"
    assert stats["connects"] == 1 and stats["reuses"] == 1 and stats["checkouts"] == 2
    assert made[0].rollbacks == 2 and made[0].commits == 0
    assert made[0].closed is False, "归还时把连接关掉了：池形同虚设"
    assert pooled_flags == [True]


def test_pool_is_off_by_default_and_reports_no_pool(monkeypatch, no_pools) -> None:
    made: list[FakeConnection] = []
    monkeypatch.setattr(db_module, "open_connection", _fake_opener(made))
    target = mssql_target()
    configure_pool(target, max_size=1)
    query(target, "SELECT 1")
    query(target, "SELECT 1")
    assert pool_for(target) is None
    assert len(made) == 2, "未开启池化却复用连接：published 数字的基线不再是逐查询建连"
    assert all(conn.closed for conn in made)


def test_disabling_the_pool_closes_the_idle_connections(monkeypatch, no_pools) -> None:
    made: list[FakeConnection] = []
    monkeypatch.setattr(db_module, "open_connection", _fake_opener(made))
    target = mssql_target()
    configure_pool(target, max_size=2)
    query(target, "SELECT 1")
    configure_pool(target, max_size=1)
    assert pool_for(target) is None
    assert made[0].closed is True


def test_an_erroring_connection_is_discarded_not_reused(monkeypatch, no_pools) -> None:
    made: list[FakeConnection] = []
    monkeypatch.setattr(db_module, "open_connection", _fake_opener(made))
    target = mssql_target()
    configure_pool(target, max_size=2)
    assert query(target, "SELECT 1")["success"] is True
    # The handle now fails: an aborted transaction or a broken TDS pipe poisons it.
    made[0].error = Exception("connection reset")
    second = query(target, "SELECT 1")
    assert second["success"] is False and second["error_code"] == "sql_error"
    assert pool_for(target).stats()["discards"] == 1
    assert made[0].closed is True, "坏连接只是没返回池里，却没有被关闭"
    assert query(target, "SELECT 1")["success"] is True
    assert len(made) == 2, "池把刚丢弃的坏句柄又发给了下一条查询"
    assert made[1].closed is False


def test_pool_exhaustion_is_its_own_error_code(monkeypatch, no_pools) -> None:
    made: list[FakeConnection] = []
    monkeypatch.setattr(db_module, "open_connection", _fake_opener(made))
    target = mssql_target()
    pool = configure_pool(target, max_size=2)
    pool.checkout_wait_sec = 0.05
    held = [pool.checkout() for _ in range(2)]
    result = query(target, "SELECT 1")
    assert result["success"] is False
    assert result["error_code"] == "pool_unavailable", (
        "池满被当成 SQL 错误：模型会去改写一条本来没问题的 SQL"
    )
    for connection_id, conn in held:
        pool.release(connection_id, conn)
    assert query(target, "SELECT 1")["success"] is True


def test_pooled_sqlite_serves_other_threads(tmp_path: Path, no_pools) -> None:
    """sqlite3's same-thread guard would reject a pooled handle; the pool owns exclusivity."""
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "pool.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(10)])
    conn.commit()
    conn.close()

    target = SqliteTarget(path)
    configure_pool(target, max_size=2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: query(target, "SELECT id FROM t ORDER BY id LIMIT 2"), range(6)))
    assert all(item["success"] for item in results), results
    stats = pool_for(target).stats()
    assert stats["connects"] <= 2, "每个线程各建一条连接：池上限没有生效"
    assert stats["reuses"] > 0


def test_stale_idle_connection_is_evicted(monkeypatch, no_pools) -> None:
    import time

    made: list[FakeConnection] = []
    monkeypatch.setattr(db_module, "open_connection", _fake_opener(made))
    target = mssql_target()
    pool = configure_pool(target, max_size=2)
    query(target, "SELECT 1")
    # Age the pooled handle past its idle TTL instead of sleeping on it.
    connection_id, conn, _ = pool._idle[0]
    pool._idle[0] = (connection_id, conn, time.monotonic() - pool.idle_ttl_sec - 1)
    query(target, "SELECT 1")
    assert conn.closed is True
    assert len(made) == 2
    assert pool.stats()["discards"] == 1


def test_a_bare_path_and_a_target_register_the_same_pool(tmp_path: Path, no_pools) -> None:
    """``query()`` accepts a bare path, so pooling must be keyed the same way.

    A pool registered under ``str(path)`` while the executor looks up
    ``SqliteTarget.describe()`` would be configured, reported, and never used.
    """
    path = tmp_path / "key.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    pool = configure_pool(path, max_size=2)
    assert pool is not None
    assert pool_for(path) is pool
    assert pool_for(SqliteTarget(path)) is pool
    assert query(path, "SELECT id FROM t")["success"] is True
    assert pool.stats()["checkouts"] == 1


def test_a_pooled_timeout_reports_timeout_and_the_next_query_still_runs(
    tmp_path: Path, no_pools
) -> None:
    """Measured on the demo library: an interrupted statement leaves the pool usable.

    The deadline is a client-side interrupt, so the statement error escapes the pool's
    context manager and the handle is discarded; the next call must rebuild rather than
    hand back a connection with a stale deadline attached.
    """
    path = tmp_path / "slow.db"
    sqlite3.connect(path).close()
    target = SqliteTarget(path)
    configure_pool(target, max_size=2)
    heavy = (
        "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 40000000) "
        "SELECT COUNT(*) FROM seq"
    )
    slow = query(target, heavy, timeout_ms=100)
    assert slow["success"] is False and slow["error_code"] == "timeout"
    assert slow["elapsed_ms"] < 1000, "中止没有生效，连接被拖在语句里"
    follow_up = query(target, "SELECT 1")
    assert follow_up["success"] is True, "超时后的句柄污染了池：后续查询全部失败"
    stats = pool_for(target).stats()
    assert stats["discards"] == 1 and stats["connects"] == 2
