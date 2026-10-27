from __future__ import annotations

import re
from dataclasses import replace

import pytest

from nl2sql_agent.agent import AGENT_PROMPT_VERSION, GRAY_PROMPT_VERSION, NL2SQLAgent
from nl2sql_agent.config import Settings
from nl2sql_agent.gray import BUCKETS, bucket, in_gray, share
from nl2sql_agent.observability import summarize
from nl2sql_agent.runtime import Runtime, create_runtime

QUESTION = "2026年6月华东地区销售订单总金额是多少？"
FEW_SHOT = "问：6月华东销售额是多少？\nSQL：SELECT SUM(Amount) FROM SalesOrder"


def _runtime(settings: Settings) -> Runtime:
    return create_runtime(settings)


def test_bucket_is_a_pure_function_of_key_and_salt() -> None:
    assert bucket("session:abc", salt="gray-v1") == bucket("session:abc", salt="gray-v1")
    assert bucket("session:abc", salt="gray-v1") != bucket("session:abc", salt="gray-v2")
    assert 0 <= bucket("session:abc") < BUCKETS


def test_extremes_are_exact_not_approximate() -> None:
    for key in ("a", "b", "session:123"):
        assert in_gray(key, 0) is False
        assert in_gray(key, 100) is True


def test_share_explains_the_assignment() -> None:
    row = share("session:abc", 10, salt="gray-v1")
    assert row["key"] == "session:abc"
    assert row["threshold"] == 1000
    assert row["candidate"] is in_gray("session:abc", 10, salt="gray-v1")
    assert 0 <= row["bucket"] < BUCKETS


def test_ten_percent_is_roughly_ten_percent_of_keys() -> None:
    keys = [f"session:{index}" for index in range(2000)]
    selected = sum(1 for key in keys if in_gray(key, 10))
    # Deterministic sample, not a random one: this bound is a smoke test that the
    # threshold is not off by a factor of ten, not a statistical claim.
    assert 150 <= selected <= 250


def test_a_key_keeps_its_arm_across_turns_and_salts_are_reproducible() -> None:
    candidate = next(
        key for key in (f"session:{index}" for index in range(200)) if in_gray(key, 10)
    )
    first = share(candidate, 10)
    second = share(candidate, 10)
    assert first == second
    assert first["candidate"] is True


def test_agent_routes_a_candidate_key_and_records_the_bucket(tmp_path) -> None:
    settings = Settings(home=tmp_path, provider="mock", gray_percent=10, gray_salt="test")
    runtime = _runtime(settings)
    key = next(k for k in (f"session:{i}" for i in range(200)) if in_gray(k, 10, salt="test"))
    result = runtime.agent.run(QUESTION, gray_key=key)
    assert result.prompt_version == GRAY_PROMPT_VERSION
    assert result.gray_arm == "candidate"
    assert result.gray_bucket == bucket(key, salt="test")
    assert result.to_dict()["gray_arm"] == "candidate"


def test_agent_routes_a_base_key_and_keeps_the_bucket_for_audit(tmp_path) -> None:
    settings = Settings(home=tmp_path, provider="mock", gray_percent=10, gray_salt="test")
    runtime = _runtime(settings)
    key = next(k for k in (f"session:{i}" for i in range(200)) if not in_gray(k, 10, salt="test"))
    result = runtime.agent.run(QUESTION, gray_key=key)
    assert result.prompt_version == AGENT_PROMPT_VERSION
    assert result.gray_arm == "base"
    assert result.gray_bucket == bucket(key, salt="test")


def test_gray_off_keeps_the_previous_defaults(tmp_path) -> None:
    runtime = _runtime(Settings(home=tmp_path, provider="mock"))
    result = runtime.agent.run(QUESTION)
    assert result.prompt_version == AGENT_PROMPT_VERSION
    assert result.gray_arm == "base"
    assert result.gray_bucket is None


def test_force_prompt_selects_an_arm_and_rejects_typos(tmp_path) -> None:
    runtime = _runtime(Settings(home=tmp_path, provider="mock", gray_percent=100))
    forced = runtime.agent.run(QUESTION, force_prompt=GRAY_PROMPT_VERSION)
    assert forced.prompt_version == GRAY_PROMPT_VERSION
    assert forced.gray_arm == "candidate"
    assert forced.gray_bucket is None
    with pytest.raises(ValueError):
        runtime.agent.run(QUESTION, force_prompt="agent-v5-typo")


