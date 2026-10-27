from __future__ import annotations

import json

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.config import Settings
from nl2sql_agent.llm import LLMError, LLMResponse
from nl2sql_agent.runtime import Runtime
from nl2sql_agent.voting import (
    MAX_PROMPT_TABLES,
    LLMSecondOpinion,
    RuleBasedSecondOpinion,
    extract_sql,
    should_verify,
)

REVENUE = "2026年6月华东地区销售订单总金额是多少？"
BOM = "整机设备的BOM所有子件及单台总用量是多少？"
OUT_OF_SCOPE = "客户满意度评分是多少？"


def _settings(**overrides) -> Settings:
    data = {"provider": "mock", "dual_channel": True}
    data.update(overrides)
    return Settings(**data)


class FakeModel:
    """Records how the second channel called it, so the tool-free contract is visible."""

    def __init__(self, content: str = "", usage: dict[str, int] | None = None, error: str = "") -> None:
        self.content = content
        self.usage = usage or {}
        self.error = error
        self.calls = 0
        self.messages: list[dict[str, str]] = []
        self.tools: object = "unused"

    def chat(self, messages, tools=None) -> LLMResponse:
        self.calls += 1
        self.messages = messages
        self.tools = tools
        if self.error:
            raise LLMError(self.error)
        return LLMResponse(content=self.content, usage=self.usage, finish_reason="stop")


def test_should_verify_only_asks_on_answered_executed_questions() -> None:
    settings = _settings()
    assert should_verify(settings, status="ok", confidence=0.5, category="metric", executed=True)
    assert not should_verify(settings, status="needs_clarification", confidence=0.1, category="metric", executed=True)
    assert not should_verify(settings, status="ok", confidence=0.1, category="metric", executed=False)
    assert not should_verify(
        _settings(dual_channel=False), status="ok", confidence=0.0, category="bom", executed=True
    )


def test_should_verify_triggers_on_low_confidence_or_a_risky_category() -> None:
    settings = _settings()
    # 高于阈值又不在高危类别：不值得为它多花一次模型调用
    assert not should_verify(settings, status="ok", confidence=0.92, category="metric", executed=True)
    # 类别命中即触发，即使置信度很高
    assert should_verify(settings, status="ok", confidence=0.92, category="bom", executed=True)
    assert should_verify(settings, status="ok", confidence=0.92, category="kitting", executed=True)
    # 阈值可配置；类别清空后只看置信度
    strict = _settings(dual_channel_confidence_threshold=0.95, dual_channel_categories=())
    assert should_verify(strict, status="ok", confidence=0.92, category="metric", executed=True)
    loose = _settings(dual_channel_confidence_threshold=0.0, dual_channel_categories=())
    assert not should_verify(loose, status="ok", confidence=0.92, category="bom", executed=True)


def test_extract_sql_accepts_the_contract_and_a_bare_statement() -> None:
    assert extract_sql('{"sql": "SELECT 1 FROM t;"}') == "SELECT 1 FROM t"
    assert extract_sql("```sql\nSELECT 1 FROM t\n```") == "SELECT 1 FROM t"
    assert extract_sql("SELECT 1 FROM t") == "SELECT 1 FROM t"
    assert extract_sql("WITH c AS (SELECT 1) SELECT * FROM c") == "WITH c AS (SELECT 1) SELECT * FROM c"


def test_extract_sql_rejects_statements_that_are_not_reads() -> None:
    assert extract_sql("") == ""
    assert extract_sql("抱歉，我无法回答") == ""
    assert extract_sql("UPDATE sales_order SET region = '华东'") == ""
    assert extract_sql('{"answer": "没有 SQL"}') == ""
    assert extract_sql('{"sql": "DELETE FROM sales_order"}') == ""


def test_rule_channel_writes_for_the_demo_catalog(runtime: Runtime) -> None:
    channel = RuleBasedSecondOpinion(runtime.catalog, runtime.settings.demo_today)
    opinion = channel.generate(REVENUE, ["sales_order", "order_item"])
    assert opinion.source == "rule"
    assert opinion.ok
    assert "sales_order" in opinion.sql

    quiet = channel.generate(OUT_OF_SCOPE, ["sales_order"])
    assert not quiet.ok
    assert quiet.sql == ""
    assert quiet.error


def test_llm_channel_uses_a_tool_free_call_and_hides_protected_columns(runtime: Runtime) -> None:
    model = FakeModel(
        content=json.dumps({"sql": "SELECT COUNT(*) AS order_count FROM sales_order WHERE region = '华东'"}),
        usage={"prompt_tokens": 120, "completion_tokens": 17},
    )
    opinion = LLMSecondOpinion(model, runtime.catalog, Settings()).generate(REVENUE, ["sales_order"])

    assert opinion.ok and opinion.source == "llm"
    assert opinion.tokens == 137
    assert model.calls == 1
    assert model.tools is None
    prompt = model.messages[0]["content"]
    assert "SQLite" in prompt
    assert "order_id" in prompt
    assert "customer_phone" not in prompt
    assert "internal_note" not in prompt


def test_llm_channel_prefers_the_primary_tables_and_falls_back_to_linking(runtime: Runtime) -> None:
    model = FakeModel(content='{"sql": "SELECT 1 AS x"}')
    LLMSecondOpinion(model, runtime.catalog, Settings()).generate(BOM, ["bom", "material"])
    assert model.messages[0]["content"].count("可用字段:") == 2

    blind = FakeModel(content='{"sql": "SELECT 1 AS x"}')
    LLMSecondOpinion(blind, runtime.catalog, Settings()).generate(BOM, ["不存在的表"])
    prompt = blind.messages[0]["content"]
    assert "可用字段:" in prompt
    assert prompt.count("可用字段:") <= MAX_PROMPT_TABLES


def test_llm_channel_reports_why_it_has_no_opinion(runtime: Runtime) -> None:
    silent = LLMSecondOpinion(FakeModel(content="口径不足", usage={"total_tokens": 12}), runtime.catalog, Settings())
    opinion = silent.generate(REVENUE, ["sales_order"])
    assert not opinion.ok
    assert "没有返回 SQL" in opinion.error
    assert opinion.tokens == 12

    broken = LLMSecondOpinion(FakeModel(error="连接超时"), runtime.catalog, Settings())
    failed = broken.generate(REVENUE, ["sales_order"])
    assert failed.sql == "" and failed.tokens == 0
    assert "模型调用失败" in failed.error

    empty = Catalog(domain="空", dialect="sqlite", as_of_date="", tables={})
    nowhere = LLMSecondOpinion(FakeModel(content='{"sql": "SELECT 1 AS x"}'), empty, Settings()).generate(
        REVENUE, []
    )
    assert "找不到可参考的表" in nowhere.error
