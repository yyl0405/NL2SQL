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
