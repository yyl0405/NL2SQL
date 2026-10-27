from __future__ import annotations

import json
from dataclasses import replace

from nl2sql_agent.agent import NL2SQLAgent, _FINALIZE_NUDGE, _trace_stage
from nl2sql_agent.config import Settings
from nl2sql_agent.conversation import Conversation
from nl2sql_agent.llm import LLMResponse, ToolCall
from nl2sql_agent.runtime import Runtime
from nl2sql_agent.voting import SecondOpinion


class ScriptedLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0
        # What the model actually saw on each round, so a test can tell "the loop
        # nudged once" apart from "the loop happened to make more calls".
        self.prompts: list[list[dict]] = []

    def chat(self, messages, tools=None) -> LLMResponse:
        self.calls += 1
        self.prompts.append([dict(item) for item in messages])
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


def test_progress_callback_is_observational_only(runtime: Runtime) -> None:
    """A streaming consumer that dies mid-turn must not change the answer."""

    baseline = runtime.agent.run("2026年6月华东地区销售订单总金额是多少？")
    seen: list[str] = []

    def exploding_progress(step: dict) -> None:
        seen.append(str(step["tool"]))
        raise RuntimeError("SSE client went away")

    streamed = runtime.agent.run(
        "2026年6月华东地区销售订单总金额是多少？", progress=exploding_progress
    )
    assert seen == [item["tool"] for item in streamed.tool_trace]
    assert streamed.status == baseline.status
    assert streamed.sql == baseline.sql
    assert streamed.rows == baseline.rows


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
    assert "62280" not in result.answer


def test_an_unexecuted_claim_loses_its_digits_whatever_the_counter_word_is(
    runtime: Runtime,
) -> None:
    """The unit whitelist is only a backstop: a claimed 「ok」 with no execution is the trigger."""

    llm = ScriptedLLM(
        [
            LLMResponse(
                content=json.dumps(
                    {"status": "ok", "answer": "总数为 12,847 家", "confidence": 0.95},
                    ensure_ascii=False,
                )
            )
        ]
    )
    agent = NL2SQLAgent(runtime.catalog, llm, runtime.agent.tools, runtime.settings)
    result = agent.run("总数是多少？")
    assert result.status == "needs_clarification"
    assert "12,847" not in result.answer
    assert result.sql is None


def test_a_claim_without_a_single_tool_call_is_nudged_once_to_open_the_catalog(
    runtime: Runtime,
) -> None:
    """A rule that names the table is not a result: the model still has to look."""

    llm = ScriptedLLM(
        [
            LLMResponse(
                content=json.dumps(
                    {"status": "ok", "answer": "总数为 12 家", "confidence": 0.9},
                    ensure_ascii=False,
                )
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_clarification", "answer": "请补充统计范围"},
                    ensure_ascii=False,
                )
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog, llm, runtime.agent.tools, replace(runtime.settings, provider="openai")
    )
    result = agent.run("总数是多少？")
    assert llm.calls == 2
    assert result.status == "needs_clarification"
    assert "12" not in result.answer


def test_a_mock_claim_without_tools_is_not_nudged(runtime: Runtime) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                content=json.dumps(
                    {"status": "ok", "answer": "总数为 12 家", "confidence": 0.9},
                    ensure_ascii=False,
                )
            )
        ]
    )
    agent = NL2SQLAgent(runtime.catalog, llm, runtime.agent.tools, runtime.settings)
    result = agent.run("总数是多少？")
    assert llm.calls == 1
    assert "12" not in result.answer


def test_a_claim_after_schema_inspection_is_nudged_once_to_execute(runtime: Runtime) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="get_table_schema", arguments={"table": "Customer"})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "ok", "answer": "总数为 12 家", "confidence": 0.9},
                    ensure_ascii=False,
                )
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_clarification", "answer": "请补充统计范围"},
                    ensure_ascii=False,
                )
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog, llm, runtime.agent.tools, replace(runtime.settings, provider="openai")
    )
    result = agent.run("总数是多少？")
    assert llm.calls == 3
    assert "12" not in result.answer


