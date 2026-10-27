from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

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


def test_default_filter_warns_without_blocking(guard: SQLGuard, catalog) -> None:
    """A house convention must not become an authority boundary.

    ``required_filters`` blocks execution; ``default_filters`` only tells the model
    the usual predicate, so "含已删除的合同" and other deliberate full-table reads
    stay answerable.
    """
    order = catalog.table("sales_order")
    tables = dict(catalog.tables)
    tables[order.name.lower()] = replace(
        order,
        default_filters=({"column": "region", "value": "华东", "reason": "默认只看华东"},),
    )
    check = SQLGuard(replace(catalog, tables=tables))

    missing = check.check("SELECT o.order_id FROM sales_order o")
    assert missing.valid
    assert "default_filter_missing" in {issue.code for issue in missing.warnings}
    assert "default_filter_missing" not in {issue.code for issue in missing.errors}

    present = check.check("SELECT o.order_id FROM sales_order o WHERE o.region = '华东'")
    assert present.valid
    assert "default_filter_missing" not in {issue.code for issue in present.warnings}

    untouched = SQLGuard(catalog).check("SELECT o.order_id FROM sales_order o")
    assert "default_filter_missing" not in {issue.code for issue in untouched.warnings}


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


def test_mssql_table_hint_allow_list(catalog: Catalog) -> None:
    guard = SQLGuard(replace(catalog, dialect="mssql"))
    sql = (
        "SELECT o.order_id, o.region FROM sales_order o WITH (NOLOCK) "
        "WHERE o.status = 'pending'"
    )
    assert guard.check(sql).valid
    rejected = guard.check(sql.replace("NOLOCK", "UPDLOCK"))
    assert "table_hint" in _codes(rejected)
    assert "UPDLOCK" in rejected.errors[0].message


def test_table_hints_are_rejected_on_sqlite(catalog: Catalog) -> None:
    result = SQLGuard(catalog).check("SELECT o.order_id FROM sales_order o WITH (NOLOCK)")
    assert "table_hint" in _codes(result)
    assert "sqlite" in result.errors[0].message


def test_mssql_offset_fetch_is_rejected(catalog: Catalog) -> None:
    guard = SQLGuard(replace(catalog, dialect="mssql"))
    sql = (
        "SELECT o.order_id FROM sales_order o ORDER BY o.order_date "
        "OFFSET 10 ROWS FETCH NEXT 10 ROWS ONLY"
    )
    assert "offset_fetch" in _codes(guard.check(sql))


def test_cte_with_is_not_mistaken_for_a_table_hint(guard: SQLGuard) -> None:
    sql = (
        "WITH recent AS (SELECT order_id FROM sales_order WHERE status = 'pending') "
        "SELECT r.order_id FROM recent r"
    )
    assert guard.check(sql).valid


def test_backquoted_identifiers_do_not_bypass_table_checks(guard: SQLGuard) -> None:
    """``FROM `internal_audit` `` used to be invisible to the tokenizer.

    Bracket- and double-quoted identifiers were already unquoted before the table scan;
    backticks were not, so a backquoted query produced zero table references and every
    table-scoped rule (disabled table, unknown table, required filters) silently passed.
    """
    blocked = guard.check("SELECT `order_id` FROM `internal_audit`")
    assert "disabled_table" in _codes(blocked)
    catalog_probe = guard.check("SELECT `t`.`name` FROM `sqlite_master` AS `t`")
    assert "unknown_table" in _codes(catalog_probe)


def test_backquoted_qualified_columns_still_get_column_checks(guard: SQLGuard) -> None:
    leaked = guard.check("SELECT `o`.`customer_phone` FROM `sales_order` AS `o`")
    assert "sensitive_column" in _codes(leaked)
    assert guard.check(
        "SELECT `o`.`order_id`, `o`.`region` FROM `sales_order` AS `o` ORDER BY 1 LIMIT 1"
    ).valid


def _dialect_guard(catalog: Catalog, dialect: str) -> SQLGuard:
    return SQLGuard(replace(catalog, dialect=dialect, _link_index=None, _link_key=None))


def test_dialect_shape_rules_are_scoped_to_the_catalog_dialect(catalog: Catalog) -> None:
    """One SQL, three catalogs: only the dialect that cannot run it says so."""
    top = "SELECT TOP 3 o.order_id FROM sales_order AS o ORDER BY o.order_id"
    sqlite_view = _dialect_guard(catalog, "sqlite")
    assert not sqlite_view.check(top).valid
    assert "dialect_top" in _codes(sqlite_view.check(top))
    assert "dialect_top" not in _codes(_dialect_guard(catalog, "mssql").check(top))
    # An unrecognised dialect gets no shape rules at all: guessing here would block
    # correct SQL, which is the failure mode this layer exists to avoid.
    oracle = _dialect_guard(catalog, "oracle")
    assert oracle.check(top).valid


