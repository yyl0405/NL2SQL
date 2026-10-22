from __future__ import annotations

import json
from dataclasses import replace

from nl2sql_agent.agent import NL2SQLAgent
from nl2sql_agent.config import Settings
from nl2sql_agent.llm import LLMResponse, ToolCall
from nl2sql_agent.runtime import Runtime


class ScriptedLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def chat(self, messages, tools=None) -> LLMResponse:
        self.calls += 1
        if not self.responses:
            raise AssertionError("scripted model ran out of responses")
        return self.responses.pop(0)


def test_revenue_question_end_to_end(runtime: Runtime) -> None:
    result = runtime.agent.run("2026年6月华东地区销售订单总金额是多少？")
    assert result.status == "ok"
    assert result.sql
    assert result.row_count == 1
    assert result.rows[0][0] == 62280.0
    tools = [item["tool"] for item in result.tool_trace]
    assert tools[0] == "search_tables"
    assert "validate_sql" in tools
    assert "execute_sql" in tools
    assert result.steps == len(tools) + 1
    assert result.elapsed_ms >= 0


def test_refusal_has_no_sql(runtime: Runtime) -> None:
    result = runtime.agent.run("客户满意度评分是多少？")
    assert result.status == "needs_clarification"
    assert result.sql is None
    assert result.rows == []
    assert result.row_count == 0


def test_unsupported_question_is_clarified(runtime: Runtime) -> None:
    result = runtime.agent.run("今天杭州天气怎么样？")
    assert result.status == "needs_clarification"
    assert result.sql is None


def test_agent_never_claims_ok_without_execution(runtime: Runtime) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                content=json.dumps(
                    {
                        "status": "ok",
                        "answer": "销售额是 62280 元",
                        "sql": "SELECT 1",
                        "confidence": 0.99,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    agent = NL2SQLAgent(runtime.catalog, llm, runtime.agent.tools, runtime.settings)
    result = agent.run("6月销售额是多少？")
    assert result.status == "needs_clarification"
    assert result.confidence <= 0.4
    assert result.rows == []


def test_agent_hands_unknown_tool_errors_back_to_model(runtime: Runtime) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="missing_tool", arguments={})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_human", "answer": "无法完成", "confidence": 0.1},
                    ensure_ascii=False,
                )
            ),
        ]
    )
    agent = NL2SQLAgent(runtime.catalog, llm, runtime.agent.tools, runtime.settings)
    result = agent.run("随便问问")
    assert result.status == "needs_human"
    assert result.tool_trace[0]["ok"] is False
    assert "未知工具" in result.tool_trace[0]["summary"]


def test_agent_accumulates_token_usage(runtime: Runtime) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="search_tables", arguments={"query": "订单", "top_k": 1})
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_human", "answer": "无法完成", "confidence": 0.1},
                    ensure_ascii=False,
                ),
                finish_reason="stop",
                usage={"prompt_tokens": 20, "completion_tokens": 3},
            ),
        ]
    )
    agent = NL2SQLAgent(runtime.catalog, llm, runtime.agent.tools, runtime.settings)
    result = agent.run("随便问问")
    assert result.status == "needs_human"
    assert result.tokens == 35
    assert result.to_dict()["tokens"] == 35


def test_agent_stops_at_max_steps(runtime: Runtime) -> None:
    settings = replace(runtime.settings, max_steps=2)

    class LoopingLLM:
        def chat(self, messages, tools=None) -> LLMResponse:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="loop",
                        name="search_tables",
                        arguments={"query": "订单", "top_k": 1},
                    )
                ],
                finish_reason="tool_calls",
            )

    agent = NL2SQLAgent(runtime.catalog, LoopingLLM(), runtime.agent.tools, settings)
    result = agent.run("不断搜索")
    assert result.status == "max_steps"
    assert result.steps == 2
    assert result.error == "max_steps_exceeded"


