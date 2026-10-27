from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from nl2sql_agent.config import Settings
from nl2sql_agent.conversation import Turn
from nl2sql_agent.runtime import Runtime, create_runtime

fastapi = pytest.importorskip("fastapi", reason="Web 控制台需要 pip install -e '.[web]'")
pytest.importorskip("httpx", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.exceptions import HTTPException  # noqa: E402

from nl2sql_agent.web.app import SessionStore, create_app  # noqa: E402

REVENUE = "2026年6月华东地区销售订单总金额是多少？"
FOLLOW_UP = "那华南呢？"


@pytest.fixture()
def client(runtime: Runtime):
    with TestClient(create_app(runtime)) as session:
        yield session


def test_health_reports_the_runtime(client) -> None:
    payload = client.get("/api/health").json()
    assert payload["provider"] == "mock"
    assert payload["model"] == "RuleBasedLLM"
    # The route is part of "which build is answering": without it a console left in
    # the other mode looks healthy and silently behaves differently.
    assert payload["mode"] == "auto"
    assert payload["single_turn_cases"] == 123
    assert payload["multi_turn_cases"] == 10
    assert payload["multi_turn_enabled"] is True
    assert len(payload["schema_hash"]) == 16
    assert payload["guard"] == {"principal": "", "ast_guard": "off"}
    # Empty list is the fact "pooling is off", not a broken reading: the default
    # NL2SQL_DB_POOL_SIZE=1 never registers a pool.
    assert payload["pool"] == []


def test_health_says_the_mock_runtime_has_no_resilience_wrapper(client) -> None:
    payload = client.get("/api/health").json()["llm_resilience"]
    assert payload["enabled"] is False
    assert "mock" in payload["reason"]


def test_health_shows_the_pool_without_exposing_a_host(client) -> None:
    from nl2sql_agent.tools.db import MssqlTarget, close_pools, configure_pool

    target = MssqlTarget(
        server="db.internal", port=1433, database="erp_test", username="reader", password="secret"
    )
    # A pool registers without opening anything, so this exercises the reporting path
    # without a driver: what must not leak is the host and the account.
    assert configure_pool(target, max_size=2) is not None
    try:
        payload = client.get("/api/health").json()
    finally:
        close_pools()
    entry = payload["pool"][0]
    assert entry["target"] == "mssql:erp_test"
    assert entry["max_size"] == 2 and entry["closed"] is False
    body = json.dumps(payload)
    assert "secret" not in body and "db.internal" not in body and "reader" not in body


def test_health_names_the_role_and_the_view_it_serves(settings: Settings) -> None:
    scoped = create_runtime(replace(settings, principal_id="rep_east"))
    with TestClient(create_app(scoped)) as session:
        payload = session.get("/api/health").json()
    assert payload["guard"] == {"principal": "rep_east", "ast_guard": "off"}
    # Experience records are keyed by the agent-facing view's hash. Reporting the
    # full catalog's here would make the console flag every stored experience as
    # "schema 已变更" for any role that narrows the schema.
    assert payload["schema_hash"] == scoped.agent.schema_hash
    assert payload["schema_hash"] != scoped.catalog.schema_hash()


def test_ask_is_stateless_for_the_agent_but_stateful_for_the_session(client) -> None:
    session_id = client.post("/api/sessions").json()["session_id"]
    first = client.post(f"/api/sessions/{session_id}/ask", json={"question": REVENUE}).json()
    assert first["result"]["rows"][0][0] == 62280.0
    assert first["conversation"]["turns"][0]["status"] == "ok"

    second = client.post(f"/api/sessions/{session_id}/ask", json={"question": FOLLOW_UP}).json()
    assert second["result"]["effective_question"] == "2026年6月华南地区销售订单总金额是多少？"
    assert second["result"]["rewrite_source"] == "rule"
    assert second["result"]["rows"][0][0] == 94410.0
    assert len(second["conversation"]["turns"]) == 2


def test_an_ok_answer_hands_the_narrower_questions_to_the_console(client) -> None:
    # The console's pills read ``result.alternatives``; an assume-tier gap that never
    # reaches the payload would make the shipped 口径 prompt invisible in the browser.
    session_id = client.post("/api/sessions").json()["session_id"]
    result = client.post(f"/api/sessions/{session_id}/ask", json={"question": "销售额是多少？"}).json()["result"]

    assert result["status"] == "ok"
    assert result["clarification"] is None
    assert len(result["alternatives"]) == 3
    assert result["clarify"]["codes"] == ["time_range_missing"]


def test_reset_clears_the_conversation(client) -> None:
    session_id = client.post("/api/sessions").json()["session_id"]
    client.post(f"/api/sessions/{session_id}/ask", json={"question": REVENUE})
    client.delete(f"/api/sessions/{session_id}")
    assert client.get(f"/api/sessions/{session_id}").json()["turns"] == []


def test_unknown_session_and_empty_question_are_rejected(client) -> None:
    assert client.get("/api/sessions/does-not-exist").status_code == 404
    session_id = client.post("/api/sessions").json()["session_id"]
    assert client.post(f"/api/sessions/{session_id}/ask", json={"question": "   "}).status_code == 422


def test_schema_exposes_guardrails_and_column_flags(client) -> None:
    payload = client.get("/api/schema").json()
    names = {table["name"] for table in payload["tables"]}
    assert {"sales_order", "material", "inventory"} <= names
    assert payload["relationships"] and payload["business_rules"]
    columns = {
        column["name"]
        for table in payload["tables"]
        for column in table["columns"]
        if column["sensitive"]
    }
    assert columns, "schema 应至少标出一个敏感列"


def test_schema_search_ranks_by_bm25(client) -> None:
    hits = client.get("/api/schema/search", params={"q": "库存低于安全库存的物料"}).json()["tables"]
    assert hits and hits[0]["name"] == "material"
    assert hits[0]["score"] >= hits[-1]["score"]


def test_experience_and_failure_endpoints_are_read_only(client) -> None:
    experience = client.get("/api/experience", params={"status": "verified"}).json()
    assert set(experience["stats"]) >= {"total", "verified", "candidates", "failures_open"}
    assert all(item["status"] == "verified" for item in experience["items"])
    assert client.get("/api/experience", params={"status": "bogus"}).status_code == 422

    failures = client.get("/api/failures").json()
    assert failures["items"] == []
    assert failures["stats"]["failures_total"] == 0
    assert failures["stats"]["failures_open"] == 0


def test_eval_run_can_include_the_multi_turn_suite(client) -> None:
    single = client.post("/api/eval/run", json={"multi_turn": False}).json()["report"]
    assert (single["passed"], single["total"]) == (61, 123)
    assert single["by_kind"]["single"]["cases"] == 123
    assert "multi" not in single["by_kind"]

    mixed = client.post("/api/eval/run", json={"multi_turn": True}).json()["report"]
    assert (mixed["passed"], mixed["total"]) == (71, 133)
    assert mixed["metrics"]["multi_turn_accuracy"] == 1.0
    assert mixed["by_kind"]["multi"]["avg_turns"] == 2.0
    assert mixed["judge"]["enabled"] is False


def test_eval_run_with_judge_is_refused_without_credentials(runtime: Runtime) -> None:
    with TestClient(create_app(runtime)) as client:
        response = client.post("/api/eval/run", json={"multi_turn": False, "judge": True})
    assert response.status_code == 400
    assert "Judge" in response.json()["detail"]


def test_reports_are_listed_and_path_traversal_is_blocked(client, runtime: Runtime) -> None:
    report_dir = Path(runtime.settings.home) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "eval_manual.json").write_text(json.dumps({"passed": 1}), encoding="utf-8")

    names = [item["name"] for item in client.get("/api/eval/reports").json()["items"]]
    assert "eval_manual.json" in names
    assert client.get("/api/eval/reports/eval_manual.json").json() == {"passed": 1}
    assert client.get("/api/eval/reports/eval_missing.json").status_code == 404
    assert client.get("/api/eval/reports/.env").status_code == 400


