"""Embedding providers and a schema-hash keyed vector cache for linking.

The vector channel is an optional add-on: the core package still has no
third-party dependencies and ``NL2SQL_EMBEDDING=none`` keeps the BM25 path
untouched. Two providers ship here:

* :class:`HashEmbedding` is deterministic and offline. It hashes CJK/English
  tokens into a fixed number of buckets, so it measures the *pipeline*
  (cache, fusion, degradation) without pretending to be a semantic model.
* :class:`OpenAIEmbedding` calls any OpenAI-compatible ``/embeddings`` endpoint
  in small batches. Only schema text is sent, never rows or values.

:class:`SchemaIndex` caches one vector per enabled table under the catalog
``schema_hash`` plus a document fingerprint. Renaming a table, adding a column,
editing a comment or switching models all produce a new cache file instead of
silently reusing stale vectors.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence, Protocol

from .text import tokenize

if TYPE_CHECKING:  # pragma: no cover - annotations only, avoids an import cycle
    from .catalog import Catalog


class EmbeddingError(RuntimeError):
    pass


class EmbeddingProvider(Protocol):
    name: str
    calls: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = sum(value * value for value in vector) ** 0.5
    if not norm:
        return vector
    return [value / norm for value in vector]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, 0.0 when either side is the zero vector.

    A zero vector is what a hashed embedding returns for text with no tokens, and
    treating it as "dissimilar to everything" is the only reading that keeps an
    empty example from ranking above a real match.
    """
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)



@dataclass
class HashEmbedding:
    """Offline stand-in: hashed token counts, L2-normalised.

    This is deliberately lexical, so it can never be cited as evidence that
    semantic recall works. It exists so the cache, fusion and degradation paths
    are testable (and demo-able) without a network or a key.
    """

    dimensions: int = 256
    name: str = field(init=False)
    calls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.name = f"hash:{self.dimensions}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in tokenize(text):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                vector[index] += 1.0 if digest[4] & 1 else -1.0
            vectors.append(_l2_normalize(vector))
        return vectors


@dataclass
class OpenAIEmbedding:
    """OpenAI-compatible embeddings with small batches and explicit errors."""

    model: str
    api_key: str = field(repr=False)
    base_url: str = ""
    timeout: float = 120.0
    batch_size: int = 10
    client: object = field(default=None, repr=False)
    name: str = field(init=False)
    calls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.name = f"openai:{self.model}"
        if self.client is None:
            if not self.api_key:
                raise EmbeddingError(
                    "NL2SQL_EMBEDDING=openai 但未配置 Key。请设置 NL2SQL_EMBEDDING_API_KEY "
                    "（或 NL2SQL_API_KEY），或改用 NL2SQL_EMBEDDING=hash 离线运行。"
                )
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise EmbeddingError("缺少 openai 依赖，请执行: pip install -e '.[llm]'") from exc
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url or None,
                timeout=self.timeout,
            )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        step = max(1, int(self.batch_size))
        for start in range(0, len(texts), step):
            batch = list(texts[start : start + step])
            if not batch:
                continue
            self.calls += 1
            try:
                response = self.client.embeddings.create(model=self.model, input=batch)
            except Exception as exc:  # noqa: BLE001 - provider errors are opaque
                raise EmbeddingError(f"embedding 调用失败（{self.model}）: {exc}") from exc
            items = sorted(
                response.data, key=lambda item: getattr(item, "index", 0)
            )
            if len(items) != len(batch):
                raise EmbeddingError(
                    f"embedding 返回数量不匹配：请求 {len(batch)} 条，返回 {len(items)} 条"
                )
            vectors.extend([list(item.embedding) for item in items])
        return vectors


def build_embedding_provider(settings) -> EmbeddingProvider | None:
    """Provider factory; ``none`` (the default) keeps linking purely lexical."""
    choice = (getattr(settings, "embedding_provider", "none") or "none").strip().lower()
    if choice in {"", "none", "off"}:
        return None
    if choice == "hash":
        return HashEmbedding(dimensions=max(8, int(settings.embedding_dimensions)))
    if choice == "openai":
        return OpenAIEmbedding(
            model=settings.embedding_model,
            api_key=(settings.embedding_api_key or settings.api_key).strip(),
            base_url=(settings.embedding_base_url or settings.base_url).strip(),
            timeout=settings.request_timeout_sec,
            batch_size=settings.embedding_batch_size,
        )
    raise ValueError(f"未知 NL2SQL_EMBEDDING: {choice}，可选 none / hash / openai")


