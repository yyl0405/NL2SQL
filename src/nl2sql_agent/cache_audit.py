"""Offline measurement of the three cache layers.

* Result cache: same question twice, then a punctuation/whitespace variant that
  normalizes to the same key, then a different question that must miss.
* Embedding cache: build the schema index twice against one temp directory; the
  second build must load from disk without calling the provider.
* Prompt prefix: build the system prompt with two different few-shot blocks and
  report how many leading characters stay identical. This measures the *layout*
  only — whether a provider actually served a cached prefix is visible solely in
  its ``cached_tokens`` usage field, not offline.

Nothing here is a gate. The mock provider keeps the run reproducible without a
network; a an online provider can be selected explicitly and costs real tokens.
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .config import Settings
from .embeddings import HashEmbedding, SchemaIndex
from .runtime import create_runtime

DEFAULT_CACHE_QUESTION = "2026年6月华东地区销售订单总金额是多少？"
DISTINCT_CACHE_QUESTION = "库存低于安全库存的物料有哪些？"

_EXAMPLE_A = (
    "问题: 2026年6月华东销售总额是多少？\n"
    "SQL: SELECT SUM(o.amount) FROM sales_order o WHERE o.region = '华东' AND o.order_month = '2026-06'"
)
_EXAMPLE_B = (
    "问题: 库存低于安全库存的物料有哪些？\n"
    "SQL: SELECT m.name, s.qty, m.safety_stock FROM inventory s JOIN material m ON m.id = s.material_id "
    "WHERE s.qty < m.safety_stock"
)


def _common_prefix(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def measure_prompt_prefix(agent: Any) -> dict[str, Any]:
    """Character-level evidence that the volatile few-shot sits at the tail."""
    with_a = agent._system_prompt(_EXAMPLE_A)
    with_b = agent._system_prompt(_EXAMPLE_B)
    without = agent._system_prompt("")
    common = _common_prefix(with_a, with_b)
    return {
        "prompt_chars": len(with_a),
        "base_prompt_chars": len(without),
        "common_prefix_chars": common,
        "stable_ratio": round(common / len(with_a), 4) if with_a else 0.0,
        "volatile_chars": len(with_a) - common,
        "few_shot_at_tail": with_a.rstrip().endswith(_EXAMPLE_A.rsplit("\n", 1)[-1]),
        "note": "前缀比例只度量 system prompt 布局；网关是否真的命中 prompt 缓存要看 usage.cached_tokens",
    }


def _result_brief(result: Any) -> dict[str, Any]:
    return {
        "status": result.status,
        "cached": bool(result.cached),
        "tokens": int(result.tokens),
        "steps": int(result.steps),
        "elapsed_ms": float(result.elapsed_ms),
        "cache": dict(result.cache or {}),
    }


def measure_result_cache(
    settings: Settings, question: str, *, provider: str = "mock"
) -> dict[str, Any]:
    runtime = create_runtime(settings, provider=provider)
    agent = runtime.agent
    cold = agent.run(question)
    warm = agent.run(question)
    variant = f"  {question.replace('？', '?').replace('，', ',')}\n"
    normalized = agent.run(variant)
    distinct = agent.run(DISTINCT_CACHE_QUESTION if question != DISTINCT_CACHE_QUESTION else "各区域销售额排名前3")
    stats = runtime.cache.stats() if runtime.cache is not None else {"enabled": False}
    measurable = cold.status == "ok" and bool(cold.sql) and not cold.error
    return {
        "provider": provider,
        "question": question,
        "cache_key_normalizes": bool(normalized.cached),
        "cold": _result_brief(cold),
        "warm": _result_brief(warm),
        "normalized_variant": _result_brief(normalized),
        "distinct_question": _result_brief(distinct),
        "stats": stats,
        "measurable": measurable,
        "note": (
            "只有 ok 且有执行 SQL 的轮次才入缓存；命中轮不调模型、不执行 SQL，tokens/steps 记为 0。"
            if measurable
            else "冷启动这一轮不是 ok（或没有执行 SQL），结果缓存没有可测量的命中。"
        ),
    }


def measure_embedding_cache(settings: Settings) -> dict[str, Any]:
    catalog = Catalog.from_path(settings.schema_path, linking=settings.linking)
    provider = HashEmbedding(dimensions=settings.embedding_dimensions)
    with tempfile.TemporaryDirectory(prefix="nl2sql-cache-") as tmp:
        first = SchemaIndex(catalog, provider, cache_dir=tmp)
        first.vectors()
        calls_after_first = provider.calls
        second = SchemaIndex(catalog, provider, cache_dir=tmp)
        second.vectors()
        files = sorted(path.name for path in Path(tmp).glob("*.json"))
        return {
            "provider": provider.name,
            "documents": len(first.documents),
            "cache_files": len(files),
            "first": {
                "cache_hit": first.stats.cache_hit,
                "calls": first.stats.calls,
                "elapsed_ms": first.stats.elapsed_ms,
            },
            "second": {
                "cache_hit": second.stats.cache_hit,
                "calls": second.stats.calls,
                "elapsed_ms": second.stats.elapsed_ms,
            },
            "provider_calls_total": provider.calls,
            "reused_without_embedding": second.stats.cache_hit and provider.calls == calls_after_first,
            "note": "缓存文件按 schema_hash + provider + 文档指纹命名；文档变了就重新嵌入，不会复用旧向量。",
        }


def run_cache_audit(
    settings: Settings, *, provider: str = "mock", question: str = ""
) -> dict[str, Any]:
    settings = dataclasses.replace(
        settings,
        provider=provider,
        result_cache_ttl_sec=settings.result_cache_ttl_sec or 60.0,
        result_cache_max_entries=max(settings.result_cache_max_entries, 8),
        # The audit must never append to the deployment's event log.
        log_path=None,
    )
    question = question or DEFAULT_CACHE_QUESTION
    result = measure_result_cache(settings, question, provider=provider)
    runtime = create_runtime(settings, provider=provider)
    prompt = measure_prompt_prefix(runtime.agent)
    return {
        "provider": provider,
        "question": question,
        "result_cache": result,
        "embedding_cache": measure_embedding_cache(settings),
        "prompt_prefix": prompt,
    }
