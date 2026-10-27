"""One dialect table, four facts about it, and the bug its absence caused.

Before ``dialects.py`` each layer kept its own list of "which spellings mean SQL
Server": the guard's shape rules, sqlglot's read dialect, the agent prompt and the
dual-channel prompt. The prompt only recognised ``mssql``/``sqlserver``, so a catalog
declaring ``dialect: "tsql"`` was rejected by the guard for writing ``LIMIT`` while its
own prompt told the model the database was *SQLite*. These tests pin the agreed facts
and, more importantly, pin the prompt strings that every published number was
measured against — unifying the table must not silently rewrite a prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nl2sql_agent.agent import NL2SQLAgent
from nl2sql_agent.catalog import Catalog, Column, Table
from nl2sql_agent.config import Settings
from nl2sql_agent.dialects import (
    CANONICAL,
    REGISTRY,
    get,
    normalize,
    unknown,
)
from nl2sql_agent.ast_guard import map_dialect
from nl2sql_agent.guard import SQLGuard
from nl2sql_agent.tools.base import ToolRegistry
from nl2sql_agent.tools.db import (
    MssqlTarget,
    PostgresTarget,
    SqliteTarget,
    dialect_of,
    dialect_sql,
)
from nl2sql_agent.voting import LLMSecondOpinion


def catalog(dialect: str) -> Catalog:
    return Catalog(
        domain="方言测试",
        dialect=dialect,
        as_of_date="2026-07-01",
        tables={
            "orders": Table(
                name="orders",
                description="订单主表",
                columns=(
                    Column(name="id", type="int", description="订单标识"),
                    Column(name="amount", type="decimal", description="金额"),
                    Column(name="region", type="varchar", description="区域"),
                ),
            )
        },
        business_rules=["金额只统计未取消订单"],
    )


def system_prompt(dialect: str) -> str:
    agent = NL2SQLAgent(catalog(dialect), None, ToolRegistry(), Settings())  # type: ignore[arg-type]
    return agent._system_prompt()


def channel_prompt(dialect: str) -> str:
    return LLMSecondOpinion(None, catalog(dialect), Settings())._prompt(["orders"])  # type: ignore[arg-type]


def test_alias_spellings_all_reach_one_canonical_name() -> None:
    for raw in ("mssql", "sqlserver", "SQL Server", "tsql"):
        assert normalize(raw) == "tsql", raw
    for raw in ("postgres", "postgresql", "PG"):
        assert normalize(raw) == "postgres", raw
    for raw in ("sqlite", "sqlite3"):
        assert normalize(raw) == "sqlite", raw


def test_unknown_engine_is_not_guessed() -> None:
    assert normalize("oracle") == ""
    assert normalize("") == ""
    profile = get("oracle")
    assert profile.name == ""
    # Echoing what the catalog said is the point: falling back to a known label is the
    # exact lie this module removes.
    assert profile.label == "oracle"
    assert "SQLite" not in (profile.label + profile.row_limit_hint)


@pytest.mark.parametrize("canonical", CANONICAL)
def test_every_registered_dialect_is_fully_described(canonical: str) -> None:
    profile = REGISTRY[canonical]
    assert profile.name == canonical
    for field_name in ("label", "channel_label", "row_limit_hint", "forbidden", "paging"):
        assert getattr(profile, field_name), f"{canonical}.{field_name} 为空"
    assert profile.quote_start and profile.quote_end


def test_tsql_and_postgres_shape_rules_agree_with_their_prompts() -> None:
    # The bug: LIMIT was a guard error for a tsql catalog whose prompt said SQLite.
    # Both halves must now name the same engine, and the AST parser must read the same
    # grammar as the guard — one table, three consumers.
    for dialect in ("tsql", "mssql", "sqlserver"):
        assert "数据库: SQL Server（只读）" in system_prompt(dialect)
        assert "SQLite" not in system_prompt(dialect)
        assert map_dialect(dialect) == "tsql"
    for dialect in ("postgres", "postgresql"):
        assert "数据库: PostgreSQL（只读）" in system_prompt(dialect)
    guard_report = SQLGuard(catalog("tsql")).check(
        "SELECT id FROM orders LIMIT 10"
    )
    assert any(item.code == "dialect_limit" for item in guard_report.issues), guard_report.issues


def test_unregistered_engine_is_passed_through_to_the_parser_not_guessed() -> None:
    # sqlglot knows engines this project has no backend for; falling back to "sqlite"
    # there would silently parse the wrong grammar.
    assert map_dialect("oracle") == "oracle"
    assert map_dialect("") == "sqlite"


def test_sqlite_and_mssql_prompt_lines_are_unchanged_by_the_refactor() -> None:
    # Published EX / guard-check numbers were measured against these exact strings.
    sqlite_prompt = system_prompt("sqlite")
    assert "数据库: SQLite（只读）" in sqlite_prompt
    assert "只生成单条 SQLite SELECT/WITH 查询。列必须显式列出；金额保留 2 位，比率保留 4 位；必要时加 LIMIT。" in sqlite_prompt
    assert "- 禁止 SELECT *、写操作、多语句、DDL、PRAGMA、ATTACH。" in sqlite_prompt
    mssql_prompt = system_prompt("mssql")
    assert "数据库: SQL Server（只读）" in mssql_prompt
    assert "只生成单条 SQL Server SELECT/WITH 查询。列必须显式列出；金额保留 2 位，比率保留 4 位；必要时加 TOP n。" in mssql_prompt
    assert (
        "- 禁止 SELECT *、写操作、多语句、DDL、EXEC、临时表、OFFSET/FETCH 分页（2008 R2 不支持）。"
        in mssql_prompt
    )


def test_dual_channel_prompt_keeps_its_grammar_suffix() -> None:
    # ``channel_label`` exists so joining the dialect table does not rewrite this line:
    # the P3 voting measurements were taken against "SQL Server (T-SQL)", not "SQL Server".
    assert "自己写出一条 SQL Server (T-SQL) 只读 SELECT" in channel_prompt("mssql")
    assert "必要时加 TOP n" in channel_prompt("mssql")
    assert "自己写出一条 SQLite 只读 SELECT" in channel_prompt("sqlite")
    assert "PostgreSQL (PL/pgSQL)" in channel_prompt("postgres")
    assert "oracle" in channel_prompt("oracle")
    assert "SQLite" not in channel_prompt("oracle")


def test_bracket_identifiers_escape_by_doubling_the_closing_character() -> None:
    assert get("tsql").quote("a]b") == "[a]]b]"
    assert get("sqlite").quote('a"b') == '"a""b"'
    assert get("mysql").quote("a`b") == "`a``b`"
    assert get("postgres").quote("order") == '"order"'


def test_placeholder_style_follows_the_driver() -> None:
    assert [get("sqlite").bind(0), get("tsql").bind(1)] == ["?", "?"]
    assert [get("postgres").bind(0), get("mysql").bind(1)] == ["%s", "%s"]


def test_distinct_value_lookup_caps_rows_the_dialects_own_way() -> None:
    tsql_sql, tsql_params = dialect_sql(
        MssqlTarget("srv", 1433, "db", "ro", "secret"), table="orders", column="region", limit=20, pattern="华"
    )
    assert "SELECT DISTINCT TOP (20)" in tsql_sql
    assert "LIMIT" not in tsql_sql
    assert "[region] IS NOT NULL" in tsql_sql
    assert tsql_params == ["%华%"]
    assert "%华%" not in tsql_sql, "搜索词被内联进 SQL：值查找成了第二个注入面"

    pg_sql, pg_params = dialect_sql(
        PostgresTarget("srv", 5432, "db", "ro", "secret"), table="orders", column="region", limit=20
    )
    assert "SELECT DISTINCT" in pg_sql and "LIMIT 20" in pg_sql
    assert "TOP" not in pg_sql
    assert '"region"' in pg_sql
    assert pg_params == []


def test_target_backends_map_to_the_same_canonical_dialect_as_the_catalog() -> None:
    assert dialect_of(SqliteTarget(Path("var/demo_erp.db"))).name == "sqlite"
    assert dialect_of(MssqlTarget("s", 1, "d", "u", "p")).name == "tsql"
    assert dialect_of(PostgresTarget("s", 1, "d", "u", "p")).name == "postgres"
    assert dialect_of("mssql").name == "tsql"
    with pytest.raises(TypeError):
        dialect_of(object())  # type: ignore[arg-type]


def test_session_setup_runs_read_only_before_the_timeout() -> None:
    assert get("sqlite").session_setup(5000) == ()
    assert get("tsql").session_setup(5000) == ("SET LOCK_TIMEOUT 5000",)
    assert get("postgres").session_setup(5000) == (
        "SET default_transaction_read_only = on",
        "SET statement_timeout = 5000",
    )
    # A zero/negative deadline would mean "no timeout"; the floor keeps every statement bounded.
    assert get("postgres").session_setup(0)[-1] == "SET statement_timeout = 1"


def test_mysql_stays_a_catalog_dialect_with_no_session_statements() -> None:
    # Registered for prompts and parsing; there is no MySQL executor here, so inventing
    # session SQL would ship an untested driver path.
    assert get("mysql").session_setup(5000) == ()
    assert unknown("mysql") is not get("mysql")
