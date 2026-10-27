from __future__ import annotations

from dataclasses import replace

import pytest

from nl2sql_agent.config import Settings
from nl2sql_agent.conversation import Turn
from nl2sql_agent.runtime import Runtime, create_runtime

pytest.importorskip("fastapi", reason="Web 控制台需要 pip install -e '.[web]'")
pytest.importorskip("httpx", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

from nl2sql_agent.web.app import create_app  # noqa: E402

QUESTION = "按状态统计订单数量"
GOOD_SQL = "SELECT status, COUNT(*) AS n FROM sales_order GROUP BY status"
SUGGESTED_SQL = "SELECT status, COUNT(*) AS cnt FROM sales_order GROUP BY status"


@pytest.fixture()
def client(runtime: Runtime):
    with TestClient(create_app(runtime)) as session:
        yield session


class _Result:
    status = "ok"
    elapsed_ms = 1.0
    trace_id = "trace-feedback"

    def __init__(self, question: str, sql: str = GOOD_SQL, status: str = "ok") -> None:
        self.question = question
        self.sql = sql
        self.status = status

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "effective_question": self.question,
            "sql": self.sql,
            "trace_id": self.trace_id,
        }

    def to_turn(self) -> Turn:
        return Turn(
            question=self.question,
            effective_question=self.question,
            status=self.status,
            sql=self.sql,
            trace_id=self.trace_id,
        )


def _ask(client: TestClient, monkeypatch, runtime: Runtime, question: str = QUESTION) -> str:
    monkeypatch.setattr(
        runtime.agent, "run", lambda q, history=None, trace_id="", **kwargs: _Result(q)
    )
    session_id = client.post("/api/sessions").json()["session_id"]
    response = client.post(f"/api/sessions/{session_id}/ask", json={"question": question})
    assert response.status_code == 200
    return session_id


def test_feedback_reads_the_turn_not_the_request(client, runtime: Runtime, monkeypatch) -> None:
    session_id = _ask(client, monkeypatch, runtime)

    response = client.post(
        f"/api/sessions/{session_id}/feedback",
        json={"reason": "result_wrong", "turn_index": 0, "detail": "分类列不对",
              "question": "伪造问题", "sql": "SELECT 1"},
    )

    assert response.status_code == 200
    payload = response.json()
    feedback = payload["feedback"]
    assert feedback["question"] == QUESTION
    assert feedback["sql"] == GOOD_SQL
    assert feedback["trace_id"] == "trace-feedback"
    assert feedback["session_id"] == session_id
    assert feedback["turn_index"] == 0
    assert payload["failure"]["reason"] == "user_feedback:result_wrong"
    assert payload["candidate"] is None


def test_feedback_rejects_unknown_reason_and_unanswered_turns(client, runtime: Runtime, monkeypatch) -> None:
    session_id = _ask(client, monkeypatch, runtime)
    bad = client.post(
        f"/api/sessions/{session_id}/feedback", json={"reason": "looks_bad", "turn_index": 0}
    )
    assert bad.status_code == 422

    out_of_range = client.post(
        f"/api/sessions/{session_id}/feedback", json={"reason": "result_wrong", "turn_index": 9}
    )
    assert out_of_range.status_code == 422

    empty_session = client.post("/api/sessions").json()["session_id"]
    empty = client.post(
        f"/api/sessions/{empty_session}/feedback", json={"reason": "result_wrong"}
    )
    assert empty.status_code == 422

    missing = client.post(
        "/api/sessions/does-not-exist/feedback", json={"reason": "result_wrong"}
    )
    assert missing.status_code == 404


def test_feedback_requires_a_turn_with_sql(client, runtime: Runtime, monkeypatch) -> None:
    monkeypatch.setattr(
        runtime.agent,
        "run",
        lambda q, history=None, trace_id="", **kwargs: _Result(
            q, sql="", status="needs_clarification"
        ),
    )
    session_id = client.post("/api/sessions").json()["session_id"]
    assert client.post(f"/api/sessions/{session_id}/ask", json={"question": QUESTION}).status_code == 200

    response = client.post(
        f"/api/sessions/{session_id}/feedback", json={"reason": "result_wrong"}
    )
    assert response.status_code == 422
    assert "SQL" in response.json()["detail"]


def test_suggested_sql_must_pass_the_guard(client, runtime: Runtime, monkeypatch) -> None:
    session_id = _ask(client, monkeypatch, runtime)
    response = client.post(
        f"/api/sessions/{session_id}/feedback",
        json={"reason": "field_wrong", "suggested_sql": "SELECT * FROM sales_order"},
    )
    assert response.status_code == 422
    assert "护栏" in response.json()["detail"]


def test_approve_and_reject_reviews_are_audited(client, runtime: Runtime, monkeypatch) -> None:
    session_id = _ask(client, monkeypatch, runtime)
    first = client.post(
        f"/api/sessions/{session_id}/feedback",
        json={"reason": "metric_wrong", "suggested_sql": SUGGESTED_SQL},
    ).json()
    assert first["candidate"]["status"] == "candidate"
    feedback_id = first["feedback"]["id"]

    no_sql = client.post(
        f"/api/feedback/{feedback_id}/review", json={"action": "approve", "note": "ok"}
    )
    assert no_sql.status_code == 400
    assert "SQL" in no_sql.json()["detail"]

    approved = client.post(
        f"/api/feedback/{feedback_id}/review",
        json={"action": "approve", "sql": SUGGESTED_SQL, "note": "口径确认", "reviewer": "analyst"},
    )
    assert approved.status_code == 200
    assert approved.json()["feedback"]["status"] == "approved"
    assert approved.json()["experience"]["status"] == "verified"
    assert client.get("/api/feedback?status=open").json()["items"] == []
    assert len(client.get("/api/feedback?status=approved").json()["items"]) == 1
    assert client.get("/api/experience?status=verified").json()["items"]

    second = client.post(
        f"/api/sessions/{session_id}/feedback", json={"reason": "result_wrong", "turn_index": 0}
    ).json()
    rejected = client.post(
        f"/api/feedback/{second['feedback']['id']}/review",
        json={"action": "reject", "note": "用户看错了"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["feedback"]["status"] == "rejected"
    assert client.post(
        f"/api/feedback/{second['feedback']['id']}/review",
        json={"action": "reject", "note": "重复处理"},
    ).status_code == 400


def test_review_rejects_an_unknown_feedback(client) -> None:
    assert client.post(
        "/api/feedback/999/review", json={"action": "reject", "note": "不存在"}
    ).status_code == 404


def test_turn_payload_exposes_the_trace_id(client, runtime: Runtime, monkeypatch) -> None:
    session_id = _ask(client, monkeypatch, runtime)
    turns = client.get(f"/api/sessions/{session_id}").json()["turns"]
    assert turns[0]["trace_id"] == "trace-feedback"