def test_approved_schema_rules_do_not_replace_the_tool_workflow(runtime: Runtime) -> None:
    from nl2sql_agent.schema_overlay import apply_overlay

    catalog = apply_overlay(
        runtime.catalog,
        {
            "table_aliases": {"Customer": ["签约客户"]},
            "column_aliases": {},
            "business_rules": ["客户主数据只有一张表。"],
        },
    )
    agent = NL2SQLAgent(catalog, runtime.agent.llm, runtime.agent.tools, runtime.settings)
    assert "客户主数据只有一张表" in agent._system_prompt()
    assert "只决定用哪张表/哪个字段" in agent._system_prompt()
    bare = NL2SQLAgent(
        replace(catalog, business_rules=[]),
        runtime.agent.llm,
        runtime.agent.tools,
        runtime.settings,
    )
    assert "只决定用哪张表/哪个字段" not in bare._system_prompt()


def test_real_model_is_nudged_to_inspect_schema_before_clarifying(runtime: Runtime) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_clarification", "answer": "请明确合同金额口径"},
                    ensure_ascii=False,
                )
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="search_tables", arguments={"query": "合同", "top_k": 3})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_human", "answer": "查完 schema 仍缺少关键字段", "confidence": 0.1},
                    ensure_ascii=False,
                )
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog,
        llm,
        runtime.agent.tools,
        replace(runtime.settings, provider="openai"),
    )
    result = agent.run("合同金额是多少？")
    assert llm.calls == 3
    assert result.tool_trace[0]["tool"] == "search_tables"
    assert result.status == "needs_human"


def test_mock_clarification_is_not_nudged(runtime: Runtime) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_clarification", "answer": "请明确范围"},
                    ensure_ascii=False,
                )
            )
        ]
    )
    agent = NL2SQLAgent(runtime.catalog, llm, runtime.agent.tools, runtime.settings)
    result = agent.run("合同金额是多少？")
    assert llm.calls == 1
    assert result.status == "needs_clarification"
    assert result.tool_trace == []


def test_real_model_is_nudged_before_giving_up_after_schema(runtime: Runtime) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="get_table_schema",
                        arguments={"tables": ["sales_order"]},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_human", "answer": "无法确定口径"},
                    ensure_ascii=False,
                )
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_human", "answer": "仍缺少关键字段"},
                    ensure_ascii=False,
                )
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog,
        llm,
        runtime.agent.tools,
        replace(runtime.settings, provider="openai"),
    )
    result = agent.run("销售额是多少？")
    assert llm.calls == 3
    assert result.status == "needs_human"


def test_real_model_is_nudged_to_execute_a_drafted_sql(runtime: Runtime) -> None:
    sql = "SELECT order_id FROM sales_order LIMIT 5"
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="get_table_schema",
                        arguments={"tables": ["sales_order"]},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "status": "needs_clarification",
                        "answer": "需要确认范围",
                        "sql": sql,
                    },
                    ensure_ascii=False,
                )
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="c2", name="execute_sql", arguments={"sql": sql})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "ok", "answer": "已查询", "sql": sql, "confidence": 0.9},
                    ensure_ascii=False,
                )
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog,
        llm,
        runtime.agent.tools,
        replace(runtime.settings, provider="openai"),
    )
    result = agent.run("销售订单有多少条？")
    assert llm.calls == 4
    assert result.status == "ok"
    assert result.row_count >= 1
    assert "execute_sql" in [item["tool"] for item in result.tool_trace]


