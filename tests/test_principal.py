from __future__ import annotations

from pathlib import Path

import pytest

from nl2sql_agent.ast_guard import AstAnalyzer
from nl2sql_agent.catalog import Catalog
from nl2sql_agent.config import Settings
from nl2sql_agent.guard import SQLGuard
from nl2sql_agent.principal import (
    Principal,
    PrincipalError,
    RowFilter,
    list_principals,
    load_principal,
    principal_from_payload,
)

PRINCIPALS = Path(__file__).resolve().parents[1] / "src" / "nl2sql_agent" / "data" / "principals.json"


def _codes(result) -> set[str]:
    return {issue.code for issue in result.errors}


def _messages(result) -> str:
    return " | ".join(issue.message for issue in result.errors)


@pytest.fixture()
def rep_east() -> Principal:
    return load_principal(PRINCIPALS, "rep_east")


@pytest.fixture()
def planner() -> Principal:
    return load_principal(PRINCIPALS, "planner_multi_region")


def test_row_filter_accepts_equality_and_rejects_other_values() -> None:
    row_filter = RowFilter(table="sales_order", column="region", values=("华东",))
    assert row_filter.satisfied_by("SELECT 1 FROM sales_order o WHERE o.region = '华东'")
    assert not row_filter.satisfied_by("SELECT 1 FROM sales_order o WHERE o.region = '华南'")
    assert not row_filter.satisfied_by("SELECT 1 FROM sales_order o")


def test_row_filter_accepts_in_within_scope_only() -> None:
    row_filter = RowFilter(table="sales_order", column="region", values=("华东", "华南"))
    assert row_filter.satisfied_by("WHERE o.region IN ('华东', '华南')")
    assert row_filter.satisfied_by("WHERE o.region IN ('华南','华东')")
    assert row_filter.satisfied_by("WHERE o.region IN ('华东')")
    assert not row_filter.satisfied_by("WHERE o.region IN ('华东', '华北')")


def test_row_filter_fails_closed_on_shapes_it_cannot_verify() -> None:
    row_filter = RowFilter(table="sales_order", column="region", values=("华东",))
    assert not row_filter.satisfied_by("WHERE o.region IN (SELECT region FROM auth_regions)")
    assert not row_filter.satisfied_by("WHERE o.region LIKE '华东%'")
    assert not row_filter.satisfied_by("WHERE o.region = '华东' OR o.region = '华北'")
    assert not RowFilter("sales_order", "region", ()).satisfied_by("WHERE region = '华东'")


def test_row_filter_reads_quoted_and_bare_literals() -> None:
    assert RowFilter("t", "name", ("O'Brien",)).satisfied_by("WHERE name = 'O''Brien'")
    assert RowFilter("t", "year", ("2026",)).satisfied_by("WHERE year = 2026")
    assert RowFilter("t", "warehouse", ("WH-A",)).satisfied_by("WHERE warehouse = 'WH-A'")


def test_required_predicate_is_human_readable() -> None:
    assert (
        RowFilter("sales_order", "region", ("华东",)).required_predicate
        == "sales_order.region = '华东'"
    )
    assert (
        RowFilter("sales_order", "region", ("华东", "华南")).required_predicate
        == "sales_order.region IN ('华东', '华南')"
    )


def test_principal_visibility_rules() -> None:
    principal = principal_from_payload(
        {
            "id": "p",
            "tables": ["Sales_Order"],
            "exclude_columns": {"sales_order": ["secret"]},
            "only_columns": {"material": ["material_id"]},
        }
    )
    assert principal.visible("sales_order")
    assert not principal.visible("material")
    assert principal.column_visible("sales_order", "region")
    assert not principal.column_visible("sales_order", "secret")
    assert principal.column_visible("material", "material_id")
    assert not principal.column_visible("material", "standard_cost")
    assert principal_from_payload({"id": "x"}).visible("anything")


def test_load_principal_errors(tmp_path: Path) -> None:
    with pytest.raises(PrincipalError, match="权限配置不存在"):
        load_principal(tmp_path / "missing.json", "rep_east")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(PrincipalError, match="不是合法 JSON"):
        load_principal(bad, "rep_east")
    with pytest.raises(PrincipalError, match="可选"):
        load_principal(PRINCIPALS, "does_not_exist")


def test_list_principals_describes_every_role() -> None:
    rows = list_principals(PRINCIPALS)
    assert {row["id"] for row in rows} == {
        "analyst_full",
        "rep_east",
        "wh_a_keeper",
        "buyer_transit",
        "planner_multi_region",
    }


def test_view_hides_tables_columns_and_prunes_relationships(catalog: Catalog, rep_east) -> None:
    view = rep_east.view(catalog)
    assert view.table("material") is None
    assert view.table("sales_order") is not None
    table = view.table("sales_order")
    assert table.column("sales_rep") is None
    assert table.column("customer_phone") is None
    assert table.column("order_id") is not None
    assert table.required_filters == ({"column": "region", "value": "华东", "reason": "权限范围：sales_order.region = '华东'"},)
    for rel in view.relationships:
        for side in ("left", "right"):
            assert str(rel[side]).split(".")[0].lower() in {
                "sales_order",
                "order_item",
                "shipment",
            }


