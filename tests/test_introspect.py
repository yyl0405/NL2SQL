"""Introspection builds a catalog the agent can actually link against."""

from __future__ import annotations

import json
from pathlib import Path

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.introspect import (
    COLUMN_SQL,
    FOREIGN_KEY_SQL,
    PRIMARY_KEY_SQL,
    TABLE_SQL,
    IntrospectOptions,
    introspect,
    parse_join_hints,
)

TABLES = [
    ("Customer", "U", "客户主档"),
    ("Contract", "U", "销售合同"),
    ("BasicData", "U", "基础数据字典"),
    ("OperationLog", "U", "系统操作日志"),
    ("ProductionOrder", "U", "生产订单"),
]

COLUMNS = [
    ("Customer", "ID", "int", 4, 10, 0, 0, "主键"),
    ("Customer", "Name", "nvarchar", 200, 0, 0, 1, "客户名称"),
    ("Customer", "Phone", "nvarchar", 100, 0, 0, 1, "联系电话"),
    ("Contract", "ID", "int", 4, 10, 0, 0, "主键"),
    ("Contract", "CustomerID", "int", 4, 10, 0, 0, "客户ID[关联表:Customer,关联字段:ID]"),
    ("Contract", "ContractAmount", "decimal", 9, 18, 2, 1, "合同金额"),
    ("Contract", "SectorID", "int", 4, 10, 0, 1, "行业[关联表:BasicData,关联字段:ID,前置条件:DictType=3]"),
    ("BasicData", "ID", "int", 4, 10, 0, 0, "主键"),
    ("BasicData", "DictType", "int", 4, 10, 0, 0, "字典类型"),
    ("OperationLog", "ID", "int", 4, 10, 0, 0, "主键"),
    ("ProductionOrder", "ID", "int", 4, 10, 0, 0, "主键"),
    ("ProductionOrder", "ContractID", "int", 4, 10, 0, 1, "合同ID"),
    ("ProductionOrder", "PlannedStart", "datetime", 8, 0, 0, 1, "计划开工"),
]

PRIMARY_KEYS = [
    ("Customer", "ID"),
    ("Contract", "ID"),
]

FOREIGN_KEYS = [
    ("Contract", "ID", "ProductionOrder", "ContractID"),
]


class FakeCursor:
    def __init__(self, data: dict[str, list[tuple]]) -> None:
        self.data = data
        self.rows: list[tuple] = []

    def _payload(self, sql: str) -> list[tuple]:
        matches = [payload for marker, payload in self.data.items() if marker in sql]
        if len(matches) != 1:
            raise AssertionError(f"SQL matched {len(matches)} fixtures: {sql}")
        return matches[0]

    def execute(self, sql: str, *params) -> "FakeCursor":
        self.rows = self._payload(sql)
        return self

    def fetchall(self) -> list[tuple]:
        return self.rows


class FakeConnection:
    def __init__(self, data: dict[str, list[tuple]]) -> None:
        self.data = data

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.data)


def fake_connection() -> FakeConnection:
    # markers must each match exactly one statement; COLUMN_SQL also joins sys.objects
    return FakeConnection(
        {
            "o.type AS object_type": TABLES,
            "c.name AS column_name": COLUMNS,
            "TABLE_CONSTRAINTS": PRIMARY_KEYS,
            "fkc.referenced_object_id": FOREIGN_KEYS,
        }
    )


def test_each_statement_has_a_unique_marker() -> None:
    """The fixtures above map 1:1 to the real statements, so SQL drift fails here
    instead of silently returning the wrong rows."""
    statements = (TABLE_SQL, COLUMN_SQL, PRIMARY_KEY_SQL, FOREIGN_KEY_SQL)
    for sql, marker in (
        (TABLE_SQL, "o.type AS object_type"),
        (COLUMN_SQL, "c.name AS column_name"),
        (PRIMARY_KEY_SQL, "TABLE_CONSTRAINTS"),
        (FOREIGN_KEY_SQL, "fkc.referenced_object_id"),
    ):
        assert marker in sql
        assert sum(1 for other in statements if marker in other) == 1


def test_real_driver_pads_the_char_2_object_type() -> None:
    """``sys.objects.type`` is CHAR(2), so pyodbc hands back ``'U '``.

    Without stripping, every table of a 554-table ERP database silently
    disappeared from the an attached catalog.
    """
    padded = FakeConnection(
        {
            "o.type AS object_type": [(n, "U ", c) for n, _, c in TABLES],
            "c.name AS column_name": COLUMNS,
            "TABLE_CONSTRAINTS": PRIMARY_KEYS,
            "fkc.referenced_object_id": FOREIGN_KEYS,
        }
    )
    assert introspect(padded)["_stats"]["tables"] == len(TABLES)
    assert introspect(padded, IntrospectOptions(include_views=True))["_stats"]["tables"] == len(TABLES)