def test_max_steps_keeps_the_last_successful_execution(runtime: Runtime) -> None:
    sql = "SELECT order_id FROM sales_order LIMIT 3"
    responses = [
        LLMResponse(
            tool_calls=[
                ToolCall(id="c1", name="get_table_schema", arguments={"tables": ["sales_order"]})
            ],
            finish_reason="tool_calls",
        ),
        LLMResponse(
            tool_calls=[ToolCall(id="c2", name="execute_sql", arguments={"sql": sql})],
            finish_reason="tool_calls",
        ),
    ]
    responses += [
        LLMResponse(
            tool_calls=[
                ToolCall(id=f"x{i}", name="search_tables", arguments={"query": "订单", "top_k": 1})
            ],
            finish_reason="tool_calls",
        )
        for i in range(runtime.settings.max_steps - 2)
    ]
    agent = NL2SQLAgent(
        runtime.catalog,
        ScriptedLLM(responses),
        runtime.agent.tools,
        replace(runtime.settings, provider="openai"),
    )
    result = agent.run("销售订单有多少条？")
    assert result.status == "ok"
    assert result.rows
    assert "达到最大工具调用轮次" in result.assumptions[0]


def test_unexecuted_clarification_cannot_claim_numbers(runtime: Runtime) -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                content=json.dumps(
                    {
                        "status": "needs_clarification",
                        "answer": "本月共 127 台设备",
                        "sql": "SELECT COUNT(*) FROM sales_order",
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    agent = NL2SQLAgent(runtime.catalog, llm, runtime.agent.tools, runtime.settings)
    result = agent.run("本月发货多少台设备？")
    assert result.status == "needs_clarification"
    assert result.sql is None
    assert "127" not in result.answer


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


def _conversation_after(runtime: Runtime, question: str) -> Conversation:
    conversation = Conversation()
    conversation.add(runtime.agent.run(question).to_turn())
    return conversation


def _agent_with_llm(runtime: Runtime, llm) -> NL2SQLAgent:
    """The same agent with a different model, so a test can pin model behaviour."""
    return NL2SQLAgent(
        runtime.catalog,
        llm,
        runtime.agent.tools,
        runtime.settings,
        store=runtime.store,
        schema_hash=runtime.agent.schema_hash,
    )


def test_history_rewrites_a_follow_up(runtime: Runtime) -> None:
    conversation = _conversation_after(runtime, "2026年6月华东地区销售订单总金额是多少？")
    result = runtime.agent.run("那华南呢？", history=conversation)
    assert result.status == "ok"
    assert result.rewrite_source == "rule"
    assert result.effective_question == "2026年6月华南地区销售订单总金额是多少？"
    assert result.rows[0][0] == 94410.0
    assert result.question == "那华南呢？"


def test_history_accumulates_across_three_turns(runtime: Runtime) -> None:
    conversation = _conversation_after(runtime, "2026年6月华东地区销售订单总金额是多少？")
    conversation.add(runtime.agent.run("那华南呢？", history=conversation).to_turn())
    third = runtime.agent.run("那5月呢？", history=conversation)
    assert third.effective_question == "2026年5月华南地区销售订单总金额是多少？"
    conversation.add(third.to_turn())
    assert len(conversation) == 3
    assert conversation.pending_slots().region == "华南"


FOLLOW_UP_PROMPT = {"status": "needs_clarification", "answer": "需要更多信息", "confidence": 0.2}


def test_follow_up_turn_carries_the_sql_that_already_worked(runtime: Runtime) -> None:
    """A narrowing follow-up must not re-explore a 500-table catalog from zero."""
    conversation = _conversation_after(runtime, "2026年6月华东地区销售订单总金额是多少？")
    agent = _agent_with_llm(runtime, RecordingLLM(FOLLOW_UP_PROMPT))
    agent.run("那华南呢？", history=conversation)
    assert agent.llm.messages[1]["content"] == "2026年6月华南地区销售订单总金额是多少？"
    system = agent.llm.messages[0]["content"]
    assert "上一轮已在这套表上成功执行过一条查询" in system
    assert "SELECT" in system
    assert "不要照抄" in system
    # the carried block must not leak the previous turn's filter into the question
    assert "华东" not in agent.llm.messages[1]["content"]


def test_carrying_prior_sql_is_a_switch_and_single_turn_never_carries(runtime: Runtime) -> None:
    conversation = _conversation_after(runtime, "2026年6月华东地区销售订单总金额是多少？")
    quiet = _agent_with_llm(runtime, RecordingLLM(FOLLOW_UP_PROMPT))
    quiet.settings.carry_prior_sql = False
    quiet.run("那华南呢？", history=conversation)
    assert "上一轮" not in quiet.llm.messages[0]["content"]

    solo = _agent_with_llm(runtime, RecordingLLM(FOLLOW_UP_PROMPT))
    solo.run("2026年6月华东地区销售订单总金额是多少？")
    assert "上一轮" not in solo.llm.messages[0]["content"]


class ProbingLLM:
    """Keeps calling search_values without ever writing a query."""

    def __init__(self) -> None:
        self.rounds = 0
        self.nudges = 0

    def __init__(self, tool: str = "search_values") -> None:
        self.rounds = 0
        self.nudges = 0
        self.tool = tool

    def chat(self, messages, tools=None) -> LLMResponse:
        self.rounds += 1
        self.nudges = max(
            self.nudges,
            sum(
                1
                for message in messages
                if message.get("role") == "user" and "只做探索" in str(message.get("content"))
            ),
        )
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    id=f"probe-{self.rounds}",
                    name=self.tool,
                    arguments={"table": "sales_order", "column": "status", "query": "x"}
                    if self.tool == "search_values"
                    else {"query": "合同"},
                )
            ],
            finish_reason="tool_calls",
        )