def test_console_static_assets_are_served(client) -> None:
    index = client.get("/")
    assert index.status_code == 200
    assert "NL2SQL" in index.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/app.css").status_code == 200


def test_session_store_evicts_the_least_recently_used() -> None:
    store = SessionStore(limit=2)
    for name in ("a", "b", "c"):
        store.create(name)
    assert len(store) == 2
    with pytest.raises(HTTPException):
        store.get("a")
    with pytest.raises(HTTPException):
        store.lock("a")
    store.get("b")
    store.create("d")
    assert store.get("b") is not None
    with pytest.raises(HTTPException):
        store.get("c")


def test_reset_keeps_the_same_session_lock() -> None:
    store = SessionStore(limit=2)
    store.create("a")
    lock = store.lock("a")
    store.reset("a")
    assert store.lock("a") is lock
    assert store.get("a").turns == []


class _FakeResult:
    status = "ok"
    elapsed_ms = 1.0

    def __init__(self, question: str) -> None:
        self.question = question

    def to_dict(self) -> dict:
        return {"status": "ok", "effective_question": self.question, "rows": []}

    def to_turn(self) -> Turn:
        return Turn(question=self.question, effective_question=self.question, status="ok")


def test_same_session_turns_are_serialized(client, runtime: Runtime, monkeypatch) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    order: list[tuple[str, str]] = []

    def fake_run(
        question: str, history=None, trace_id: str = "", **kwargs
    ) -> _FakeResult:
        order.append(("start", question))
        if question == "第一问":
            first_started.set()
            assert release_first.wait(5), "测试没有放行第一问"
        order.append(("end", question))
        return _FakeResult(question)

    monkeypatch.setattr(runtime.agent, "run", fake_run)
    session_id = client.post("/api/sessions").json()["session_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            client.post, f"/api/sessions/{session_id}/ask", json={"question": "第一问"}
        )
        assert first_started.wait(5)
        second = pool.submit(
            client.post, f"/api/sessions/{session_id}/ask", json={"question": "第二问"}
        )
        time.sleep(0.1)
        # The second turn must be waiting on the session lock, not inside the agent:
        # it would otherwise read a history that does not contain the first answer.
        assert order == [("start", "第一问")]
        release_first.set()
        assert first.result(5).status_code == 200
        assert second.result(5).status_code == 200

    assert order == [
        ("start", "第一问"),
        ("end", "第一问"),
        ("start", "第二问"),
        ("end", "第二问"),
    ]
    turns = client.get(f"/api/sessions/{session_id}").json()["turns"]
    assert [turn["question"] for turn in turns] == ["第一问", "第二问"]


