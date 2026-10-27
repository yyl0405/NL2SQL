"""Optional LLM rerank: strict JSON contract, never a hard dependency."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.rerank import LLMReranker, RerankError

DEMO_SCHEMA = Path(__file__).resolve().parents[1] / "src" / "nl2sql_agent" / "data" / "demo_schema.json"


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[list[dict], object]] = []

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        return SimpleNamespace(content=self.content)


class RaisingReranker:
    def rerank(self, question, hits, top_k):
        raise RerankError("rerank backend down")


def _hits(question: str = "库存低于安全库存的物料", top_k: int = 5):
    return Catalog.from_path(DEMO_SCHEMA).search_tables(question, top_k=top_k)


def test_reranker_follows_the_json_contract_and_uses_no_tools() -> None:
    hits = _hits()
    llm = FakeLLM('{"tables": ["inventory", "material"]}')
    result = LLMReranker(llm).rerank("库存低于安全库存的物料", hits, top_k=2)
    assert [hit.table.name for hit in result] == ["inventory", "material"]
    assert llm.calls and llm.calls[0][1] is None
    prompt = llm.calls[0][0][1]["content"]
    assert "最多返回 2 个表名" in prompt


def test_unknown_names_are_dropped_and_missing_hits_are_appended() -> None:
    hits = _hits()
    llm = FakeLLM('{"tables": ["not_a_table", "bom"]}')
    result = LLMReranker(llm).rerank("BOM子件用量", hits, top_k=3)
    assert result[0].table.name == "bom"
    assert len(result) == 3
    assert [hit.table.name for hit in result].count("bom") == 1


def test_reranker_rejects_responses_without_a_usable_table() -> None:
    hits = _hits()
    with pytest.raises(RerankError, match="不是 JSON 对象"):
        LLMReranker(FakeLLM("我想选 inventory")).rerank("库存", hits, top_k=2)
    with pytest.raises(RerankError, match="tables"):
        LLMReranker(FakeLLM('{"tables": []}')).rerank("库存", hits, top_k=2)
    with pytest.raises(RerankError, match="不在候选集"):
        LLMReranker(FakeLLM('{"tables": ["nope"]}')).rerank("库存", hits, top_k=2)


def test_no_call_when_there_is_nothing_to_rerank() -> None:
    llm = FakeLLM('{"tables": ["inventory"]}')
    hits = _hits(top_k=2)
    result = LLMReranker(llm).rerank("库存", hits, top_k=2)
    assert result == hits
    assert llm.calls == []


def test_catalog_search_keeps_original_order_when_rerank_fails() -> None:
    question = "库存低于安全库存的物料"
    plain = Catalog.from_path(DEMO_SCHEMA)
    expected = [hit.table.name for hit in plain.search(question, top_k=3).hits]

    catalog = Catalog.from_path(DEMO_SCHEMA, reranker=RaisingReranker(), rerank_candidates=6)
    result = catalog.search(question, top_k=3)
    assert [hit.table.name for hit in result.hits] == expected
    assert result.reranked is False
    assert "rerank backend down" in result.rerank_error
    assert result.to_dict()["retrieval"]["reranked"] is False
    assert result.to_dict()["retrieval"]["rerank_error"] == "rerank backend down"