def _probing_agent(
    runtime: Runtime, tool: str = "search_values", **overrides
) -> tuple[ProbingLLM, NL2SQLAgent]:
    llm = ProbingLLM(tool)
    settings = replace(runtime.settings, provider="openai", **overrides)
    agent = NL2SQLAgent(
        runtime.catalog,
        llm,
        runtime.agent.tools,
        settings,
        store=runtime.store,
        schema_hash=runtime.agent.schema_hash,
    )
    return llm, agent


def test_table_listing_spin_gets_exactly_one_nudge(runtime: Runtime) -> None:
    """The loop that actually burned the budget on the attached catalog re-listed tables.

    The old counter only watched ``search_values``, so a model cycling through
    ``search_tables`` reached ``max_steps`` with no SQL and no nudge.
    """
    llm, agent = _probing_agent(runtime, tool="search_tables", max_value_probes=3)
    result = agent.run("按客户统计合同金额")
    assert result.status == "max_steps"
    assert llm.nudges == 1
    assert llm.rounds == 12


def test_value_probing_spin_gets_exactly_one_nudge(runtime: Runtime) -> None:
    llm, agent = _probing_agent(runtime, max_value_probes=3)
    result = agent.run("按客户统计合同金额")
    assert result.status == "max_steps"
    assert llm.nudges == 1
    assert llm.rounds == 12


def test_probing_nudge_is_off_for_the_mock_model_and_when_disabled(runtime: Runtime) -> None:
    llm, agent = _probing_agent(runtime, max_value_probes=0)
    agent.settings.provider = "openai"
    agent.run("按客户统计合同金额")
    assert llm.nudges == 0

    mock_llm, mock_agent = _probing_agent(runtime, max_value_probes=2)
    mock_agent.settings.provider = "mock"
    mock_agent.run("按客户统计合同金额")
    assert mock_llm.nudges == 0


def test_single_turn_answer_is_unchanged_by_multi_turn_support(runtime: Runtime) -> None:
    question = "2026年6月华东地区销售订单总金额是多少？"
    plain = runtime.agent.run(question).to_dict()
    with_empty_history = runtime.agent.run(question, history=Conversation()).to_dict()
    # wall-clock timings are per-run noise, including the ones recorded on each tool step
    for outcome in (plain, with_empty_history):
        outcome["elapsed_ms"] = 0.0
        for step in outcome["tool_trace"]:
            step.pop("elapsed_ms", None)
    assert plain == with_empty_history
    assert plain["rewrite_source"] == "none"