def test_native_spellings_pass_on_their_own_dialect(catalog: Catalog) -> None:
    """The false-positive control: every one of these is that dialect's own idiom."""
    native = {
        "tsql": [
            "SELECT TOP 3 o.order_id FROM sales_order AS o ORDER BY o.order_id",
            "SELECT [o].[order_id], [o].[region] FROM [sales_order] AS [o] ORDER BY [o].[order_id]",
            "SELECT ROUND(SUM(ISNULL(oi.discount, 0)), 2) AS d FROM order_item AS oi",
            "SELECT CONVERT(char(7), o.order_date, 120) AS ym FROM sales_order AS o",
            "SELECT o.order_id + ' / ' + o.customer_name AS label FROM sales_order AS o",
            "SELECT t.order_id FROM (SELECT o.order_id AS order_id, ROW_NUMBER() OVER (ORDER BY o.order_id) AS rn FROM sales_order AS o) AS t WHERE t.rn <= 3",
        ],
        "postgres": [
            "SELECT o.order_id FROM sales_order AS o ORDER BY o.order_id LIMIT 3",
            "SELECT o.order_id FROM sales_order AS o ORDER BY o.order_id FETCH FIRST 3 ROWS ONLY",
            "SELECT EXTRACT(YEAR FROM o.order_date) AS y FROM sales_order AS o",
            "SELECT o.order_id || ' / ' || o.customer_name AS label FROM sales_order AS o",
            "SELECT ROUND(SUM(oi.qty * oi.unit_price) / 7::numeric, 2) AS a FROM order_item AS oi",
            "SELECT to_char(o.order_date, 'YYYY-MM') AS ym FROM sales_order AS o",
            'SELECT o."order_id" FROM sales_order AS o ORDER BY o."order_id"',
        ],
        "mysql": [
            "SELECT `o`.`order_id`, `o`.`region` FROM `sales_order` AS `o` ORDER BY `o`.`order_id` LIMIT 1",
            "SELECT YEAR(o.order_date) AS y FROM sales_order AS o",
            "SELECT CONCAT(o.order_id, ' / ', o.customer_name) AS label FROM sales_order AS o",
            "SELECT ROUND(SUM(IFNULL(oi.discount, 0)), 2) AS d FROM order_item AS oi",
            "SELECT o.order_id FROM sales_order AS o ORDER BY o.order_id LIMIT 3 OFFSET 3",
        ],
        "sqlite": [
            "SELECT o.order_id FROM sales_order AS o ORDER BY o.order_id LIMIT 3 OFFSET 3",
            "SELECT strftime('%Y-%m', o.order_date) AS ym FROM sales_order AS o",
            "SELECT o.order_id || ' / ' || o.customer_name AS label FROM sales_order AS o",
            "SELECT ROUND(SUM(IFNULL(oi.discount, 0)), 2) AS d FROM order_item AS oi",
            "SELECT CAST(SUM(oi.qty * oi.unit_price) AS DECIMAL(18, 2)) AS a FROM order_item AS oi",
        ],
    }
    for dialect, statements in native.items():
        checked = _dialect_guard(catalog, dialect)
        for sql in statements:
            result = checked.check(sql)
            assert result.valid, f"{dialect}: {sql} -> {[i.code for i in result.errors]}"


def test_foreign_dialect_shapes_are_rejected(catalog: Catalog) -> None:
    cases = [
        ("mssql", "SELECT o.order_id FROM sales_order AS o ORDER BY o.order_id LIMIT 3", "dialect_limit"),
        ("mssql", "SELECT `o`.`order_id` FROM `sales_order` AS `o`", "dialect_backtick_quote"),
        ("mssql", "SELECT o.order_id || ' / ' FROM sales_order AS o", "dialect_concat_operator"),
        ("mssql", "SELECT SUM(oi.qty) FILTER (WHERE 1 = 1)::numeric FROM order_item AS oi", "dialect_postgres_cast"),
        ("postgres", "SELECT TOP 3 o.order_id FROM sales_order AS o", "dialect_top"),
        ("postgres", "SELECT [o].[order_id] FROM [sales_order] AS [o]", "dialect_bracket_quote"),
        ("postgres", "SELECT ROUND(SUM(ISNULL(oi.discount, 0)), 2) AS d FROM order_item AS oi", "dialect_foreign_function"),
        ("postgres", "SELECT YEAR(o.order_date) AS y FROM sales_order AS o", "dialect_foreign_function"),
        ("mysql", "SELECT TOP 3 o.order_id FROM sales_order AS o", "dialect_top"),
        ("mysql", 'SELECT "o"."order_id" FROM sales_order AS "o"', "dialect_double_quote_ref"),
        ("mysql", "SELECT o.order_id || ' / ' || o.customer_name AS l FROM sales_order AS o", "dialect_concat_operator"),
        ("mysql", "SELECT o.order_id + ' / ' + o.customer_name AS l FROM sales_order AS o", "dialect_concat_operator_plus"),
        ("mysql", "SELECT o.order_id FROM sales_order AS o WHERE o.region ILIKE '%东%'", "dialect_ilike"),
        ("sqlite", "SELECT TOP 3 o.order_id FROM sales_order AS o", "dialect_top"),
        ("sqlite", "SELECT EXTRACT(YEAR FROM o.order_date) AS y FROM sales_order AS o", "dialect_foreign_function"),
        ("sqlite", "SELECT o.order_id FROM sales_order AS o ORDER BY o.order_id OFFSET 3 ROWS FETCH NEXT 3 ROWS ONLY", "dialect_fetch_rows"),
        ("sqlite", "SELECT [o].[order_id] FROM [sales_order] AS [o]", "dialect_bracket_quote"),
    ]
    for dialect, sql, code in cases:
        result = _dialect_guard(catalog, dialect).check(sql)
        assert not result.valid, f"{dialect}: {sql} should be rejected"
        assert code in _codes(result), f"{dialect}: {sql} -> {[i.code for i in result.errors]}"