def test_view_does_not_mutate_the_full_catalog(catalog: Catalog, rep_east) -> None:
    before = catalog.schema_hash()
    rep_east.view(catalog)
    assert catalog.schema_hash() == before
    assert catalog.table("material") is not None


def test_multi_value_row_filter_is_not_injected_as_a_required_filter(
    catalog: Catalog, planner
) -> None:
    view = planner.view(catalog)
    table = view.table("sales_order")
    assert all(str(item.get("column")) != "region" for item in table.required_filters)
    assert [len(row_filter.values) for row_filter in planner.row_filters] == [2]


def test_guard_reports_permission_instead_of_unknown(catalog: Catalog, rep_east) -> None:
    guard = SQLGuard(catalog, principal=rep_east)
    result = guard.check("SELECT m.material_name FROM material m")
    assert "permission_table" in _codes(result)
    assert "unknown_table" not in _codes(result)


def test_guard_blocks_missing_and_out_of_scope_row_filters(catalog: Catalog, rep_east) -> None:
    guard = SQLGuard(catalog, principal=rep_east)
    assert "row_filter_missing" in _codes(guard.check("SELECT o.order_id FROM sales_order o"))
    assert "row_filter_missing" in _codes(
        guard.check("SELECT o.order_id FROM sales_order o WHERE o.region = '华南'")
    )
    assert guard.check("SELECT o.order_id FROM sales_order o WHERE o.region = '华东'").valid


def test_guard_blocks_hidden_column_qualified_and_bare(catalog: Catalog, rep_east) -> None:
    guard = SQLGuard(catalog, principal=rep_east)
    qualified = guard.check(
        "SELECT o.order_id, o.sales_rep FROM sales_order o WHERE o.region = '华东'"
    )
    assert "permission_column" in _codes(qualified)
    bare = guard.check("SELECT sales_rep FROM sales_order WHERE region = '华东'")
    assert "permission_column" in _codes(bare)


def test_guard_suggestions_never_leak_hidden_columns(catalog: Catalog, rep_east) -> None:
    result = SQLGuard(catalog, principal=rep_east).check(
        "SELECT o.sales_reo FROM sales_order o WHERE o.region = '华东'"
    )
    assert "unknown_column" in _codes(result)
    assert "sales_rep" not in _messages(result)
    unfiltered = SQLGuard(catalog).check("SELECT o.sales_reo FROM sales_order o")
    assert "sales_rep" in _messages(unfiltered)


def test_sensitive_still_wins_over_an_unrestricted_principal(catalog: Catalog) -> None:
    analyst = load_principal(PRINCIPALS, "analyst_full")
    result = SQLGuard(catalog, principal=analyst).check("SELECT o.customer_phone FROM sales_order o")
    assert "sensitive_column" in _codes(result)


def test_unrestricted_principal_matches_default_behaviour(catalog: Catalog) -> None:
    analyst = load_principal(PRINCIPALS, "analyst_full")
    sql = "SELECT o.region, SUM(oi.qty * oi.unit_price) AS amount FROM sales_order o JOIN order_item oi ON oi.order_id = o.order_id GROUP BY o.region"
    assert SQLGuard(catalog).check(sql).to_dict() == SQLGuard(catalog, principal=analyst).check(sql).to_dict()


def test_ast_discovered_table_still_hits_permissions(catalog: Catalog, rep_east) -> None:
    pytest.importorskip("sqlglot")
    result = SQLGuard(catalog, principal=rep_east, analyzer=AstAnalyzer("sqlite")).check(
        "SELECT m.material_name FROM[material] m"
    )
    assert "permission_table" in _codes(result)


def test_runtime_splits_agent_view_from_guard_catalog(tmp_path: Path) -> None:
    from nl2sql_agent.runtime import create_runtime

    settings = Settings(home=tmp_path, provider="mock", principal_id="rep_east")
    runtime = create_runtime(settings)
    assert runtime.principal is not None
    assert runtime.principal.id == "rep_east"
    assert runtime.agent.catalog.table("material") is None
    assert runtime.guard.catalog.table("material") is not None
    assert runtime.agent.schema_hash != runtime.catalog.schema_hash()


def test_runtime_without_principal_keeps_one_catalog(tmp_path: Path) -> None:
    from nl2sql_agent.runtime import create_runtime

    runtime = create_runtime(Settings(home=tmp_path, provider="mock"))
    assert runtime.principal is None
    assert runtime.agent.catalog is runtime.catalog
    assert runtime.agent.schema_hash == runtime.catalog.schema_hash()


def test_runtime_raises_on_unknown_principal(tmp_path: Path) -> None:
    from nl2sql_agent.runtime import create_runtime

    settings = Settings(home=tmp_path, provider="mock", principal_id="ghost")
    with pytest.raises(PrincipalError):
        create_runtime(settings)


def test_principals_path_defaults_to_the_packaged_file(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path)
    assert settings.principals_path == PRINCIPALS
    override = tmp_path / "roles.json"
    override.write_text("[]", encoding="utf-8")
    settings.principals_override = override
    assert settings.principals_path == override