def test_introspect_maps_tables_columns_and_comments() -> None:
    payload = introspect(fake_connection(), IntrospectOptions(domain="ERP", as_of_date="2026-10-22"))
    stats = payload["_stats"]
    assert payload["dialect"] == "mssql"
    assert payload["as_of_date"] == "2026-10-22"
    names = {t["name"] for t in payload["tables"]}
    assert names == {"Customer", "Contract", "BasicData", "OperationLog", "ProductionOrder"}

    contract = next(t for t in payload["tables"] if t["name"] == "Contract")
    assert contract["description"] == "销售合同"
    assert contract["primary_key"] == "ID"
    amount = next(c for c in contract["columns"] if c["name"] == "ContractAmount")
    assert amount["type"] == "decimal(18,2)"
    assert amount["description"] == "合同金额"
    assert stats["columns"] == len(COLUMNS)


def test_sensitive_and_disabled_flags_are_marked() -> None:
    payload = introspect(fake_connection())
    customer = next(t for t in payload["tables"] if t["name"] == "Customer")
    phone = next(c for c in customer["columns"] if c["name"] == "Phone")
    assert phone.get("sensitive") is True
    assert payload["_stats"]["sensitive_columns"] == 1

    log = next(t for t in payload["tables"] if t["name"] == "OperationLog")
    assert log.get("disabled") is True
    assert payload["_stats"]["disabled_tables"] == 1

    no_flags = introspect(
        fake_connection(),
        IntrospectOptions(mark_sensitive=False, mark_disabled=False),
    )
    assert no_flags["_stats"]["sensitive_columns"] == 0
    assert no_flags["_stats"]["disabled_tables"] == 0


def test_join_hints_become_relationships_only_for_synced_tables() -> None:
    payload = introspect(fake_connection())
    relationships = {(r["left"], r["right"]) for r in payload["relationships"]}
    assert ("Customer.ID", "Contract.CustomerID") in relationships
    assert ("BasicData.ID", "Contract.SectorID") in relationships
    # foreign keys are structural facts, independent of comments/join hints
    assert ("Contract.ID", "ProductionOrder.ContractID") in relationships
    assert payload["_stats"]["foreign_keys"] == 1
    assert payload["_stats"]["fk_relationships"] == 1

    only_contract = introspect(
        fake_connection(), IntrospectOptions(include=[r"^Contract$"])
    )
    assert only_contract["_stats"]["tables"] == 1
    # the hint is still counted, but it cannot become a relationship to a table we did not sync
    assert only_contract["_stats"]["join_hints"] == 2
    assert only_contract["_stats"]["foreign_keys"] == 1
    assert only_contract["_stats"]["fk_relationships"] == 0
    assert only_contract["relationships"] == []


def test_preconditions_are_suggestions_not_enforced_filters(tmp_path: Path) -> None:
    payload = introspect(fake_connection())
    assert payload["filter_suggestions"] == [
        {"table": "Contract", "column": "SectorID", "condition": "DictType=3"}
    ]
    assert payload["_stats"]["filter_suggestions"] == 1
    # guard-enforced required_filters stay empty until a human confirms the discriminator
    payload.pop("_stats")
    path = tmp_path / "erp_schema.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    catalog = Catalog.from_path(path)
    assert all(not table.required_filters for table in catalog.tables.values())


def test_parse_join_hints_handles_full_width_and_conditions() -> None:
    hints = parse_join_hints("机型[关联表:BasicData，关联字段:KeyCode，前置条件:DictType＝17 and ParentKey=0]")
    assert len(hints) == 1
    assert hints[0]["table"] == "BasicData"
    assert hints[0]["column"] == "KeyCode"
    assert "DictType" in hints[0]["condition"]
    assert parse_join_hints("无关联的普通列") == []


def test_generated_catalog_is_loadable_and_links_on_chinese_comments(tmp_path: Path) -> None:
    payload = introspect(fake_connection())
    payload.pop("_stats")
    path = tmp_path / "erp_schema.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    catalog = Catalog.from_path(path)
    assert catalog.dialect == "mssql"
    # the log table is disabled, so it must never be recalled
    hits = [hit.table.name for hit in catalog.search_tables("合同金额", top_k=3)]
    assert hits[0] == "Contract"
    assert "OperationLog" not in catalog.enabled_names()
    described = catalog.describe_tables(["Contract"])
    assert described["relationships"]
    assert described["tables"][0]["columns"]
