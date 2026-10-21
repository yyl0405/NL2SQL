"""The NL2SQL agent loop: plan, call tools, reflect on errors, answer."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog
from .config import Settings
from .llm import ChatModel, LLMError, LLMResponse, ToolCall
from .text import parse_json_object, to_jsonable
from .tools.base import ToolRegistry

_ALLOWED_STATUS = {"ok", "needs_clarification", "needs_human", "refused", "failed"}


@dataclass
class AgentResult:
    question: str
    status: str
    answer: str = ""
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    confidence: float = 0.0
    steps: int = 0
    elapsed_ms: float = 0.0
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "answer": self.answer,
            "sql": self.sql,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "confidence": round(self.confidence, 3),
            "steps": self.steps,
            "elapsed_ms": self.elapsed_ms,
            "tool_trace": self.tool_trace,
            "assumptions": self.assumptions,
            "error": self.error,
        }


class NL2SQLAgent:
    def __init__(
        self,
        catalog: Catalog,
        llm: ChatModel,
        tools: ToolRegistry,
        settings: Settings,
    ) -> None:
        self.catalog = catalog
        self.llm = llm
        self.tools = tools
        self.settings = settings

    def run(self, question: str) -> AgentResult:
        started = time.monotonic()
        question = (question or "").strip()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": question},
        ]
        trace: list[dict[str, Any]] = []
        last_sql: str | None = None
        last_execution: dict[str, Any] | None = None
        response: LLMResponse | None = None

        for step in range(1, max(2, self.settings.max_steps) + 1):
            try:
                response = self.llm.chat(messages, tools=self.tools.specs())
            except LLMError as exc:
                return self._result(
                    question,
                    status="failed",
                    answer=f"模型调用失败: {exc}",
                    error=str(exc),
                    steps=step - 1,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    trace=trace,
                )

            if not response.tool_calls:
                return self._finalize(
                    question,
                    response,
                    last_sql,
                    last_execution,
                    trace,
                    step,
                    started,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )

            for call in response.tool_calls:
                call_started = time.monotonic()
                payload = self.tools.call(call.name, call.arguments)
                payload["tool"] = call.name
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(to_jsonable(payload), ensure_ascii=False, default=str),
                    }
                )
                trace.append(
                    {
                        "step": step,
                        "tool": call.name,
                        "arguments": to_jsonable(call.arguments),
                        "ok": bool(payload.get("ok")),
                        "summary": _summarize_tool_result(call.name, payload),
                        "elapsed_ms": round((time.monotonic() - call_started) * 1000, 2),
                    }
                )
                data = payload.get("data") if payload.get("ok") else None
                if isinstance(data, dict):
                    if call.name == "validate_sql" and data.get("sql"):
                        last_sql = str(data["sql"])
                    elif call.name == "execute_sql":
                        if data.get("sql"):
                            last_sql = str(data["sql"])
                        if data.get("success"):
                            last_execution = data
                    elif call.name == "repair_sql" and data.get("sql"):
                        last_sql = str(data["sql"])

        return self._result(
            question,
            status="max_steps",
            answer="Agent 达到最大工具调用轮次，未能形成结论。",
            sql=last_sql,
            execution=last_execution,
            steps=max(2, self.settings.max_steps),
            elapsed_ms=(time.monotonic() - started) * 1000,
            trace=trace,
            error="max_steps_exceeded",
        )

    def _finalize(
        self,
        question: str,
        response: LLMResponse,
        last_sql: str | None,
        last_execution: dict[str, Any] | None,
        trace: list[dict[str, Any]],
        steps: int,
        started: float,
    ) -> AgentResult:
        payload = parse_json_object(response.content) or {}
        answer = str(payload.get("answer") or response.content or "").strip()
        status = str(payload.get("status") or "").strip().lower()
        sql = payload.get("sql") or last_sql
        confidence = _as_float(payload.get("confidence"), default=0.5)
        assumptions = payload.get("assumptions")
        assumptions = [str(item) for item in assumptions] if isinstance(assumptions, list) else []

        if status not in _ALLOWED_STATUS:
            if last_execution:
                status = "ok"
            elif any(word in answer for word in ("请补充", "无法", "不包含", "不支持")):
                status = "needs_clarification"
            else:
                status = "needs_human"

        if status == "ok" and not last_execution:
            status = "needs_clarification"
            confidence = min(confidence, 0.4)
            answer = answer or "模型没有执行 SQL，无法给出可验证的结论。"
        if status == "ok" and last_execution:
            answer = answer or _fallback_answer(last_execution)

        error = ""
        if status not in {"ok", "needs_clarification", "refused"}:
            error = str(payload.get("error") or "answer_not_verified")

        return self._result(
            question,
            status=status,
            answer=answer,
            sql=sql,
            execution=last_execution,
            confidence=confidence,
            assumptions=assumptions,
            steps=steps,
            elapsed_ms=(time.monotonic() - started) * 1000,
            trace=trace,
            error=error,
        )

    def _result(
        self,
        question: str,
        *,
        status: str,
        answer: str,
        sql: str | None = None,
        execution: dict[str, Any] | None = None,
        confidence: float = 0.0,
        assumptions: list[str] | None = None,
        steps: int = 0,
        elapsed_ms: float = 0.0,
        trace: list[dict[str, Any]] | None = None,
        error: str = "",
    ) -> AgentResult:
        return AgentResult(
            question=question,
            status=status,
            answer=answer,
            sql=sql,
            columns=list((execution or {}).get("columns") or []),
            rows=list((execution or {}).get("rows") or []),
            row_count=int((execution or {}).get("row_count") or 0),
            confidence=confidence,
            steps=steps,
            elapsed_ms=round(elapsed_ms, 2),
            tool_trace=trace or [],
            assumptions=assumptions or [],
            error=error,
        )

    def _system_prompt(self) -> str:
        table_lines = "\n".join(
            f"- {table.name}: {table.description}" for table in self.catalog.enabled_tables()
        )
        rules = "\n".join(f"- {rule}" for rule in self.catalog.business_rules)
        return f"""你是一个面向离散制造 ERP 的 NL2SQL Agent。你通过工具调用完成问数，而不是凭记忆猜表名或字段名。

