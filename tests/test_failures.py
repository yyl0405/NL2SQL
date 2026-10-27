from __future__ import annotations

from pathlib import Path

import pytest

from nl2sql_agent.config import Settings
from nl2sql_agent.memory import ExperienceStore
from nl2sql_agent.runtime import Runtime, create_runtime
from nl2sql_agent.tools.sql_tools import run_readonly_query

GOOD_SQL = "SELECT status, COUNT(*) AS order_count FROM sales_order GROUP BY status"
QUESTION = "按状态统计订单数量"


@pytest.fixture()
def store(tmp_path: Path) -> ExperienceStore:
    return ExperienceStore(tmp_path / "experience.db")


def ok_validator(_sql: str) -> tuple[bool, str]:
    return True, "ok"


def reject_validator(_sql: str) -> tuple[bool, str]:
    return False, "护栏拒绝"


def test_failure_is_queued_once_per_question(store: ExperienceStore) -> None:
    first = store.record_failure(QUESTION, reason="needs_human", schema_hash="h1")
    second = store.record_failure(QUESTION, reason="failed", sql="SELECT 1", schema_hash="h1")

    assert first["created"] is True and first["duplicate"] is False
    assert second["created"] is False and second["duplicate"] is True
    assert second["occur_count"] == 2
    assert len(store.list_failures()) == 1
    assert second["reason"] == "failed"
    assert second["last_sql"] == "SELECT 1"


def test_same_question_on_another_schema_is_a_separate_item(store: ExperienceStore) -> None:
    store.record_failure(QUESTION, reason="needs_human", schema_hash="h1")
    store.record_failure(QUESTION, reason="needs_human", schema_hash="h2")
    assert len(store.list_failures()) == 2


def test_category_is_filled_by_the_classifier_when_absent(store: ExperienceStore) -> None:
    row = store.record_failure(
        "库存低于安全库存的物料有哪些？",
        reason="max_steps",
        schema_hash="h1",
    )
    assert row["category"] == "inventory"


def test_blank_question_is_skipped(store: ExperienceStore) -> None:
    assert store.record_failure("  ", reason="failed") == {
        "created": False,
        "skipped": "empty_question",
    }
    assert store.list_failures() == []


def test_resolve_needs_a_validator_and_a_passing_sql(store: ExperienceStore) -> None:
    item = store.record_failure(QUESTION, reason="needs_human", schema_hash="h1")

    with pytest.raises(ValueError, match="validator"):
        store.resolve_failure(item["id"], GOOD_SQL, validator=None)
    with pytest.raises(ValueError, match="不能结案"):
        store.resolve_failure(item["id"], GOOD_SQL, validator=reject_validator)
    with pytest.raises(ValueError, match="SQL"):
        store.resolve_failure(item["id"], "  ", validator=ok_validator)

    still = store.get_failure(item["id"])
    assert still["status"] == "open"
    assert store.search(QUESTION, schema_hash="h1") == []


def test_resolve_closes_the_item_and_verifies_the_experience(store: ExperienceStore) -> None:
    item = store.record_failure(
        QUESTION,
        reason="needs_human",
        category="group_by",
        schema_hash="h1",
    )
    result = store.resolve_failure(
        item["id"],
        GOOD_SQL,
        validator=ok_validator,
        note="状态口径已确认",
    )

    assert result["failure"]["status"] == "resolved"
    assert result["failure"]["resolution"] == "状态口径已确认"
    assert result["experience"]["status"] == "verified"
    assert result["experience"]["category"] == "group_by"
    hit = store.search(QUESTION, schema_hash="h1")
    assert hit and hit[0]["sql"] == GOOD_SQL


def test_recurrence_after_resolve_reopens_the_same_row(store: ExperienceStore) -> None:
    item = store.record_failure(QUESTION, reason="needs_human", schema_hash="h1")
    store.resolve_failure(item["id"], GOOD_SQL, validator=ok_validator)

    again = store.record_failure(QUESTION, reason="failed", schema_hash="h1")
    assert again["id"] == item["id"]
    assert again["status"] == "open"
    assert again["reopened"] is True
    assert again["occur_count"] == 2
    assert len(store.list_failures()) == 1


