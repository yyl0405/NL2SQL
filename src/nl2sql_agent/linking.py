"""Schema-linking strategies and the index that ranks tables for a query.

``Catalog.search_tables`` used to ship one behaviour: BM25 over table name,
synonyms, description and every column. That is tuned for the 9-table demo
catalog, where column comments are the only signal a short follow-up question
("那华南呢？") can latch on to. On a real ERP catalog with hundreds of tables it
falls apart, because column comments repeat the same generic words across the
whole database, the IDF of the query terms collapses, and BM25's length
normalisation then punishes exactly the tables that have the richest comments.

Each :class:`Strategy` here is a measurement configuration. `nl2sql-agent
link-check --variants` ranks the same cases under every strategy so a winner is
chosen with numbers, and ``strategy="auto"`` switches on catalog size:

  * few tables  -> ``baseline`` (column-heavy, length normalised)
  * many tables -> ``hybrid`` (thinned columns, no length normalisation,
    mined comment synonyms, token-coverage rerank)

Both defaults are pinned by tests: the demo path must keep its recall@5 = 0.965
and the large-catalog path must beat the baseline at every cutoff.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from .embeddings import EmbeddingError, EmbeddingProvider, SchemaIndex
from .text import BM25, tokenize

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, annotations only
    from .catalog import Catalog, Table

#: At or above this many enabled tables, column text stops paying for itself.
LARGE_CATALOG_TABLES = 50


@dataclass(frozen=True)
class Strategy:
    name: str
    include_columns: bool = True
    column_weight: float = 1.0
    name_weight: float = 3.0
    description_weight: float = 2.0
    length_norm: float = 0.75
    mined_synonyms: int = 0
    mined_weight: float = 3.0
    coverage_bonus: float = 0.0

    def describe(self) -> str:
        parts = [f"columns={'on' if self.include_columns else 'off'}"]
        if self.include_columns and self.column_weight != 1.0:
            parts.append(f"col_w={self.column_weight:g}")
        if self.length_norm != 0.75:
            parts.append(f"b={self.length_norm:g}")
        if self.mined_synonyms:
            parts.append(f"mined={self.mined_synonyms}")
        if self.coverage_bonus:
            parts.append(f"coverage={self.coverage_bonus:g}")
        return ", ".join(parts)


STRATEGIES: tuple[Strategy, ...] = (
    Strategy("baseline"),
    Strategy("table_only", include_columns=False),
    Strategy("column_downweight", column_weight=0.35),
    Strategy("no_length_norm", length_norm=0.0),
    Strategy("comment_synonyms", include_columns=False, mined_synonyms=8),
    Strategy("coverage", mined_synonyms=8, coverage_bonus=1.0, column_weight=0.35),
    Strategy("thin_columns_no_norm", column_weight=0.35, length_norm=0.0),
    Strategy("mined_no_columns_no_norm", include_columns=False, mined_synonyms=8, length_norm=0.0),
    Strategy(
        "hybrid",
        column_weight=0.35,
        length_norm=0.0,
        mined_synonyms=8,
        coverage_bonus=1.0,
    ),
)
BY_NAME: dict[str, Strategy] = {strategy.name: strategy for strategy in STRATEGIES}


def strategy_names() -> tuple[str, ...]:
    return tuple(BY_NAME)


#: Retrieval modes that need an embedding provider on top of a BM25 base.
VECTOR_MODES = ("vector", "rrf")


def fusion_mode(name: str | None) -> str:
    key = (name or "auto").strip().lower()
    return key if key in VECTOR_MODES else "bm25"


def variant_names() -> tuple[str, ...]:
    """Everything ``link-check`` can compare: BM25 strategies + vector modes."""
    return tuple(BY_NAME) + VECTOR_MODES


def strategy_by_name(names: Iterable[str] | None) -> tuple[Strategy, ...]:
    wanted = [name.strip() for name in (names or ()) if str(name).strip()]
    if not wanted:
        return (BY_NAME["baseline"],)
    unknown = [name for name in wanted if name not in BY_NAME]
    if unknown:
        raise ValueError(f"未知的 linking 策略: {', '.join(unknown)}（可选 {', '.join(BY_NAME)}）")
    return tuple(BY_NAME[name] for name in wanted)


def resolve_strategy(name: str | None, table_count: int) -> Strategy:
    key = (name or "auto").strip().lower()
    # Vector modes reuse the scale-aware BM25 base and fuse on top of it, so
    # ``auto`` still picks the base with the best measured recall for the size.
    if key in {"auto", *VECTOR_MODES}:
        return BY_NAME["hybrid"] if table_count >= LARGE_CATALOG_TABLES else BY_NAME["baseline"]
    if key not in BY_NAME:
        raise ValueError(f"未知的 linking 策略: {key}（可选 auto, {', '.join(variant_names())}）")
    return BY_NAME[key]


def _tokens(text: str, weight: float) -> list[str]:
    """Repeat a field when it counts for more than one hit, thin it out when less.

    BM25 only saturates on term frequency, so the weight knob is the number of
    copies in the document; a fractional weight strides the token list instead of
    truncating it, so nothing depends on field ordering.
    """
    raw = tokenize(text)
    if weight >= 1:
        return raw * int(round(weight))
    if weight <= 0:
        return []
    stride = max(2, int(round(1.0 / weight)))
    return raw[::stride]


def _mine_table_terms(tables: Sequence["Table"], per_table: int) -> dict[str, list[str]]:
    """Promote the most distinctive column-comment terms of each table to synonyms.

    Distinctiveness is document frequency across tables, so a term shared by a
    third of the database contributes nothing and a term unique to one module
    wins. A single df counter keeps this linear in corpus size.
    """
    counts: list[dict[str, int]] = []
    for table in tables:
        freq: dict[str, int] = {}
        for column in table.columns:
            if column.disabled:
                continue
            for token in tokenize(column.description):
                freq[token] = freq.get(token, 0) + 1
        counts.append(freq)

    df: Counter[str] = Counter()
    for freq in counts:
        df.update(freq.keys())

    total = max(1, len(tables))
    ceiling = max(2, int(total * 0.06))
    mined: dict[str, list[str]] = {}
    for table, freq in zip(tables, counts):
        scored: list[tuple[float, str]] = []
        for token, tf in freq.items():
            if tf < 2 or len(token) < 2 or df[token] > ceiling:
                continue
            scored.append((tf / (df[token] + 1.0), token))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        mined[table.name] = [token for _, token in scored[:per_table]]
    return mined


@dataclass
class RankedTable:
    table: "Table"
    score: float
    sources: tuple[str, ...] = ("bm25",)
    ranks: dict[str, int] = field(default_factory=dict)


class LinkIndex:
    """Pre-tokenized BM25 ranking index for one catalog and one strategy."""

    mode = "bm25"
    embedding = ""
    degraded = False
    degraded_reason = ""
    vector_stats: dict[str, Any] = {}

    def __init__(self, tables: Sequence["Table"], strategy: Strategy) -> None:
        self.tables = list(tables)
        self.strategy = strategy
        mined = _mine_table_terms(self.tables, strategy.mined_synonyms) if strategy.mined_synonyms else {}
        docs: list[list[str]] = []
        token_sets: list[set[str]] = []
        for table in self.tables:
            tokens: list[str] = []
            tokens.extend(_tokens(table.name, strategy.name_weight))
            for synonym in table.synonyms:
                tokens.extend(_tokens(synonym, strategy.name_weight))
            tokens.extend(_tokens(table.description, strategy.description_weight))
            if strategy.include_columns:
                for column in table.columns:
                    if column.disabled:
                        continue
                    tokens.extend(_tokens(column.name, strategy.column_weight))
                    tokens.extend(_tokens(column.description, strategy.column_weight))
                    for synonym in column.synonyms:
                        tokens.extend(_tokens(synonym, strategy.column_weight))
            for term in mined.get(table.name, ()):
                tokens.extend([term] * int(round(strategy.mined_weight)))
            docs.append(tokens)
            token_sets.append(set(tokens))
        self._docs = docs
        self._sets = token_sets
        self._bm25 = BM25(docs, b=strategy.length_norm)

    def rank(self, query: str, top_k: int = 20) -> list[tuple["Table", float]]:
        query_tokens = tokenize(query)
        scores = self._bm25.scores(query_tokens)
        lowered = (query or "").lower()
        wanted = set(query_tokens)
        ranked: list[tuple[float, "Table"]] = []
        for table, doc, score, token_set in zip(self.tables, self._docs, scores, self._sets):
            value = score
            for label in (table.name, *table.synonyms):
                if label and label.lower() in lowered:
                    value += 2.5
            if self.strategy.coverage_bonus and wanted:
                value += self.strategy.coverage_bonus * 4.0 * (len(wanted & token_set) / len(wanted))
            ranked.append((value, table))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [(table, value) for value, table in ranked[: max(0, top_k)]]

    def rank_with_sources(self, query: str, top_k: int = 20) -> list[RankedTable]:
        return [
            RankedTable(table=table, score=score, sources=("bm25",), ranks={"bm25": position})
            for position, (table, score) in enumerate(self.rank(query, top_k), 1)
        ]


class VectorIndex:
    """Cosine ranking over cached table vectors; embeds the query on demand."""

    def __init__(
        self,
        tables: Sequence["Table"],
        vectors: Sequence[Sequence[float]],
        provider: EmbeddingProvider,
    ) -> None:
        if len(tables) != len(vectors):
            raise EmbeddingError(f"向量数量 {len(vectors)} 与表数量 {len(tables)} 不一致")
        self.tables = list(tables)
        self.vectors = [list(vector) for vector in vectors]
        self.provider = provider
        self.query_calls = 0

    @property
    def name(self) -> str:
        return self.provider.name

    def rank(self, query: str, top_k: int = 20) -> list[tuple["Table", float]]:
        text = (query or "").strip()
        if not text or top_k <= 0:
            return []
        self.query_calls += 1
        query_vector = self.provider.embed([text])[0]
        if len(query_vector) != len(self.vectors[0] if self.vectors else []):
            raise EmbeddingError(
                f"查询向量维度 {len(query_vector)} 与目录向量维度 "
                f"{len(self.vectors[0]) if self.vectors else 0} 不一致"
            )
        scored = [
            (table, _cosine(query_vector, vector))
            for table, vector in zip(self.tables, self.vectors)
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0].name))
        return scored[:top_k]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def reciprocal_rank_fusion(
    rankings: Sequence[tuple[str, Sequence["Table"]]], k: int = 60
) -> list[RankedTable]:
    """Reciprocal rank fusion: ``sum(1 / (k + rank))`` across channels.

    Rank-based fusion sidesteps the fact that BM25 scores and cosine
    similarities live on incomparable scales.
    """
    k = max(1, int(k))
    scores: dict[str, float] = {}
    tables: dict[str, "Table"] = {}
    per_channel: dict[str, dict[str, int]] = {}
    for label, items in rankings:
        for position, table in enumerate(items, 1):
            scores[table.name] = scores.get(table.name, 0.0) + 1.0 / (k + position)
            tables.setdefault(table.name, table)
            per_channel.setdefault(table.name, {})[label] = position
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    return [
        RankedTable(
            table=tables[name],
            score=score,
            sources=tuple(per_channel[name]),
            ranks=per_channel[name],
        )
        for name, score in ordered
    ]


class FusedLinkIndex:
    """BM25 base plus an optional vector channel, fused or vector-only."""

    def __init__(
        self,
        bm25: LinkIndex,
        vector: VectorIndex | None,
        mode: str = "rrf",
        rrf_k: int = 60,
        initial_error: str = "",
    ) -> None:
        self.bm25 = bm25
        self.vector = vector
        self.mode = mode
        self.rrf_k = rrf_k
        self.tables = bm25.tables
        self.strategy = bm25.strategy
        self.degraded = bool(initial_error) or vector is None
        self.degraded_reason = initial_error
        self.vector_stats: dict[str, Any] = {}

    @property
    def embedding(self) -> str:
        return self.vector.name if self.vector else ""

    def rank_with_sources(self, query: str, top_k: int = 20) -> list[RankedTable]:
        depth = max(top_k, 20)
        vector_ranked: list[tuple["Table", float]] = []
        if self.vector is not None:
            try:
                vector_ranked = self.vector.rank(query, depth)
            except EmbeddingError as exc:
                # One failed query embedding must not take down lexical search.
                self.degraded = True
                self.degraded_reason = str(exc)
        bm25_ranked = self.bm25.rank(query, depth)
        if not vector_ranked:
            return [
                RankedTable(table=table, score=score, sources=("bm25",), ranks={"bm25": position})
                for position, (table, score) in enumerate(bm25_ranked, 1)
            ][:top_k]
        if self.mode == "vector":
            return [
                RankedTable(
                    table=table, score=score, sources=("vector",), ranks={"vector": position}
                )
                for position, (table, score) in enumerate(vector_ranked, 1)
            ][:top_k]
        fused = reciprocal_rank_fusion(
            [
                ("bm25", [table for table, _ in bm25_ranked]),
                ("vector", [table for table, _ in vector_ranked]),
            ],
            self.rrf_k,
        )
        return fused[:top_k]

    def rank(self, query: str, top_k: int = 20) -> list[tuple["Table", float]]:
        return [(item.table, item.score) for item in self.rank_with_sources(query, top_k)]


def build_index(
    catalog: "Catalog",
    strategy: Strategy,
    mode: str = "bm25",
    embedder: EmbeddingProvider | None = None,
    cache_dir: Any = None,
    rrf_k: int = 60,
) -> LinkIndex | FusedLinkIndex:
    bm25 = LinkIndex(catalog.enabled_tables(), strategy)
    if mode == "bm25":
        return bm25
    if embedder is None:
        raise ValueError(
            "向量检索需要 embedding provider：设置 NL2SQL_EMBEDDING=hash|openai，"
            "或把 NL2SQL_LINKING 改回 auto/BM25 策略。"
        )
    try:
        schema_index = SchemaIndex(catalog, embedder, cache_dir)
        vectors = schema_index.vectors()
        fused = FusedLinkIndex(bm25, VectorIndex(bm25.tables, vectors, embedder), mode, rrf_k)
        fused.vector_stats = schema_index.stats.to_dict()
        return fused
    except EmbeddingError as exc:
        # Index construction is the one place where a provider outage can be
        # reported before any query runs; degrade instead of failing the agent.
        return FusedLinkIndex(bm25, None, mode, rrf_k, initial_error=str(exc))


def build_named_index(
    catalog: "Catalog",
    name: str,
    embedder: EmbeddingProvider | None = None,
    cache_dir: Any = None,
    rrf_k: int = 60,
) -> LinkIndex | FusedLinkIndex:
    """Build an index from a CLI variant name (BM25 strategy or vector mode)."""
    key = (name or "auto").strip().lower()
    mode = fusion_mode(key)
    strategy = resolve_strategy(key, len(catalog.enabled_tables()))
    return build_index(catalog, strategy, mode=mode, embedder=embedder, cache_dir=cache_dir, rrf_k=rrf_k)