@dataclass
class EmbeddingStats:
    texts: int = 0
    calls: int = 0
    cache_hit: bool = False
    elapsed_ms: int = 0
    cache_path: str = ""
    cache_error: str = ""

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "texts": self.texts,
            "calls": self.calls,
            "cache_hit": self.cache_hit,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.cache_path:
            data["cache"] = Path(self.cache_path).name
        if self.cache_error:
            data["cache_error"] = self.cache_error
        return data


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in value.lower())
    return "-".join(part for part in cleaned.split("-") if part)[:48] or "embedding"


def _documents_hash(texts: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for text in texts:
        digest.update(text.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


class DocumentIndex:
    """Cache one vector per document under a scope hash plus a document fingerprint.

    Subclasses decide what a document is — catalog tables, verified experience
    questions. Renaming a table, adding a column, editing a comment, adding an
    example or switching models all produce a new cache file instead of silently
    reusing stale vectors, because a vector that no longer matches its text is
    worse than no vector: it still returns a confident ranking.
    """

    VERSION = 1

    def __init__(
        self,
        documents: Sequence[str],
        *,
        schema_hash: str,
        provider: EmbeddingProvider,
        cache_dir: Path | str | None = None,
    ) -> None:
        self.documents = list(documents)
        self.schema_hash = str(schema_hash or "")
        self.provider = provider
        self.docs_hash = _documents_hash(self.documents)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.stats = EmbeddingStats(texts=len(self.documents))

    @property
    def cache_path(self) -> Path | None:
        if self.cache_dir is None:
            return None
        name = f"{self.schema_hash or 'noschema'}-{_slug(self.provider.name)}-{self.docs_hash}.json"
        return self.cache_dir / name

    def vectors(self) -> list[list[float]]:
        started = time.perf_counter()
        cached = self._load()
        if cached is not None:
            self.stats.cache_hit = True
            self.stats.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return cached
        calls_before = int(getattr(self.provider, "calls", 0))
        vectors = self.provider.embed(self.documents)
        self.stats.calls = int(getattr(self.provider, "calls", 0)) - calls_before
        self._validate(vectors, source=self.provider.name)
        self.stats.elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._store(vectors)
        return vectors

    def _validate(self, vectors: Sequence[Sequence[float]], source: str) -> None:
        if len(vectors) != len(self.documents):
            raise EmbeddingError(
                f"{source} 返回 {len(vectors)} 条向量，文档需要 {len(self.documents)} 条"
            )
        if vectors and not vectors[0]:
            raise EmbeddingError(f"{source} 返回了空向量")
        width = len(vectors[0]) if vectors else 0
        if any(len(vector) != width for vector in vectors):
            raise EmbeddingError(f"{source} 返回的向量维度不一致")

    def _load(self) -> list[list[float]] | None:
        path = self.cache_path
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("version") != self.VERSION
                or payload.get("provider") != self.provider.name
                or payload.get("schema_hash") != self.schema_hash
                or payload.get("docs_hash") != self.docs_hash
                or payload.get("documents") != len(self.documents)
            ):
                return None
            vectors = payload.get("vectors")
            if not isinstance(vectors, list):
                return None
            self._validate(vectors, source="缓存")
            self.stats.cache_path = str(path)
            return [list(map(float, vector)) for vector in vectors]
        except (OSError, ValueError, TypeError):
            # A truncated or hand-edited cache is not worth failing a query over.
            return None

    def _store(self, vectors: Sequence[Sequence[float]]) -> None:
        path = self.cache_path
        if path is None:
            return
        payload = {
            "version": self.VERSION,
            "provider": self.provider.name,
            "schema_hash": self.schema_hash,
            "docs_hash": self.docs_hash,
            "documents": len(self.documents),
            "dimensions": len(vectors[0]) if vectors else 0,
            "vectors": [[round(value, 6) for value in vector] for vector in vectors],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, path)
            self.stats.cache_path = str(path)
        except OSError as exc:
            self.stats.cache_error = str(exc)


class SchemaIndex(DocumentIndex):
    """Vector cache aligned with ``catalog.enabled_tables()``."""

    def __init__(
        self,
        catalog: "Catalog",
        provider: EmbeddingProvider,
        cache_dir: Path | str | None = None,
    ) -> None:
        documents = catalog.table_documents()
        super().__init__(
            [text for _, text in documents],
            schema_hash=catalog.schema_hash(),
            provider=provider,
            cache_dir=cache_dir,
        )
        self.names = [table.name for table, _ in documents]

