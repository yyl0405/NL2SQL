"""Read-only database targets: SQLite (demo), SQL Server and PostgreSQL (real instances).

Every target exposes the same ``query()`` contract so the agent tools, the evaluator
and the CLI stay backend agnostic. Non-SQLite drivers are optional and imported
lazily: a missing ``pyodbc``/``psycopg`` surfaces as a ``driver_missing`` tool error
instead of an import-time crash, so the demo path and the whole test suite run with
no driver installed at all.

Dialect *facts* (quoting, row caps, placeholder style, session statements) live in
``../dialects.py``; this module only owns connections, execution and pooling.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .. import observability
from ..dialects import Dialect, get as dialect_for
from ..text import to_jsonable


@dataclass(frozen=True)
class SqliteTarget:
    path: Path
    dialect: str = "sqlite"

    def describe(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class MssqlTarget:
    server: str
    port: int
    database: str
    username: str
    password: str
    driver: str = "ODBC Driver 17 for SQL Server"
    trust_server_certificate: bool = True

    dialect: str = "mssql"

    def describe(self) -> str:
        """Connection label safe to show in a UI or an API response."""
        return f"mssql:{self.database}"

    def dsn(self) -> str:
        return (
            f"DRIVER={{{self.driver}}};SERVER={self.server},{self.port};"
            f"DATABASE={self.database};UID={self.username};PWD={self.password};"
            f"TrustServerCertificate={'yes' if self.trust_server_certificate else 'no'};"
        )


@dataclass(frozen=True)
class PostgresTarget:
    host: str
    port: int
    database: str
    username: str
    password: str
    sslmode: str = "prefer"

    dialect: str = "postgres"

    def describe(self) -> str:
        return f"postgres:{self.database}"

    def dsn(self) -> str:
        """libpq keyword string — accepted by both ``psycopg`` (3) and ``psycopg2``."""
        return (
            f"host={self.host} port={self.port} dbname={self.database} "
            f"user={self.username} password={self.password} sslmode={self.sslmode}"
        )


Target = SqliteTarget | MssqlTarget | PostgresTarget

#: The runtime tuple behind the ``Target`` alias. Callers that must distinguish a target
#: object from a bare path use this instead of re-listing the classes.
TARGET_TYPES = (SqliteTarget, MssqlTarget, PostgresTarget)


def dialect_of(target: Target | str) -> Dialect:
    if isinstance(target, str):
        return dialect_for(target)
    if not isinstance(target, TARGET_TYPES):
        raise TypeError(f"未知数据库后端: {target!r}")
    return dialect_for(target.dialect)


def connect_readonly(target: Target | str | Path, *, pooled: bool = False) -> Any:
    """Open a read-only SQLite connection.

    SQLite uses ``file:...?mode=ro``; a bare path is still accepted so existing
    callers keep working. Server backends have no read-only URI mode — their
    read-only-ness comes from the login (deployment) plus the session statement below.

    ``pooled`` relaxes sqlite3's same-thread guard: a pooled handle is by definition
    handed to whichever thread checks it out. Exclusivity is the pool's job (one
    checkout at a time), not sqlite's.
    """
    if isinstance(target, (MssqlTarget, PostgresTarget)):
        raise TypeError(f"{type(target).__name__} 连接请使用 open_connection()；服务端没有只读 URI 模式")
    path = Path(target.path) if isinstance(target, SqliteTarget) else Path(target)
    if not path.exists():
        raise FileNotFoundError(f"数据库不存在: {path}")
    conn = sqlite3.connect(
        path.as_uri() + "?mode=ro",
        uri=True,
        timeout=5,
        check_same_thread=not pooled,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _connect_server(target: Target, timeout_ms: int) -> Any:
    """Open a server connection and apply the dialect's session statements.

    Statements run under autocommit (a ``SET`` inside a transaction that later rolls
    back would not persist), then autocommit is switched off so every query sits in a
    transaction that the pool rolls back on release.
    """
    dialect = dialect_of(target)
    if isinstance(target, MssqlTarget):
        try:
            import pyodbc  # noqa: PLC0415 - optional dependency, lazily imported
        except ImportError as exc:
            raise ModuleNotFoundError(
                "SQL Server 后端需要 pyodbc：pip install -e '.[mssql]'"
            ) from exc
        conn = pyodbc.connect(target.dsn(), timeout=max(1, int(timeout_ms / 1000)))
        conn.timeout = max(1, int(timeout_ms / 1000))
        return conn
    if isinstance(target, PostgresTarget):
        try:
            import psycopg  # noqa: PLC0415 - psycopg (3) preferred when present
        except ImportError:
            try:
                import psycopg2 as psycopg  # noqa: PLC0415 - same DBAPI surface we use
            except ImportError as exc:
                raise ModuleNotFoundError(
                    "PostgreSQL 后端需要 psycopg 或 psycopg2：pip install -e '.[postgres]'"
                ) from exc
        conn = psycopg.connect(target.dsn(), connect_timeout=max(1, int(timeout_ms / 1000)))
        _apply_session_setup(conn, dialect, timeout_ms)
        return conn
    raise TypeError(f"未知数据库后端: {target!r}")


def _apply_session_setup(conn: Any, dialect: Dialect, timeout_ms: int) -> None:
    statements = dialect.session_setup(timeout_ms)
    if not statements:
        return
    conn.autocommit = True
    cursor = conn.cursor()
    for statement in statements:
        cursor.execute(statement)
    conn.autocommit = False


def open_connection(target: Target, timeout_ms: int, *, pooled: bool = False):
    """Open a fresh connection. Returns ``(connection, backend_name)``."""
    if isinstance(target, SqliteTarget):
        return connect_readonly(target, pooled=pooled), "sqlite"
    if isinstance(target, MssqlTarget):
        return _connect_server(target, timeout_ms), "mssql"
    if isinstance(target, PostgresTarget):
        return _connect_server(target, timeout_ms), "postgres"
    raise TypeError(f"未知数据库后端: {target!r}")


def dialect_sql(
    target: Target, *, table: str, column: str, limit: int, pattern: str | None = None
) -> tuple[str, list[Any]]:
    """``SELECT DISTINCT <column>`` with the dialect's own row-cap syntax.

    The pattern is bound, never inlined — value lookup must not become a second
    injection surface next to the SQL the model writes.
    """
    return dialect_of(target).distinct_values(
        table=table, column=column, limit=limit, pattern=pattern
    )


class ConnectionPool:
    """A small fixed-capacity pool for one read-only target.

    Three properties matter more than throughput: a physical connection is opened at
    most ``max_size`` times per target; release **rolls back** rather than commits, so
    a pooled connection can never be the place where a write lands; and a connection
    that errored is discarded instead of returned to the pool, because an aborted
    transaction on PostgreSQL and a broken TDS pipe both poison the handle.
    """

    def __init__(
        self,
        target: Target,
        *,
        timeout_ms: int = 5000,
        max_size: int = 4,
        idle_ttl_sec: float = 300.0,
        checkout_wait_sec: float = 10.0,
    ) -> None:
        self.target = target
        self.timeout_ms = max(100, int(timeout_ms))
        self.max_size = max(1, int(max_size))
        self.idle_ttl_sec = float(idle_ttl_sec)
        self.checkout_wait_sec = float(checkout_wait_sec)
        self._idle: list[tuple[str, Any, float]] = []
        self._lock = threading.Lock()
        self._available = threading.Condition(self._lock)
        self._in_use = 0
        self._size = 0
        self._connects = 0
        self._checkouts = 0
        self._reuses = 0
        self._discards = 0
        self._closed = False

    def _evict_stale(self, now: float) -> None:
        keep: list[tuple[str, Any, float]] = []
        for item in self._idle:
            if self.idle_ttl_sec > 0 and now - item[2] > self.idle_ttl_sec:
                self._close_quietly(item[1])
                self._size -= 1
                self._discards += 1
            else:
                keep.append(item)
        self._idle = keep

    @staticmethod
    def _close_quietly(conn: Any) -> None:
        try:
            conn.close()
        except Exception:  # a connection we are throwing away cannot report anything useful
            pass

    def _new(self) -> tuple[str, Any]:
        conn, _ = open_connection(self.target, self.timeout_ms, pooled=True)
        self._connects += 1
        return observability.new_connection_id(), conn

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "target": self.target.describe(),
                "max_size": self.max_size,
                "size": self._size,
                "in_use": self._in_use,
                "connects": self._connects,
                "checkouts": self._checkouts,
                "reuses": self._reuses,
                "discards": self._discards,
                "closed": self._closed,
            }

    def checkout(self) -> tuple[str, Any]:
        deadline = time.monotonic() + self.checkout_wait_sec
        with self._available:
            while True:
                if self._closed:
                    raise RuntimeError(f"连接池已关闭: {self.target.describe()}")
                self._evict_stale(time.monotonic())
                if self._idle:
                    connection_id, conn, _ = self._idle.pop(0)
                    self._reuses += 1
                    break
                if self._size < self.max_size:
                    self._size += 1
                    try:
                        connection_id, conn = self._new()
                    except Exception:
                        self._size -= 1
                        raise
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"连接池 {self.target.describe()} 在 {self.checkout_wait_sec}s 内没有可用连接"
                        f"（上限 {self.max_size}）"
                    )
                self._available.wait(remaining)
            self._in_use += 1
            self._checkouts += 1
            return connection_id, conn

    def release(self, connection_id: str, conn: Any, *, broken: bool = False) -> None:
        with self._available:
            self._in_use -= 1
            if self._closed or broken:
                self._size -= 1
                self._discards += 1
                self._available.notify()
                self._close_quietly(conn)
                return
            try:
                # Never commit: the pooled handle only ever reads, and a rollback also
                # clears any aborted transaction left by a failed statement.
                conn.rollback()
            except Exception:
                self._size -= 1
                self._discards += 1
                self._available.notify()
                self._close_quietly(conn)
                return
            self._idle.append((connection_id, conn, time.monotonic()))
            self._available.notify()

    @contextmanager
    def connection(self) -> Iterator[tuple[str, Any]]:
        """Check out one connection; an exception inside the block discards it."""
        connection_id, conn = self.checkout()
        try:
            yield connection_id, conn
        except Exception:
            self.release(connection_id, conn, broken=True)
            raise
        else:
            self.release(connection_id, conn)

    def close(self) -> None:
        with self._available:
            self._closed = True
            idle, self._idle = self._idle, []
            self._size -= len(idle)
            self._available.notify_all()
        for _, conn, _ in idle:
            self._close_quietly(conn)


_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


def configure_pool(
    target: Target | str | Path,
    *,
    timeout_ms: int = 5000,
    max_size: int = 4,
    idle_ttl_sec: float = 300.0,
) -> ConnectionPool | None:
    """Register the pool for ``target``; ``max_size <= 1`` disables pooling and returns ``None``.

    Re-configuring replaces the pool instead of growing it: the old handle keeps serving
    whoever already checked it out, then closes.

    Coerced because ``query()`` accepts a bare path: a pool registered under a different
    key than the one the executor looks up would silently never be used.
    """
    target = coerce_target(target)
    key = target.describe()
    with _pools_lock:
        previous = _pools.pop(key, None)
        if max_size <= 1:
            fresh = None
        else:
            fresh = ConnectionPool(
                target, timeout_ms=timeout_ms, max_size=max_size, idle_ttl_sec=idle_ttl_sec
            )
            _pools[key] = fresh
    if previous is not None:
        previous.close()
    return fresh


def pool_for(target: Target | str | Path) -> ConnectionPool | None:
    with _pools_lock:
        return _pools.get(coerce_target(target).describe())


def pool_stats() -> list[dict[str, Any]]:
    with _pools_lock:
        return [pool.stats() for pool in _pools.values()]


def close_pools() -> None:
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


def _rows_to_lists(cursor, max_rows: int) -> tuple[list[str], list[list[Any]], bool, int]:
    columns = [desc[0] for desc in (cursor.description or [])]
    fetched = cursor.fetchmany(max_rows)
    truncated = cursor.fetchone() is not None
    rows = [list(row) for row in fetched]
    return columns, rows, truncated, len(rows)


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _payload(tables: tuple[list[str], list[list[Any]], bool, int], started: float) -> dict[str, Any]:
    columns, rows, truncated, row_count = tables
    return {
        "success": True,
        "columns": columns,
        "rows": to_jsonable(rows),
        "row_count": row_count,
        "truncated": truncated,
        "elapsed_ms": _elapsed(started),
    }


def _sqlite_failure(exc: Exception, timeout_ms: int, started: float) -> dict[str, Any]:
    if isinstance(exc, FileNotFoundError):
        return {"success": False, "error": str(exc), "error_code": "db_not_found", "elapsed_ms": _elapsed(started)}
    if isinstance(exc, sqlite3.OperationalError) and "interrupted" in str(exc).lower():
        return {
            "success": False,
            "error": f"查询超过 {timeout_ms}ms 被中止，请缩小扫描范围或加过滤条件",
            "error_code": "timeout",
            "elapsed_ms": _elapsed(started),
        }
    if isinstance(exc, sqlite3.Error):
        return {"success": False, "error": str(exc), "error_code": "sql_error", "elapsed_ms": _elapsed(started)}
    raise exc


def _server_failure(exc: Exception, timeout_ms: int, started: float) -> dict[str, Any]:
    error = str(exc)
    if isinstance(exc, ModuleNotFoundError):
        # Driver text stays verbatim: it is the one error whose message is the fix.
        return {"success": False, "error": error, "error_code": "driver_missing", "elapsed_ms": _elapsed(started)}
    timeout_hit = "timeout" in error.lower() or "timed out" in error.lower() or "statement_timeout" in error
    if timeout_hit:
        return {
            "success": False,
            "error": f"查询超过 {timeout_ms}ms 被中止，请缩小扫描范围或加过滤条件",
            "error_code": "timeout",
            "elapsed_ms": _elapsed(started),
        }
    # pyodbc/psycopg messages vary by driver version; keep the text verbatim.
    return {"success": False, "error": error, "error_code": "sql_error", "elapsed_ms": _elapsed(started)}


def _pool_failure(exc: Exception, started: float) -> dict[str, Any]:
    """Pool exhaustion/closure is an operational failure, so it gets its own error code.

    Reporting it as ``sql_error`` would send the model into a repair loop over SQL that
    was never the problem.
    """
    return {
        "success": False,
        "error": f"连接池不可用：{exc}",
        "error_code": "pool_unavailable",
        "elapsed_ms": _elapsed(started),
    }


def _exec_sqlite(conn: Any, sql: str, params: list[Any], *, max_rows: int, timeout_ms: int, started: float) -> dict[str, Any]:
    # SQLite has no server-side statement timeout, so the deadline is a client-side
    # progress handler: sqlite interrupts the statement at the next VM step past it.
    deadline = time.perf_counter() + timeout_ms / 1000.0
    conn.set_progress_handler(lambda: int(time.perf_counter() > deadline), 1000)
    cursor = conn.execute(sql, params) if params else conn.execute(sql)
    return _payload(_rows_to_lists(cursor, max_rows), started)


def _exec_dbapi(
    conn: Any, sql: str, params: list[Any], *, max_rows: int, started: float, always_bind: bool
) -> dict[str, Any]:
    cursor = conn.cursor()
    # ``always_bind`` is a driver difference, not a style choice: pyodbc accepts an empty
    # argument list, psycopg treats any non-None arguments as placeholder input and would
    # choke on a literal ``%`` in a parameterless statement.
    if params or always_bind:
        cursor.execute(sql, params)
    else:
        cursor.execute(sql)
    return _payload(_rows_to_lists(cursor, max_rows), started)


def _run_sqlite(
    target: SqliteTarget,
    sql: str,
    params: list[Any],
    *,
    max_rows: int,
    timeout_ms: int,
    started: float,
    meta: dict[str, Any],
) -> dict[str, Any]:
    pool = pool_for(target)
    if pool is None:
        # Opening the file is inside the try: a missing database has always come back as
        # a ``db_not_found`` tool result, not as an exception through the tool loop.
        conn = None
        try:
            conn = connect_readonly(target)
            meta["connection"] = observability.new_connection_id()
            return _exec_sqlite(conn, sql, params, max_rows=max_rows, timeout_ms=timeout_ms, started=started)
        except (FileNotFoundError, sqlite3.Error) as exc:
            return _sqlite_failure(exc, timeout_ms, started)
        finally:
            if conn is not None:
                conn.close()
    try:
        with pool.connection() as (connection_id, conn):
            meta["connection"] = connection_id
            meta["pooled"] = True
            return _exec_sqlite(conn, sql, params, max_rows=max_rows, timeout_ms=timeout_ms, started=started)
    except (FileNotFoundError, sqlite3.Error) as exc:
        return _sqlite_failure(exc, timeout_ms, started)
    except (TimeoutError, RuntimeError) as exc:
        return _pool_failure(exc, started)


def _run_server(
    target: Target,
    sql: str,
    params: list[Any],
    *,
    max_rows: int,
    timeout_ms: int,
    started: float,
    meta: dict[str, Any],
) -> dict[str, Any]:
    always_bind = isinstance(target, MssqlTarget)
    pool = pool_for(target)
    try:
        if pool is None:
            conn, _ = open_connection(target, timeout_ms)
            meta["connection"] = observability.new_connection_id()
            try:
                return _exec_dbapi(
                    conn, sql, params, max_rows=max_rows, started=started, always_bind=always_bind
                )
            finally:
                conn.close()
        with pool.connection() as (connection_id, conn):
            meta["connection"] = connection_id
            meta["pooled"] = True
            return _exec_dbapi(
                conn, sql, params, max_rows=max_rows, started=started, always_bind=always_bind
            )
    except ModuleNotFoundError as exc:
        return _server_failure(exc, timeout_ms, started)
    except (TimeoutError, RuntimeError) as exc:
        return _pool_failure(exc, started)
    except Exception as exc:  # a driver error is a tool result, never a stack trace
        return _server_failure(exc, timeout_ms, started)


def _run(
    target: Target,
    sql: str,
    params: list[Any] | None = None,
    *,
    max_rows: int = 200,
    timeout_ms: int = 5000,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a read-only statement and return the shared result contract.

    ``meta`` is filled with the physical connection id and whether it came from a pool.
    It stays out of the result dict on purpose: the tool contract is what the model and
    the evaluator see, and connection bookkeeping is an operational fact, not an answer.
    """
    max_rows = max(1, min(1000, int(max_rows)))
    timeout_ms = max(100, int(timeout_ms))
    # Same clock as the SQLite progress deadline above; perf_counter because a 15.6ms
    # tick granularity would make both the timing and the timeout coarse.
    started = time.perf_counter()
    params = list(params or [])
    sink = meta if meta is not None else {}
    target = coerce_target(target)
    if isinstance(target, SqliteTarget):
        return _run_sqlite(
            target, sql, params, max_rows=max_rows, timeout_ms=timeout_ms, started=started, meta=sink
        )
    if isinstance(target, (MssqlTarget, PostgresTarget)):
        return _run_server(
            target, sql, params, max_rows=max_rows, timeout_ms=timeout_ms, started=started, meta=sink
        )
    raise TypeError(f"未知数据库后端: {target!r}")


