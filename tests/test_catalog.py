from __future__ import annotations

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.config import Settings


def test_catalog_loads_tables(catalog: Catalog) -> None:
    assert catalog.domain.startswith("离散制造")
    assert {table.name for table in catalog.enabled_tables()} == {
        "sales_order",
        "order_item",
        "material",
        "inventory",
        "purchase_order",
        "work_order",
        "shipment",
        "bom",
        "dim_lookup",
    }
    assert catalog.table("SALES_ORDER") is not None
    assert catalog.table("[sales_order]") is not None
    assert catalog.table("internal_audit").disabled is True
    assert catalog.table("missing_table") is None


def test_catalog_column_flags(catalog: Catalog) -> None:
    table = catalog.table("sales_order")
    assert table is not None
    assert table.column("customer_phone").sensitive is True
    assert table.column("internal_note").disabled is True
    assert table.column("ORDER_ID").name == "order_id"


def test_schema_hash_is_stable_and_short(catalog: Catalog) -> None:
    other = Catalog.from_path(Settings().schema_path)
    assert len(catalog.schema_hash()) == 16
    assert catalog.schema_hash() == other.schema_hash()


def test_search_tables_finds_inventory_tables(catalog: Catalog) -> None:
    hits = catalog.search_tables("哪些物料库存低于安全库存", top_k=5)
    names = [hit.table.name for hit in hits]
    assert "material" in names
    assert "inventory" in names
    assert all(hit.score > 0 for hit in hits)


def test_search_tables_boosts_exact_synonym(catalog: Catalog) -> None:
    hits = catalog.search_tables("BOM 物料清单", top_k=3)
    assert hits[0].table.name == "bom"


def test_describe_tables_excludes_disabled_and_includes_relationship(catalog: Catalog) -> None:
    data = catalog.describe_tables(["sales_order", "internal_audit", "missing"])
    assert [table["name"] for table in data["tables"]] == ["sales_order"]
    column_names = {column["name"] for column in data["tables"][0]["columns"]}
    assert "internal_note" not in column_names
    assert "customer_phone" in column_names
    assert data["missing"] == ["internal_audit", "missing"]
    assert any(rel["right"].startswith("order_item") for rel in data["relationships"])
    assert data["business_rules"]


def test_relationships_for(catalog: Catalog) -> None:
    relations = catalog.relationships_for(["material"])
    assert len(relations) == 5
