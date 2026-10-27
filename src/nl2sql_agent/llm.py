"""LLM interfaces and an OpenAI-compatible tool-calling client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Settings


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class ChatModel(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        ...


def usage_tokens(usage: dict[str, int] | None) -> int:
    """Total tokens from a provider usage block, tolerant of missing fields."""
    if not usage:
        return 0
    total = usage.get("total_tokens")
    if total is None:
        total = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    try:
        return max(0, int(total))
    except (TypeError, ValueError):
        return 0


def usage_breakdown(usage: dict[str, int] | None) -> dict[str, int]:
    """Prompt/completion/cached split; absent fields stay 0 instead of guessed.

    DashScope and OpenAI both report cached prefix tokens in
    ``prompt_tokens_details.cached_tokens``; DeepSeek uses
    ``prompt_cache_hit_tokens``. The client folds both spellings into
    ``cached_tokens`` while parsing the raw response.
    """
    usage = usage or {}

    def _int(key: str) -> int:
        try:
            return max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    prompt = _int("prompt_tokens")
    completion = _int("completion_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": min(_int("cached_tokens"), prompt) if prompt else _int("cached_tokens"),
        "total_tokens": usage_tokens(usage),
    }


class OpenAICompatibleLLM:
    """Works with DashScope, DeepSeek, vLLM, Ollama and other compatible APIs."""

    def __init__(self, settings: Settings) -> None:
        if not settings.api_key:
            raise LLMError(
                "NL2SQL_PROVIDER=openai 但未配置 API Key。请在 .env 中设置 NL2SQL_API_KEY，"
                "或改用 NL2SQL_PROVIDER=mock 离线运行。"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError("缺少 openai 依赖，请执行: pip install -e '.[llm]'") from exc
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout_sec,
        )

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": self.settings.max_tokens,
        }
        if self.settings.disable_thinking:
            # DashScope/DeepSeek reasoning models otherwise spend the token budget
            # on reasoning and can truncate the JSON/SQL output.
            kwargs["extra_body"] = {"enable_thinking": False}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surface a concise provider error
            raise LLMError(f"LLM 调用失败: {exc}") from exc
        if not response.choices:
            raise LLMError("LLM 响应缺少 choices")
        message = response.choices[0].message
        tool_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            raw_arguments = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {"__raw_arguments__": raw_arguments}
            tool_calls.append(
                ToolCall(id=call.id or f"call-{len(tool_calls)}", name=call.function.name, arguments=arguments)
            )
        usage: dict[str, int] = {}
        usage_obj = getattr(response, "usage", None)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage_obj, key, None) if usage_obj is not None else None
            if value is not None:
                usage[key] = int(value)
        details = getattr(usage_obj, "prompt_tokens_details", None) if usage_obj is not None else None
        cached = getattr(details, "cached_tokens", None) if details is not None else None
        if cached is None and usage_obj is not None:
            cached = getattr(usage_obj, "prompt_cache_hit_tokens", None)
        if cached is not None:
            usage["cached_tokens"] = int(cached)
        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            finish_reason=response.choices[0].finish_reason or "",
            usage=usage,
        )
