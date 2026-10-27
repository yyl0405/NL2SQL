"""The deterministic pipeline: route selection, bounded repair, escalation.

The pipeline is the shipped-product shape - retrieve, generate once, guard,
execute, phrase - and the tool loop is what it escalates to. These tests pin both
halves: the pipeline must win with two model calls when the catalog is enough,
and must never lose a turn the loop could still answer.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from nl2sql_agent.agent import NL2SQLAgent
from nl2sql_agent.config import Settings
from nl2sql_agent.llm import LLMError, LLMResponse, ToolCall
from nl2sql_agent.pipeline import (
    MAX_PHRASE_ROWS,
    MAX_PIPELINE_TABLES,
    MAX_VALUE_PROBES,
    _TEXT_TYPE,
    answer_from,
    assumptions_from,
    execution_repair_prompt,
    generation_prompt,
    guard_repair_prompt,
    phrasing_messages,
    probe_candidates,
    relationship_block,
    schema_block,
    value_mentions,
    value_probe_block,
)
from nl2sql_agent.runtime import Runtime

REVENUE = "2026年6月华东地区销售订单总金额是多少？"
# 与工具循环主通道同一条口径：护栏通过、执行返回 62280.0
REVENUE_SQL = (
    "SELECT ROUND(SUM(oi.qty * oi.unit_price * (1 - oi.discount)), 2) AS total_amount "
    "FROM sales_order AS o JOIN order_item AS oi ON oi.order_id = o.order_id "
    "WHERE o.region = '华东' AND o.order_date >= '2026-06-01' AND o.order_date < '2026-07-01'"
)
# 护栏放行、数据库报错：正是"先修复、修不动再升级"的分界
BROKEN_SQL = "SELECT unknown_func(order_id) AS total_amount FROM sales_order WHERE region = '华东'"
PIPELINE_TOOLS = [
    "search_tables",
    "generate_sql",
    "validate_sql",
    "execute_sql",
    "phrase_answer",
]


class ScriptedLLM:
    """Responses in order; an Exception in the list raises on that round."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.tool_rounds = 0
        self.prompts: list[list[dict]] = []

    def chat(self, messages, tools=None) -> LLMResponse:
        self.calls += 1
        if tools:
            self.tool_rounds += 1
        self.prompts.append([dict(item) for item in messages])
        if not self.responses:
            raise AssertionError("scripted model ran out of responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _sql_response(sql: str, assumptions: list[str] | None = None) -> LLMResponse:
    return LLMResponse(
        content=json.dumps({"sql": sql, "assumptions": assumptions or []}, ensure_ascii=False)
    )


def _phrase_response(answer: str) -> LLMResponse:
    return LLMResponse(content=json.dumps({"answer": answer}, ensure_ascii=False))


def _final_response(status: str, answer: str, sql: str = "") -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {"status": status, "answer": answer, "sql": sql, "confidence": 0.9},
            ensure_ascii=False,
        )
    )