def test_ambiguous_follow_up_never_reaches_the_model(runtime: Runtime) -> None:
    conversation = _conversation_after(runtime, "2026年6月华东地区销售订单总金额是多少？")
    agent = _agent_with_llm(runtime, ScriptedLLM([]))
    result = agent.run("那库存情况怎么样？", history=conversation)
    assert result.status == "needs_clarification"
    assert result.sql is None
    assert result.steps == 0
    assert result.clarification is not None
    assert len(result.clarification.options) == 3
    assert "1." in result.answer


def test_multi_turn_switch_off_keeps_the_raw_follow_up(runtime: Runtime) -> None:
    conversation = _conversation_after(runtime, "2026年6月华东地区销售订单总金额是多少？")
    runtime.settings.multi_turn_enabled = False
    result = runtime.agent.run("那华南呢？", history=conversation)
    assert result.rewrite_source == "off"
    assert result.effective_question == "那华南呢？"
    assert result.steps >= 1


def test_failure_queue_stores_the_effective_question(runtime: Runtime) -> None:
    conversation = _conversation_after(runtime, "2026年6月华东地区销售订单总金额是多少？")
    agent = _agent_with_llm(
        runtime,
        ScriptedLLM(
            [LLMResponse(content=json.dumps({"status": "needs_human", "answer": "需要人工确认"}))]
        ),
    )
    result = agent.run("那华南呢？", history=conversation)
    assert result.status == "needs_human"
    failures = runtime.store.list_failures()
    assert [row["question"] for row in failures] == ["2026年6月华南地区销售订单总金额是多少？"]


REVENUE = "2026年6月华东地区销售订单总金额是多少？"
BOM = "整机设备的BOM所有子件及单台总用量是多少？"
OUT_OF_SCOPE = "客户满意度评分是多少？"
# 与主通道等价但写法不同：折扣已计入，结果集一致
AGREE_SQL = (
    "SELECT ROUND(SUM(oi.qty * oi.unit_price * (1 - oi.discount)), 2) AS total_amount "
    "FROM sales_order AS o JOIN order_item AS oi ON oi.order_id = o.order_id "
    "WHERE o.region = '华东' AND o.order_date >= '2026-06-01' AND o.order_date < '2026-07-01'"
)
# 少了折扣项：同一样例、不同的数，正是必须由人裁决的口径分歧
DISAGREE_SQL = (
    "SELECT ROUND(SUM(oi.qty * oi.unit_price), 2) AS total_amount "
    "FROM sales_order o JOIN order_item oi ON oi.order_id = o.order_id "
    "WHERE o.region = '华东' AND o.order_date >= '2026-06-01' AND o.order_date < '2026-07-01'"
)


class StaticSecondOpinion:
    """A second channel with a pinned verdict, so the vote is the only variable."""

    source = "static"

    def __init__(self, sql: str = "", error: str = "", tokens: int = 0, raises: str = "") -> None:
        self.sql = sql
        self.error = error
        self.tokens = tokens
        self.raises = raises
        self.calls = 0
        self.tables: list[str] = []

    def generate(self, question: str, tables: list[str]) -> SecondOpinion:
        self.calls += 1
        self.tables = list(tables)
        if self.raises:
            raise RuntimeError(self.raises)
        if not self.sql:
            return SecondOpinion(source=self.source, error=self.error, tokens=self.tokens)
        return SecondOpinion(sql=self.sql, source=self.source, tokens=self.tokens)


def _voting_agent(runtime: Runtime, channel: StaticSecondOpinion, **overrides) -> NL2SQLAgent:
    """Always vote (threshold 1.0) so a test controls the vote, not the trigger."""
    values = {"dual_channel": True, "dual_channel_confidence_threshold": 1.0}
    values.update(overrides)
    settings = replace(runtime.settings, **values)
    return NL2SQLAgent(
        runtime.catalog,
        runtime.agent.llm,
        runtime.agent.tools,
        settings,
        store=runtime.store,
        schema_hash=runtime.agent.schema_hash,
        second_opinion=channel,
        guard=runtime.guard,
        aligner=runtime.agent.aligner,
    )


