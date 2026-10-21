from __future__ import annotations

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.guard import SQLGuard, sql_has_select_star

VALID_SQL = (
    "SELECT o.region, SUM(oi.qty * oi.unit_price * (1 - oi.discount)) AS amount "
    "FROM sales_order o JOIN order_item oi ON oi.order_id = o.order_id "
    "WHERE o.order_date >= '2026-06-01' GROUP BY o.region"
)


def _codes(result) -> set[str]:
    return {issue.code for issue in result.errors}


def test_valid_query_passes(guard: SQLGuard) -> None:
    result = guard.check(VALID_SQL)
    assert result.valid
    assert result.tables == ["order_item", "sales_order"]
    assert "sales_order.order_id" in result.columns


def test_select_star_is_rejected(guard: SQLGuard) -> None:
    assert "select_star" in _codes(guard.check("SELECT * FROM material"))
    assert "select_star" in _codes(guard.check("SELECT m.* FROM material m"))
    assert guard.check("SELECT COUNT(*) FROM sales_order").valid


def test_write_and_ddl_are_rejected(guard: SQLGuard) -> None:
    for sql in (
        "DELETE FROM sales_order",
        "UPDATE sales_order SET status = 'closed'",
        "DROP TABLE sales_order",
        "PRAGMA table_info(sales_order)",
        "ATTACH DATABASE 'x.db' AS x",
    ):
        result = guard.check(sql)
        assert not result.valid
        assert {"not_select", "write_keyword"} & _codes(result)


def test_multi_statement_is_rejected(guard: SQLGuard) -> None:
    result = guard.check("SELECT order_id FROM sales_order; SELECT order_id FROM sales_order")
    assert "multi_statement" in _codes(result)


def test_trailing_semicolon_is_allowed(guard: SQLGuard) -> None:
    result = guard.check("SELECT order_id FROM sales_order;")
    assert result.valid
    assert not result.sql.endswith(";")


def test_disabled_table_is_rejected(guard: SQLGuard) -> None:
    result = guard.check("SELECT audit_id FROM internal_audit")
    assert "disabled_table" in _codes(result)
    assert "internal_audit" not in result.tables


def test_unknown_table_is_rejected(guard: SQLGuard) -> None:
    assert "unknown_table" in _codes(guard.check("SELECT id FROM not_a_table"))


def test_sensitive_and_disabled_columns_are_rejected(catalog: Catalog) -> None:
    guard = SQLGuard(catalog)
    phone = guard.check("SELECT customer_phone FROM sales_order")
    assert "sensitive_column" in _codes(phone)
    note = guard.check("SELECT internal_note FROM sales_order")
    assert "disabled_column" in _codes(note)
    permissive = SQLGuard(catalog, allow_sensitive=True)
    assert permissive.check("SELECT customer_phone FROM sales_order").valid


def test_unknown_qualified_column_suggests_name(guard: SQLGuard) -> None:
    result = guard.check("SELECT o.custmer_name FROM sales_order o")
    assert not result.valid
    issue = next(issue for issue in result.errors if issue.code == "unknown_column")
    assert "customer_name" in issue.message


def test_alias_columns_are_resolved(guard: SQLGuard) -> None:
    assert guard.check("SELECT o.order_id FROM sales_order AS o").valid
    result = guard.check("SELECT o.customer_name FROM sales_order AS o WHERE o.region = '华东'")
    assert result.valid
    assert "sales_order.customer_name" in result.columns
    assert "sales_order.region" in result.columns


def test_required_filter_is_enforced(guard: SQLGuard) -> None:
    missing = guard.check("SELECT lookup_code, lookup_name FROM dim_lookup")
    assert "required_filter" in _codes(missing)
    present = guard.check(
        "SELECT lookup_code, lookup_name FROM dim_lookup WHERE lookup_type = 'material_category'"
    )
    assert present.valid


def test_multiple_ctes_are_recognized(guard: SQLGuard) -> None:
    sql = (
        "WITH RECURSIVE tree(parent_id, child_id, qty, level) AS ("
        "SELECT b.parent_material_id, b.child_material_id, b.qty_per_parent, 1 FROM bom b "
        "WHERE b.parent_material_id = 'FG001' UNION ALL "
        "SELECT t.child_id, b.child_material_id, t.qty * b.qty_per_parent, t.level + 1 "
        "FROM tree t JOIN bom b ON b.parent_material_id = t.child_id), "
        "req AS (SELECT material_id, SUM(qty) AS required_qty FROM tree GROUP BY material_id) "
        "SELECT r.material_id, r.required_qty FROM req r"
    )
    result = guard.check(sql)
    assert result.valid, [issue.message for issue in result.errors]
    assert result.tables == ["bom"]


def test_comma_separated_from_tables_are_extracted(guard: SQLGuard) -> None:
    result = guard.check(
        "SELECT m.material_id FROM material m, inventory i WHERE m.material_id = i.material_id"
    )
    assert result.valid
    assert result.tables == ["inventory", "material"]


def test_comments_and_string_literals_do_not_trigger_keywords(guard: SQLGuard) -> None:
    commented = guard.check("SELECT order_id FROM sales_order -- DROP TABLE sales_order")
    assert commented.valid
    literal = guard.check("SELECT order_id FROM sales_order WHERE status = 'update'")
    assert literal.valid


def test_empty_and_oversized_sql(guard: SQLGuard) -> None:
    assert "empty_sql" in _codes(guard.check("   "))
    assert "sql_too_long" in _codes(guard.check("SELECT " + "a" * 20001))


def test_sql_has_select_star_helper() -> None:
    assert sql_has_select_star("SELECT * FROM t")
    assert sql_has_select_star("select top 10 * from t")
    assert not sql_has_select_star("SELECT COUNT(*) FROM t")
    assert not sql_has_select_star("SELECT a FROM t")
