from __future__ import annotations

from nl2sql_agent.repair import SQLRepairer


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
