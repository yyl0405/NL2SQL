"""One table of dialect facts, shared by the guard, the prompts and the executor.

Before this module, four layers each kept their own copy of "which spellings mean
SQL Server": ``guard.py`` (shape rules), ``ast_guard.py`` (sqlglot read dialect),
the agent prompt and the dual-channel prompt. The copies did not agree. A catalog
declaring ``dialect: "tsql"`` was told off by the guard for using ``LIMIT`` while
the prompt described its database as *SQLite* — the prompt only recognised
``mssql``/``sqlserver``, so anything else fell through to the demo dialect.

Every field here is a *fact about the dialect*, not a policy: quoting characters,
how a row cap is spelled, which placeholder the driver binds with, what a session
timeout statement looks like. Callers decide what to do with them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Spellings seen in the wild (catalog JSON, sqlglot, driver docs) -> canonical name.
ALIASES: dict[str, str] = {
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "mssql": "tsql",
    "sqlserver": "tsql",
    "sql server": "tsql",
    "sql srvr": "tsql",
    "tsql": "tsql",
    "postgres": "postgres",
    "postgresql": "postgres",
    "pg": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
}

CANONICAL: tuple[str, ...] = ("sqlite", "tsql", "postgres", "mysql")


def normalize(name: str) -> str:
    """Canonical dialect name, or ``""`` for anything unknown.

    Unknown is deliberately not guessed: the guard sets no cross-dialect shape rule
    for a dialect it does not know, and a made-up label would print a lie in a report.
    """
    return ALIASES.get(str(name or "").strip().lower(), "")


@dataclass(frozen=True)
class Dialect:
    name: str
    label: str
    #: The dual-channel prompt names the grammar too ("SQL Server (T-SQL)"). Keeping it
    #: as data rather than folding it into ``label`` means unifying the dialect table does
    #: not silently rewrite a prompt that earlier measurements were taken against.
    channel_label: str
    quote_start: str
    quote_end: str
    placeholder: str
    text_cast: str
    order_by: str
    row_cap: str
    row_limit_hint: str
    forbidden: str
    paging: str
    timeout_sql: tuple[str, ...] = ()
    read_only_sql: tuple[str, ...] = ()

    def quote(self, name: Any) -> str:
        # Bracket identifiers escape by doubling the *closing* character (``]]``);
        # a doubled opening bracket is not an escape in T-SQL and would change the name.
        identifier = str(name)
        return self.quote_start + identifier.replace(self.quote_end, self.quote_end * 2) + self.quote_end

    def bind(self, position: int = 0) -> str:
        """Placeholder for the ``position``-th parameter, in this dialect's driver style."""
        if self.placeholder == "format":
            return "%s"
        if self.placeholder == "numeric":
            return f":{position + 1}"
        return "?"

    def distinct_values(
        self, *, table: str, column: str, limit: int, pattern: str | None = None
    ) -> tuple[str, list[Any]]:
        """``SELECT DISTINCT <column>`` capped the dialect's own way, with bound parameters.

        The search pattern is always a bound parameter — never inlined text — so value
        lookup cannot become a second injection surface next to the model's SQL.
        """
        q = self.quote
        cast = self.text_cast
        capped = int(limit)
        selected = f"CAST({q(column)} AS {cast}) AS value"
        head = (
            f"SELECT DISTINCT TOP ({capped}) {selected}"
            if self.row_cap == "top"
            else f"SELECT DISTINCT {selected}"
        )
        sql = f"{head} FROM {q(table)} WHERE {q(column)} IS NOT NULL"
        params: list[Any] = []
        if pattern:
            sql += f" AND CAST({q(column)} AS {cast}) LIKE {self.bind()}"
            params.append(f"%{pattern}%")
        sql += f" ORDER BY {self.order_by}"
        if self.row_cap != "top":
            sql += f" LIMIT {capped}"
        return sql, params

    def session_setup(self, timeout_ms: int) -> tuple[str, ...]:
        """Statements to run on a fresh (or checked-out) connection.

        Read-only first, then the timeout: on PostgreSQL both are session-level, so a
        pooled connection keeps them across checkouts and a statement can never run
        without a deadline.
        """
        statements = list(self.read_only_sql)
        if self.timeout_sql:
            statements.extend(statement.format(ms=max(1, int(timeout_ms))) for statement in self.timeout_sql)
        return tuple(statements)


SQLITE = Dialect(
    name="sqlite",
    label="SQLite",
    channel_label="SQLite",
    quote_start='"',
    quote_end='"',
    placeholder="qmark",
    text_cast="TEXT",
    order_by="value",
    row_cap="limit",
    row_limit_hint="必要时加 LIMIT",
    forbidden="禁止 SELECT *、写操作、多语句、DDL、PRAGMA、ATTACH",
    paging="LIMIT n",
)

TSQL = Dialect(
    name="tsql",
    label="SQL Server",
    channel_label="SQL Server (T-SQL)",
    quote_start="[",
    quote_end="]",
    placeholder="qmark",
    text_cast="NVARCHAR(4000)",
    order_by="1",
    row_cap="top",
    row_limit_hint="必要时加 TOP n",
    forbidden="禁止 SELECT *、写操作、多语句、DDL、EXEC、临时表、OFFSET/FETCH 分页（2008 R2 不支持）",
    paging="TOP n（2008 R2 不支持 OFFSET/FETCH）",
    timeout_sql=("SET LOCK_TIMEOUT {ms}",),
)

POSTGRES = Dialect(
    name="postgres",
    label="PostgreSQL",
    channel_label="PostgreSQL (PL/pgSQL)",
    quote_start='"',
    quote_end='"',
    placeholder="format",
    text_cast="TEXT",
    order_by="value",
    row_cap="limit",
    row_limit_hint="必要时加 LIMIT",
    forbidden="禁止 SELECT *、写操作、多语句、DDL、COPY、系统目录查询",
    paging="LIMIT n OFFSET m",
    timeout_sql=("SET statement_timeout = {ms}",),
    read_only_sql=("SET default_transaction_read_only = on",),
)

MYSQL = Dialect(
    name="mysql",
    label="MySQL",
    channel_label="MySQL",
    quote_start="`",
    quote_end="`",
    placeholder="format",
    text_cast="CHAR",
    order_by="value",
    row_cap="limit",
    row_limit_hint="必要时加 LIMIT",
    forbidden="禁止 SELECT *、写操作、多语句、DDL、LOCK IN SHARE MODE",
    paging="LIMIT n OFFSET m",
    # MySQL is a catalog dialect only: no executor target exists for it here, so it gets
    # no session statements. Wiring one up would mean shipping an untested driver path.
)

REGISTRY: dict[str, Dialect] = {
    item.name: item for item in (SQLITE, TSQL, POSTGRES, MYSQL)
}


def unknown(raw: str) -> Dialect:
    """A dialect nobody registered: neutral prompt text, no executor.

    The label echoes what the catalog said instead of falling back to a known one,
    because a wrong label is the exact bug this module exists to remove.
    """
    name = str(raw or "").strip() or "未知方言"
    return Dialect(
        name="",
        label=name,
        channel_label=name,
        quote_start='"',
        quote_end='"',
        placeholder="qmark",
        text_cast="TEXT",
        order_by="value",
        row_cap="limit",
        row_limit_hint="必要时限制返回行数",
        forbidden="禁止 SELECT *、写操作、多语句、DDL",
        paging="未知",
    )


def get(name: str) -> Dialect:
    """Dialect for prompt/executor use; unknown spellings get the neutral profile."""
    canonical = normalize(name)
    return REGISTRY[canonical] if canonical else unknown(name)
