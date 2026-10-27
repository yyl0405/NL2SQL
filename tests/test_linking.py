"""Schema linking stays a measured choice, not a hardcoded ranking."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nl2sql_agent.catalog import Catalog, Column, Table
from nl2sql_agent.cli import _cmd_link_check
from nl2sql_agent.embeddings import EmbeddingError, HashEmbedding
from nl2sql_agent.linking import (
    BY_NAME,
    LARGE_CATALOG_TABLES,
    FusedLinkIndex,
    LinkIndex,
    VectorIndex,
    _mine_table_terms,
    _tokens,
    build_index,
    build_named_index,
    fusion_mode,
    reciprocal_rank_fusion,
    resolve_strategy,
    strategy_by_name,
    strategy_names,
    variant_names,
)

DATA = Path(__file__).resolve().parents[1] / "src" / "nl2sql_agent" / "data"
DEMO_SCHEMA = DATA / "demo_schema.json"
LINKING_CASES = DATA / "linking_cases.jsonl"


def wide_catalog(generics: int = 60, filler_columns: int = 60, mentioners: int = 12) -> Catalog:
    """A catalog that looks like a real ERP module map.

    The answer table carries the richest comments and the most columns, and a
    dozen small tables mention the query term too. BM25's length normalisation
    then buries the answer below the thin tables — the exact failure measured on
    a 543-table SQL Server catalog.
    """
    tables: dict[str, Table] = {}
    for index in range(generics):
        name = f"GEN_Table{index:03d}"
        columns = [
            Column(name="Code", type="nvarchar", description="编号"),
            Column(name="CreateDate", type="datetime", description="创建时间"),
        ]
        if index < mentioners:
            columns.append(Column(name="Amount", type="decimal", description="合同金额"))
        tables[name.lower()] = Table(
            name=name, description=f"通用单据{index}", columns=tuple(columns)
        )
    target = [
        Column(name="ContractAmount", type="decimal", description="合同金额"),
        Column(name="ReceiptAmount", type="decimal", description="收款金额"),
        Column(name="ReceiptDate", type="datetime", description="收款日期"),
        Column(name="UnpaidDays", type="int", description="未到款天数"),
    ]
    target.extend(
        Column(name=f"F{index}", type="nvarchar", description=f"扩展属性字段{index}")
        for index in range(filler_columns)
    )
    tables["contract"] = Table(
        name="Contract", description="合同主表", columns=tuple(target)
    )
    return Catalog(domain="test", dialect="mssql", as_of_date="", tables=tables)


def test_auto_strategy_follows_catalog_scale() -> None:
    assert resolve_strategy("auto", LARGE_CATALOG_TABLES - 1).name == "baseline"
    assert resolve_strategy("auto", LARGE_CATALOG_TABLES).name == "hybrid"
    assert resolve_strategy(None, 900).name == "hybrid"
    assert resolve_strategy("coverage", 900).name == "coverage"


def test_unknown_strategy_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知的 linking 策略"):
        resolve_strategy("bm25-plus-magic", 10)
    with pytest.raises(ValueError, match="未知的 linking 策略"):
        strategy_by_name(["baseline", "nope"])
    assert strategy_by_name(None) == (BY_NAME["baseline"],)
    assert set(strategy_names()) <= set(BY_NAME)


def _names(index: LinkIndex, query: str, top_k: int = 5) -> list[str]:
    return [table.name for table, _ in index.rank(query, top_k)]


def test_hybrid_outranks_baseline_on_a_wide_catalog() -> None:
    catalog = wide_catalog()
    query = "合同金额"
    base = build_index(catalog, BY_NAME["baseline"])
    hybrid = build_index(catalog, BY_NAME["hybrid"])
    assert _names(hybrid, query)[0] == "Contract"
    # length normalisation plus full-weight column text pushes the richest table
    # below the thin ones once the catalog is wide
    assert "Contract" not in _names(base, query)


def test_baseline_still_wins_on_a_short_follow_up() -> None:
    """A follow-up like "那华南呢？" only matches through column text.

    Turning columns off is exactly what the large-catalog strategy does, so the
    demo catalog must keep the baseline and the existing table-recall numbers.
    """
    catalog = Catalog.from_path(DEMO_SCHEMA)
    hits = catalog.search_tables("那华南地区的订单金额呢？", top_k=5)
    assert catalog.link_index().strategy.name == "baseline"
    assert hits and hits[0].score > 0


def test_baseline_is_what_the_demo_catalog_uses() -> None:
    catalog = Catalog.from_path(DEMO_SCHEMA)
    assert len(catalog.enabled_tables()) < LARGE_CATALOG_TABLES
    assert catalog.link_index().strategy is BY_NAME["baseline"]
    # and the index is cached: the second call must not rebuild it
    assert catalog.link_index() is catalog.link_index()


def test_auto_rule_switches_on_catalog_size() -> None:
    demo = Catalog.from_path(DEMO_SCHEMA)
    assert demo.linking == "auto"
    assert demo.link_index().strategy.name == "baseline"

    wide = wide_catalog()
    assert wide.link_index().strategy.name == "hybrid"


def test_hybrid_wins_on_average_not_on_one_lucky_query() -> None:
    catalog = wide_catalog()
    queries = ["合同金额", "未到款金额", "扩展属性字段", "合同金额与收款金额"]
    base = build_index(catalog, BY_NAME["baseline"])
    hybrid = build_index(catalog, BY_NAME["hybrid"])

    def recall_at_5(index: LinkIndex) -> float:
        hits = sum(
            1 for q in queries if "Contract" in [table.name for table, _ in index.rank(q, 5)]
        )
        return hits / len(queries)

    assert recall_at_5(hybrid) > recall_at_5(base)


def test_fractional_weight_thins_tokens_without_losing_coverage() -> None:
    text = "合同金额未到款金额"
    kept = _tokens(text, 0.35)
    full = _tokens(text, 1.0)
    assert kept and len(kept) < len(full)
    assert set(kept) <= set(full)
    assert _tokens(text, 0) == []
    assert _tokens("", 3) == []


def test_mined_synonyms_skip_terms_shared_across_the_database() -> None:
    catalog = wide_catalog()
    mined = _mine_table_terms(catalog.enabled_tables(), per_table=20)
    assert "收款" in mined["Contract"]
    for name, terms in mined.items():
        assert not {"创建", "时间", "编号", "建时"} & set(terms), name
    # the thin tables hold nothing but shared words, so they mine no synonyms at all
    assert mined["GEN_Table030"] == []


class FixedProvider:
    """Returns pre-set vectors: enough to test cosine order without a model."""

    calls = 0

    def __init__(self, name: str, query_vector: list[float]) -> None:
        self.name = name
        self.query_vector = query_vector

    def embed(self, texts):
        self.calls += 1
        return [list(self.query_vector) for _ in texts]


class BrokenProvider:
    name = "broken:2"
    calls = 0

    def embed(self, texts):
        self.calls += 1
        raise EmbeddingError("provider offline")


def _tables(*names: str) -> list[Table]:
    return [Table(name=name, description=f"{name} 表", columns=(Column("ID", "int"),)) for name in names]


def test_vector_index_ranks_by_cosine() -> None:
    left, right = _tables("Left", "Right")
    index = VectorIndex([left, right], [[1.0, 0.0], [0.0, 1.0]], FixedProvider("fixed:2", [0.0, 1.0]))
    assert [table.name for table, _ in index.rank("q", 2)] == ["Right", "Left"]
    assert index.rank("", 2) == []
    assert index.query_calls == 1


def test_rrf_fuses_ranks_not_raw_scores() -> None:
    a, b, c = _tables("A", "B", "C")
    fused = reciprocal_rank_fusion([("bm25", [a, b]), ("vector", [b, c])], k=60)
    assert [item.table.name for item in fused] == ["B", "A", "C"]
    assert fused[0].sources == ("bm25", "vector")
    assert fused[0].ranks == {"bm25": 2, "vector": 1}
    assert fused[1].sources == ("bm25",)


def test_fusion_only_returns_names_present_in_a_channel() -> None:
    a, b = _tables("A", "B")
    fused = reciprocal_rank_fusion([("bm25", [a]), ("vector", [])], k=10)
    assert [item.table.name for item in fused] == ["A"]
    assert fused[0].sources == ("bm25",)


def test_query_embedding_failure_degrades_to_bm25(tmp_path: Path) -> None:
    catalog = Catalog.from_path(DEMO_SCHEMA)
    bm25 = build_index(catalog, BY_NAME["baseline"])
    vector = VectorIndex(
        bm25.tables, [[1.0, 0.0] for _ in bm25.tables], BrokenProvider()
    )
    fused = FusedLinkIndex(bm25, vector, "rrf")
    query = "库存低于安全库存的物料"
    expected = [table.name for table, _ in bm25.rank(query, 5)]
    result = fused.rank_with_sources(query, 5)
    assert [item.table.name for item in result] == expected
    assert fused.degraded is True
    assert "provider offline" in fused.degraded_reason


def test_index_build_failure_degrades_to_bm25(tmp_path: Path) -> None:
    catalog = Catalog.from_path(DEMO_SCHEMA)
    index = build_index(
        catalog,
        BY_NAME["baseline"],
        mode="rrf",
        embedder=BrokenProvider(),
        cache_dir=tmp_path,
    )
    assert isinstance(index, FusedLinkIndex)
    assert index.degraded is True and index.embedding == ""
    query = "库存低于安全库存的物料"
    expected = [table.name for table, _ in index.bm25.rank(query, 5)]
    assert [item.table.name for item in index.rank_with_sources(query, 5)] == expected


def test_vector_modes_require_a_provider() -> None:
    catalog = Catalog.from_path(DEMO_SCHEMA)
    with pytest.raises(ValueError, match="embedding"):
        build_index(catalog, BY_NAME["baseline"], mode="rrf")
    assert resolve_strategy("rrf", 5).name == "baseline"
    assert resolve_strategy("vector", 500).name == "hybrid"
    assert fusion_mode("rrf") == "rrf"
    assert fusion_mode("hybrid") == "bm25"
    assert fusion_mode(None) == "bm25"
    assert set(variant_names()) >= {"baseline", "vector", "rrf"}


def test_search_reports_per_table_sources(tmp_path: Path) -> None:
    lexical = Catalog.from_path(DEMO_SCHEMA).search("库存低于安全库存的物料", top_k=5)
    assert lexical.mode == "bm25"
    assert lexical.embedding == ""
    assert all(hit.sources == ("bm25",) for hit in lexical.hits)
    assert all("bm25" in hit.ranks for hit in lexical.hits)

    catalog = Catalog.from_path(
        DEMO_SCHEMA,
        linking="rrf",
        embedder=HashEmbedding(dimensions=64),
        embedding_cache=tmp_path,
    )
    result = catalog.search("库存低于安全库存的物料", top_k=5)
    assert result.mode == "rrf"
    assert result.embedding == "hash:64"
    assert result.degraded is False
    assert any("vector" in hit.sources for hit in result.hits)
    payload = result.to_dict()
    assert payload["retrieval"]["mode"] == "rrf"
    assert payload["retrieval"]["embedding"] == "hash:64"
    assert payload["retrieval"]["vector"]["texts"] == len(catalog.enabled_tables())
    assert payload["retrieval"]["vector"]["cache_hit"] is False
    # the in-process index is cached per configuration: no rebuild, no disk read
    again = catalog.search("各供应商在途采购数量", top_k=5)
    assert again.vector_stats["cache_hit"] is False
    # a fresh catalog with the same cache directory loads the vectors from disk
    fresh = Catalog.from_path(
        DEMO_SCHEMA,
        linking="rrf",
        embedder=HashEmbedding(dimensions=64),
        embedding_cache=tmp_path,
    )
    assert fresh.search("各供应商在途采购数量", top_k=5).vector_stats["cache_hit"] is True


def _load_linking_cases() -> list[dict]:
    lines = LINKING_CASES.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_committed_linking_set_has_twenty_labeled_cases() -> None:
    cases = _load_linking_cases()
    assert len(cases) == 20
    catalog = Catalog.from_path(DEMO_SCHEMA)
    enabled = set(catalog.enabled_names())
    for case in cases:
        assert case["question"]
        assert case["expected_tables"]
        assert set(case["expected_tables"]) <= enabled, case["id"]


def test_baseline_recall_on_the_committed_linking_set() -> None:
    cases = _load_linking_cases()
    index = build_named_index(Catalog.from_path(DEMO_SCHEMA), "baseline")
    recall = 0.0
    for case in cases:
        got = {item.table.name for item in index.rank_with_sources(case["question"], 5)}
        recall += len(got & set(case["expected_tables"])) / len(case["expected_tables"])
    # pinned at 0.975 in the P4 measurement; the floor leaves room for
    # harmless ranking changes without letting a regression pass silently
    assert recall / len(cases) >= 0.95


def test_link_check_cli_compares_bm25_and_rrf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NL2SQL_EMBEDDING", "hash")
    args = SimpleNamespace(
        cases=str(LINKING_CASES),
        top_k=5,
        variants=["baseline", "rrf"],
        json=True,
        home=str(tmp_path),
    )
    assert _cmd_link_check(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [run["summary"]["mode"] for run in payload["runs"]] == ["bm25", "rrf"]
    assert payload["runs"][1]["summary"]["embedding"].startswith("hash:")
    assert payload["runs"][1]["summary"]["degraded"] is False


def test_link_check_cli_rejects_vector_variants_without_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NL2SQL_EMBEDDING", "none")
    args = SimpleNamespace(
        cases=str(LINKING_CASES), top_k=5, variants=["vector"], json=True, home=str(tmp_path)
    )
    assert _cmd_link_check(args) == 2
    assert "需要向量通道" in capsys.readouterr().err


def test_linking_field_weights_reproduce_the_documented_emphasis() -> None:
    table = Table(
        name="Contract",
        description="合同主表",
        synonyms=("销售合同",),
        columns=(Column(name="Amount", type="decimal", description="合同金额"),),
    )
    index = LinkIndex([table], BY_NAME["baseline"])
    doc = index._docs[0]
    assert doc.count("contract") == 3
    # table name + synonyms at x3, description at x2, column text at x1
    assert doc.count("合同主") == 2
    assert doc.count("销售合") == 3
    assert doc.count("合同金") == 1