def test_dual_channel_agreement_raises_confidence(runtime: Runtime) -> None:
    channel = StaticSecondOpinion(sql=AGREE_SQL, tokens=400)
    result = _voting_agent(runtime, channel).run(REVENUE)

    assert result.status == "ok"
    assert result.confidence == 0.95
    assert result.assumptions[-1] == "双通道独立生成并执行，结果集一致。"
    assert [entry["role"] for entry in result.channels] == ["primary", "second"]
    second = result.channels[1]
    assert second["source"] == "static"
    assert second["agreement"] is True
    assert second["row_count"] == 1
    # the vote is answered from the tables the primary SQL really touched
    assert set(channel.tables) == {"sales_order", "order_item"}
    # cost is reported, but a vote is not a tool-calling step
    assert result.tokens == 400
    assert result.steps == runtime.agent.run(REVENUE).steps
    assert result.to_dict()["channels"][1]["agreement"] is True
    assert runtime.store.list_failures() == []


def test_dual_channel_disagreement_goes_to_a_human_instead_of_choosing(runtime: Runtime) -> None:
    result = _voting_agent(runtime, StaticSecondOpinion(sql=DISAGREE_SQL)).run(REVENUE)

    assert result.status == "needs_human"
    assert result.error == "dual_channel_disagreement"
    assert result.confidence <= 0.25
    assert "第 1 行不同" in result.answer
    assert "系统不默认取舍" in result.answer
    # both candidates stay visible: that is what a human needs to close the case
    assert result.sql
    assert result.rows == [[62280.0]]
    assert result.channels[1]["agreement"] is False
    failures = runtime.store.list_failures()
    assert [row["reason"] for row in failures] == ["needs_human"]
    assert failures[0]["question"] == REVENUE


def test_dual_channel_rejects_a_second_sql_the_guard_blocks(runtime: Runtime) -> None:
    result = _voting_agent(runtime, StaticSecondOpinion(sql="SELECT * FROM sales_order")).run(REVENUE)

    assert result.status == "ok"
    assert result.confidence == 0.92
    second = result.channels[1]
    assert second["status"] == "guard_failed"
    assert second["agreement"] is None
    assert "护栏" in second["error"]
    assert "未经交叉验证" in result.assumptions[-1]
    assert runtime.store.list_failures() == []


def test_dual_channel_records_a_second_sql_the_database_refuses(runtime: Runtime) -> None:
    broken = (
        "SELECT unknown_func(order_id) AS total_amount FROM sales_order WHERE region = '华东'"
    )
    result = _voting_agent(runtime, StaticSecondOpinion(sql=broken)).run(REVENUE)

    assert result.status == "ok"
    assert result.confidence == 0.92
    second = result.channels[1]
    assert second["status"] == "execution_failed"
    assert second["agreement"] is None
    assert "no such function" in second["error"]


def test_dual_channel_without_a_second_sql_leaves_the_conclusion(runtime: Runtime) -> None:
    result = _voting_agent(runtime, StaticSecondOpinion(error="规则通道未能生成 SQL")).run(REVENUE)

    assert result.status == "ok"
    assert result.confidence == 0.92
    second = result.channels[1]
    assert second["status"] == "not_generated"
    assert second["sql"] is None
    assert second["agreement"] is None
    assert "规则通道未能生成 SQL" in second["error"]
    assert "未经交叉验证" in result.assumptions[-1]


def test_dual_channel_a_broken_generator_cannot_break_the_answer(runtime: Runtime) -> None:
    result = _voting_agent(runtime, StaticSecondOpinion(raises="复核通道炸了")).run(REVENUE)

    assert result.status == "ok"
    assert result.confidence == 0.92
    assert result.channels[1]["status"] == "not_generated"
    assert "复核通道炸了" in result.channels[1]["error"]