def _tool_call(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id=f"c-{name}", name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


def _pipeline_agent(runtime: Runtime, responses: list, **overrides) -> NL2SQLAgent:
    """A real-provider agent on the demo catalog, with the pipeline forced on.

    ``provider=openai`` is what the pipeline is designed for; the model stays
    scripted, so nothing in this file touches the network.
    """
    settings = replace(runtime.settings, provider="openai", mode="pipeline", **overrides)
    return NL2SQLAgent(
        runtime.catalog,
        ScriptedLLM(responses),
        runtime.agent.tools,
        settings,
    )


def test_pipeline_answers_without_the_tool_loop(runtime: Runtime) -> None:
    agent = _pipeline_agent(runtime, [_sql_response(REVENUE_SQL), _phrase_response("62280.00 元")])
    result = agent.run(REVENUE)

    assert result.status == "ok"
    assert result.mode == "pipeline"
    assert result.steps == 2
    assert [item["tool"] for item in result.tool_trace] == PIPELINE_TOOLS
    assert result.rows[0][0] == 62280.0
    assert result.answer == "62280.00 元"
    assert result.confidence == 0.8
    # The pipeline never offers tools to the model: 2 calls, 0 tool rounds.
    assert agent.llm.tool_rounds == 0
    assert not any(item["tool"] == "get_table_schema" for item in result.tool_trace)


def test_pipeline_prompts_carry_the_retrieved_schema(runtime: Runtime) -> None:
    agent = _pipeline_agent(runtime, [_sql_response(REVENUE_SQL), _phrase_response("62280.00 元")])
    result = agent.run(REVENUE)

    system = agent.llm.prompts[0][0]["content"]
    assert "sales_order" in system and "可用字段" in system
    assert agent.llm.prompts[0][1]["content"] == REVENUE
    assert result.prompt_hash and result.few_shot is not None


def test_pipeline_repairs_a_guard_rejection_once(runtime: Runtime) -> None:
    agent = _pipeline_agent(
        runtime,
        [
            _sql_response("SELECT * FROM sales_order"),
            _sql_response(REVENUE_SQL, ["折扣已计入"]),
            _phrase_response("62280.00 元"),
        ],
    )
    result = agent.run(REVENUE)

    assert result.mode == "pipeline"
    assert result.steps == 3
    tools = [item["tool"] for item in result.tool_trace]
    assert tools.count("validate_sql") == 2
    assert any(item["stage"] == "guard" for item in result.tool_trace)
    assert result.rows[0][0] == 62280.0
    assert "折扣已计入" in result.assumptions


def test_pipeline_repairs_a_database_error(runtime: Runtime) -> None:
    agent = _pipeline_agent(
        runtime,
        [_sql_response(BROKEN_SQL), _sql_response(REVENUE_SQL), _phrase_response("62280.00 元")],
    )
    result = agent.run(REVENUE)

    assert result.mode == "pipeline"
    assert result.rows[0][0] == 62280.0
    tools = [item["tool"] for item in result.tool_trace]
    assert tools.count("execute_sql") == 2
    assert "repair_sql" in tools


def test_pipeline_escalates_when_the_tables_are_not_enough(runtime: Runtime) -> None:
    """An empty SQL is the model saying "this slice is not enough", not a failure."""

    agent = _pipeline_agent(
        runtime,
        [
            _sql_response(""),
            _tool_call("execute_sql", {"sql": REVENUE_SQL}),
            _final_response("ok", "62280.00 元", REVENUE_SQL),
        ],
    )
    result = agent.run(REVENUE)

    assert result.mode == "agent"
    assert result.status == "ok"
    assert result.rows[0][0] == 62280.0
    # An escalated turn is the loop's own turn: the pipeline leaves no trace behind.
    assert not any(item["tool"] == "phrase_answer" for item in result.tool_trace)


def test_pipeline_escalates_after_two_guard_rejections(runtime: Runtime) -> None:
    agent = _pipeline_agent(
        runtime,
        [
            _sql_response("SELECT * FROM sales_order"),
            _sql_response("SELECT * FROM order_item"),
            _tool_call("search_tables", {"query": "订单"}),
            _final_response("needs_human", "需要人工确认口径"),
        ],
    )
    result = agent.run(REVENUE)

    assert result.mode == "agent"
    assert result.status == "needs_human"


def test_empty_retrieval_escalates_before_any_model_call(runtime: Runtime) -> None:
    """No candidate tables means nothing to generate from - do not spend a call."""

    agent = _pipeline_agent(
        runtime,
        [
            _tool_call("execute_sql", {"sql": REVENUE_SQL}),
            _final_response("ok", "62280.00 元", REVENUE_SQL),
        ],
    )
    agent.tools = _NoTablesRegistry(agent.tools)
    result = agent.run("客户满意度评分是多少？")

    assert result.mode == "agent"
    # The first prompt the model saw was the agent loop's, not the pipeline's.
    assert "这一轮没有工具" not in agent.llm.prompts[0][0]["content"]


class _NoTablesRegistry:
    """Tool registry that reports no candidate tables, so retrieval is the failure."""

    def __init__(self, inner) -> None:
        self.inner = inner

    def specs(self):
        return self.inner.specs()

    def call(self, name, arguments=None):
        if name == "search_tables":
            return {"ok": True, "data": {"count": 0, "tables": [], "retrieval": {}}}
        return self.inner.call(name, arguments)


def test_lost_phrasing_keeps_the_executed_number(runtime: Runtime) -> None:
    agent = _pipeline_agent(
        runtime,
        [_sql_response(REVENUE_SQL, ["金额含折扣"]), LLMError("措辞调用超时")],
    )
    result = agent.run(REVENUE)

    assert result.status == "ok"
    assert result.mode == "pipeline"
    assert result.steps == 1
    assert "62280" in result.answer
    assert "金额含折扣" in result.assumptions
    assert any("结果措辞" in item for item in result.assumptions)


def test_auto_route_keeps_the_packaged_demo_on_the_loop(runtime: Runtime) -> None:
    settings = replace(runtime.settings, provider="openai", mode="auto")
    agent = NL2SQLAgent(
        runtime.catalog, ScriptedLLM([]), runtime.agent.tools, settings
    )

    assert agent._uses_pipeline() is False


def test_auto_route_takes_the_pipeline_on_a_synced_catalog(runtime: Runtime) -> None:
    # A deployment configures NL2SQL_SCHEMA_PATH: that is exactly what `auto` reads.
    settings = replace(
        runtime.settings,
        provider="openai",
        mode="auto",
        schema_override=Settings().schema_path,
    )
    agent = NL2SQLAgent(runtime.catalog, ScriptedLLM([]), runtime.agent.tools, settings)

    assert agent._uses_pipeline() is True


def test_explicit_modes_override_the_catalog(runtime: Runtime) -> None:
    forced = replace(runtime.settings, provider="mock", mode="pipeline")
    loop = replace(
        runtime.settings,
        provider="openai",
        mode="agent",
        schema_override=Settings().schema_path,
    )
    assert NL2SQLAgent(
        runtime.catalog, ScriptedLLM([]), runtime.agent.tools, forced
    )._uses_pipeline() is True
    assert NL2SQLAgent(
        runtime.catalog, ScriptedLLM([]), runtime.agent.tools, loop
    )._uses_pipeline() is False


# --- prompt rendering -------------------------------------------------------


def test_generation_prompt_offers_the_escape_hatch(catalog) -> None:
    prompt = generation_prompt(catalog, Settings(), names=["sales_order"])

    assert '"sql": ""' in prompt
    assert "没有工具" in prompt
    assert "禁止 SELECT *" in prompt
    assert prompt.index("可参考的表") < prompt.index("硬性要求")
    assert len(catalog.enabled_tables()) > MAX_PIPELINE_TABLES


def test_generation_prompt_includes_examples_and_prior_sql(catalog) -> None:
    prompt = generation_prompt(
        catalog,
        Settings(),
        names=["sales_order"],
        examples="问：销售额\nSQL：SELECT SUM(...)",
        carry="上一轮已在这套表上成功执行过一条查询：",
    )

    assert "参考经验" in prompt and "SELECT SUM(...)" in prompt
    assert "上一轮已在这套表上成功执行过一条查询" in prompt


def test_schema_block_hides_disabled_and_sensitive_columns(catalog) -> None:
    block = schema_block(catalog, ["sales_order"])

    assert "order_id" in block
    assert "customer_phone" not in block
    assert "internal_note" not in block


def test_schema_block_shows_required_and_default_predicates(catalog) -> None:
    table = catalog.table("dim_lookup")
    block = schema_block(catalog, ["dim_lookup"])

    assert table.required_filters
    assert "必要 JOIN/过滤条件" in block
    assert "lookup_type = 'material_category'" in block


def test_schema_block_skips_disabled_tables(catalog) -> None:
    names = [table.name for table in catalog.tables.values() if table.disabled]
    assert "internal_audit" in names
    assert schema_block(catalog, names) == ""


def test_relationship_block_lists_only_the_given_tables(catalog) -> None:
    block = relationship_block(catalog, ["sales_order", "order_item"])

    assert "sales_order.order_id = order_item.order_id" in block
    assert "material.material_id = order_item.material_id" in block
    assert "purchase_order" not in block


def test_relationship_block_is_empty_without_edges(catalog) -> None:
    assert relationship_block(catalog, ["dim_lookup"]) == "（无）"


def test_repair_prompts_quote_the_failure() -> None:
    guard = guard_repair_prompt(["禁止 SELECT *", "字段不存在: foo"])
    assert "禁止 SELECT *" in guard and "字段不存在: foo" in guard
    assert "unknown_func" in execution_repair_prompt("no such function: unknown_func")


def test_phrasing_messages_bound_the_rows() -> None:
    execution = {
        "columns": ["n"],
        "rows": [[index] for index in range(MAX_PHRASE_ROWS + 30)],
        "row_count": MAX_PHRASE_ROWS + 30,
        "truncated": True,
    }
    messages = phrasing_messages(REVENUE, REVENUE_SQL, execution)

    content = messages[1]["content"]
    assert len(messages) == 2
    assert f"这里只给出前 {MAX_PHRASE_ROWS} 行" in content
    assert content.count("],[") == MAX_PHRASE_ROWS - 1
    assert REVENUE_SQL in content


def test_phrasing_contract_falls_back_to_raw_text() -> None:
    assert answer_from(json.dumps({"answer": "共 3 条"})) == "共 3 条"
    assert answer_from("共 3 条") == "共 3 条"
    assert answer_from("") == ""
    assert assumptions_from(json.dumps({"sql": "x", "assumptions": ["口径A", "  "]})) == ["口径A"]
    assert assumptions_from("不是 JSON") == []


def test_mode_is_validated_at_config_time(tmp_path, monkeypatch) -> None:
    assert Settings(home=tmp_path).mode == "auto"
    assert Settings(home=tmp_path, mode="PIPELINE").mode == "pipeline"
    with pytest.raises(ValueError, match="未知 NL2SQL_MODE"):
        Settings(home=tmp_path, mode="pipline")
    monkeypatch.setenv("NL2SQL_MODE", "agent")
    assert Settings.load(home=tmp_path).mode == "agent"


# --- value linking ----------------------------------------------------------


def test_value_mentions_picks_codes_not_identifiers(catalog) -> None:
    known = {item.name for item in catalog.tables.values()}
    known.update(["order_id", "code", "name"])

    assert value_mentions("物料编码表里M7机型的名称是什么？", known) == ["M7"]
    # 纯字母串和列名同名的是标识符，不是取值
    assert value_mentions("按 Code 查一下", known) == []
    assert value_mentions("订单 ORD-1001 与工单 WO-22 的状态", known)[:1] == ["ORD-1001"]
    assert value_mentions("2.15.08 这个物料", known) == ["2.15.08"]
    assert value_mentions("没有代码的问法", known) == []


def test_probe_candidates_only_offers_text_columns(catalog) -> None:
    pairs = probe_candidates(catalog, ["sales_order"], limit=50)
    offered = {column for _table, column in pairs}
    columns = catalog.table("sales_order").columns

    assert "order_id" in offered
    assert "customer_phone" not in offered  # 敏感
    assert "internal_note" not in offered  # 禁用
    numeric = [c.name for c in columns if not _TEXT_TYPE.search(c.type or "")]
    assert not set(numeric) & offered


def test_probe_candidates_respects_the_probe_budget(catalog) -> None:
    assert len(probe_candidates(catalog, ["sales_order", "order_item"], limit=3)) == 3
    assert probe_candidates(catalog, [], limit=MAX_VALUE_PROBES) == []


def test_value_probe_block_needs_a_hit() -> None:
    assert value_probe_block([]) == ""
    assert value_probe_block([{"table": "t", "column": "c", "mention": "M7", "values": []}]) == ""
    block = value_probe_block(
        [{"table": "item_code", "column": "Name", "mention": "M7", "values": ["M7机型"]}]
    )
    assert "M7机型" in block and "item_code.Name" in block


def test_pipeline_probes_a_code_before_generating(runtime: Runtime) -> None:
    """A code in the question reaches the prompt as the stored value, not a guess."""

    sql = "SELECT status FROM sales_order WHERE order_id = 'ORD-1001'"
    agent = _pipeline_agent(runtime, [_sql_response(sql), _phrase_response("已发货")])
    result = agent.run("订单 ORD-1001 的发货状态是什么？")

    assert result.mode == "pipeline"
    system = agent.llm.prompts[0][0]["content"]
    assert "运行时探测到的真实取值" in system
    assert "ORD-1001" in system
    assert "search_values" in [item["tool"] for item in result.tool_trace]


def test_pipeline_skips_probing_when_the_question_has_no_code(runtime: Runtime) -> None:
    agent = _pipeline_agent(runtime, [_sql_response(REVENUE_SQL), _phrase_response("62280.00 元")])
    result = agent.run(REVENUE)

    assert [item["tool"] for item in result.tool_trace] == PIPELINE_TOOLS
    assert "运行时探测到的真实取值" not in agent.llm.prompts[0][0]["content"]


def test_generation_prompt_states_the_projection_and_filter_rules(catalog) -> None:
    prompt = generation_prompt(catalog, Settings(), names=["sales_order"])

    assert "IS NOT NULL" in prompt
    assert "把那个度量一起写进 SELECT" in prompt
