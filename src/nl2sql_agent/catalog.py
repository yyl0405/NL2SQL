"""Schema catalog and dependency-free lexical schema linking."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .text import BM25, tokenize


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str = ""
    synonyms: tuple[str, ...] = ()
    sensitive: bool = False
    disabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
        }
        if self.synonyms:
            data["synonyms"] = list(self.synonyms)
        if self.sensitive:
            data["sensitive"] = True
        if self.disabled:
            data["disabled"] = True
        return data


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    synonyms: tuple[str, ...] = ()
    primary_key: tuple[str, ...] = ()
    columns: tuple[Column, ...] = ()
    required_filters: tuple[dict[str, str], ...] = ()
    disabled: bool = False

    def column(self, name: str) -> Column | None:
        target = (name or "").strip().lower()
        for col in self.columns:
            if col.name.lower() == target:
                return col
        return None

    def to_dict(self, include_disabled: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "primary_key": list(self.primary_key),
            "columns": [c.to_dict() for c in self.columns],
        }
        if self.synonyms:
            data["synonyms"] = list(self.synonyms)
        if self.required_filters:
            data["required_filters"] = [dict(f) for f in self.required_filters]
        if self.disabled:
            data["disabled"] = True
        if not include_disabled:
            data["columns"] = [c.to_dict() for c in self.columns if not c.disabled]
        return data


@dataclass
class TableHit:
    table: Table
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.table.name,
            "description": self.table.description,
            "score": round(self.score, 4),
            "columns": [c.name for c in self.table.columns if not c.disabled],
        }


@dataclass
class Catalog:
    domain: str
    dialect: str
    as_of_date: str
    tables: dict[str, Table]
    relationships: list[dict[str, str]] = field(default_factory=list)
    business_rules: list[str] = field(default_factory=list)

    @classmethod
    def from_path(cls, path: str | Path) -> "Catalog":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        tables: dict[str, Table] = {}
        for raw in payload.get("tables", []):
            columns = tuple(
                Column(
                    name=str(col["name"]),
                    type=str(col.get("type", "")),
                    description=str(col.get("description", "")),
                    synonyms=tuple(col.get("synonyms", ()) or ()),
                    sensitive=bool(col.get("sensitive", False)),
                    disabled=bool(col.get("disabled", False)),
                )
                for col in raw.get("columns", [])
            )
            table = Table(
                name=str(raw["name"]),
                description=str(raw.get("description", "")),
                synonyms=tuple(raw.get("synonyms", ()) or ()),
                primary_key=tuple(
                    part.strip()
                    for part in str(raw.get("primary_key", "")).split(",")
                    if part.strip()
                ),
                columns=columns,
                required_filters=tuple(raw.get("required_filters", ()) or ()),
                disabled=bool(raw.get("disabled", False)),
            )
            tables[table.name.lower()] = table
        return cls(
            domain=str(payload.get("domain", "")),
            dialect=str(payload.get("dialect", "sqlite")),
            as_of_date=str(payload.get("as_of_date", "")),
            tables=tables,
            relationships=list(payload.get("relationships", [])),
            business_rules=list(payload.get("business_rules", [])),
        )

    def table(self, name: str) -> Table | None:
        cleaned = (name or "").strip().strip('"[]`').lower()
        if "." in cleaned:
            cleaned = cleaned.split(".")[-1]
        return self.tables.get(cleaned)

    def enabled_tables(self) -> list[Table]:
        return [t for t in self.tables.values() if not t.disabled]

    def enabled_names(self) -> list[str]:
        return [t.name for t in self.enabled_tables()]

    def schema_hash(self) -> str:
        parts: list[str] = []
        for name in sorted(self.tables):
            table = self.tables[name]
            parts.append(name)
            parts.append(str(table.disabled))
            parts.extend(sorted(col.name.lower() for col in table.columns))
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def search_tables(self, query: str, top_k: int = 5) -> list[TableHit]:
        docs: list[list[str]] = []
        for table in self.enabled_tables():
            tokens: list[str] = []
            tokens.extend(tokenize(table.name) * 3)
            for syn in table.synonyms:
                tokens.extend(tokenize(syn) * 3)
            tokens.extend(tokenize(table.description) * 2)
            for col in table.columns:
                if col.disabled:
                    continue
                tokens.extend(tokenize(col.name))
                tokens.extend(tokenize(col.description))
                for syn in col.synonyms:
                    tokens.extend(tokenize(syn))
            docs.append(tokens)

        enabled = self.enabled_tables()
        bm25 = BM25(docs)
        scores = bm25.scores(tokenize(query))
        lowered = (query or "").lower()
        boosted: list[float] = []
        for table, score in zip(enabled, scores):
            bonus = 0.0
            for label in (table.name, *table.synonyms):
                if label and label.lower() in lowered:
                    bonus += 2.5
            boosted.append(score + bonus)
        ranked = sorted(
            zip(enabled, boosted),
            key=lambda pair: (-pair[1], pair[0].name),
        )
        if top_k <= 0:
            return []
        return [TableHit(table=t, score=s) for t, s in ranked[:top_k] if s > 0] or [
            TableHit(table=t, score=s) for t, s in ranked[: min(top_k, len(ranked))]
        ]

    def describe_tables(self, names: Iterable[str]) -> dict[str, Any]:
        selected: list[Table] = []
        missing: list[str] = []
        for name in names:
            table = self.table(name)
            if table is None or table.disabled:
                missing.append(str(name))
            else:
                selected.append(table)
        selected_names = {t.name for t in selected}
        relationships = [
            rel
            for rel in self.relationships
            if rel.get("left", "").split(".")[0] in selected_names
            or rel.get("right", "").split(".")[0] in selected_names
        ]
        return {
            "tables": [t.to_dict(include_disabled=False) for t in selected],
            "relationships": relationships,
            "business_rules": self.business_rules,
            "missing": missing,
        }

    def relationships_for(self, table_names: Iterable[str]) -> list[dict[str, str]]:
        names = {str(n).lower() for n in table_names}
        return [
            rel
            for rel in self.relationships
            if rel.get("left", "").split(".")[0].lower() in names
            or rel.get("right", "").split(".")[0].lower() in names
        ]