def coerce_target(target: Target | str | Path) -> Target:
    """A bare path still means "query that SQLite file", for older callers."""
    if isinstance(target, TARGET_TYPES):
        return target
    return SqliteTarget(Path(target))


def query(
    target: Target | str | Path,
    sql: str,
    params: list[Any] | None = None,
    *,
    max_rows: int = 200,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """``_run`` 加一条观测事件：数据库这一层的耗时与错误码才是 P95 的分母。

    SQL 全文默认不进日志（见 ``observability``），只留指纹；同一条语句慢在哪、
    失败在哪一类，都能凭 trace_id 与上游对上。连接号让"这条日志用的是第几个物理连接"
    可追——于是"池化到底有没有减少连接"是一个能从日志算出来的事实，不是配置里的一句话。
    """
    meta: dict[str, Any] = {}
    target = coerce_target(target)
    result = _run(target, sql, params, max_rows=max_rows, timeout_ms=timeout_ms, meta=meta)
    observability.log_db_call(
        dialect=target.dialect,
        target=target.describe(),
        ok=bool(result.get("success")),
        elapsed_ms=float(result.get("elapsed_ms") or 0.0),
        row_count=int(result.get("row_count") or 0),
        truncated=bool(result.get("truncated")),
        error_code=str(result.get("error_code") or ""),
        sql=sql,
        connection=str(meta.get("connection") or ""),
        pooled=bool(meta.get("pooled")),
    )
    return result