def test_resolve_rejects_sql_the_guard_or_database_refuses(tmp_path: Path) -> None:
    runtime: Runtime = create_runtime(Settings(home=tmp_path, provider="mock"))
    store = runtime.store
    schema_hash = runtime.catalog.schema_hash()

    def validator(sql: str) -> tuple[bool, str]:
        check = runtime.guard.check(sql)
        if not check.valid:
            return False, "; ".join(issue.message for issue in check.errors)
        execution = run_readonly_query(runtime.settings.db_path, check.sql, max_rows=10)
        if not execution.get("success"):
            return False, str(execution.get("error", "execution failed"))
        return True, "ok"

    blocked = store.record_failure("各物料的库存金额", reason="needs_human", schema_hash=schema_hash)
    with pytest.raises(ValueError, match=r"SELECT \*"):
        store.resolve_failure(blocked["id"], "SELECT * FROM inventory", validator=validator)
    assert store.get_failure(blocked["id"])["status"] == "open"

    broken = store.record_failure("各供应商的欠款", reason="needs_human", schema_hash=schema_hash)
    with pytest.raises(ValueError, match="不能结案"):
        store.resolve_failure(
            broken["id"],
            "SELECT supplier_name, SUM(nope) AS x FROM purchase_order GROUP BY supplier_name",
            validator=validator,
        )
    assert store.get_failure(broken["id"])["status"] == "open"

    resolved = store.record_failure(QUESTION, reason="needs_human", schema_hash=schema_hash)
    result = store.resolve_failure(resolved["id"], GOOD_SQL, validator=validator)
    assert result["failure"]["status"] == "resolved"
    assert result["experience"]["status"] == "verified"


def test_candidates_never_reach_retrieval(store: ExperienceStore) -> None:
    store.add(QUESTION, GOOD_SQL, category="group_by", verified=False, schema_hash="h1")
    assert store.search(QUESTION, schema_hash="h1") == []

    item = store.record_failure(QUESTION, reason="needs_human", schema_hash="h1")
    store.resolve_failure(item["id"], GOOD_SQL, validator=ok_validator)
    hits = store.search(QUESTION, schema_hash="h1")
    assert len(hits) == 1
    assert hits[0]["quality"] == "verified"
    assert hits[0]["sql"] == GOOD_SQL


def test_same_category_examples_rank_first(store: ExperienceStore) -> None:
    store.add(QUESTION, GOOD_SQL, category="group_by", verified=True, schema_hash="h1")
    store.add(
        "库存低于安全库存的物料有哪些？",
        "SELECT material_id FROM material",
        category="inventory",
        verified=True,
        schema_hash="h1",
    )

    loose = store.search("各状态订单数量统计", schema_hash="h1", category="group_by", category_boost=0.0)
    boosted = store.search("各状态订单数量统计", schema_hash="h1", category="group_by", category_boost=0.3)
    assert loose[0]["category"] == boosted[0]["category"] == "group_by"
    assert boosted[0]["score"] >= loose[0]["score"]

    strict = store.search("库存低于安全库存", schema_hash="h1", category="inventory", strict_category=True)
    assert [item["category"] for item in strict] == ["inventory"]


def test_failure_stats_counts(store: ExperienceStore) -> None:
    store.record_failure("问题一", reason="needs_human", category="metric", schema_hash="h1")
    item = store.record_failure("问题二", reason="failed", category="bom", schema_hash="h1")
    store.record_failure("问题一", reason="needs_human", category="metric", schema_hash="h1")
    store.resolve_failure(item["id"], GOOD_SQL, validator=ok_validator)

    stats = store.failure_stats()
    assert stats["failures_total"] == 2
    assert stats["failures_open"] == 1
    assert stats["failures_resolved"] == 1
    assert stats["failure_occurrences"] == 3
    assert stats["failures_by_category"] == {"metric": 1}
    assert store.stats()["failures_open"] == 1
