"""JSON-schema tool definitions and a small tool registry."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from ..text import to_jsonable


class ToolError(RuntimeError):
    """Raised when a tool cannot run at all (bad arguments / unknown tool)."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Tool:
    spec: ToolSpec
    handler: Callable[..., Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec.to_openai() for tool in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"未知工具: {name}"}
        args = dict(arguments or {})
        try:
            signature = inspect.signature(tool.handler)
            signature.bind(**args)
        except TypeError as exc:
            return {"ok": False, "error": f"工具 {name} 参数错误: {exc}"}
        try:
            data = tool.handler(**args)
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - tool errors must be fed back to the model
            return {"ok": False, "error": f"工具 {name} 执行失败: {exc}"}
        return {"ok": True, "data": to_jsonable(data)}


def string_param(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}
