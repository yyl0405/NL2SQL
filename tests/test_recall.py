"""Recall channels: legacy-exact lexical default, diversity selection, vector fusion.

Two things must hold at once. The default configuration has to reproduce the pre-P1-4
ordering exactly, because every published number was measured with it. And a vector
channel must never widen what is retrievable: `candidate` and `deprecated` rows stay
invisible no matter how good their embedding looks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nl2sql_agent.config import EXPERIENCE_RECALL_MODES, Settings
from nl2sql_agent.embeddings import EmbeddingError, HashEmbedding
from nl2sql_agent.memory import ExperienceStore
from nl2sql_agent.recall import (
    RECALL_MODES,
    ExperienceRecall,
    RecallResult,
    _unit_scale,
    recall_from_settings,
)

AMOUNT = "2026年6月华东地区销售订单总金额是多少？"
AMOUNT_SQL = "SELECT SUM(qty) AS total FROM order_item"
AMOUNT_NEAR = "2026年6月华东地区销售订单总金额是多少啊？"
AMOUNT_PARA = "六月华东区域订单金额合计是多少？"
INVENTORY = "各仓库的物料库存数量是多少？"
INVENTORY_SQL = "SELECT warehouse_id, SUM(qty) AS total FROM inventory GROUP BY warehouse_id"
QUERY = "2026年6月华东地区销售订单金额"


def unit(cosine: float) -> list[float]:
    """A 2-D vector whose cosine with [1, 0] is exactly ``cosine``."""
    return [cosine, (1.0 - cosine * cosine) ** 0.5]


class StaticProvider:
    """Deterministic non-lexical signal: the vector says what the tokens do not."""

    name = "static:test"

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [self.mapping[text] for text in texts]


class FailingProvider:
    name = "failing:test"
    calls = 0

    def embed(self, texts):
        raise EmbeddingError("provider 不可用")


class ShortProvider:
    name = "short:test"
    calls = 0

    def embed(self, texts):
        return [[0.0, 1.0] for _ in texts[:-1]]


@pytest.fixture()
def store(tmp_path: Path) -> ExperienceStore:
    store = ExperienceStore(tmp_path / "experience.db")
    store.add(AMOUNT, AMOUNT_SQL, schema_hash="h1", verified=True)
    store.add(AMOUNT_NEAR, AMOUNT_SQL, schema_hash="h1", verified=True)
    store.add(AMOUNT_PARA, AMOUNT_SQL, schema_hash="h1", verified=True)
    store.add(INVENTORY, INVENTORY_SQL, schema_hash="h1", verified=True)
    return store


def _recall(store: ExperienceStore, **kwargs) -> ExperienceRecall:
    kwargs.setdefault("mode", "lexical")
    kwargs.setdefault("diversity", 0.0)
    return ExperienceRecall(store, cache_dir=kwargs.pop("cache_dir", None), **kwargs)


def test_config_and_recall_agree_on_mode_names() -> None:
    assert EXPERIENCE_RECALL_MODES == RECALL_MODES


def test_diversity_zero_reproduces_the_legacy_order(store: ExperienceStore) -> None:
    legacy = store.search(QUERY, schema_hash="h1", top_k=2, category="metric", record=False)
    recall = _recall(store, mode="lexical", diversity=0.0)
    assert [row["id"] for row in recall.search(QUERY, schema_hash="h1", top_k=2, category="metric")] == [
        row["id"] for row in legacy
    ]
    assert recall.last.displaced_by_diversity == 0
    assert recall.last.mode == "lexical"
    assert "provider" not in recall.last.to_dict()


def test_diversity_trades_the_near_duplicate_for_a_different_shape(tmp_path: Path) -> None:
    store = ExperienceStore(tmp_path / "experience.db")
    first = store.add(AMOUNT, AMOUNT_SQL, schema_hash="h1", verified=True)
    near = store.add(AMOUNT_NEAR, AMOUNT_SQL, schema_hash="h1", verified=True)
    other = store.add(INVENTORY, INVENTORY_SQL, schema_hash="h1", verified=True)

    plain = _recall(store, diversity=0.0).search(
        QUERY, schema_hash="h1", top_k=2, min_score=0.0
    )
    assert {int(row["id"]) for row in plain} == {int(first["id"]), int(near["id"])}

    result = _recall(store, diversity=0.6).rank(QUERY, schema_hash="h1", top_k=2, min_score=0.0)
    ids = {int(row["id"]) for row in result.items}
    assert int(first["id"]) in ids
    assert int(near["id"]) not in ids
    assert int(other["id"]) in ids
    assert result.displaced_by_diversity == 1
    # Same three rows, same scores, different shape rule.
    assert {int(row["id"]) for row in plain} != ids


def test_retrieval_scope_is_identical_in_every_mode(tmp_path: Path) -> None:
    store = ExperienceStore(tmp_path / "experience.db")
    store.add(AMOUNT, AMOUNT_SQL, schema_hash="h1")  # candidate
    hidden = store.add(INVENTORY, INVENTORY_SQL, schema_hash="h1", verified=True)
    store.deprecate(int(hidden["id"]))
    for mode in RECALL_MODES:
        recall = ExperienceRecall(
            store,
            provider=HashEmbedding(),
            cache_dir=tmp_path,
            mode=mode,
            diversity=0.5,
            gold_boost=0.2,
        )
        assert recall.search(AMOUNT, schema_hash="h1", top_k=3) == []


def test_vector_mode_embeds_documents_and_reuses_the_cache(store: ExperienceStore, tmp_path: Path) -> None:
    provider = HashEmbedding(dimensions=64)
    recall = ExperienceRecall(store, provider=provider, cache_dir=tmp_path, mode="vector")
    first = recall.search(QUERY, schema_hash="h1", top_k=2)
    assert first and recall.last.mode == "vector"
    assert recall.last.provider == provider.name
    assert recall.last.embedding["calls"] == 1
    assert recall.last.embedding["cache_hit"] is False

    again = ExperienceRecall(store, provider=provider, cache_dir=tmp_path, mode="vector")
    assert [row["id"] for row in again.search(QUERY, schema_hash="h1", top_k=2)] == [row["id"] for row in first]
    assert again.last.embedding["cache_hit"] is True
    assert again.last.embedding["calls"] == 0  # documents unchanged: nothing re-embedded

    cached = list(Path(tmp_path).glob("h1-hash-64-*.json"))
    assert cached, list(Path(tmp_path).iterdir())

    store.add("订单状态分布", "SELECT status FROM sales_order GROUP BY status", schema_hash="h1", verified=True)
    changed = ExperienceRecall(store, provider=provider, cache_dir=tmp_path, mode="vector")
    # The cache is keyed by the candidate set, so a new row only costs re-embedding once
    # it is actually above the score threshold and joins the documents.
    changed.search(QUERY, schema_hash="h1", top_k=5, min_score=0.0)
    assert changed.last.embedding["cache_hit"] is False
    assert changed.last.embedding["calls"] == 1  # a new library means new vectors, never stale ones


def test_vector_channel_degrades_to_lexical_instead_of_failing(store: ExperienceStore, tmp_path: Path) -> None:
    recall = ExperienceRecall(store, provider=FailingProvider(), cache_dir=tmp_path, mode="rrf")
    items = recall.search(QUERY, schema_hash="h1", top_k=2)
    assert items
    assert recall.last.mode == "lexical"
    assert recall.last.degraded is True
    assert "provider 不可用" in recall.last.degraded_reason

    broken = ExperienceRecall(store, provider=ShortProvider(), cache_dir=tmp_path, mode="vector")
    broken.search(QUERY, schema_hash="h1", top_k=2)
    assert broken.last.degraded is True


def test_rrf_fuses_rank_positions_not_raw_scores(store: ExperienceStore, tmp_path: Path) -> None:
    # Lexical: amount > near > para > inventory. The vectors disagree at the top: they
    # like `near` best and bury the lexical champion last.
    provider = StaticProvider(
        {
            QUERY: [1.0, 0.0],
            AMOUNT: unit(0.1),
            AMOUNT_NEAR: unit(0.99),
            AMOUNT_PARA: unit(0.8),
            INVENTORY: unit(0.5),
        }
    )
    lexical = _recall(store).rank(QUERY, schema_hash="h1", top_k=4, min_score=0.0)
    vector = ExperienceRecall(store, provider=provider, cache_dir=tmp_path, mode="vector")
    assert [row["question"] for row in lexical.items] == [AMOUNT, AMOUNT_NEAR, AMOUNT_PARA, INVENTORY]
    assert [row["question"] for row in vector.rank(QUERY, schema_hash="h1", top_k=4, min_score=0.0).items] == [
        AMOUNT_NEAR,
        AMOUNT_PARA,
        INVENTORY,
        AMOUNT,
    ]

    fused = ExperienceRecall(store, provider=provider, cache_dir=tmp_path, mode="rrf")
    items = fused.search(QUERY, schema_hash="h1", top_k=4, min_score=0.0)
    # Neither channel's order survives: the vector's champion gives way to rank agreement,
    # and the row the vector likes but the tokens cannot see stays behind the lexical one.
    assert [row["question"] for row in items] == [AMOUNT_NEAR, AMOUNT, AMOUNT_PARA, INVENTORY]
    # Each score is the sum of two reciprocal ranks, so the 0.99-vs-0.1 cosine spread and
    # the 0.75-vs-0.0 lexical spread contribute the same amount per position.
    assert items[0]["score"] == pytest.approx(1 / 61 + 1 / 62, abs=1e-4)
    assert items[1]["score"] == pytest.approx(1 / 61 + 1 / 64, abs=1e-4)
    assert items[3]["score"] == pytest.approx(1 / 63 + 1 / 64, abs=1e-4)
    assert fused.last.mode == "rrf"


def test_negative_cosines_keep_the_order(store: ExperienceStore, tmp_path: Path) -> None:
    provider = StaticProvider(
        {
            QUERY: [1.0, 0.0],
            AMOUNT: unit(-0.9),
            AMOUNT_NEAR: unit(-0.5),
            AMOUNT_PARA: unit(-0.7),
            INVENTORY: unit(-0.1),
        }
    )
    recall = ExperienceRecall(store, provider=provider, cache_dir=tmp_path, mode="vector")
    items = recall.search(QUERY, schema_hash="h1", top_k=2, min_score=0.0)
    assert items[0]["question"] == INVENTORY  # the least-relevant-looking row is still the best of four negatives
    assert items[0]["score"] == pytest.approx(-0.1)
    assert items[1]["question"] == AMOUNT_NEAR


def test_unit_scale_is_monotonic_and_safe() -> None:
    assert _unit_scale([]) == []
    assert _unit_scale([0.2, 0.2, 0.2]) == [0.0, 0.0, 0.0]
    assert _unit_scale([-0.9, -0.5, -0.1]) == [0.0, 0.5, 1.0]


def test_rank_selects_without_counting_a_hit(store: ExperienceStore) -> None:
    recall = _recall(store)
    hits = {int(row["id"]): int(row["hit_count"]) for row in store.verified_rows("h1")}
    picked = int(recall.rank(QUERY, schema_hash="h1", top_k=1).items[0]["id"])
    assert {int(row["id"]): int(row["hit_count"]) for row in store.verified_rows("h1")} == hits
    recall.search(QUERY, schema_hash="h1", top_k=1)
    counted = {int(row["id"]): int(row["hit_count"]) for row in store.verified_rows("h1")}
    assert counted[picked] == hits[picked] + 1
    assert sum(counted.values()) == sum(hits.values()) + 1


def test_settings_keep_the_vector_channel_opt_in(tmp_path: Path) -> None:
    store = ExperienceStore(tmp_path / "experience.db")
    settings = Settings(provider="mock", experience_recall="rrf", experience_vector=False)
    recall = recall_from_settings(store, settings, HashEmbedding())
    assert recall.mode == "lexical" and recall.provider is None

    settings = Settings(provider="mock", embedding_provider="hash", experience_recall="rrf", experience_vector=True)
    assert recall_from_settings(store, settings, None).mode == "lexical"
    armed = recall_from_settings(store, settings, HashEmbedding())
    assert armed.mode == "rrf" and armed.provider is not None


def test_recall_settings_validate() -> None:
    with pytest.raises(ValueError, match="NL2SQL_EXPERIENCE_RECALL"):
        Settings(provider="mock", experience_recall="semantic")
    with pytest.raises(ValueError, match="NL2SQL_EMBEDDING"):
        Settings(provider="mock", embedding_provider="none", experience_vector=True)
    assert 0.0 <= Settings(provider="mock", few_shot_diversity=4.0).few_shot_diversity <= 1.0


def test_recall_result_defaults_are_quiet() -> None:
    assert RecallResult().to_dict() == {
        "mode": "lexical",
        "candidates": 0,
        "selected": 0,
        "displaced_by_diversity": 0,
    }
