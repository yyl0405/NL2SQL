"""Retry and circuit breaking for the model client.

Only the an online provider is wrapped. The offline mock has no network failure
modes, so wrapping it would add timing code that no measurement exercises.

The retry predicate is deliberately narrow: timeouts, connection errors, rate
limits and 5xx responses. A 400 (bad request, schema rejection, invalid tool
payload) is a fact about the request, not a transient condition, and repeating
it only spends tokens. The circuit breaker counts **attempts**, not turns: a
request that burns its one retry contributes two failures, because both were
real calls to a server that was not answering.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .llm import ChatModel, LLMError, LLMResponse

_TRANSIENT_NAMES = {
    "APITimeoutError",
    "APIConnectionError",
    "RateLimitError",
    "InternalServerError",
    "ServiceUnavailableError",
}
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
)

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


def is_transient(exc: BaseException) -> bool:
    """Walk the exception chain: the SDK's typed errors sit under our ``LLMError``."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError)):
            return True
        if type(current).__name__ in _TRANSIENT_NAMES:
            return True
        status = getattr(current, "status_code", None)
        if isinstance(status, int) and status in _TRANSIENT_STATUS:
            return True
        message = str(current).lower()
        if isinstance(current, LLMError) and any(marker in message for marker in _TRANSIENT_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


class CircuitBreaker:
    """Thread-safe failure gate with one half-open probe.

    ``failures <= 0`` disables it (calls always pass). After ``failures``
    consecutive failures it opens for ``reset_timeout`` seconds; the next call
    is a single probe — success closes it, failure re-opens it immediately.
    """

    def __init__(
        self,
        failures: int = 5,
        reset_timeout: float = 30.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(0, int(failures))
        self.reset_timeout = max(0.0, float(reset_timeout))
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = 0.0
        self._state = CLOSED
        self._probing = False

    def _state_now(self) -> str:
        if self._state == OPEN and self._clock() - self._opened_at >= self.reset_timeout:
            return HALF_OPEN
        return self._state

    def allow(self) -> bool:
        with self._lock:
            state = self._state_now()
            if state == CLOSED:
                return True
            if state == HALF_OPEN:
                if self._state != HALF_OPEN:
                    self._state = HALF_OPEN
                    self._probing = False
                if self._probing:
                    return False
                self._probing = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._state = CLOSED
            self._failures = 0
            self._probing = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            half_open_probe = self._state == HALF_OPEN or self._probing
            self._probing = False
            if half_open_probe or (
                self.failure_threshold > 0 and self._failures >= self.failure_threshold
            ):
                self._state = OPEN
                self._opened_at = self._clock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = self._state_now()
            retry_after_ms = (
                max(0, round((self._opened_at + self.reset_timeout - self._clock()) * 1000))
                if state == OPEN
                else 0
            )
            return {
                "state": state,
                "consecutive_failures": self._failures,
                "failure_threshold": self.failure_threshold,
                "reset_timeout_sec": self.reset_timeout,
                "retry_after_ms": retry_after_ms,
            }


class ResilientChatModel:
    """``ChatModel`` wrapper: transient retries in front of a shared breaker."""

    def __init__(
        self,
        inner: ChatModel,
        *,
        retries: int = 1,
        backoff_sec: float = 2.0,
        max_backoff_sec: float = 30.0,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.inner = inner
        self.retries = max(0, int(retries))
        self.backoff_sec = max(0.0, float(backoff_sec))
        self.max_backoff_sec = max(0.0, float(max_backoff_sec))
        self.breaker = breaker
        self._sleep = sleep

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        if self.breaker is not None and not self.breaker.allow():
            raise LLMError("模型服务熔断中：连续失败，已暂停调用；请稍后重试或检查模型服务")
        attempt = 0
        while True:
            try:
                response = self.inner.chat(messages, tools=tools)
            except Exception as exc:
                if self.breaker is not None:
                    self.breaker.record_failure()
                if attempt >= self.retries or not is_transient(exc):
                    raise
                delay = min(self.max_backoff_sec, self.backoff_sec * (2**attempt))
                if delay > 0:
                    self._sleep(delay)
                attempt += 1
                if self.breaker is not None and not self.breaker.allow():
                    raise LLMError(
                        f"模型服务熔断中：重试前已连续失败 {self.breaker.snapshot()['consecutive_failures']} 次"
                    ) from exc
            else:
                if self.breaker is not None:
                    self.breaker.record_success()
                return response

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.retries > 0 or self.breaker is not None,
            "retries": self.retries,
            "backoff_sec": self.backoff_sec,
            "circuit": self.breaker.snapshot() if self.breaker is not None else None,
        }
