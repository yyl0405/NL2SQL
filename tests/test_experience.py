from __future__ import annotations

from pathlib import Path

import pytest

from nl2sql_agent.memory import ExperienceStore

QUESTION = "2026年6月华东地区销售订单总金额是多少？"
SQL = "SELECT SUM(qty) AS total FROM order_item"


@pytest.fixture()
def store(tmp_path: Path) -> ExperienceStore:
    return ExperienceStore(tmp_path / "experience.db")


def test_candidate_is_hidden_from_search(store: ExperienceStore) -> None:
    record = store.add(QUESTION, SQL, category="metric", schema_hash="h1")
    assert record["status"] == "candidate"
    assert store.search(QUESTION, schema_hash="h1") == []


def test_verify_enables_reuse(store: ExperienceStore) -> None:
    record = store.add(QUESTION, SQL, category="metric", schema_hash="h1")
    store.verify(int(record["id"]))
    hits = store.search(QUESTION, schema_hash="h1")
    assert len(hits) == 1
    assert hits[0]["sql"] == SQL
    assert hits[0]["score"] == 1.0
    assert store.stats()["reuse_hits"] == 1


def test_schema_hash_isolation(store: ExperienceStore) -> None:
    record = store.add(QUESTION, SQL, schema_hash="h1")
    store.verify(int(record["id"]))
    assert store.search(QUESTION, schema_hash="h2") == []


def test_verified_add_with_validator(store: ExperienceStore) -> None:
    record = store.add(
        QUESTION,
        SQL,
        verified=True,
        schema_hash="h1",
        validator=lambda sql: (True, "ok") if sql == SQL else (False, "mismatch"),
    )
    assert record["status"] == "verified"
    assert store.search(QUESTION, schema_hash="h1")


def test_verified_add_rejects_invalid_sql(store: ExperienceStore) -> None:
    with pytest.raises(ValueError):
        store.add(
            QUESTION,
            SQL,
            verified=True,
            validator=lambda sql: (False, "forbidden"),
        )
    assert store.stats()["total"] == 0


def test_deduplicate_same_question_and_schema(store: ExperienceStore) -> None:
    first = store.add(QUESTION, SQL, schema_hash="h1")
    second = store.add(QUESTION, SQL + " WHERE 1=1", schema_hash="h1")
    assert first["created"] is True
    assert second["created"] is False
    assert store.stats()["total"] == 1

    upgraded = store.add(QUESTION, SQL + " WHERE 1=1", verified=True, schema_hash="h1")
    assert upgraded["status"] == "verified"
    assert upgraded["sql"].endswith("WHERE 1=1")


def test_verify_missing_id(store: ExperienceStore) -> None:
    with pytest.raises(KeyError):
        store.verify(999)


def test_verify_runs_validator(store: ExperienceStore) -> None:
    record = store.add(QUESTION, SQL, schema_hash="h1")
    with pytest.raises(ValueError):
        store.verify(int(record["id"]), validator=lambda sql: (False, "bad"))
    assert store.stats()["verified"] == 0


def test_search_similarity_threshold(store: ExperienceStore) -> None:
    record = store.add(QUESTION, SQL, schema_hash="h1")
    store.verify(int(record["id"]))
    assert store.search("完全不相干的天气问题", schema_hash="h1") == []
    related = store.search("2026年6月华东地区销售订单金额", schema_hash="h1")
    assert related
    assert related[0]["id"] == record["id"]


def test_empty_input_rejected(store: ExperienceStore) -> None:
    with pytest.raises(ValueError):
        store.add("", SQL)
    with pytest.raises(ValueError):
        store.add(QUESTION, "")


def test_store_closes_connections(store: ExperienceStore) -> None:
    store.add(QUESTION, SQL, schema_hash="h1")
    store.search(QUESTION, schema_hash="h1")
    store.stats()
    # On Windows an unclosed sqlite connection keeps the file locked.
    store.path.unlink()
    assert not store.path.exists()