class RecordingLLM:
    """Captures the prompt it was given, then answers without tools."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages: list[dict] = []

    def chat(self, messages, tools=None) -> LLMResponse:
        self.messages = [dict(item) for item in messages]
        return LLMResponse(
            content=json.dumps(self.payload, ensure_ascii=False),
            finish_reason="stop",
        )


class LoopingFailureLLM:
    """Always answers with the same terminal-failure payload."""

    def chat(self, messages, tools=None) -> LLMResponse:
        return LLMResponse(
            content=json.dumps({"status": "needs_human", "answer": "无法完成"}, ensure_ascii=False),
            finish_reason="stop",
        )


VERIFIED_SQL = "SELECT status, COUNT(*) AS order_count FROM sales_order GROUP BY status"


def _agent_with_experience(runtime, *, question, sql, verified, settings=None):
    settings = settings or runtime.settings
    store = runtime.store
    store.add(question, sql, category="group_by", verified=verified, schema_hash="h1")
    return NL2SQLAgent(
        runtime.catalog,
        RecordingLLM({"status": "needs_clarification", "answer": "需要更多信息", "confidence": 0.2}),
        runtime.agent.tools,
        settings,
        store=store,
        schema_hash="h1",
    )


def test_verified_experience_is_injected_as_few_shot(runtime) -> None:
    agent = _agent_with_experience(
        runtime, question="按状态统计订单数量", sql=VERIFIED_SQL, verified=True
    )
    agent.run("各状态的单子数量统计一下")
    system = agent.llm.messages[0]["content"]
    assert "参考经验" in system
    assert VERIFIED_SQL in system


def test_candidate_experience_is_never_injected(runtime) -> None:
    agent = _agent_with_experience(
        runtime, question="按状态统计订单数量", sql=VERIFIED_SQL, verified=False
    )
    agent.run("各状态的单子数量统计一下")
    assert "参考经验" not in agent.llm.messages[0]["content"]


def test_few_shot_block_is_disabled_by_settings(runtime) -> None:
    settings = replace(runtime.settings, few_shot_enabled=False)
    agent = _agent_with_experience(
        runtime,
        question="按状态统计订单数量",
        sql=VERIFIED_SQL,
        verified=True,
        settings=settings,
    )
    agent.run("各状态的单子数量统计一下")
    assert "参考经验" not in agent.llm.messages[0]["content"]


def test_few_shot_respects_count_and_character_budget(runtime) -> None:
    store = runtime.store
    for index in range(6):
        store.add(
            f"按状态统计订单数量口径{index}",
            f"SELECT {index} AS n, status FROM sales_order GROUP BY status",
            category="group_by",
            verified=True,
            schema_hash="h1",
        )
    settings = replace(runtime.settings, few_shot_max=2, few_shot_max_chars=120)
    agent = NL2SQLAgent(
        runtime.catalog,
        RecordingLLM({"status": "needs_clarification", "answer": "需要更多信息"}),
        runtime.agent.tools,
        settings,
        store=store,
        schema_hash="h1",
    )
    block = agent._few_shot_block("按状态统计订单数量口径3", "group_by")
    assert block.count("SQL：") <= 2
    assert len(block) <= 120


def test_few_shot_survives_a_broken_memory_store(runtime) -> None:
    class ExplodingStore:
        def search(self, *args, **kwargs):
            raise RuntimeError("memory down")

    agent = NL2SQLAgent(
        runtime.catalog,
        RecordingLLM({"status": "needs_clarification", "answer": "需要更多信息"}),
        runtime.agent.tools,
        runtime.settings,
        store=ExplodingStore(),
        schema_hash="h1",
    )
    assert agent._few_shot_block("随便问问", "unknown") == ""
    result = agent.run("随便问问")
    assert result.status == "needs_clarification"


def test_repair_calls_are_capped_at_two_rounds(runtime) -> None:
    calls = [
        ToolCall(id=f"r{i}", name="repair_sql", arguments={"sql": "SELECT 1", "error": "no such column"})
        for i in range(4)
    ]
    responses = [LLMResponse(tool_calls=[call], finish_reason="tool_calls") for call in calls]
    responses.append(
        LLMResponse(
            content=json.dumps({"status": "needs_human", "answer": "修复用尽"}, ensure_ascii=False),
            finish_reason="stop",
        )
    )
    agent = NL2SQLAgent(runtime.catalog, ScriptedLLM(responses), runtime.agent.tools, runtime.settings)
    result = agent.run("随便问问")

    repairs = [item for item in result.tool_trace if item["tool"] == "repair_sql"]
    assert len(repairs) == 4
    assert sum(1 for item in repairs if item["ok"]) == runtime.settings.max_repairs == 2
    assert sum(1 for item in repairs if not item["ok"] and "上限" in item["summary"]) == 2


def test_terminal_failure_is_queued_and_ok_answers_are_not(runtime) -> None:
    agent = NL2SQLAgent(
        runtime.catalog,
        LoopingFailureLLM(),
        runtime.agent.tools,
        runtime.settings,
        store=runtime.store,
        schema_hash="h1",
    )
    agent.run("按状态统计订单数量")
    failures = runtime.store.list_failures()
    assert len(failures) == 1
    assert failures[0]["reason"] == "needs_human"
    assert failures[0]["category"] == "group_by"

    agent.run("按状态统计订单数量")
    assert len(runtime.store.list_failures()) == 1
    assert runtime.store.list_failures()[0]["occur_count"] == 2

    runtime.agent.run("2026年6月华东地区销售订单总金额是多少？")
    assert len(runtime.store.list_failures()) == 1


def test_failure_capture_can_be_disabled(runtime) -> None:
    settings = replace(runtime.settings, capture_failures=False)
    agent = NL2SQLAgent(
        runtime.catalog,
        ScriptedLLM(
            [
                LLMResponse(
                    content=json.dumps({"status": "failed", "answer": "模型故障"}, ensure_ascii=False)
                )
            ]
        ),
        runtime.agent.tools,
        settings,
        store=runtime.store,
        schema_hash="h1",
    )
    agent.run("按状态统计订单数量")
    assert runtime.store.list_failures() == []


def test_clarification_and_refusal_are_not_failures(runtime) -> None:
    for status in ("needs_clarification", "refused"):
        agent = NL2SQLAgent(
            runtime.catalog,
            ScriptedLLM(
                [LLMResponse(content=json.dumps({"status": status, "answer": "请补充范围"}, ensure_ascii=False))]
            ),
            runtime.agent.tools,
            runtime.settings,
            store=runtime.store,
            schema_hash="h1",
        )
        agent.run(f"问题{status}")
    assert runtime.store.list_failures() == []
