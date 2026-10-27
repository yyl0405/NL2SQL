"""Draft schema-to-business-concept mappings with an LLM, without trusting them.

The output is a local review file. A suggestion becomes runtime context only after
``schema-review approve`` writes it into the overlay. That separation is the point:
the model is allowed to propose, but only a reviewer can make a mapping authoritative.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .catalog import Catalog
from .llm import ChatModel, LLMError
from .text import parse_json_object
from .tools.db import Target, dialect_of, query

DEFAULT_CONCEPTS = (
    "销售订单",
    "客户",
    "物料",
    "库存",
    "采购订单",
    "工单",
    "BOM",
    "齐套",
    "发货",
    "供应商",
    "金额",
    "时间",
)

ProfileFn = Callable[[Any, int], dict[str, Any]]


@dataclass(frozen=True)
class SuggestOptions:
    concepts: tuple[str, ...] = DEFAULT_CONCEPTS
    top_tables: int = 3
    sample_rows: int = 3
    max_columns: int = 12
    timeout_ms: int = 5000


def suggest_schema(
    catalog: Catalog,
    *,
    llm: ChatModel,
    target: Target | None = None,
    options: SuggestOptions | None = None,
    profile_fn: ProfileFn | None = None,
) -> dict[str, Any]:
    """Ask the model for candidate mappings and return a reviewable draft file."""
    opts = options or SuggestOptions()
    suggestions: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    profile = profile_fn
    if profile is None and target is not None:
        def profile(table: Any, rows: int) -> dict[str, Any]:
            return profile_table(target, table, rows)

    for concept in opts.concepts:
        hits = catalog.search(concept, top_k=opts.top_tables).hits
        if not hits:
            observations.append({"concept": concept, "status": "no_candidate"})
            continue
        candidates = []
        for hit in hits:
            evidence = _table_evidence(hit.table, opts.max_columns)
            if target is not None and profile is not None:
                evidence["samples"] = profile(hit.table, opts.sample_rows)
            candidates.append(evidence)
        try:
            payload = _ask_model(llm, concept, candidates)
        except (LLMError, ValueError, TimeoutError) as exc:
            observations.append({"concept": concept, "status": "llm_failed", "error": str(exc)})
            continue
        suggestion = _normalize_suggestion(concept, payload, candidates, catalog)
        if suggestion is None:
            observations.append({"concept": concept, "status": "invalid_model_output"})
            continue
        suggestions.append(suggestion)

    return {
        "version": 1,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_hash": catalog.schema_hash(),
        "status": "draft",
        "requires_human_review": True,
        "suggestions": suggestions,
        "observations": observations,
    }


def write_suggestions(path: str | Path, payload: dict[str, Any]) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_suggestions(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    if not file.exists():
        return {"suggestions": [], "observations": []}
    return json.loads(file.read_text(encoding="utf-8"))


def profile_table(target: Target, table: Any, sample_rows: int = 3) -> dict[str, Any]:
    """Read a tiny non-sensitive sample for evidence; failures are reported, not hidden."""
    if table is None:
        return {"success": False, "error": "table_not_found"}
    columns = [column for column in table.columns if not column.sensitive and not column.disabled]
    if not columns:
        return {"success": True, "row_count": 0, "rows": []}
    selected = columns[:12]
    dialect = dialect_of(target)
    projection = ", ".join(dialect.quote(column.name) for column in selected)
    limit = max(1, min(5, int(sample_rows)))
    if target.dialect == "mssql":
        sql = f"SELECT TOP ({limit}) {projection} FROM {dialect.quote(table.name)}"
    else:
        sql = f"SELECT {projection} FROM {dialect.quote(table.name)} LIMIT {limit}"
    result = query(target, sql, max_rows=limit, timeout_ms=3000)
    if not result.get("success"):
        return {"success": False, "error": str(result.get("error_code") or result.get("error") or "query_failed")}
    return {
        "success": True,
        "row_count": int(result.get("row_count") or 0),
        "columns": [column.name for column in selected],
        "rows": result.get("rows") or [],
    }


def _table_evidence(table: Any, max_columns: int) -> dict[str, Any]:
    return {
        "table": table.name,
        "description": table.description,
        "synonyms": list(table.synonyms),
        "primary_key": list(table.primary_key),
        "disabled": bool(table.disabled),
        "columns": [
            {
                "name": column.name,
                "type": column.type,
                "description": column.description,
                "synonyms": list(column.synonyms),
                "sensitive": bool(column.sensitive),
            }
            for column in table.columns[:max_columns]
        ],
    }


def _ask_model(llm: ChatModel, concept: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    system = (
        "你在为 NL2SQL 系统提出数据库表语义映射草稿。只能根据给定候选表和字段判断，"
        "不得发明表名或字段名。输出必须是 JSON 对象，不要 Markdown。"
    )
    user = (
        f"业务概念：{concept}\n"
        "请从候选中选择最可能承载该概念的一张表，并给出字段别名、证据和一条待人工确认的业务规则。\n"
        "JSON 契约：\n"
        "{\n"
        '  "table": "候选中的真实表名",\n'
        '  "confidence": 0.0,\n'
        '  "reason": "为什么",\n'
        '  "evidence": ["证据1"],\n'
        '  "field_aliases": {"字段名": ["中文别名"]},\n'
        '  "business_rule": "一句话规则",\n'
        '  "ambiguous": false\n'
        "}\n"
        f"候选：{json.dumps(candidates, ensure_ascii=False)[:12000]}"
    )
    response = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
    payload = parse_json_object(response.content or "")
    if not isinstance(payload, dict):
        raise ValueError("模型没有返回 JSON 对象")
    return payload


def _normalize_suggestion(
    concept: str,
    payload: dict[str, Any],
    candidates: list[dict[str, Any]],
    catalog: Catalog,
) -> dict[str, Any] | None:
    table_name = str(payload.get("table") or "").strip()
    candidate_names = {str(item.get("table") or "") for item in candidates}
    table = catalog.table(table_name)
    if table is None or table.name not in candidate_names:
        return None
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    evidence = payload.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    field_aliases: dict[str, list[str]] = {}
    raw_aliases = payload.get("field_aliases") or {}
    if isinstance(raw_aliases, dict):
        for column, aliases in raw_aliases.items():
            if table.column(str(column)) is None:
                continue
            values = aliases if isinstance(aliases, list) else [aliases]
            cleaned = [str(item).strip() for item in values if str(item).strip()]
            if cleaned:
                field_aliases[str(column)] = cleaned
    return {
        "id": f"{concept}@{table.name}".lower(),
        "concept": concept,
        "table": table.name,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(payload.get("reason") or ""),
        "evidence": [str(item) for item in evidence][:8],
        "field_aliases": field_aliases,
        "business_rule": str(payload.get("business_rule") or "").strip(),
        "ambiguous": bool(payload.get("ambiguous") or False),
        "review_status": "pending",
        "schema_hash": catalog.schema_hash(),
    }
