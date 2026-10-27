"""Counterexamples: recorded wrong answers, and the three places they may be read.

The acceptance rule for this layer is that wrong experience never reaches the model, so
the important test here is a negative one: after a failure is recorded, the failed SQL
must not appear in retrieval, in the few-shot block, or in the system prompt — while the
gate that reads the table must still be able to refuse it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nl2sql_agent.memory import ExperienceStore

QUESTION = "各状态的订单数量统计"
WRONG_SQL = "SELECT status, COUNT(*) FROM sales_order GROUP BY secret_table"
OK_SQL = "SELECT status, COUNT(*) AS n FROM sales_order GROUP BY status"


@pytest.fixture()
def store(tmp_path: Path) -> ExperienceStore:
    return ExperienceStore(tmp_path / "experience.db")


def test_failure_with_sql_records_a_counterexample(store: ExperienceStore) -> None:
    result = store.record_failure(
        QUESTION,
        reason="guard_rejected",
        sql=WRONG_SQL,
        error_code="unknown_table",
        schema_hash="h1",
    )
    row = result["counterexample"]
    assert row["error_code"] == "unknown_table"
    assert row["status"] == "open"
    assert row["occur_count"] == 1
    assert store.stats()["counterexamples_open"] == 1


def test_failure_without_sql_writes_no_counterexample(store: ExperienceStore) -> None:
    result = store.record_failure(QUESTION, reason="needs_human", schema_hash="h1")
    assert "counterexample" not in result
    assert store.list_counterexamples() == []


def test_repeated_wrong_sql_counts_once_per_shape(store: ExperienceStore) -> None:
    store.record_failure(QUESTION, reason="guard_rejected", sql=WRONG_SQL, schema_hash="h1")
    store.record_failure(QUESTION, reason="guard_rejected", sql=WRONG_SQL, schema_hash="h1")
    rows = store.list_counterexamples()
    assert len(rows) == 1
    assert rows[0]["occur_count"] == 2
    # The failure queue counts the question; the counterexample counts the answer shape.
    assert store.failure_stats()["failure_occurrences"] == 2

    store.record_failure(QUESTION, reason="execution_error", sql=OK_SQL, schema_hash="h1")
    assert len(store.list_counterexamples()) == 2
    assert store.counterexample_stats()["counterexample_occurrences"] == 3


def test_has_counterexample_matches_question_and_sql_shape(store: ExperienceStore) -> None:
    store.record_failure(
        QUESTION, reason="guard_rejected", sql=WRONG_SQL, error_code="unknown_table", schema_hash="h1"
    )
    assert store.has_counterexample(QUESTION, WRONG_SQL, schema_hash="h1") is not None
    assert store.has_counterexample(QUESTION, WRONG_SQL + " ", schema_hash="h1") is not None
    assert store.has_counterexample(QUESTION, WRONG_SQL, schema_hash="h2") is None
    assert store.has_counterexample(QUESTION, OK_SQL, schema_hash="h1") is None


def test_resolving_a_failure_closes_its_counterexamples(store: ExperienceStore) -> None:
    item = store.record_failure(
        QUESTION, reason="needs_human", sql=WRONG_SQL, error_code="execution_error", schema_hash="h1"
    )
    resolved = store.resolve_failure(item["id"], OK_SQL, validator=lambda sql: (True, "ok"))
    assert resolved["resolved_counterexamples"] == 1
    assert store.list_counterexamples(status="open") == []
    assert len(store.list_counterexamples(status="resolved")) == 1
    # The corrected pair is a real experience now; the wrong one stays as history.
    hits = store.search(QUESTION, schema_hash="h1")
    assert [row["sql"] for row in hits] == [OK_SQL]


def test_admission_gate_refuses_a_recorded_wrong_sql(runtime) -> None:
    runtime.store.record_failure(
        QUESTION,
        reason="guard_rejected",
        sql=OK_SQL,
        error_code="column_not_aggregated",
        schema_hash=runtime.catalog.schema_hash(),
    )
    result = _call(runtime, "save_experience", {"question": QUESTION, "sql": OK_SQL})
    assert result["ok"] is False
    assert "失败反例队列" in result["error"]
    assert runtime.store.stats()["candidates"] == 0

    other = _call(runtime, "save_experience", {"question": "待发货订单有哪些", "sql": OK_SQL})
    assert other["ok"] is True


def test_counterexamples_never_reach_the_prompt(runtime) -> None:
    schema_hash = runtime.catalog.schema_hash()
    for sql in (WRONG_SQL, "SELECT * FROM sales_order"):
        runtime.store.record_failure(
            QUESTION, reason="guard_rejected", sql=sql, error_code="unknown_table", schema_hash=schema_hash
        )
        runtime.store.record_failure(
            "各仓库的库存量", reason="execution_error", sql=sql, error_code="column_not_found", schema_hash=schema_hash
        )
    agent = runtime.agent
    assert runtime.store.list_counterexamples(status="open")
    assert agent._few_shot_block(QUESTION, "group_by") == ""
    assert WRONG_SQL not in agent._system_prompt(agent._few_shot_block(QUESTION, "group_by"))

    search = _call(runtime, "search_experience", {"question": QUESTION})
    assert search["ok"] is True
    assert search["data"]["count"] == 0
    assert all(WRONG_SQL not in str(item) for item in search["data"]["examples"])


def _call(runtime, name: str, arguments: dict) -> dict:
    return runtime.agent.tools.call(name, arguments)
