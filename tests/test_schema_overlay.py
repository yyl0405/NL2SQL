"""The overlay is the only place where a model suggestion becomes runtime truth."""

from __future__ import annotations

import json
from pathlib import Path

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.schema_overlay import (
    apply_overlay,
    empty_overlay,
    load_overlay,
    overlay_from_approved_suggestion,
    save_overlay,
)


def write_catalog(path: Path) -> None:
    payload = {
        "domain": "synthetic",
        "dialect": "sqlite",
        "as_of_date": "2026-07-01",
        "business_rules": ["既有口径"],
        "relationships": [],
        "tables": [
            {
                "name": "Customer",
                "description": "客户主档",
                "primary_key": "ID",
                "synonyms": ["客户主档"],
                "columns": [
                    {"name": "ID", "type": "int", "description": "主键"},
                    {"name": "CustomerName", "type": "nvarchar", "description": "名称"},
                ],
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_missing_overlay_is_empty(tmp_path: Path) -> None:
    assert load_overlay(tmp_path / "missing.json") == empty_overlay()


def test_apply_overlay_merges_aliases_and_rules(tmp_path: Path) -> None:
    write_catalog(tmp_path / "schema.json")
    catalog = Catalog.from_path(tmp_path / "schema.json")
    overlay = {
        "schema_hash": catalog.schema_hash(),
        "table_aliases": {"Customer": ["签约客户", "客户主档"]},
        "column_aliases": {"Customer.CustomerName": ["客户名称"]},
        "business_rules": ["客户名称取 Customer.CustomerName", "既有口径"],
    }
    merged = apply_overlay(catalog, overlay)
    table = merged.table("customer")
    assert table is not None
    assert "签约客户" in table.synonyms
    assert "客户主档" in table.synonyms
    column = table.column("CustomerName")
    assert column is not None
    assert "客户名称" in column.synonyms
    assert merged.business_rules == ["既有口径", "客户名称取 Customer.CustomerName"]
    assert merged.schema_hash() == catalog.schema_hash()
    assert merged.search("签约客户", top_k=1).hits[0].table.name == "Customer"


def test_apply_overlay_ignores_unknown_names(tmp_path: Path) -> None:
    write_catalog(tmp_path / "schema.json")
    catalog = Catalog.from_path(tmp_path / "schema.json")
    merged = apply_overlay(
        catalog,
        {
            "table_aliases": {"Ghost": ["幽灵"]},
            "column_aliases": {"Customer.Ghost": ["幽灵列"]},
            "default_filters": {"Ghost": ["IsDeleted = 0"]},
            "business_rules": ["新口径"],
        },
    )
    table = merged.table("Customer")
    assert table is not None
    assert "幽灵" not in table.synonyms
    assert all(column.name != "Ghost" for column in table.columns)
    assert table.default_filters == ()
    assert merged.business_rules[-1] == "新口径"


def test_apply_overlay_merges_default_filters_scoped_to_one_table(tmp_path: Path) -> None:
    write_catalog(tmp_path / "schema.json")
    catalog = Catalog.from_path(tmp_path / "schema.json")
    merged = apply_overlay(
        catalog,
        {
            "default_filters": {
                "Customer": [{"column": "IsDeleted", "value": "0", "reason": "0=未删除"}],
            }
        },
    )
    table = merged.table("Customer")
    assert table is not None
    assert table.default_filters == (
        {"column": "IsDeleted", "value": "0", "reason": "0=未删除"},
    )
    # The model reads defaults through the same schema payload as everything else.
    assert table.to_dict()["default_filters"] == [
        {"column": "IsDeleted", "value": "0", "reason": "0=未删除"}
    ]
    # A per-table convention is not a structure change: experiences stay valid.
    assert merged.schema_hash() == catalog.schema_hash()
    assert catalog.table("Customer").default_filters == ()


def test_default_filters_accept_shorthand_and_dedup(tmp_path: Path) -> None:
    path = tmp_path / "overlay.json"
    save_overlay(
        path,
        {
            "default_filters": {
                "Customer": [
                    "IsDeleted = 0",
                    {"column": "IsDeleted", "value": "0"},
                    {"column": "State", "value": "5", "reason": "默认已生效"},
                ]
            }
        },
    )
    overlay = load_overlay(path)
    assert overlay["default_filters"]["Customer"] == [
        {"column": "IsDeleted", "value": "0"},
        {"column": "State", "value": "5", "reason": "默认已生效"},
    ]


def test_approving_suggestion_writes_reviewable_overlay(tmp_path: Path) -> None:
    suggestion = {
        "id": "客户@customer",
        "concept": "客户",
        "table": "Customer",
        "field_aliases": {"CustomerName": ["客户名称"]},
        "business_rule": "客户名称取 Customer.CustomerName",
        "schema_hash": "abc",
    }
    overlay = overlay_from_approved_suggestion(empty_overlay(), suggestion, reviewer="yyl")
    assert overlay["table_aliases"]["Customer"] == ["客户"]
    assert overlay["column_aliases"]["Customer.CustomerName"] == ["客户名称"]
    assert overlay["business_rules"] == ["客户名称取 Customer.CustomerName"]
    assert overlay["approved"][0]["reviewer"] == "yyl"
    save_overlay(tmp_path / "overlay.json", overlay)
    assert load_overlay(tmp_path / "overlay.json")["table_aliases"] == overlay["table_aliases"]


def test_apply_overlay_disables_listed_tables(tmp_path: Path) -> None:
    write_catalog(tmp_path / "schema.json")
    catalog = Catalog.from_path(tmp_path / "schema.json")
    merged = apply_overlay(catalog, {"disabled_tables": {"Customer": "历史副本，请用签约客户表"}})
    table = merged.table("Customer")
    assert table is not None
    assert table.disabled is True
    assert "历史副本" in table.description
    assert "Customer" not in merged.enabled_names()
    # Visibility is a structure change, so experiences must go stale with it.
    assert merged.schema_hash() != catalog.schema_hash()


def test_apply_overlay_ignores_unknown_disabled_table(tmp_path: Path) -> None:
    write_catalog(tmp_path / "schema.json")
    catalog = Catalog.from_path(tmp_path / "schema.json")
    merged = apply_overlay(catalog, {"disabled_tables": {"Ghost": "不存在"}})
    assert merged.schema_hash() == catalog.schema_hash()
    assert merged.table("Customer").disabled is False


def test_disabled_tables_accept_list_and_dict_forms(tmp_path: Path) -> None:
    path = tmp_path / "overlay.json"
    save_overlay(path, {"disabled_tables": ["Customer", {"Orders": "副本"}]})
    overlay = load_overlay(path)
    assert overlay["disabled_tables"] == {"Customer": "", "Orders": "副本"}
    assert "disabled_tables" in empty_overlay()