def test_dual_channel_off_leaves_the_pipeline_untouched(runtime: Runtime) -> None:
    baseline = runtime.agent.run(REVENUE).to_dict()
    channel = StaticSecondOpinion(sql=DISAGREE_SQL)
    agent = _voting_agent(runtime, channel, dual_channel=False)
    outcome = agent.run(REVENUE).to_dict()

    assert channel.calls == 0
    assert outcome["channels"] == []
    assert outcome["status"] == "ok"
    for key in ("elapsed_ms", "channels"):
        baseline.pop(key, None)
        outcome.pop(key, None)
    for step in baseline["tool_trace"]:
        step.pop("elapsed_ms", None)
    for step in outcome["tool_trace"]:
        step.pop("elapsed_ms", None)
    assert outcome == baseline


def test_dual_channel_votes_only_where_an_error_would_cost(runtime: Runtime) -> None:
    category_only = _voting_agent(
        runtime,
        StaticSecondOpinion(sql=AGREE_SQL),
        dual_channel_confidence_threshold=0.0,
        dual_channel_categories=("bom",),
    )
    assert category_only.run(REVENUE).channels == []
    assert category_only.run(BOM).channels[1]["agreement"] is False

    low_confidence = _voting_agent(
        runtime,
        StaticSecondOpinion(sql=AGREE_SQL),
        dual_channel_confidence_threshold=0.93,
        dual_channel_categories=(),
    )
    assert low_confidence.run(REVENUE).channels[1]["agreement"] is True

    refusal = _voting_agent(runtime, StaticSecondOpinion(sql=AGREE_SQL))
    assert refusal.run(OUT_OF_SCOPE).channels == []
    assert refusal.run(REVENUE).channels[1]["agreement"] is True


def test_trace_stage_keeps_guard_and_execution_failures_distinguishable() -> None:
    assert _trace_stage("execute_sql", {"ok": True, "data": {"success": False, "error": "no such column"}}) == "execution"
    assert _trace_stage("execute_sql", {"ok": True, "data": {"success": False, "stage": "guard"}}) == "guard"
    assert _trace_stage("validate_sql", {"ok": True, "data": {"valid": False, "errors": []}}) == "guard"
    assert _trace_stage("execute_sql", {"ok": True, "data": {"success": True, "row_count": 3}}) == ""
    assert _trace_stage("search_tables", {"ok": True, "data": {"tables": []}}) == ""
    # 工具层自身的失败（参数越界、修复上限）不是护栏也不是数据库
    assert _trace_stage("repair_sql", {"ok": False, "error": "已达上限"}) == "tool"


def test_model_explained_clarification_is_not_decorated_with_examples(runtime: Runtime) -> None:
    """The model named the missing mapping; canned demo picks would talk over it."""
    reason = "「华东」在区域列里找不到对应编码，请给出确切的区域编码后重新提问。"
    agent = _agent_with_llm(
        runtime,
        RecordingLLM({"status": "needs_clarification", "answer": reason, "confidence": 0.2}),
    )
    result = agent.run("只看华东地区的合同份数")
    assert result.status == "needs_clarification"
    assert result.clarification is not None
    assert result.clarification.options == []
    assert reason in result.answer


def _nudge_rounds(llm: ScriptedLLM) -> list[int]:
    """Which model rounds saw the finalize nudge."""
    return [
        index
        for index, prompt in enumerate(llm.prompts)
        if any(_FINALIZE_NUDGE in str(item.get("content") or "") for item in prompt)
    ]


def test_exploring_after_a_successful_execution_is_nudged_once(runtime: Runtime) -> None:
    """The mirror case of the probe nudge: it executed, then wandered.

    Without this the loop burns the remaining budget on re-exploration and the
    turn ends in ``max_steps`` even though a real result was already in hand.
    """
    sql = "SELECT order_id FROM sales_order LIMIT 5"
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="execute_sql", arguments={"sql": sql})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="c2", name="search_tables", arguments={"query": "销售订单"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c3", name="get_table_schema", arguments={"tables": ["sales_order"]})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "ok", "answer": "已查询", "sql": sql, "confidence": 0.9},
                    ensure_ascii=False,
                )
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog,
        llm,
        runtime.agent.tools,
        replace(runtime.settings, provider="openai"),
    )
    result = agent.run("销售订单有多少条？")
    assert result.status == "ok"
    assert result.row_count >= 1
    # Injected once, and only visible from the round after it was appended.
    assert _nudge_rounds(llm) == [3]


