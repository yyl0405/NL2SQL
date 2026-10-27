from __future__ import annotations

from nl2sql_agent.repair import SQLRepairer, classify_tsql_error


def test_clean_trailing_comma_and_semicolon() -> None:
    repairer = SQLRepairer()
    result = repairer.repair("SELECT order_id, FROM sales_order;")
    assert result.changed
    assert "clean_syntax" in result.applied
    assert result.sql == "SELECT order_id FROM sales_order"


def test_clean_nested_trailing_comma() -> None:
    result = SQLRepairer().repair("SELECT COALESCE(SUM(qty), 0,) AS total FROM order_item")
    assert result.sql == "SELECT COALESCE(SUM(qty), 0) AS total FROM order_item"


def test_remove_invalid_projection_keeps_other_clauses() -> None:
    sql = (
        "SELECT o.order_id, o.amount FROM sales_order o "
        "WHERE o.status = 'pending' ORDER BY o.order_id"
    )
    result = SQLRepairer().repair(sql, "no such column: o.amount")
    assert result.changed
    assert "remove_invalid_projection" in result.applied
    assert "o.amount" not in result.sql
    assert "WHERE o.status = 'pending'" in result.sql
    assert "ORDER BY o.order_id" in result.sql


def test_remove_invalid_projection_chinese_error() -> None:
    result = SQLRepairer().repair(
        "SELECT CustomerID, Email FROM Customer",
        "列名 'Email' 无效。",
    )
    assert "Email" not in result.sql
    assert "CustomerID" in result.sql


def test_does_not_remove_only_projection() -> None:
    sql = "SELECT o.amount FROM sales_order o"
    result = SQLRepairer().repair(sql, "no such column: o.amount")
    assert result.sql == sql
    assert not result.changed


def test_wrap_group_by_arithmetic_for_8120() -> None:
    sql = (
        "SELECT a.amount + b.tax AS total, a.region "
        "FROM a JOIN b ON a.id = b.id GROUP BY a.region"
    )
    result = SQLRepairer().repair(
        sql,
        "8120 - 选择列表中的列 'a.amount' 无效，因为该列没有包含在聚合函数或 GROUP BY 子句中。",
    )
    assert result.changed
    assert "wrap_group_by_expression" in result.applied
    assert "SUM(a.amount + b.tax)" in result.sql


def test_does_not_double_wrap_aggregate() -> None:
    sql = (
        "SELECT SUM(a.amount + b.tax) AS total, a.region "
        "FROM a JOIN b ON a.id = b.id GROUP BY a.region"
    )
    result = SQLRepairer().repair(sql, "8120 - 选择列表中的列无效")
    assert result.sql == sql
    assert not result.changed


def test_no_error_means_no_projection_changes() -> None:
    sql = "SELECT order_id FROM sales_order WHERE status = 'pending'"
    result = SQLRepairer().repair(sql)
    assert result.sql == sql
    assert not result.changed
    assert result.notes


def test_empty_sql_is_returned_unchanged() -> None:
    result = SQLRepairer().repair("   ")
    assert result.sql == ""
    assert not result.changed


def test_classify_tsql_error_only_matches_known_codes() -> None:
    assert classify_tsql_error("列名 'Email' 无效。 (207) (SQLExecDirectW)") == (207, "invalid_column")
    assert classify_tsql_error("'(102)'") == (102, "syntax")
    assert classify_tsql_error("8120 - 选择列表中的列无效") is None
    assert classify_tsql_error("no such column: amount") is None


def test_pyodbc_207_error_removes_only_the_invalid_projection() -> None:
    error = (
        "('42S22', \"[42S22] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"
        "列名 'Email' 无效。 (207) (SQLExecDirectW)\")"
    )
    result = SQLRepairer().repair("SELECT CustomerID, Email FROM Customer", error)
    assert result.error_code == 207
    assert result.error_kind == "invalid_column"
    assert result.applied == ["remove_invalid_projection"]
    assert result.sql == "SELECT CustomerID FROM Customer"


def test_pyodbc_102_error_only_cleans_syntax() -> None:
    error = "('42000', \"[42000] ... ',' 附近有语法错误。 (102) (SQLExecDirectW)\")"
    result = SQLRepairer().repair("SELECT order_id, FROM sales_order", error)
    assert result.error_code == 102
    assert result.error_kind == "syntax"
    assert result.applied == ["clean_syntax"]
    assert result.sql == "SELECT order_id FROM sales_order"


def test_syntax_error_does_not_trigger_projection_removal() -> None:
    sql = "SELECT order_id, amount FROM sales_order"
    result = SQLRepairer().repair(sql, "near \"amount\": syntax error (102)")
    assert result.error_kind == "syntax"
    assert "remove_invalid_projection" not in result.applied
    assert result.sql == sql


def test_8120_code_lets_an_english_message_reuse_the_wrap_rule() -> None:
    sql = (
        "SELECT a.amount + b.tax AS total, a.region "
        "FROM a JOIN b ON a.id = b.id GROUP BY a.region"
    )
    error = (
        "('42000', \"[42000] ... Column 'a.amount' is invalid in the select list "
        "because it is not contained in either an aggregate function or the GROUP BY "
        "clause. (8120)\")"
    )
    result = SQLRepairer().repair(sql, error)
    assert result.error_kind == "not_in_group_by"
    assert "wrap_group_by_expression" in result.applied
    assert "SUM(a.amount + b.tax)" in result.sql
