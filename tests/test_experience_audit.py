"""The paired recall instrument: corpus resolution, arm symmetry, and its invariants.

Numbers produced by this instrument belong in documents, not in assertions: the
paraphrase corpus is assistant-authored measurement material, so a test that pinned
0.906 would turn a corpus edit into a false regression. What is pinned here is the
machinery — every pair resolves or is reported, every arm scores the same queries, and
the default configuration stays identical to the legacy store path.
"""

from __future__ import annotations

import json
from pathlib import Path

from nl2sql_agent.config import Settings
from nl2sql_agent.experience_audit import audit_fingerprint, load_pairs, run_experience_audit
from nl2sql_agent.memory import ExperienceStore


def _write_cases(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return path


def _corpus(tmp_path: Path) -> tuple[Path, Path]:
    frozen = _write_cases(
        tmp_path / "frozen.jsonl",
        [
            {
                "id": "amount",
                "question": "2026年6月华东地区销售订单总金额是多少？",
                "gold_sql": "SELECT SUM(qty) FROM order_item",
                "category": "metric",
            },
            {
                "id": "inventory",
                "question": "各仓库的物料库存数量是多少？",
                "gold_sql": "SELECT warehouse_id, SUM(qty) FROM inventory GROUP BY warehouse_id",
                "category": "inventory",
            },
            {
                "id": "status",
                "question": "各状态的订单数量是多少？",
                "gold_sql": "SELECT status, COUNT(*) FROM sales_order GROUP BY status",
                "category": "group_by",
            },
            {"id": "refusal", "question": "把订单全删了", "expected_status": "rejected"},
        ],
    )
    paraphrases = _write_cases(
        tmp_path / "paraphrase.jsonl",
        [
            {
                "id": "amount__para",
                "question": "2026年6月华东的销售订单金额合计是多少",
                "paraphrase_of": "amount",
                "category": "metric",
            },
            {
                "id": "inventory__para",
                "question": "库存低于多少的物料有哪些",
                "paraphrase_of": "inventory",
                "category": "inventory",
            },
            {"id": "orphan__para", "question": "孤儿改写", "paraphrase_of": "missing"},
        ],
    )
    return frozen, paraphrases


def test_load_pairs_reports_orphans_and_skips_cases_without_sql(tmp_path: Path) -> None:
    frozen, paraphrases = _corpus(tmp_path)
    rows, pairs, skipped = load_pairs(frozen, paraphrases)
    assert len(rows) == 3 and skipped == 1
    unresolved = next(pair["unresolved"] for pair in pairs if "unresolved" in pair)
    assert unresolved == ["orphan__para"]
    assert {pair["target_id"] for pair in pairs if "unresolved" not in pair} == {"amount", "inventory"}


def test_audit_scores_every_arm_on_the_same_pairs(tmp_path: Path) -> None:
    frozen, paraphrases = _corpus(tmp_path)
    report = run_experience_audit(
        frozen_path=frozen,
        paraphrase_path=paraphrases,
        settings=Settings(provider="mock"),
        embedding="hash",
    )
    assert report["measured"] is True
    assert report["queries"] == 2
    assert report["frozen_cases"] == 3
    assert report["frozen_without_sql"] == 1
    assert report["unresolved"] == ["orphan__para"]
    assert report["invariant_violations"] == []
    assert [profile["profile"] for profile in report["profiles"]] == ["paired", "full"]
    assert [profile["library_rows"] for profile in report["profiles"]] == [2, 3]

    for profile in report["profiles"]:
        assert set(profile["arms"]) == {"lexical", "diversity", "gold-half", "vector-hash", "rrf-hash"}
        for arm, data in profile["arms"].items():
            assert data["queries"] == 2
            assert data["recall"]["@5"] >= data["recall"]["@3"] >= data["recall"]["@1"]
            if arm in {"vector-hash", "rrf-hash"}:
                assert data["mode"] in {"vector", "rrf"}
                assert data["degraded"] == 0
                assert data["provider"].startswith("hash:")
            else:
                assert data["mode"] == "lexical"
        gold = profile["gold_split"]["gold-half"]
        promoted, plain = gold["gold_targets"].split("/"), gold["plain_targets"].split("/")
        assert int(promoted[1]) + int(plain[1]) == 2
    assert audit_fingerprint(report)


def test_audit_can_report_the_paired_library_only(tmp_path: Path) -> None:
    frozen, paraphrases = _corpus(tmp_path)
    report = run_experience_audit(
        frozen_path=frozen,
        paraphrase_path=paraphrases,
        settings=Settings(provider="mock"),
        include_full_library=False,
    )
    assert [profile["profile"] for profile in report["profiles"]] == ["paired"]


def test_audit_never_writes_the_configured_experience_store(tmp_path: Path) -> None:
    frozen, paraphrases = _corpus(tmp_path)
    settings = Settings(home=tmp_path, provider="mock")
    store = ExperienceStore(settings.experience_path)
    store.add("已有经验", "SELECT 1 AS n", verified=True, schema_hash="h1")
    before = store.stats()
    run_experience_audit(
        frozen_path=frozen, paraphrase_path=paraphrases, settings=settings, embedding="hash"
    )
    assert ExperienceStore(settings.experience_path).stats() == before
