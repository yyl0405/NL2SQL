"""Human-reviewed semantic layer merged over a raw schema catalog.

``schema-sync`` knows structure; it does not know whether a table is the business
"sales order". This module is the narrow place where a reviewer's answer becomes
runtime context: approved table/column aliases and business rules are merged into
the catalog, while unreviewed model suggestions never reach the agent.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from .catalog import Catalog, Column, Table


def empty_overlay(schema_hash: str = "") -> dict[str, Any]:
    return {
        "version": 1,
        "schema_hash": schema_hash,
        "table_aliases": {},
        "column_aliases": {},
        "default_filters": {},
        "disabled_tables": {},
        "business_rules": [],
        "approved": [],
    }


def load_overlay(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    if not file.exists():
        return empty_overlay()
    payload = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("schema overlay 必须是 JSON 对象")
    overlay = empty_overlay(str(payload.get("schema_hash") or ""))
    overlay["table_aliases"] = _alias_map(payload.get("table_aliases"))
    overlay["column_aliases"] = _alias_map(payload.get("column_aliases"))
    overlay["default_filters"] = _filter_map(payload.get("default_filters"))
    overlay["disabled_tables"] = _reason_map(payload.get("disabled_tables"))
    overlay["business_rules"] = _unique_strings(payload.get("business_rules") or [])
    approved = payload.get("approved") or []
    overlay["approved"] = approved if isinstance(approved, list) else []
    return overlay


def save_overlay(path: str | Path, overlay: dict[str, Any]) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_overlay(catalog: Catalog, overlay: dict[str, Any]) -> Catalog:
    """Return a catalog with approved aliases/rules merged; unknown names are ignored."""
    tables = dict(catalog.tables)
    for raw_name, aliases in (overlay.get("table_aliases") or {}).items():
        key = _table_key(str(raw_name))
        table = tables.get(key)
        if table is None:
            continue
        tables[key] = replace(table, synonyms=_unique_strings([*table.synonyms, *(aliases or [])]))

    for raw_key, aliases in (overlay.get("column_aliases") or {}).items():
        table_name, _, column_name = str(raw_key).partition(".")
        table = tables.get(_table_key(table_name))
        if table is None or not column_name:
            continue
        columns = list(table.columns)
        changed = False
        for index, column in enumerate(columns):
            if column.name.lower() != column_name.lower():
                continue
            columns[index] = replace(
                column,
                synonyms=_unique_strings([*column.synonyms, *(aliases or [])]),
            )
            changed = True
            break
        if changed:
            tables[_table_key(table_name)] = replace(table, columns=tuple(columns))

    for raw_name, filters in (overlay.get("default_filters") or {}).items():
        key = _table_key(str(raw_name))
        table = tables.get(key)
        if table is None:
            continue
        tables[key] = replace(
            table,
            default_filters=tuple(_unique_filters([*table.default_filters, *(filters or [])])),
        )

    # Visibility is a structure decision, not a hint: a table listed here never
    # reaches search_tables / get_table_schema, and the guard rejects it by name,
    # so the model cannot pick a legacy copy of a table it was never shown.
    for raw_name, reason in (overlay.get("disabled_tables") or {}).items():
        key = _table_key(str(raw_name))
        table = tables.get(key)
        if table is None or table.disabled:
            continue
        description = table.description
        note = str(reason or "").strip()
        tables[key] = replace(
            table,
            disabled=True,
            description=(f"{description}（已停用：{note}）" if note and description else description),
        )

    rules = _unique_strings([*catalog.business_rules, *(overlay.get("business_rules") or [])])
    return replace(
        catalog,
        tables=tables,
        business_rules=rules,
        _link_index=None,
        _link_key=None,
    )


def overlay_from_approved_suggestion(
    overlay: dict[str, Any],
    suggestion: dict[str, Any],
    *,
    reviewer: str = "",
) -> dict[str, Any]:
    """Fold one approved suggestion into an overlay without mutating the input."""
    result = deepcopy(overlay)
    result.setdefault("table_aliases", {})
    result.setdefault("column_aliases", {})
    result.setdefault("business_rules", [])
    result.setdefault("approved", [])

    table = str(suggestion.get("table") or "").strip()
    concept = str(suggestion.get("concept") or "").strip()
    if not table:
        raise ValueError("候选映射缺少表名，不能批准")
    if concept:
        _append_alias(result["table_aliases"], table, concept)

    for column, aliases in (suggestion.get("field_aliases") or {}).items():
        for alias in aliases or []:
            _append_alias(result["column_aliases"], f"{table}.{column}", str(alias))

    rule = str(suggestion.get("business_rule") or "").strip()
    if rule:
        result["business_rules"] = _unique_strings([*result["business_rules"], rule])

    result["approved"] = [
        *[item for item in result["approved"] if item.get("id") != suggestion.get("id")],
        {
            "id": suggestion.get("id"),
            "concept": concept,
            "table": table,
            "reviewer": reviewer,
            "suggestion_schema_hash": suggestion.get("schema_hash", ""),
        },
    ]
    return result


def _alias_map(raw: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        aliases = value if isinstance(value, list) else [value]
        cleaned = _unique_strings(aliases)
        if cleaned:
            result[str(key)] = cleaned
    return result


def _filter_map(raw: Any) -> dict[str, list[dict[str, str]]]:
    """Per-table default filters, authored either as objects or as ``"Col = val"``."""
    result: dict[str, list[dict[str, str]]] = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        filters = _unique_filters(value if isinstance(value, list) else [value])
        if filters:
            result[str(key)] = filters
    return result


def _unique_filters(values: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values or []:
        if isinstance(value, dict):
            column = str(value.get("column", "")).strip()
            literal = str(value.get("value", "")).strip()
            reason = str(value.get("reason", "")).strip()
        else:
            column, _, literal = str(value).partition("=")
            column, literal = column.strip(), literal.strip().strip("'\"")
            reason = ""
        if not column or not literal:
            continue
        key = (column.lower(), literal.lower())
        if key in seen:
            continue
        seen.add(key)
        entry = {"column": column, "value": literal}
        if reason:
            entry["reason"] = reason
        result.append(entry)
    return result


def _append_alias(mapping: dict[str, list[str]], key: str, alias: str) -> None:
    if not alias:
        return
    mapping[key] = _unique_strings([*mapping.get(key, []), alias])


def _reason_map(raw: Any) -> dict[str, str]:
    """``{"Table": "reason"}``, ``["Table"]`` or a mix of both → name → reason."""
    result: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            name = str(key).strip()
            if name:
                result[name] = str(value or "").strip()
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                for key, value in item.items():
                    name = str(key).strip()
                    if name:
                        result[name] = str(value or "").strip()
                continue
            name = str(item).strip()
            if name:
                result[name] = ""
    return result


def _table_key(name: str) -> str:
    cleaned = (name or "").strip().strip('"[]`').lower()
    if "." in cleaned:
        cleaned = cleaned.split(".")[-1]
    return cleaned


def _unique_strings(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            result.append(text)
    return result
