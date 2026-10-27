"""P2-5 缓存：键隔离、TTL/LRU、只缓存成功轮、评测绕过、prompt 前缀布局。"""

from __future__ import annotations

from pathlib import Path

import pytest

from nl2sql_agent.cache import ResultCache, cache_key
from nl2sql_agent.cache_audit import measure_prompt_prefix, run_cache_audit
from nl2sql_agent.config import Settings
from nl2sql_agent.conversation import Conversation, Turn
from nl2sql_agent.runtime import Runtime, create_runtime

REVENUE = "2026年6月华东地区销售订单总金额是多少？"
CLARIFY = "客户满意度评分是多少？"


@pytest.fixture()
def cached_runtime(tmp_path: Path) -> Runtime:
    return create_runtime(
        Settings(
            home=tmp_path,
            provider="mock",
            result_cache_ttl_sec=60,
            result_cache_max_entries=8,
        )
    )


def test_cache_key_normalizes_punctuation_and_whitespace() -> None:
    base = dict(schema_hash="schema-1", principal="rep_east", allow_sensitive=False)
    left = cache_key(question=REVENUE, **base)
    right = cache_key(question=f"  {REVENUE.replace('？', '?')}\n", **base)
    assert left == right


def test_cache_key_separates_schema_permission_and_policy() -> None:
    base = dict(question=REVENUE, schema_hash="schema-1", principal="rep_east", allow_sensitive=False)
    assert cache_key(**base) != cache_key(**{**base, "schema_hash": "schema-2"})
    assert cache_key(**base) != cache_key(**{**base, "principal": "rep_west"})
    assert cache_key(**base) != cache_key(**{**base, "allow_sensitive": True})
    assert cache_key(**base) != cache_key(**{**base, "policy": {"prompt": "agent-v5"}})
    assert cache_key(**base) != cache_key(
        **{**base, "context": ({"q": "上一问", "sql": "SELECT 1", "status": "ok"},)}
    )


def test_result_cache_expires_at_ttl_and_reports_the_expiry() -> None:
    now = [0.0]
    cache = ResultCache(ttl_sec=10, clock=lambda: now[0])
    cache.put("k", {"value": 1})
    now[0] = 9.9
    found = cache.get("k")
    assert found is not None and found[0] == {"value": 1}
    now[0] = 10.0
    assert cache.get("k") is None
    stats = cache.stats()
    assert stats["expired"] == 1
    assert stats["entries"] == 0


def test_result_cache_is_a_bounded_lru() -> None:
    cache = ResultCache(ttl_sec=60, max_entries=2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == (1, pytest.approx(0.0, abs=1.0))
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None
    assert cache.stats()["evictions"] == 1


def test_disabled_cache_stores_nothing() -> None:
    cache = ResultCache(ttl_sec=0)
    cache.put("k", 1)
    assert cache.enabled is False
    assert cache.get("k") is None
    assert cache.stats() == {
        "enabled": False,
        "ttl_sec": 0.0,
        "max_entries": 128,
        "entries": 0,
        "hits": 0,
        "misses": 0,
        "hit_rate": None,
        "expired": 0,
        "evictions": 0,
    }


def test_agent_serves_a_warm_hit_without_calling_the_model(cached_runtime: Runtime) -> None:
    agent = cached_runtime.agent
    cold = agent.run(REVENUE, trace_id="trace-cold")
    warm = agent.run(f"  {REVENUE.replace('？', '?')}\n", trace_id="trace-warm")

    assert cold.status == "ok" and cold.cached is False
    assert warm.cached is True
    assert warm.trace_id == "trace-warm" and warm.trace_id != cold.trace_id
    assert warm.sql == cold.sql
    assert warm.tokens == warm.prompt_tokens == warm.completion_tokens == 0
    assert warm.steps == 0 and warm.tool_trace == []
    assert warm.cache["hit"] is True
    assert warm.cache["saved_steps"] == cold.steps
    assert warm.cache["saved_elapsed_ms"] == pytest.approx(cold.elapsed_ms)
    assert agent.cache.stats()["hits"] == 1


def test_warm_hit_is_a_deep_copy(cached_runtime: Runtime) -> None:
    agent = cached_runtime.agent
    cold = agent.run(REVENUE)
    warm = agent.run(REVENUE)
    warm.rows.append(["不存在的行"])
    assert cold.rows != warm.rows


def test_agent_caches_only_ok_turns(cached_runtime: Runtime) -> None:
    first = cached_runtime.agent.run(CLARIFY)
    second = cached_runtime.agent.run(CLARIFY)
    assert first.status != "ok" and second.cached is False
    assert cached_runtime.cache.stats()["entries"] == 0
    assert cached_runtime.cache.stats()["hits"] == 0


def test_history_changes_the_key_even_for_a_standalone_question(cached_runtime: Runtime) -> None:
    agent = cached_runtime.agent
    history = Conversation(
        turns=[
            Turn(
                question="6月华东销售额是多少？",
                effective_question="6月华东销售额是多少？",
                status="ok",
                sql="SELECT 1",
            )
        ]
    )
    assert agent._cache_key(REVENUE, None) != agent._cache_key(REVENUE, history)
    assert agent.run(REVENUE, history=history).cached is False


def test_use_cache_false_bypasses_the_cache(cached_runtime: Runtime) -> None:
    first = cached_runtime.agent.run(REVENUE, use_cache=False)
    second = cached_runtime.agent.run(REVENUE, use_cache=False)
    assert first.cached is False and second.cached is False
    stats = cached_runtime.cache.stats()
    assert stats["entries"] == 0 and stats["hits"] == 0


def test_cache_is_off_by_default(settings: Settings) -> None:
    runtime = create_runtime(settings)
    assert runtime.cache is None
    first = runtime.agent.run(REVENUE)
    second = runtime.agent.run(REVENUE)
    assert first.cached is False and second.cached is False


def test_result_to_dict_exposes_cache_and_token_split(cached_runtime: Runtime) -> None:
    cached_runtime.agent.run(REVENUE)
    payload = cached_runtime.agent.run(REVENUE).to_dict()
    assert payload["cached"] is True
    assert payload["cache"]["hit"] is True
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
        assert payload[key] == 0


def test_few_shot_sits_after_the_output_contract(cached_runtime: Runtime) -> None:
    prompt = cached_runtime.agent._system_prompt("问题: 示例\nSQL: SELECT 1")
    assert prompt.index("参考经验") > prompt.index("最终输出必须是一个 JSON 对象")


def test_prompt_prefix_stays_stable_across_different_few_shot_blocks(
    cached_runtime: Runtime,
) -> None:
    measured = measure_prompt_prefix(cached_runtime.agent)
    assert measured["few_shot_at_tail"] is True
    assert measured["stable_ratio"] >= 0.9
    assert measured["common_prefix_chars"] >= measured["base_prompt_chars"]


def test_cache_audit_measures_all_three_layers(tmp_path: Path) -> None:
    report = run_cache_audit(Settings(home=tmp_path, provider="mock"))
    result = report["result_cache"]
    assert result["measurable"] is True
    assert result["cold"]["cached"] is False and result["warm"]["cached"] is True
    assert result["cache_key_normalizes"] is True
    assert result["distinct_question"]["cached"] is False

    embedding = report["embedding_cache"]
    assert embedding["first"]["cache_hit"] is False
    assert embedding["second"]["cache_hit"] is True
    assert embedding["provider_calls_total"] == 1
    assert embedding["reused_without_embedding"] is True

    prompt = report["prompt_prefix"]
    assert prompt["few_shot_at_tail"] is True
    assert prompt["stable_ratio"] >= 0.9
