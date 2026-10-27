"""Row-level and column-level access control for NL2SQL queries.

A `Principal` describes what one caller may see. It is applied in two places,
deliberately differently:

* the **agent-facing catalog** is filtered, so unauthorized tables and columns
  never reach the prompt, `search_tables` or `get_table_schema` — the model
  cannot be tempted by schema it was never allowed to know about;
* the **guard** keeps the full catalog plus the principal, so a blocked
  reference is reported as `permission_table` / `permission_column` instead of
  the misleading `unknown_table` / `unknown_column`.

Row filters are *mandatory predicates*, not rewrites: the SQL must contain
`column = 'value'` (or `IN (...)`) itself. Silently injecting a predicate would
change a query the model believes it wrote, and the guard's job is to tell the
truth about what a query does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .catalog import Catalog, Table


class PrincipalError(RuntimeError):
    pass


@dataclass(frozen=True)
class RowFilter:
    table: str
    column: str
    values: tuple[str, ...]

    @property
    def required_predicate(self) -> str:
        if len(self.values) == 1:
            return f"{self.table}.{self.column} = '{self.values[0]}'"
        joined = ", ".join(f"'{value}'" for value in self.values)
        return f"{self.table}.{self.column} IN ({joined})"

    def satisfied_by(self, sql: str) -> bool:
        """True only when every textual `column = …` / `column IN (…)` predicate
        on this column stays inside the authorized value set — subqueries,
        `LIKE`, and an out-of-scope arm of an `OR` all fail closed."""
        column = re.escape(self.column)
        allowed = {value.strip().casefold() for value in self.values}
        if not allowed:
            return False
        pattern = (
            rf"\b{column}\s*(?:=\s*(?P<eq>'(?:[^']|'')*'|[^\s,()'=]+)"
            rf"|\s+in\s*\(\s*(?P<in>[^()]*)\))"
        )
        matched = False
        for match in re.finditer(pattern, sql, re.IGNORECASE):
            literal = match.group("eq")
            if literal is not None:
                values = {literal.strip("'").replace("''", "'").strip().casefold()}
            else:
                values = {
                    item.strip().strip("'\"").replace("''", "'").casefold()
                    for item in (match.group("in") or "").split(",")
                    if item.strip()
                }
            if not values or not values <= allowed:
                return False
            matched = True
        return matched


@dataclass
class Principal:
    """One caller's visible schema plus mandatory row predicates."""

    id: str
    display_name: str = ""
    tables: tuple[str, ...] | None = None
    exclude_tables: tuple[str, ...] = ()
    exclude_columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    only_columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    row_filters: tuple[RowFilter, ...] = ()
    note: str = ""

    def visible(self, table_name: str) -> bool:
        name = (table_name or "").strip().lower()
        if name in {t.lower() for t in self.exclude_tables}:
            return False
        if self.tables is None:
            return True
        return name in {t.lower() for t in self.tables}

    def column_visible(self, table_name: str, column_name: str) -> bool:
        table_key = (table_name or "").strip().lower()
        column_key = (column_name or "").strip().lower()
        if column_key in self.exclude_columns.get(table_key, ()):
            return False
        allowed = self.only_columns.get(table_key)
        if allowed is not None and column_key not in allowed:
            return False
        return True

    def filters_for(self, table_name: str) -> tuple[RowFilter, ...]:
        name = (table_name or "").strip().lower()
        return tuple(f for f in self.row_filters if f.table.strip().lower() == name)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "tables": list(self.tables) if self.tables is not None else "all",
            "exclude_tables": list(self.exclude_tables),
            "exclude_columns": {k: list(v) for k, v in self.exclude_columns.items()},
            "only_columns": {k: list(v) for k, v in self.only_columns.items()},
            "row_filters": [
                {"table": f.table, "column": f.column, "values": list(f.values)}
                for f in self.row_filters
            ],
            "note": self.note,
        }

    def view(self, catalog: Catalog) -> Catalog:
        """The catalog this principal is allowed to see (used for prompts/tools)."""
        tables: dict[str, Table] = {}
        for key, table in catalog.tables.items():
            if table.disabled or not self.visible(table.name):
                continue
            columns = []
            for column in table.columns:
                if column.disabled or not self.column_visible(table.name, column.name):
                    continue
                columns.append(column)
            filters = list(table.required_filters) + [
                {"column": f.column, "value": f.values[0], "reason": f"权限范围：{f.required_predicate}"}
                for f in self.filters_for(table.name)
                if len(f.values) == 1
            ]
            tables[key] = replace(
                table,
                columns=tuple(columns),
                required_filters=tuple(dict(item) for item in filters),
            )
        names = {name.split(".")[0].lower() for name in tables}
        relationships = [
            rel
            for rel in catalog.relationships
            if str(rel.get("left", "")).split(".")[0].lower() in names
            and str(rel.get("right", "")).split(".")[0].lower() in names
        ]
        return replace(
            catalog,
            tables=tables,
            relationships=relationships,
            _link_index=None,
            _link_key=None,
        )


def _string_tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    return tuple(str(item) for item in raw)


def _column_map(raw: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(table).strip().lower(): tuple(str(item).strip().lower() for item in _string_tuple(columns))
        for table, columns in raw.items()
    }


def principal_from_payload(raw: dict[str, Any]) -> Principal:
    row_filters: list[RowFilter] = []
    for item in raw.get("row_filters", []) or []:
        if not isinstance(item, dict):
            continue
        values = item.get("values")
        if values is None and item.get("value") is not None:
            values = [item.get("value")]
        row_filters.append(
            RowFilter(
                table=str(item.get("table", "")),
                column=str(item.get("column", "")),
                values=_string_tuple(values),
            )
        )
    tables = raw.get("tables")
    return Principal(
        id=str(raw.get("id", "")).strip(),
        display_name=str(raw.get("display_name", "")),
        tables=None if tables is None else _string_tuple(tables),
        exclude_tables=_string_tuple(raw.get("exclude_tables")),
        exclude_columns=_column_map(raw.get("exclude_columns")),
        only_columns=_column_map(raw.get("only_columns")),
        row_filters=tuple(row_filters),
        note=str(raw.get("note", "")),
    )


def load_principal(path: str | Path, principal_id: str) -> Principal:
    file = Path(path)
    if not file.is_file():
        raise PrincipalError(f"权限配置不存在: {file}")
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PrincipalError(f"权限配置不是合法 JSON: {file}") from exc
    items = payload.get("principals") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise PrincipalError(f"权限配置缺少 principals 数组: {file}")
    wanted = (principal_id or "").strip().lower()
    for raw in items:
        if isinstance(raw, dict) and str(raw.get("id", "")).strip().lower() == wanted:
            return principal_from_payload(raw)
    known = ", ".join(str(raw.get("id")) for raw in items if isinstance(raw, dict))
    raise PrincipalError(f"权限配置里没有 id={principal_id}；可选: {known or '(空)'}")


def list_principals(path: str | Path) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.is_file():
        return []
    payload = json.loads(file.read_text(encoding="utf-8"))
    items = payload.get("principals") if isinstance(payload, dict) else payload
    return [principal_from_payload(raw).describe() for raw in items or [] if isinstance(raw, dict)]
