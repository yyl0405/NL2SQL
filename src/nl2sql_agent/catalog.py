"""Schema catalog and dependency-free lexical schema linking."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .linking import FusedLinkIndex, LinkIndex, build_index, fusion_mode, resolve_strategy


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
    default_filters: tuple[dict[str, str], ...] = ()
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
        if self.default_filters:
            data["default_filters"] = [dict(f) for f in self.default_filters]
        if self.disabled:
            data["disabled"] = True
        if not include_disabled:
            data["columns"] = [c.to_dict() for c in self.columns if not c.disabled]
        return data


@dataclass
class TableHit:
    table: Table
    score: float
    sources: tuple[str, ...] = ()
    ranks: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.table.name,
            "description": self.table.description,
            "score": round(self.score, 4),
            "columns": [c.name for c in self.table.columns if not c.disabled],
        }
        if self.sources:
            data["sources"] = list(self.sources)
        if self.ranks:
            data["ranks"] = dict(self.ranks)
        return data


@dataclass
class SearchResult:
    """Hits plus the retrieval configuration that produced them."""

    hits: list[TableHit]
    mode: str = "bm25"
    embedding: str = ""
    degraded: bool = False
    degraded_reason: str = ""
    reranked: bool = False
    rerank_error: str = ""
    vector_stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        retrieval: dict[str, Any] = {
            "mode": self.mode,
            "embedding": self.embedding or "none",
            "degraded": self.degraded,
            "reranked": self.reranked,
        }
        if self.degraded_reason:
            retrieval["degraded_reason"] = self.degraded_reason
        if self.rerank_error:
            retrieval["rerank_error"] = self.rerank_error
        if self.vector_stats:
            retrieval["vector"] = dict(self.vector_stats)
        return {
            "count": len(self.hits),
            "tables": [hit.to_dict() for hit in self.hits],
            "retrieval": retrieval,
        }


@dataclass
class Catalog:
    domain: str
    dialect: str
    as_of_date: str
    tables: dict[str, Table]
    relationships: list[dict[str, str]] = field(default_factory=list)
    business_rules: list[str] = field(default_factory=list)
    linking: str = "auto"
    embedder: Any = field(default=None, repr=False, compare=False)
    embedding_cache: Path | None = field(default=None, repr=False, compare=False)
    reranker: Any = field(default=None, repr=False, compare=False)
    rerank_candidates: int = 12
    rrf_k: int = 60
    _link_index: Any = field(default=None, repr=False, compare=False)
    _link_key: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        linking: str = "auto",
        embedder: Any = None,
        embedding_cache: Path | str | None = None,
        reranker: Any = None,
        rerank_candidates: int = 12,
        rrf_k: int = 60,
    ) -> "Catalog":
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
                default_filters=tuple(raw.get("default_filters", ()) or ()),
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
            linking=linking,
            embedder=embedder,
            embedding_cache=Path(embedding_cache) if embedding_cache else None,
            reranker=reranker,
            rerank_candidates=rerank_candidates,
            rrf_k=rrf_k,
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

    def link_index(self) -> LinkIndex | FusedLinkIndex:
        tables = self.enabled_tables()
        strategy = resolve_strategy(self.linking, len(tables))
        mode = fusion_mode(self.linking)
        provider = getattr(self.embedder, "name", "") if self.embedder is not None else ""
        key = (strategy.name, mode, provider, str(self.embedding_cache or ""), self.rrf_k)
        if self._link_index is None or self._link_key != key:
            self._link_key = key
            self._link_index = build_index(
                self,
                strategy,
                mode=mode,
                embedder=self.embedder,
                cache_dir=self.embedding_cache,
                rrf_k=self.rrf_k,
            )
        return self._link_index

    def table_documents(self) -> list[tuple[Table, str]]:
        """One embedding document per enabled table.

        Disabled and sensitive columns are left out: schema text is sent to an
        external embedding endpoint, and value-level fields have no business
        being part of a retrieval document. Questions about them should be
        refused by the guard, not retrieved by a vector.
        """
        documents: list[tuple[Table, str]] = []
        for table in self.enabled_tables():
            parts: list[str] = [table.name, table.description]
            parts.extend(table.synonyms)
            for column in table.columns:
                if column.disabled or column.sensitive:
                    continue
                parts.append(column.name)
                parts.append(column.description)
                parts.extend(column.synonyms)
            text = " ".join(part.strip() for part in parts if part and part.strip())
            documents.append((table, text))
        return documents

    def schema_hash(self) -> str:
        parts: list[str] = []
        for name in sorted(self.tables):
            table = self.tables[name]
            parts.append(name)
            parts.append(str(table.disabled))
            parts.extend(sorted(col.name.lower() for col in table.columns))
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def search(self, query: str, top_k: int = 5) -> SearchResult:
        index = self.link_index()
        if top_k <= 0:
            return SearchResult(hits=[], mode=index.mode, embedding=index.embedding)
        depth = max(top_k, self.rerank_candidates) if self.reranker is not None else top_k
        ranked = index.rank_with_sources(query, depth)
        hits = [
            TableHit(table=item.table, score=item.score, sources=item.sources, ranks=item.ranks)
            for item in ranked
            if item.score > 0
        ]
        # a question that matches nothing still gets candidates: the agent needs
        # something to inspect before it can decide the catalog has no answer
        if not hits:
            hits = [
                TableHit(table=item.table, score=item.score, sources=item.sources, ranks=item.ranks)
                for item in ranked
            ]

        reranked = False
        rerank_error = ""
        if self.reranker is not None and len(hits) > top_k:
            try:
                hits = self.reranker.rerank(query, hits, top_k)
                reranked = True
            except Exception as exc:  # noqa: BLE001 - optional stage must never break search
                rerank_error = str(exc)
        return SearchResult(
            hits=hits[:top_k] if top_k > 0 else hits,
            mode=index.mode,
            embedding=index.embedding,
            degraded=index.degraded,
            degraded_reason=index.degraded_reason,
            reranked=reranked,
            rerank_error=rerank_error,
            vector_stats=dict(index.vector_stats or {}),
        )

    def search_tables(self, query: str, top_k: int = 5) -> list[TableHit]:
        return self.search(query, top_k=top_k).hits

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
