from __future__ import annotations

import json
from pathlib import Path

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.schema_dupes import cluster_duplicates, find_duplicates


def _table(name: str, columns: list[str], **extra: object) -> dict:
    return {
        "name": name,
        "description": "",
        "primary_key": "id",
        "columns": [{"name": column, "type": "int", "description": ""} for column in columns],
        **extra,
    }


def _catalog(tmp_path: Path, tables: list[dict]) -> Catalog:
    path = tmp_path / "schema.json"
    path.write_text(
        json.dumps(
            {
                "domain": "synthetic",
                "dialect": "sqlite",
                "as_of_date": "2026-07-01",
                "business_rules": [],
                "relationships": [],
                "tables": tables,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return Catalog.from_path(path)


def test_finds_column_twins_and_ignores_unrelated_tables(tmp_path: Path) -> None:
    catalog = _catalog(
        tmp_path,
        [
            _table("Customer", ["id", "name", "phone", "addr", "level"]),
            _table("客户表", ["id", "name", "phone", "addr", "level", "legacy_flag"]),
            _table("Orders", ["id", "customer_id", "amount"]),
        ],
    )
    pairs = find_duplicates(catalog, min_overlap=0.85)
    assert [(pair.left, pair.right) for pair in pairs] == [("Customer", "客户表")]
    # Containment of the *smaller* table: the copy adds one column, so it still matches.
    assert pairs[0].overlap == 1.0
    assert pairs[0].shared == ("addr", "id", "level", "name", "phone")
    assert pairs[0].to_dict()["left_columns"] == 5
    assert pairs[0].to_dict()["right_columns"] == 6


def test_shared_key_columns_are_not_a_twin(tmp_path: Path) -> None:
    """``id`` + ``name`` show up in every ERP table.

    Containment says a 2-column table is fully inside a 5-column one, which is
    true and useless; the column floor is what keeps the candidate list reviewable.
    """
    catalog = _catalog(
        tmp_path,
        [
            _table("Customer", ["id", "name", "phone", "addr", "level"]),
            _table("Supplier", ["id", "name"]),
        ],
    )
    assert find_duplicates(catalog, min_overlap=0.85) == []
    assert find_duplicates(catalog, min_overlap=0.5) == []
    loosened = find_duplicates(catalog, min_overlap=0.5, min_columns=2)
    assert [(pair.left, pair.right) for pair in loosened] == [("Customer", "Supplier")]


def test_already_disabled_tables_are_not_reported(tmp_path: Path) -> None:
    catalog = _catalog(
        tmp_path,
        [
            _table("Customer", ["id", "name"]),
            _table("客户表", ["id", "name"], disabled=True),
        ],
    )
    assert find_duplicates(catalog, min_overlap=0.85) == []


def test_limit_and_exclude_keep_the_list_reviewable(tmp_path: Path) -> None:
    tables = [_table(f"T{index}", ["id", "name", "amount", "remark"]) for index in range(5)]
    catalog = _catalog(tmp_path, tables)
    assert len(find_duplicates(catalog, min_overlap=0.9, limit=3)) == 3
    assert find_duplicates(catalog, min_overlap=0.9, limit=0)
    excluded = find_duplicates(catalog, min_overlap=0.9, exclude=["T0", "T1", "T2"])
    assert {(pair.left, pair.right) for pair in excluded} <= {("T3", "T4")}


def test_clusters_collapse_a_template_family_into_one_decision(tmp_path: Path) -> None:
    """Nine pairs from one template family are one review item, not nine.

    A reviewer decides "these are copies" or "these are per-form custom tables";
    listing every pair hides the pair that actually matters.
    """
    tables = [_table(f"T{index}", ["id", "name", "amount", "remark"]) for index in range(5)]
    tables.append(_table("Customer", ["id", "name", "phone", "addr", "level"]))
    tables.append(_table("客户表", ["id", "name", "phone", "addr", "level", "legacy_flag"]))
    catalog = _catalog(tmp_path, tables)
    clusters = cluster_duplicates(find_duplicates(catalog, min_overlap=0.9, limit=0))
    sizes = sorted(len(cluster.members) for cluster in clusters)
    assert sizes == [2, 5]
    biggest = max(clusters, key=lambda cluster: len(cluster.members))
    assert biggest.members == ("T0", "T1", "T2", "T3", "T4")
    assert biggest.pairs == 10  # 5 tables produce 10 pairs; the decision is still one
    pair_cluster = min(clusters, key=lambda cluster: len(cluster.members))
    assert pair_cluster.members == ("Customer", "客户表")
    assert pair_cluster.to_dict()["pairs"] == 1
