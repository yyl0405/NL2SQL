"""Suggestion is a draft generator: it may propose, but it may not invent tables."""

from __future__ import annotations

import json
from pathlib import Path

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.llm import LLMError, LLMResponse
from nl2sql_agent.schema_suggest import (
    SuggestOptions,
    load_suggestions,
    suggest_schema,
    write_suggestions,
)


class FakeLLM:
    def __init__(self, payload: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.payload = payload or {}
        self.error = error
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return LLMResponse(content=json.dumps(self.payload, ensure_ascii=False))


def catalog(tmp_path: Path) -> Catalog:
    payload = {
        "domain": "synthetic",
        "dialect": "sqlite",
        "as_of_date": "2026-07-01",
        "tables": [
            {
                "name": "Customer",
                "description": "客户主档",
                "primary_key": "ID",
                "synonyms": ["客户主档"],
                "columns": [
                    {"name": "ID", "type": "int", "description": "主键"},
                    {"name": "CustomerName", "type": "nvarchar", "description": "客户名称"},
                    {"name": "Phone", "type": "nvarchar", "description": "电话", "sensitive": True},
                ],
            }
        ],
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return Catalog.from_path(path)


def test_suggest_schema_records_a_pending_draft(tmp_path: Path) -> None:
    model = FakeLLM(
        {
            "table": "Customer",
            "confidence": 0.82,
            "reason": "有客户名称字段",
            "evidence": ["CustomerName 描述为客户名称"],
            "field_aliases": {"CustomerName": ["客户名称"], "Missing": ["不应保留"]},
            "business_rule": "客户名称取 Customer.CustomerName",
        }
    )
    result = suggest_schema(
        catalog(tmp_path),
        llm=model,
        options=SuggestOptions(concepts=("客户",), top_tables=2, sample_rows=0),
    )
    assert model.calls == 1
    assert result["requires_human_review"] is True
    assert result["status"] == "draft"
    item = result["suggestions"][0]
    assert item["concept"] == "客户"
    assert item["table"] == "Customer"
    assert item["review_status"] == "pending"
    assert item["field_aliases"] == {"CustomerName": ["客户名称"]}
    assert item["confidence"] == 0.82


def test_suggest_schema_rejects_invented_table(tmp_path: Path) -> None:
    result = suggest_schema(
        catalog(tmp_path),
        llm=FakeLLM({"table": "Ghost", "confidence": 1.0}),
        options=SuggestOptions(concepts=("客户",), sample_rows=0),
    )
    assert result["suggestions"] == []
    assert result["observations"][0]["status"] == "invalid_model_output"


def test_suggest_schema_keeps_going_after_llm_failure(tmp_path: Path) -> None:
    result = suggest_schema(
        catalog(tmp_path),
        llm=FakeLLM(error=LLMError("timeout")),
        options=SuggestOptions(concepts=("客户",), sample_rows=0),
    )
    assert result["suggestions"] == []
    assert result["observations"][0]["status"] == "llm_failed"


def test_write_and_load_suggestions_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "var" / "schema_suggestions.json"
    payload = {"suggestions": [{"id": "x"}], "observations": []}
    write_suggestions(path, payload)
    assert load_suggestions(path) == payload
