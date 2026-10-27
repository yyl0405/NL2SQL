from __future__ import annotations

from dataclasses import replace

import pytest

from nl2sql_agent.config import Settings
from nl2sql_agent.llm import LLMError, LLMResponse
from nl2sql_agent.mock_llm import RuleBasedLLM
from nl2sql_agent.resilience import (
    CircuitBreaker,
    ResilientChatModel,
    is_transient,
)
from nl2sql_agent.runtime import create_runtime


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeModel:
    def __init__(self, outcomes: list[BaseException | LLMResponse]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def chat(self, messages, tools=None) -> LLMResponse:
        assert self.outcomes, "fake model ran out of outcomes"
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RateLimited(Exception):
    status_code = 429


def test_a_timeout_is_retried_once_and_the_second_answer_wins() -> None:
    model = FakeModel([LLMError("Request timed out"), LLMResponse(content="ok")])
    sleeps: list[float] = []
    resilient = ResilientChatModel(model, retries=1, backoff_sec=2.0, sleep=sleeps.append)

    response = resilient.chat([{"role": "user", "content": "q"}])

    assert response.content == "ok"
    assert model.calls == 2
    assert sleeps == [2.0]


def test_a_request_error_is_not_retried() -> None:
    model = FakeModel([LLMError("400 invalid tool schema")])
    sleeps: list[float] = []
    resilient = ResilientChatModel(model, retries=3, backoff_sec=2.0, sleep=sleeps.append)

    with pytest.raises(LLMError, match="400"):
        resilient.chat([])
    assert model.calls == 1
    assert sleeps == []


def test_exhausted_retries_open_the_breaker_and_the_next_call_never_reaches_the_model() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failures=2, reset_timeout=30.0, clock=clock)
    model = FakeModel([LLMError("timed out"), LLMError("timed out")])
    resilient = ResilientChatModel(model, retries=1, backoff_sec=0.0, breaker=breaker)

    with pytest.raises(LLMError):
        resilient.chat([])
    # Two attempts, two failures: the threshold counts calls, not turns.
    assert model.calls == 2
    assert breaker.snapshot()["state"] == "open"

    with pytest.raises(LLMError, match="熔断"):
        resilient.chat([])
    assert model.calls == 2


def test_a_half_open_probe_that_succeeds_closes_the_breaker() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failures=1, reset_timeout=30.0, clock=clock)
    model = FakeModel([LLMError("timed out"), LLMResponse(content="recovered")])
    resilient = ResilientChatModel(model, retries=0, breaker=breaker)

    with pytest.raises(LLMError):
        resilient.chat([])
    assert breaker.snapshot()["state"] == "open"
    clock.advance(30.0)
    assert breaker.snapshot()["state"] == "half_open"

    assert resilient.chat([]).content == "recovered"
    assert breaker.snapshot()["state"] == "closed"
    assert breaker.snapshot()["consecutive_failures"] == 0


def test_a_failed_half_open_probe_reopens_immediately() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failures=1, reset_timeout=10.0, clock=clock)
    model = FakeModel([LLMError("timed out"), LLMError("timed out")])
    resilient = ResilientChatModel(model, retries=0, breaker=breaker)

    with pytest.raises(LLMError):
        resilient.chat([])
    clock.advance(10.0)
    with pytest.raises(LLMError):
        resilient.chat([])
    assert breaker.snapshot()["state"] == "open"
    assert breaker.snapshot()["consecutive_failures"] == 2


def test_half_open_admits_only_one_probe_at_a_time() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failures=1, reset_timeout=5.0, clock=clock)
    breaker.record_failure()
    clock.advance(5.0)

    assert breaker.allow() is True
    assert breaker.allow() is False, "第二个并发探测会在半开状态同时打模型"
    breaker.record_success()
    assert breaker.allow() is True


def test_breaker_zero_failures_disables_the_gate() -> None:
    breaker = CircuitBreaker(failures=0)
    for _ in range(100):
        assert breaker.allow() is True
        breaker.record_failure()
    assert breaker.allow() is True


def test_is_transient_walks_the_cause_chain_and_status_codes() -> None:
    wrapped = LLMError("LLM 调用失败")
    wrapped.__cause__ = TimeoutError("socket timed out")
    assert is_transient(wrapped) is True
    assert is_transient(LLMError("LLM 调用失败: 429 rate limit")) is False
    wrapped_429 = LLMError("LLM 调用失败")
    wrapped_429.__cause__ = RateLimited("slow down")
    assert is_transient(wrapped_429) is True
    assert is_transient(ValueError("bad schema")) is False


def test_mock_runtime_is_not_wrapped(settings: Settings) -> None:
    runtime = create_runtime(settings)
    assert isinstance(runtime.agent.llm, RuleBasedLLM)
    assert runtime.llm is runtime.agent.llm
    assert not hasattr(runtime.llm, "snapshot")


def test_openai_runtime_wraps_the_provider_with_a_breaker(settings: Settings, monkeypatch) -> None:
    from nl2sql_agent import runtime as runtime_module

    class FakeProvider:
        def __init__(self, _settings) -> None:
            self.settings = _settings

        def chat(self, messages, tools=None) -> LLMResponse:
            return LLMResponse(content="ok")

    monkeypatch.setattr(runtime_module, "OpenAICompatibleLLM", FakeProvider)
    opened = replace(
        settings,
        provider="openai",
        api_key="test-key",
        llm_retries=2,
        llm_circuit_failures=4,
        llm_circuit_reset_sec=7.0,
    )
    runtime = create_runtime(opened)

    assert isinstance(runtime.llm, ResilientChatModel)
    assert runtime.agent.llm is runtime.llm
    snapshot = runtime.llm.snapshot()
    assert snapshot["enabled"] is True
    assert snapshot["retries"] == 2
    assert snapshot["circuit"]["state"] == "closed"
    assert snapshot["circuit"]["failure_threshold"] == 4
