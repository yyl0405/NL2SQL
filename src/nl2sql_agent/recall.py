"""Experience retrieval with an optional semantic channel and shape-aware selection.

The store itself stays what it always was — SQLite, lexical scoring, no dependency on
any embedding provider. This module adds the two things the lexical path cannot do:

* **a second ranking** over the same verified rows, by embedding similarity between the
  current question and each stored question, fused with reciprocal rank fusion so the
  two channels do not have to share a scale;
* **a selection rule** instead of a plain top-k, because three near-identical phrasings
  of the same question carry the information of one, and the few-shot budget is three.

Retrieval scope never widens here: only `status = 'verified'` rows for the current
catalog hash are candidates, so `candidate`, `gold` and `deprecated` keep exactly the
visibility they have in the store. Gold is a rank prior, not extra visibility.

Nothing in this module reads the counterexample table. A wrong SQL is kept out of the
model's context by construction, and the test for that is on the prompt builder.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .embeddings import DocumentIndex, EmbeddingError, EmbeddingProvider, cosine
from .memory import ExperienceStore
from .text import mmr_select, token_union

#: `lexical` is today's behaviour; `vector` replaces the ranking; `rrf` fuses both.
RECALL_MODES = ("lexical", "vector", "rrf")

#: RRF constant: 60 keeps a rank-1 agreement worth roughly a rank-10 gap, the same
#: trade the table-linking channel settled on.
DEFAULT_RRF_K = 60


@dataclass
class RecallResult:
    """What one retrieval call did — the instrument and the log both read this."""

    items: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "lexical"
    candidates: int = 0
    displaced_by_diversity: int = 0
    degraded: bool = False
    degraded_reason: str = ""
    provider: str = ""
    embedding: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "mode": self.mode,
            "candidates": self.candidates,
            "selected": len(self.items),
            "displaced_by_diversity": self.displaced_by_diversity,
        }
        if self.provider:
            data["provider"] = self.provider
        if self.degraded:
            data["degraded"] = True
            if self.degraded_reason:
                data["degraded_reason"] = self.degraded_reason
        if self.embedding:
            data["embedding"] = self.embedding
        return data


def _documents_key(documents: Sequence[str]) -> str:
    joined = "\x00".join(documents)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _unit_scale(values: Sequence[float]) -> list[float]:
    """Min-max scale so the MMR relevance term and the 0..1 redundancy term are comparable.

    Cosine from a real embedding model can be negative; dividing by the maximum alone
    would flip the order when every score is below zero.
    """
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low <= 0:
        return [0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _fuse(vector: Sequence[float], other: Sequence[float], rrf_k: int) -> list[float]:
    """Reciprocal rank fusion of two score lists, returned index-aligned.

    Rank-based on purpose: the lexical score is a weighted Jaccard/edit-distance blend
    and the vector score is a cosine, and those numbers do not mean the same thing even
    when both sit between 0 and 1.
    """
    fused = [0.0] * len(vector)
    for scores in (vector, other):
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
        for position, index in enumerate(order, 1):
            fused[index] += 1.0 / (rrf_k + position)
    return fused


class ExperienceRecall:
    def __init__(
        self,
        store: ExperienceStore,
        *,
        provider: EmbeddingProvider | None = None,
        cache_dir: Path | str | None = None,
        mode: str = "rrf",
        diversity: float = 0.0,
        gold_boost: float = 0.0,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> None:
        self.store = store
        self.provider = provider
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.mode = mode if mode in RECALL_MODES else "lexical"
        self.diversity = float(diversity)
        self.gold_boost = float(gold_boost)
        self.rrf_k = max(1, int(rrf_k))
        self._index: DocumentIndex | None = None
        self._index_key: tuple[str, str, str] | None = None
        self.last: RecallResult = RecallResult()

    @property
    def effective_mode(self) -> str:
        """The vector channel only exists with a provider; asking for it without one is lexical."""
        return self.mode if self.provider is not None else "lexical"

    def _vector_scores(self, question: str, rows: list[dict[str, Any]], schema_hash: str) -> list[float]:
        documents = [str(row["question"]) for row in rows]
        assert self.provider is not None
        key = (schema_hash, _documents_key(documents), self.provider.name)
        if self._index is None or self._index_key != key:
            # Any change to the library rebuilds (and re-embeds) the whole document set:
            # the cache is keyed by content, so a stale vector can never be reused.
            self._index = DocumentIndex(
                documents,
                schema_hash=schema_hash,
                provider=self.provider,
                cache_dir=self.cache_dir,
            )
            self._index_key = key
        vectors = self._index.vectors()
        query_vector = self.provider.embed([question])[0]
        if len(vectors) != len(rows):
            raise EmbeddingError(f"向量数量 {len(vectors)} 与经验数量 {len(rows)} 不一致")
        return [cosine(query_vector, vector) for vector in vectors]

    def search(
        self,
        question: str,
        *,
        schema_hash: str = "",
        top_k: int = 3,
        min_score: float = 0.3,
        category: str = "",
        category_boost: float = 0.12,
        strict_category: bool = False,
        diversity: float | None = None,
        record: bool = True,
    ) -> list[dict[str, Any]]:
        result = self.rank(
            question,
            schema_hash=schema_hash,
            top_k=top_k,
            min_score=min_score,
            category=category,
            category_boost=category_boost,
            strict_category=strict_category,
            diversity=diversity,
        )
        self.last = result
        if record and result.items:
            self.store.record_hits([int(item["id"]) for item in result.items])
        return result.items

    def rank(
        self,
        question: str,
        *,
        schema_hash: str = "",
        top_k: int = 3,
        min_score: float = 0.3,
        category: str = "",
        category_boost: float = 0.12,
        strict_category: bool = False,
        diversity: float | None = None,
    ) -> RecallResult:
        """Order and select without recording a hit — this is what an instrument calls."""
        weight = self.diversity if diversity is None else float(diversity)
        mode = self.effective_mode
        rows = self.store.score(
            question,
            schema_hash=schema_hash,
            min_score=min_score,
            category=category,
            category_boost=category_boost,
            strict_category=strict_category,
            gold_boost=self.gold_boost,
        )
        if not rows:
            return RecallResult(
                items=[],
                mode=mode,
                candidates=0,
                provider=self.provider.name if self.provider else "",
            )

        lexical = [float(row["score"]) for row in rows]
        values = list(lexical)
        degraded_reason = ""
        if mode in {"vector", "rrf"}:
            try:
                vector = self._vector_scores(question, rows, schema_hash)
            except EmbeddingError as exc:
                vector = None
                degraded_reason = str(exc)
                mode = "lexical"
            if vector is not None:
                values = vector if mode == "vector" else _fuse(lexical, vector, self.rrf_k)

        scaled = _unit_scale(values)
        token_sets = [
            token_union(str(row.get("question") or ""), str(row.get("sql") or "")) for row in rows
        ]
        picks = mmr_select(scaled, token_sets, top_k=max(0, top_k), diversity=weight)
        naive = mmr_select(scaled, token_sets, top_k=max(0, top_k), diversity=0.0)
        items = [
            {
                "id": row["id"],
                "question": row["question"],
                "sql": row["sql"],
                "category": row["category"],
                "quality": row["quality"],
                "score": round(float(values[index]), 4),
            }
            for index, row in ((index, rows[index]) for index in picks)
        ]
        return RecallResult(
            items=items,
            mode=mode,
            candidates=len(rows),
            displaced_by_diversity=len(set(naive) - set(picks)),
            degraded=bool(degraded_reason),
            degraded_reason=degraded_reason,
            provider=self.provider.name if self.provider else "",
            embedding=self._index.stats.to_dict() if self._index and mode != "lexical" else {},
        )


def recall_from_settings(
    store: ExperienceStore,
    settings: Any,
    provider: EmbeddingProvider | None = None,
) -> ExperienceRecall:
    """Build the recall object the runtime uses: the vector channel is opt-in."""
    mode = str(getattr(settings, "experience_recall", "lexical") or "lexical").strip().lower()
    if mode not in RECALL_MODES:
        mode = "lexical"
    use_vector = bool(getattr(settings, "experience_vector", False)) and provider is not None
    if not use_vector:
        mode = "lexical"
    return ExperienceRecall(
        store,
        provider=provider if use_vector else None,
        cache_dir=getattr(settings, "embedding_cache_dir", None),
        mode=mode,
        diversity=float(getattr(settings, "few_shot_diversity", 0.0) or 0.0),
        gold_boost=float(getattr(settings, "experience_gold_boost", 0.0) or 0.0),
    )


__all__ = [
    "DEFAULT_RRF_K",
    "ExperienceRecall",
    "RECALL_MODES",
    "RecallResult",
    "recall_from_settings",
]