def test_shape_rules_ignore_quoted_content(catalog: Catalog) -> None:
    """A literal or a bracketed comment is not a dialect mistake.

    The concat rules are the only ones that read string literals at all, and they read
    them next to an operator; the quoting rules read the variant with literals blanked.
    """
    tsql = _dialect_guard(catalog, "mssql")
    assert tsql.check("SELECT o.customer_name FROM sales_order AS o WHERE o.customer_name = 'a || b'").valid
    postgres = _dialect_guard(catalog, "postgres")
    assert postgres.check("SELECT o.customer_name FROM sales_order AS o WHERE o.customer_name = '[x]'").valid
    # Arithmetic between two columns is not concatenation intent.
    assert postgres.check("SELECT SUM(oi.qty + oi.discount) AS s FROM order_item AS oi").valid


def test_mask_sql_variants_keep_the_dimension_each_rule_needs() -> None:
    from nl2sql_agent.guard import mask_sql

    sql = "SELECT [a].[b] FROM `t` -- [x]\nWHERE c = 'a || [d]'"
    assert "[a]" not in mask_sql(sql) and "`" not in mask_sql(sql)
    assert "[" in mask_sql(sql, unquote=False)
    assert "`" in mask_sql(sql, unquote=False)
    assert "[x]" not in mask_sql(sql, unquote=False)  # comments still stripped
    assert "a || [d]" not in mask_sql(sql)
    # The concat rules need the literal back but not the quoting: unquoting is
    # harmless there because the pattern is `'…' || …`, not `[…`.
    assert "a || d" in mask_sql(sql, blank_literals=False)
    assert "a || [d]" in mask_sql(sql, unquote=False, blank_literals=False)


def test_dialect_rejection_message_names_the_rewrite(catalog: Catalog) -> None:
    result = _dialect_guard(catalog, "sqlite").check(
        "SELECT TOP 3 o.order_id FROM sales_order AS o"
    )
    message = next(issue.message for issue in result.errors if issue.code == "dialect_top")
    assert "LIMIT" in message and "TOP" in message


def test_unicode_table_names_are_visible_to_the_guard(tmp_path: Path) -> None:
    """Naming a table in Chinese must not walk past the whitelist.

    SQL Server allows Unicode identifiers and ``mask_sql`` drops the brackets, so
    an ASCII-only identifier start extracted *no* table from ``FROM [历史副本]``:
    the guard returned a clean result for a table it had never checked, disabled
    or not. The whitelist is only as wide as the extractor that feeds it.
    """
    payload = {
        "domain": "synthetic",
        "dialect": "mssql",
        "as_of_date": "2026-07-01",
        "business_rules": [],
        "relationships": [],
        "tables": [
            {
                "name": "客户主档",
                "description": "客户",
                "primary_key": "ID",
                "columns": [
                    {"name": "ID", "type": "int", "description": ""},
                    {"name": "名称", "type": "nvarchar", "description": ""},
                ],
            },
            {
                "name": "历史副本",
                "description": "副本",
                "primary_key": "ID",
                "disabled": True,
                "columns": [{"name": "ID", "type": "int", "description": ""}],
            },
        ],
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    check = SQLGuard(Catalog.from_path(path))

    allowed = check.check("SELECT a.ID, a.名称 FROM [客户主档] a")
    assert allowed.valid
    assert allowed.tables == ["客户主档"]

    denied = check.check("SELECT COUNT(*) AS n FROM [历史副本]")
    assert not denied.valid
    assert "disabled_table" in _codes(denied)

    unquoted = check.check("SELECT COUNT(*) AS n FROM 历史副本")
    assert not unquoted.valid and "disabled_table" in _codes(unquoted)

    unknown = check.check("SELECT COUNT(*) AS n FROM [不存在的表]")
    assert not unknown.valid and "unknown_table" in _codes(unknown)

    # A number stays a number: 2024.1383 must not be read as table.column.
    numeric = check.check("SELECT a.ID FROM [客户主档] a WHERE a.ID > 2024.1383")
    assert numeric.valid
