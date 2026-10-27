"""Embedding providers and the schema-hash keyed vector cache."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nl2sql_agent.catalog import Catalog, Column, Table
from nl2sql_agent.config import Settings
from nl2sql_agent.embeddings import (
    EmbeddingError,
    HashEmbedding,
    OpenAIEmbedding,
    SchemaIndex,
    build_embedding_provider,
)


def demo_catalog(description: str = "销售订单主表", extra_column: bool = False) -> Catalog:
    columns = [
        Column(name="OrderID", type="int", description="订单编号"),
        Column(name="CustomerName", type="text", description="客户名称"),
        Column(name="CustomerPhone", type="text", description="客户电话", sensitive=True),
        Column(name="InternalNote", type="text", description="内部备注", disabled=True),
    ]
    if extra_column:
        columns.append(Column(name="Region", type="text", description="销售区域"))
    table = Table(name="SalesOrder", description=description, columns=tuple(columns))
    return Catalog(
        domain="test",
        dialect="sqlite",
        as_of_date="2026-07-01",
        tables={table.name.lower(): table},
    )


class CountingProvider:
    """Wraps HashEmbedding so cache hits are observable as missing calls."""

    name = "counting:4"

    def __init__(self) -> None:
        self.inner = HashEmbedding(dimensions=4)
        self.calls = 0
        self.received: list[list[str]] = []

    def embed(self, texts):
        self.calls += 1
        self.received.append(list(texts))
        return self.inner.embed(texts)


class FailingProvider:
    name = "failing:4"
    calls = 0

    def embed(self, texts):
        self.calls += 1
        raise EmbeddingError("provider offline")


def test_hash_embedding_is_deterministic_and_normalised() -> None:
    provider = HashEmbedding(dimensions=32)
    first = provider.embed(["库存低于安全库存的物料"])[0]
    second = provider.embed(["库存低于安全库存的物料"])[0]
    other = provider.embed(["客户合同金额"])[0]
    assert first == second
    assert first != other
    assert provider.calls == 3
    assert abs(sum(value * value for value in first) ** 0.5 - 1.0) < 1e-9


def test_build_embedding_provider_defaults_to_off() -> None:
    assert build_embedding_provider(Settings(embedding_provider="none")) is None
    provider = build_embedding_provider(Settings(embedding_provider="hash", embedding_dimensions=16))
    assert isinstance(provider, HashEmbedding)
    assert provider.name == "hash:16"
    with pytest.raises(ValueError, match="未知 NL2SQL_EMBEDDING"):
        build_embedding_provider(Settings(embedding_provider="magic"))


def test_documents_skip_sensitive_and_disabled_columns() -> None:
    documents = demo_catalog().table_documents()
    assert len(documents) == 1
    table, text = documents[0]
    assert table.name == "SalesOrder"
    assert "OrderID" in text and "订单编号" in text
    assert "CustomerPhone" not in text and "客户电话" not in text
    assert "InternalNote" not in text and "内部备注" not in text


def test_schema_index_reuses_its_cache(tmp_path: Path) -> None:
    catalog = demo_catalog()
    provider = CountingProvider()
    first = SchemaIndex(catalog, provider, tmp_path).vectors()
    second_index = SchemaIndex(catalog, provider, tmp_path)
    second = second_index.vectors()

    assert len(first) == len(second)
    for left, right in zip(first, second):
        assert left == pytest.approx(right, abs=1e-5)
    assert provider.calls == 1
    assert second_index.stats.cache_hit is True
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_cache_invalidates_on_schema_and_comment_changes(tmp_path: Path) -> None:
    provider = CountingProvider()
    SchemaIndex(demo_catalog(), provider, tmp_path).vectors()
    SchemaIndex(demo_catalog(extra_column=True), provider, tmp_path).vectors()
    SchemaIndex(demo_catalog(description="销售订单主表（新口径）"), provider, tmp_path).vectors()
    assert provider.calls == 3
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_corrupt_cache_is_rebuilt(tmp_path: Path) -> None:
    provider = CountingProvider()
    index = SchemaIndex(demo_catalog(), provider, tmp_path)
    index.vectors()
    index.cache_path.write_text("{not json", encoding="utf-8")

    again = SchemaIndex(demo_catalog(), provider, tmp_path)
    again.vectors()
    assert provider.calls == 2
    assert again.stats.cache_hit is False
    payload = json.loads(again.cache_path.read_text(encoding="utf-8"))
    assert payload["documents"] == 1


def test_provider_failure_is_an_embedding_error(tmp_path: Path) -> None:
    index = SchemaIndex(demo_catalog(), FailingProvider(), tmp_path)
    with pytest.raises(EmbeddingError, match="provider offline"):
        index.vectors()
    assert not list(tmp_path.glob("*.json"))


def test_cache_write_failure_does_not_block_retrieval(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    index = SchemaIndex(demo_catalog(), CountingProvider(), blocker)
    vectors = index.vectors()
    assert vectors and len(vectors) == 1
    assert index.stats.cache_error


class FakeEmbeddingsAPI:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[list[str]] = []

    def create(self, model: str, input: list[str]):
        self.requests.append(list(input))
        if self.fail:
            raise RuntimeError("429 rate limited")
        data = [
            SimpleNamespace(index=index, embedding=[float(index + 1), 1.0])
            for index, _ in enumerate(input)
        ]
        return SimpleNamespace(data=list(reversed(data)))


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.embeddings = FakeEmbeddingsAPI(fail=fail)


def test_openai_embedding_batches_and_keeps_request_order() -> None:
    client = FakeClient()
    provider = OpenAIEmbedding(
        model="embed-test", api_key="", client=client, batch_size=2
    )
    vectors = provider.embed(["a", "b", "c"])
    assert len(vectors) == 3
    assert [len(request) for request in client.embeddings.requests] == [2, 1]
    # indices, not response order, decide the final alignment
    assert vectors == [[1.0, 1.0], [2.0, 1.0], [1.0, 1.0]]
    assert provider.calls == 2
    assert provider.name == "openai:embed-test"


def test_openai_embedding_wraps_provider_errors() -> None:
    provider = OpenAIEmbedding(model="embed-test", api_key="", client=FakeClient(fail=True))
    with pytest.raises(EmbeddingError, match="embedding 调用失败"):
        provider.embed(["a"])


def test_openai_embedding_requires_a_key() -> None:
    with pytest.raises(EmbeddingError, match="NL2SQL_EMBEDDING_API_KEY"):
        OpenAIEmbedding(model="embed-test", api_key="")
