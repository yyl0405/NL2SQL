from __future__ import annotations

import builtins

import pytest

from nl2sql_agent.ast_guard import AstAnalyzer, AstReport, map_dialect
from nl2sql_agent.catalog import Catalog
from nl2sql_agent.guard import SQLGuard


def _require_sqlglot() -> None:
    pytest.importorskip("sqlglot")


def _codes(result) -> set[str]:
    return {issue.code for issue in result.errors}


def _block_sqlglot(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "sqlglot" or name.startswith("sqlglot."):
            raise ImportError("No module named 'sqlglot'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_map_dialect() -> None:
    assert map_dialect("mssql") == "tsql"
    assert map_dialect("SQLServer") == "tsql"
    assert map_dialect("") == "sqlite"
    assert map_dialect("sqlite3") == "sqlite"
    assert map_dialect("postgres") == "postgres"


def test_plain_select_has_no_issues() -> None:
    _require_sqlglot()
    report = AstAnalyzer("sqlite").analyze(
        "SELECT o.order_id, o.region FROM sales_order o WHERE o.region = '华东'"
    )
    assert report.usable
    assert report.statements == 1
    assert report.kinds == ["select"]
    assert report.write_nodes == []
    assert [table.qualified for table in report.real_tables] == ["sales_order"]
    analyzer = AstAnalyzer("sqlite")
    assert analyzer.issues(report, known_tables=["sales_order"]) == []


def test_count_star_is_not_a_projection_star() -> None:
    _require_sqlglot()
    report = AstAnalyzer("sqlite").analyze("SELECT COUNT(*) AS c FROM sales_order")
    assert report.star_projections == []


def test_projection_stars_are_reported() -> None:
    _require_sqlglot()
    analyzer = AstAnalyzer("sqlite")
    assert analyzer.analyze("SELECT * FROM sales_order").star_projections == ["*"]
    assert analyzer.analyze("SELECT s.* FROM sales_order s").star_projections == ["s.*"]


def test_cte_alias_is_not_a_real_table() -> None:
    _require_sqlglot()
    report = AstAnalyzer("sqlite").analyze(
        "WITH x AS (SELECT o.order_id FROM sales_order o) SELECT COUNT(x.order_id) AS c FROM x"
    )
    assert report.ctes == ["x"]
    assert [table.qualified for table in report.real_tables] == ["sales_order"]


def test_write_and_ddl_nodes_are_detected() -> None:
    _require_sqlglot()
    analyzer = AstAnalyzer("sqlite")
    assert "drop" in analyzer.analyze("DROP TABLE sales_order").write_nodes
    assert "insert" in analyzer.analyze(
        "INSERT INTO sales_order (order_id) VALUES ('X')"
    ).write_nodes
    assert "pragma" in analyzer.analyze("PRAGMA journal_mode = wal").write_nodes
    tsql = AstAnalyzer("mssql")
    assert "into" in tsql.analyze("SELECT o.order_id INTO #tmp FROM sales_order o").write_nodes
    assert "execute" in tsql.analyze("EXEC('DELETE FROM material')").write_nodes


def test_dangerous_functions_are_detected_in_both_dialects() -> None:
    _require_sqlglot()
    assert AstAnalyzer("sqlite").analyze(
        "SELECT randomblob(100000000) AS payload FROM material"
    ).dangerous == ["randomblob("]
    assert AstAnalyzer("mssql").analyze(
        "SELECT xp_cmdshell('dir') AS out FROM material"
    ).dangerous == ["xp_cmdshell("]
    # One flat set on purpose: an escalation function is escalation whichever
    # dialect it is written in.
    assert AstAnalyzer("mssql").analyze("SELECT LOAD_EXTENSION('x')").dangerous == ["LOAD_EXTENSION("]


def test_metadata_access_is_detected() -> None:
    _require_sqlglot()
    assert AstAnalyzer("sqlite").analyze(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).metadata_access == ["sqlite_master"]
    assert AstAnalyzer("mssql").analyze(
        "SELECT TABLE_NAME FROM information_schema.tables"
    ).metadata_access == ["information_schema.tables"]


def test_multi_statement_is_detected() -> None:
    _require_sqlglot()
    report = AstAnalyzer("sqlite").analyze("SELECT o.order_id FROM sales_order o; DROP TABLE material")
    assert report.statements == 2
    codes = {code for _, code, _ in AstAnalyzer("sqlite").issues(report)}
    assert {"multi_statement", "write_keyword"} <= codes


def test_unparseable_sql_reports_parse_failed() -> None:
    _require_sqlglot()
    analyzer = AstAnalyzer("sqlite")
    for sql in ("SELECT o.order_id FROM ((( sales_order o", "!!! not sql at all (((("):
        report = analyzer.analyze(sql)
        assert report.status == "parse_failed"
        assert report.error
        assert analyzer.issues(report) == [
            (
                "error",
                "ast_parse_failed",
                f"AST 护栏无法解析该语句，按 fail-closed 拒绝执行：{report.error}",
            )
        ]


def test_oversized_sql_is_not_handed_to_the_parser() -> None:
    _require_sqlglot()
    analyzer = AstAnalyzer("sqlite", max_sql_chars=64)
    long_sql = "SELECT o.order_id FROM sales_order o WHERE o.region IN (" + "'x'," * 40 + ")"
    report = analyzer.analyze(long_sql)
    assert report.status == "parse_failed"


def test_missing_sqlglot_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_sqlglot(monkeypatch)
    report = AstAnalyzer("sqlite").analyze("SELECT 1")
    assert report.status == "unavailable"
    codes = {code for level, code, _ in AstAnalyzer("sqlite").issues(report)}
    assert codes == {"ast_unavailable"}


def test_analyzer_never_raises() -> None:
    analyzer = AstAnalyzer("sqlite")
    for sql in ("", "   ", "SELECT", ";", "SELECT 'unterminated", "[[["):
        assert analyzer.analyze(sql).status in {"ok", "parse_failed", "unavailable"}


def test_report_to_dict_is_flat_enough_for_logs() -> None:
    report = AstReport(status="ok", dialect="sqlite", tables=[])
    payload = report.to_dict()
    assert payload["status"] == "ok"
    assert payload["tables"] == []
    assert payload["statements"] == 0


def test_guard_blocks_nospace_from_in_both_layers_with_different_diagnosis(
    catalog: Catalog,
) -> None:
    """`FROM[t]` is blocked twice, but only the parser says why it matters.

    The regex layer used to wave this through (no space after FROM, so no table token),
    which made it an AST-only catch. Seeing `[ ]` on a dialect that does not quote that
    way is now enough to block it on the zero-dependency path; the AST still supplies the
    real diagnosis, `internal_audit` is a disabled table.
    """
    _require_sqlglot()
    sql = "SELECT a.created_at FROM[internal_audit] a"
    regex_only = SQLGuard(catalog).check(sql)
    assert not regex_only.valid
    assert "dialect_bracket_quote" in _codes(regex_only)
    assert "disabled_table" not in _codes(regex_only)
    result = SQLGuard(catalog, analyzer=AstAnalyzer("sqlite")).check(sql)
    assert not result.valid
    assert "disabled_table" in _codes(result)
    assert result.ast["tables"] == ["internal_audit"]


def test_guard_reports_metadata_access_that_regex_misdiagnoses(catalog: Catalog) -> None:
    _require_sqlglot()
    sql = "SELECT name FROM sqlite_master WHERE type = 'table'"
    assert "unknown_table" in _codes(SQLGuard(catalog).check(sql))
    result = SQLGuard(catalog, analyzer=AstAnalyzer("sqlite")).check(sql)
    assert "metadata_access" in _codes(result)


def test_guard_ast_payload_is_attached_only_when_enabled(catalog: Catalog) -> None:
    _require_sqlglot()
    assert SQLGuard(catalog).check("SELECT o.order_id FROM sales_order o").ast == {}
    result = SQLGuard(catalog, analyzer=AstAnalyzer("sqlite")).check(
        "SELECT o.order_id FROM sales_order o"
    )
    assert result.ast["status"] == "ok"
    assert result.ast["kinds"] == ["select"]


def test_guard_does_not_duplicate_select_star(catalog: Catalog) -> None:
    _require_sqlglot()
    result = SQLGuard(catalog, analyzer=AstAnalyzer("sqlite")).check("SELECT * FROM sales_order")
    assert [issue.code for issue in result.errors].count("select_star") == 1


def test_guard_fail_closed_when_ast_enabled_but_sqlglot_missing(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_sqlglot(monkeypatch)
    result = SQLGuard(catalog, analyzer=AstAnalyzer("sqlite")).check(
        "SELECT o.order_id FROM sales_order o"
    )
    assert not result.valid
    assert "ast_unavailable" in _codes(result)


def test_guard_fail_closed_on_unparseable_sql(catalog: Catalog) -> None:
    _require_sqlglot()
    result = SQLGuard(catalog, analyzer=AstAnalyzer("sqlite")).check(
        "SELECT o.order_id FROM ((( sales_order o"
    )
    assert "ast_parse_failed" in _codes(result)


def test_ast_guard_switch_is_off_by_default(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nl2sql_agent.config import Settings

    monkeypatch.delenv("NL2SQL_AST_GUARD", raising=False)
    assert Settings(home=tmp_path).ast_guard_enabled is False
    assert Settings(home=tmp_path, ast_guard="on").ast_guard_enabled is True
    assert Settings(home=tmp_path, ast_guard="off").ast_guard_enabled is False


def test_runtime_rejects_unknown_ast_guard_value(tmp_path) -> None:
    from nl2sql_agent.config import Settings
    from nl2sql_agent.runtime import create_runtime

    with pytest.raises(ValueError, match="NL2SQL_AST_GUARD"):
        create_runtime(Settings(home=tmp_path, provider="mock", ast_guard="sometimes"))
