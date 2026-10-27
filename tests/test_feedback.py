from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nl2sql_agent.feedback import build_weekly_report
from nl2sql_agent.memory import ExperienceStore

QUESTION = "按状态统计订单数量"
WRONG_SQL = "SELECT status, COUNT(*) FROM sales_order GROUP BY secret_table"
GOOD_SQL = "SELECT status, COUNT(*) AS n FROM sales_order GROUP BY status"


@pytest.fixture()
def store(tmp_path: Path) -> ExperienceStore:
    return ExperienceStore(tmp_path / "experience.db")


def ok_validator(_sql: str) -> tuple[bool, str]:
    return True, "ok"


def test_feedback_creates_a_failure_and_a_counterexample(store: ExperienceStore) -> None:
    result = store.record_feedback(
        QUESTION,
        WRONG_SQL,
        reason="result_wrong",
        detail="状态列看起来是物料状态",
        schema_hash="h1",
        session_id="s1",
        turn_index=2,
        trace_id="t1",
    )

    feedback = result["feedback"]
    assert result["created"] is True and result["duplicate"] is False
    assert feedback["status"] == "open"
    assert feedback["reason"] == "result_wrong"
    assert feedback["trace_id"] == "t1"
    assert feedback["failure_id"] == result["failure"]["id"]
    assert result["failure"]["reason"] == "user_feedback:result_wrong"
    assert result["failure"]["counterexample"]["error_code"] == "feedback_result_wrong"
    assert store.list_failures()[0]["question"] == QUESTION
    assert store.list_counterexamples()[0]["sql"] == WRONG_SQL
    assert store.feedback_stats() == {
        "feedback_total": 1,
        "feedback_open": 1,
        "feedback_approved": 0,
        "feedback_rejected": 0,
        "feedback_by_reason": {"result_wrong": 1},
        "feedback_occurrences": 1,
    }


def test_the_criticised_sql_never_becomes_a_candidate(store: ExperienceStore) -> None:
    store.record_feedback(QUESTION, WRONG_SQL, reason="metric_wrong", schema_hash="h1")

    assert store.list(status="candidate") == []
    assert store.search(QUESTION, schema_hash="h1") == []
    # The admission gate sees the complaint, so an agent cannot save the same
    # rejected answer as a reusable example on the next turn.
    assert store.has_counterexample(QUESTION, WRONG_SQL, schema_hash="h1") is not None


def test_suggested_sql_is_stored_as_a_candidate_only(store: ExperienceStore) -> None:
    result = store.record_feedback(
        QUESTION,
        WRONG_SQL,
        reason="field_wrong",
        suggested_sql=GOOD_SQL,
        schema_hash="h1",
        session_id="s1",
        turn_index=0,
    )

    candidate = result["candidate"]
    assert candidate["status"] == "candidate"
    assert result["feedback"]["experience_id"] == candidate["id"]
    assert store.search(QUESTION, schema_hash="h1") == []


def test_repeated_click_on_the_same_turn_counts_once(store: ExperienceStore) -> None:
    first = store.record_feedback(
        QUESTION, WRONG_SQL, reason="result_wrong", schema_hash="h1", session_id="s1", turn_index=0
    )
    second = store.record_feedback(
        QUESTION, WRONG_SQL, reason="result_wrong", schema_hash="h1", session_id="s1", turn_index=0
    )

    assert second["created"] is False and second["duplicate"] is True
    assert second["feedback"]["id"] == first["feedback"]["id"]
    assert second["feedback"]["occur_count"] == 2
    # A double click is one incident for the failure queue, not two.
    assert store.failure_stats()["failure_occurrences"] == 1
    assert store.counterexample_stats()["counterexample_occurrences"] == 1


def test_approve_requires_a_passing_correction_and_verifies_it(store: ExperienceStore) -> None:
    feedback = store.record_feedback(
        QUESTION, WRONG_SQL, reason="metric_wrong", schema_hash="h1", session_id="s1", turn_index=0
    )

    with pytest.raises(ValueError, match="SQL"):
        store.review_feedback(feedback["feedback"]["id"], "approve", validator=ok_validator)
    with pytest.raises(ValueError, match="validator"):
        store.review_feedback(feedback["feedback"]["id"], "approve", sql=GOOD_SQL)
    with pytest.raises(ValueError, match="不能结案"):
        store.review_feedback(
            feedback["feedback"]["id"],
            "approve",
            sql=GOOD_SQL,
            validator=lambda _sql: (False, "guard refused"),
        )
    assert store.get_feedback(feedback["feedback"]["id"])["status"] == "open"

    result = store.review_feedback(
        feedback["feedback"]["id"],
        "approve",
        sql=GOOD_SQL,
        note="状态口径已确认",
        validator=ok_validator,
        reviewer="analyst",
    )
    assert result["feedback"]["status"] == "approved"
    assert result["feedback"]["reviewer"] == "analyst"
    assert result["experience"]["status"] == "verified"
    assert store.get_failure(feedback["failure"]["id"])["status"] == "resolved"
    assert store.list_counterexamples(status="open") == []
    hit = store.search(QUESTION, schema_hash="h1")
    assert hit and hit[0]["sql"] == GOOD_SQL