def test_a_single_recheck_after_execution_is_not_nudged(runtime: Runtime) -> None:
    sql = "SELECT order_id FROM sales_order LIMIT 5"
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="execute_sql", arguments={"sql": sql})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c2", name="get_table_schema", arguments={"tables": ["sales_order"]})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "ok", "answer": "已查询", "sql": sql, "confidence": 0.9},
                    ensure_ascii=False,
                )
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog,
        llm,
        runtime.agent.tools,
        replace(runtime.settings, provider="openai"),
    )
    result = agent.run("销售订单有多少条？")
    assert result.status == "ok"
    assert _nudge_rounds(llm) == []


def test_mock_provider_is_never_finalization_nudged(runtime: Runtime) -> None:
    """Offline measurements stay byte-identical: the nudge is a real-model path only."""
    sql = "SELECT order_id FROM sales_order LIMIT 5"
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="execute_sql", arguments={"sql": sql})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="c2", name="search_tables", arguments={"query": "销售订单"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c3", name="search_tables", arguments={"query": "订单明细"})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "ok", "answer": "已查询", "sql": sql, "confidence": 0.9},
                    ensure_ascii=False,
                )
            ),
        ]
    )
    agent = NL2SQLAgent(runtime.catalog, llm, runtime.agent.tools, runtime.settings)
    result = agent.run("销售订单有多少条？")
    assert result.status == "ok"
    assert _nudge_rounds(llm) == []


def test_exhausted_budget_still_runs_the_valid_query_it_has(runtime: Runtime) -> None:
    """The model wrote a guard-clean query and never executed it.

    8 of the 9 real-catalog ``max_steps`` failures this session had exactly this
    shape: a correct SQL in hand and no answer, because every round was spent
    exploring. The runtime now executes it through the same read-only path.
    """
    sql = "SELECT order_id FROM sales_order LIMIT 3"
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="get_table_schema", arguments={"tables": ["sales_order"]})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="c2", name="validate_sql", arguments={"sql": sql})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="c3", name="search_tables", arguments={"query": "销售订单"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="c4", name="search_tables", arguments={"query": "客户"})],
                finish_reason="tool_calls",
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog,
        llm,
        runtime.agent.tools,
        replace(runtime.settings, provider="openai", max_steps=4),
    )
    result = agent.run("销售订单有多少条？")
    assert result.status == "ok"
    assert result.sql == sql
    assert result.row_count >= 1
    forced = [
        step
        for step in result.tool_trace
        if step["tool"] == "execute_sql" and step.get("stage") == "runtime_forced"
    ]
    assert len(forced) == 1
    assert any("运行时执行" in item for item in result.assumptions)


def test_a_failing_runtime_execution_still_yields_no_number(runtime: Runtime) -> None:
    """The rescue runs the query, it does not invent one."""

    class FailingExecute:
        def __init__(self, inner) -> None:
            self.inner = inner

        def specs(self):
            return self.inner.specs()

        def call(self, name, arguments):
            if name == "execute_sql":
                return {"ok": False, "error": "engine refused the query", "elapsed_ms": 1.0}
            return self.inner.call(name, arguments)

    sql = "SELECT order_id FROM sales_order LIMIT 3"
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="validate_sql", arguments={"sql": sql})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="c2", name="search_tables", arguments={"query": "销售订单"})],
                finish_reason="tool_calls",
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog,
        llm,
        FailingExecute(runtime.agent.tools),
        replace(runtime.settings, provider="openai", max_steps=2),
    )
    result = agent.run("销售订单有多少条？")
    assert result.status == "max_steps"
    assert result.row_count == 0
    assert not result.rows
    assert any(step.get("stage") == "runtime_forced" and not step["ok"] for step in result.tool_trace)