数据库: SQLite 演示库（只读）
今天/基准日期: {self.catalog.as_of_date or self.settings.demo_today}

可用表（完整字段必须通过 get_table_schema 获取）:
{table_lines}

业务口径:
{rules}

工作流（必须遵守）:
1. 先调用 search_tables，找到与问题相关的候选表。
2. 调用 get_table_schema 查看真实字段、禁用/敏感标记、必要 JOIN 过滤条件和关系。禁止猜测字段。
3. 可选调用 search_experience，参考人工验证过的相似问法；候选经验不可信。
4. 过滤中文枚举值（客户、物料、状态、区域）不确定时，调用 search_values 对齐真实值。
5. 只生成单条 SQLite SELECT/WITH 查询。列必须显式列出；金额保留 2 位，比率保留 4 位；必要时加 LIMIT。
6. 执行前必须调用 validate_sql。校验失败就根据错误修正，不能绕过护栏。
7. 校验通过后调用 execute_sql。执行报错时调用 repair_sql，然后重新 execute_sql；确定性修复最多两轮。
8. 如果仍无法完成，转入人工，不要编造数据。

硬性约束:
- 只允许查询 schema 中启用的表和字段；禁用表、禁用字段、敏感字段一律不可查询。
- 禁止 SELECT *、写操作、多语句、DDL、PRAGMA、ATTACH。
- 经验记忆只作为 few-shot 参考；没有执行结果时不得声称“查询成功”。
- 涉及聚合时注意 JOIN 扇出，避免重复计数。

最终输出必须是一个 JSON 对象，不要输出 Markdown:
{{
  "status": "ok | needs_clarification | needs_human | refused",
  "answer": "面向业务人员的简洁回答，可包含关键数字",
  "sql": "实际执行并得到结果的 SQL，未执行则为 null",
  "confidence": 0.0,
  "assumptions": ["必要的口径假设"]
}}
"""


def _summarize_tool_result(name: str, payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return f"error: {payload.get('error', 'tool failed')}"
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return "ok"
    if name == "search_tables":
        return "tables=" + ",".join(item.get("name", "") for item in data.get("tables", [])[:6])
    if name == "get_table_schema":
        return "schemas=" + ",".join(item.get("name", "") for item in data.get("tables", []))
    if name == "search_values":
        return f"values={data.get('values', [])[:5]}"
    if name == "validate_sql":
        if data.get("valid"):
            return f"valid, tables={data.get('tables')}"
        return "invalid: " + "; ".join(
            str(item.get("message", "")) for item in data.get("errors", [])[:3]
        )
    if name == "execute_sql":
        if data.get("success"):
            return f"rows={data.get('row_count')}, truncated={data.get('truncated')}"
        return f"error: {data.get('error', '')}"
    if name == "repair_sql":
        return f"changed={data.get('changed')}, applied={data.get('applied')}"
    if name in {"search_experience", "save_experience"}:
        return f"count={data.get('count', data.get('record', {}).get('status', ''))}"
    return "ok"


def _fallback_answer(execution: dict[str, Any]) -> str:
    rows = execution.get("rows") or []
    columns = execution.get("columns") or []
    if not rows:
        return "查询完成，但没有匹配的数据。"
    if len(rows) == 1 and len(columns) == 1:
        return f"查询结果：{rows[0][0]}"
    return f"查询完成，共 {len(rows)} 行。"


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