def test_distinct_sessions_run_in_parallel(client, runtime: Runtime, monkeypatch) -> None:
    barrier = threading.Barrier(2, timeout=10)

    def fake_run(
        question: str, history=None, trace_id: str = "", **kwargs
    ) -> _FakeResult:
        # Both requests must be inside the agent at the same time; a global lock
        # would make this barrier time out and turn the test red.
        barrier.wait()
        return _FakeResult(question)

    monkeypatch.setattr(runtime.agent, "run", fake_run)
    first = client.post("/api/sessions").json()["session_id"]
    second = client.post("/api/sessions").json()["session_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(client.post, f"/api/sessions/{first}/ask", json={"question": "甲"})
        b = pool.submit(client.post, f"/api/sessions/{second}/ask", json={"question": "乙"})
        assert a.result(15).status_code == 200
        assert b.result(15).status_code == 200


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        name = "message"
        data = ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data += line[len("data:"):].strip()
        events.append((name, json.loads(data) if data else {}))
    return events


def _event_payload(events: list[tuple[str, dict]], name: str) -> dict:
    for seen, data in events:
        if seen == name:
            return data
    raise AssertionError(f"没有 {name} 事件：{[seen for seen, _ in events]}")


def test_ask_stream_pushes_each_tool_step_before_the_result(
    client, runtime: Runtime, monkeypatch
) -> None:
    def fake_run(question: str, history=None, trace_id: str = "", **kwargs) -> _FakeResult:
        progress = kwargs.get("progress")
        assert callable(progress), "流式端点必须把 progress 传给 Agent"
        progress({"step": 1, "tool": "search_tables", "ok": True, "summary": "tables=sales_order"})
        progress({"step": 2, "tool": "execute_sql", "ok": True, "summary": "rows=1"})
        return _FakeResult(question)

    monkeypatch.setattr(runtime.agent, "run", fake_run)
    session_id = client.post("/api/sessions").json()["session_id"]
    response = client.post(
        f"/api/sessions/{session_id}/ask/stream", json={"question": "合同有多少份"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _sse_events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "accepted"
    assert names.index("step") < names.index("result") < names.index("done")
    assert names.count("step") == 2
    assert _event_payload(events, "result")["result"]["status"] == "ok"
    # The turn still lands in the conversation: streaming is a view, not a second path.
    turns = client.get(f"/api/sessions/{session_id}").json()["turns"]
    assert [turn["question"] for turn in turns] == ["合同有多少份"]


def test_ask_stream_reports_agent_failure_instead_of_ending_silently(
    client, runtime: Runtime, monkeypatch
) -> None:
    def fake_run(question: str, history=None, trace_id: str = "", **kwargs) -> _FakeResult:
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(runtime.agent, "run", fake_run)
    session_id = client.post("/api/sessions").json()["session_id"]
    response = client.post(
        f"/api/sessions/{session_id}/ask/stream", json={"question": "会失败的问题"}
    )
    events = _sse_events(response.text)
    names = [name for name, _ in events]
    assert "error" in names and "done" in names
    assert "模型不可用" in _event_payload(events, "error")["message"]


def test_ask_stream_rejects_an_empty_question_like_the_plain_endpoint(client) -> None:
    session_id = client.post("/api/sessions").json()["session_id"]
    response = client.post(f"/api/sessions/{session_id}/ask/stream", json={"question": "   "})
    assert response.status_code == 422