def test_the_two_layouts_differ_only_in_few_shot_placement(tmp_path) -> None:
    runtime = _runtime(Settings(home=tmp_path, provider="mock"))
    agent: NL2SQLAgent = runtime.agent
    tail = agent._system_prompt(FEW_SHOT, layout="few_shot_tail")
    early = agent._system_prompt(FEW_SHOT, layout="few_shot_early")
    assert tail.index("最终输出必须是一个 JSON 对象") < tail.index("参考经验")
    assert early.index("参考经验") < early.index("最终输出必须是一个 JSON 对象")
    assert tail.rstrip("\n").endswith(FEW_SHOT)
    # Same instructions and same examples, only the block moved. Blank-line
    # differences are the placement's own separators, not different content.
    header = "参考经验（人工验证过的相似问法"

    def without_examples(text: str) -> str:
        start = text.index(header)
        end = text.index(FEW_SHOT, start) + len(FEW_SHOT)
        return re.sub(r"\n{2,}", "\n\n", text[:start] + text[end:]).strip("\n")

    assert without_examples(tail) == without_examples(early)


def test_without_examples_both_layouts_are_identical(tmp_path) -> None:
    agent = _runtime(Settings(home=tmp_path, provider="mock")).agent
    assert agent._system_prompt("", layout="few_shot_tail") == agent._system_prompt(
        "", layout="few_shot_early"
    )


def test_gray_arms_do_not_share_the_result_cache(tmp_path) -> None:
    settings = replace(
        Settings(home=tmp_path, provider="mock"), result_cache_ttl_sec=60.0
    )
    runtime = _runtime(settings)
    base = runtime.agent.run(QUESTION, force_prompt=AGENT_PROMPT_VERSION)
    candidate = runtime.agent.run(QUESTION, force_prompt=GRAY_PROMPT_VERSION)
    assert base.cached is False
    assert candidate.cached is False
    assert candidate.steps > 0
    warm = runtime.agent.run(QUESTION, force_prompt=AGENT_PROMPT_VERSION)
    assert warm.cached is True
    assert warm.prompt_version == AGENT_PROMPT_VERSION


def test_metrics_split_turns_by_prompt_version() -> None:
    turns = [
        {"event": "turn", "prompt_version": AGENT_PROMPT_VERSION, "status": "ok"},
        {"event": "turn", "prompt_version": AGENT_PROMPT_VERSION, "status": "needs_clarification"},
        {"event": "turn", "prompt_version": GRAY_PROMPT_VERSION, "status": "ok"},
        {"event": "turn", "prompt_version": GRAY_PROMPT_VERSION, "status": "needs_human"},
    ]
    rows = summarize(turns)["by_prompt_version"]
    assert rows[AGENT_PROMPT_VERSION] == {
        "turns": 2,
        "ok": 1,
        "needs_human": 0,
        "needs_clarification": 1,
        "ok_rate": 0.5,
        "escalation_rate": 0.0,
        "clarification_rate": 0.5,
    }
    assert rows[GRAY_PROMPT_VERSION]["escalation_rate"] == 0.5


def test_turn_event_carries_the_gray_arm_and_bucket(tmp_path) -> None:
    import io
    import json

    from nl2sql_agent.observability import configure_logging, reset_logging

    settings = Settings(home=tmp_path, provider="mock", gray_percent=10, gray_salt="test")
    runtime = _runtime(settings)
    key = next(k for k in (f"session:{i}" for i in range(200)) if in_gray(k, 10, salt="test"))
    stream = io.StringIO()
    configure_logging(None, stream=stream)
    try:
        runtime.agent.run(QUESTION, gray_key=key)
    finally:
        reset_logging()
    turn = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "turn"
    )
    assert turn["prompt_version"] == GRAY_PROMPT_VERSION
    assert turn["gray_arm"] == "candidate"
    assert turn["gray_bucket"] == bucket(key, salt="test")
