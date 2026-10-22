"""Tools for verified experience retrieval and candidate capture."""

from __future__ import annotations

from typing import Any

from ..guard import SQLGuard
from ..memory import ExperienceStore
from ..taxonomy import classify_category
from .base import Tool, ToolError, ToolSpec, string_param


def build_experience_tools(
    store: ExperienceStore,
    schema_hash: str,
    guard: SQLGuard,
    category_boost: float = 0.12,
) -> list[Tool]:
    def search_experience(question: str, top_k: int = 3) -> dict[str, Any]:
        category = classify_category(str(question or ""))
        examples = store.search(
            str(question or ""),
            schema_hash=schema_hash,
            top_k=max(1, min(10, int(top_k))),
            category=category,
            category_boost=category_boost,
        )
        return {
            "count": len(examples),
            "category": category,
            "examples": examples,
            "note": "只返回人工验证过的经验；候选经验不会进入提示词",
        }

    def save_experience(question: str, sql: str, category: str = "") -> dict[str, Any]:
        question = str(question or "").strip()
        sql = str(sql or "").strip()
        if not question or not sql:
            raise ToolError("question 和 sql 不能为空")
        check = guard.check(sql)
        if not check.valid:
            messages = "; ".join(issue.message for issue in check.errors)
            raise ToolError(f"候选 SQL 未通过护栏，不能写入经验库: {messages}")
        record = store.add(
            question,
            check.sql,
            category=str(category or "").strip() or classify_category(question),
            source="agent",
            verified=False,
            schema_hash=schema_hash,
        )
        return {
            "record": record,
            "reusable": False,
            "note": "已保存为候选经验；人工执行 experience verify 后才会被复用",
        }

    return [
        Tool(
            ToolSpec(
                name="search_experience",
                description=(
                    "Retrieve human-verified question/SQL examples from the experience memory. "
                    "Use them as few-shot references when the question is similar. Only verified "
                    "examples are returned; candidates are intentionally hidden."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "question": string_param("当前用户问题"),
                        "top_k": {"type": "integer", "default": 3, "description": "召回数量 1-10"},
                    },
                    "required": ["question"],
                },
            ),
            search_experience,
        ),
        Tool(
            ToolSpec(
                name="save_experience",
                description=(
                    "Save a successful question/SQL pair as a CANDIDATE experience. It will not be "
                    "reused until a human verifies it. Call only when the user asks to remember a query."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "question": string_param("用户问题"),
                        "sql": string_param("已经通过护栏且执行成功的 SQL"),
                        "category": string_param("问题类别，如 inventory / ranking"),
                    },
                    "required": ["question", "sql"],
                },
            ),
            save_experience,
        ),
    ]