def test_reject_needs_a_reason_and_only_undoes_its_own_damage(store: ExperienceStore) -> None:
    # A real execution failure already exists on the same question.
    existing = store.record_failure(
        QUESTION, reason="execution_error", sql=WRONG_SQL, error_code="unknown_table", schema_hash="h1"
    )
    assert existing["created"] is True
    feedback = store.record_feedback(
        QUESTION, WRONG_SQL, reason="result_wrong", schema_hash="h1", session_id="s1", turn_index=0
    )
    assert feedback["failure"]["id"] == existing["id"]

    with pytest.raises(ValueError, match="原因"):
        store.review_feedback(feedback["feedback"]["id"], "reject")

    result = store.review_feedback(
        feedback["feedback"]["id"], "reject", note="用户看错了口径"
    )
    assert result["feedback"]["status"] == "rejected"
    # The queue item predates the complaint, so rejecting the complaint must not
    # dismiss the execution failure. The original counterexample survives too.
    assert result["dismissed_failure"] is False
    assert result["resolved_counterexamples"] == 0
    assert store.get_failure(existing["id"])["status"] == "open"
    row = store.has_counterexample(QUESTION, WRONG_SQL, schema_hash="h1")
    assert row is not None and row["error_code"] == "unknown_table" and row["status"] == "open"


def test_reject_dismisses_a_feedback_owned_queue_item(store: ExperienceStore) -> None:
    feedback = store.record_feedback(
        QUESTION, WRONG_SQL, reason="field_wrong", schema_hash="h1", session_id="s1", turn_index=0
    )
    result = store.review_feedback(feedback["feedback"]["id"], "reject", note="不是字段问题")

    assert result["dismissed_failure"] is True
    assert result["resolved_counterexamples"] == 1
    assert store.get_failure(feedback["failure"]["id"])["status"] == "dismissed"
    assert store.list_counterexamples(status="open") == []


def test_unknown_reason_and_unknown_feedback_are_rejected(store: ExperienceStore) -> None:
    with pytest.raises(ValueError, match="reason"):
        store.record_feedback(QUESTION, WRONG_SQL, reason="looks_bad")
    with pytest.raises(KeyError):
        store.review_feedback(999, "reject", note="不存在")
    with pytest.raises(ValueError, match="action"):
        store.review_feedback(999, "ignore")


def test_schema_impact_counts_stale_feedback_and_failures(store: ExperienceStore) -> None:
    store.add(QUESTION, GOOD_SQL, verified=True, schema_hash="old")
    store.record_feedback(
        QUESTION, WRONG_SQL, reason="result_wrong", schema_hash="old", session_id="s1", turn_index=0
    )
    store.record_failure("另一个问题", reason="failed", schema_hash="old")

    impact = store.schema_impact("new")
    assert impact["experience"]["stale_rows"] == 1
    assert impact["experience"]["stale_hashes"] == ["old"]
    assert impact["feedback"]["on_stale"] == 1
    assert impact["failures"]["on_stale"] == 2
    assert impact["active_schema_hash"] == "new"


def _turn(ts: datetime, injected: int, tokens: int = 10) -> dict:
    return {
        "ts": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event": "turn",
        "status": "ok",
        "tokens": tokens,
        "few_shot": {"injected": injected},
    }


def test_weekly_report_hides_text_and_counts_only_the_window(
    store: ExperienceStore, tmp_path: Path
) -> None:
    now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    store.record_feedback(
        QUESTION, WRONG_SQL, reason="result_wrong", schema_hash="h1", session_id="s1", turn_index=0
    )
    store.record_failure("另一个失败问题", reason="failed", schema_hash="h1")
    log = tmp_path / "events.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in (
                _turn(now - timedelta(hours=1), injected=2, tokens=12),
                _turn(now - timedelta(days=2), injected=0, tokens=8),
                _turn(now - timedelta(days=10), injected=5, tokens=99),
            )
        ),
        encoding="utf-8",
    )

    report = build_weekly_report(
        store, active_schema_hash="h2", days=7, log_path=log, now=now
    )

    assert report["privacy"] == {"question_text": "fingerprint_only", "sql": "omitted"}
    assert report["feedback"]["total"] == 1
    assert report["feedback"]["by_reason"] == {"result_wrong": 1}
    assert report["feedback"]["top"][0]["fingerprint"]
    assert "question" not in report["feedback"]["top"][0]
    assert "sql" not in report["feedback"]["top"][0]
    assert all("question" not in item for item in report["top_failures"])
    reuse = report["experience_reuse"]
    assert reuse["configured"] is True
    assert reuse["turns"] == 2  # the 10-day-old turn is outside the window
    assert reuse["experience_reuse_hits"] == 1
    assert reuse["experience_reuse_rate"] == 0.5
    assert reuse["total_tokens"] == 20
    assert report["schema_impact"]["feedback"]["on_stale"] == 1


def test_weekly_report_can_include_text_on_request(
    store: ExperienceStore, tmp_path: Path
) -> None:
    store.record_feedback(
        QUESTION, WRONG_SQL, reason="field_wrong", detail="字段选错了", schema_hash="h1",
        session_id="s1", turn_index=0,
    )
    report = build_weekly_report(store, show_text=True, log_path=tmp_path / "missing.jsonl")
    item = report["feedback"]["top"][0]
    assert item["question"] == QUESTION
    assert item["detail"] == "字段选错了"
    assert report["privacy"]["question_text"] == "included"
    assert report["experience_reuse"]["configured"] is True
    assert report["experience_reuse"]["missing"] is True
    assert report["experience_reuse"]["experience_reuse_rate"] is None
