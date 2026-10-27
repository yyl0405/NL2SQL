"""Quality tiers, retirement, and stale-library batch handling in the experience store.

The invariant under test is that `quality` only ever changes *rank*, never *visibility*,
and that nothing in the store promotes a row to gold on its own.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nl2sql_agent.memory import ExperienceStore

QUESTION = "2026年6月华东地区销售订单总金额是多少？"
SQL = "SELECT SUM(qty) AS total FROM order_item"
OTHER = "各仓库的物料库存数量是多少？"
OTHER_SQL = "SELECT warehouse_id, SUM(qty) AS total FROM inventory GROUP BY warehouse_id"


@pytest.fixture()
def store(tmp_path: Path) -> ExperienceStore:
    return ExperienceStore(tmp_path / "experience.db")


def _verified(store: ExperienceStore, question: str = QUESTION, sql: str = SQL) -> dict:
    record = store.add(question, sql, schema_hash="h1", verified=True)
    return record


def test_gold_is_never_automatic(store: ExperienceStore) -> None:
    record = _verified(store)
    assert record["quality"] == "verified"
    assert store.add(OTHER, OTHER_SQL, schema_hash="h1", verified=True)["quality"] == "verified"
    assert store.stats()["gold"] == 0


def test_gold_promotes_rank_not_visibility(store: ExperienceStore) -> None:
    first = _verified(store)
    second = _verified(store, question="六月华东区域订单金额合计是多少？", sql=SQL)
    store.set_grade(int(second["id"]), "gold")

    query = "2026年6月华东地区销售订单金额"
    plain = [row["id"] for row in store.search(query, schema_hash="h1", top_k=2, record=False)]
    assert plain[0] == int(first["id"])

    # A gold row gains a fraction of the best match, so a large enough boost flips the order.
    boosted = store.score(query, schema_hash="h1", gold_boost=0.6)
    assert boosted[0]["id"] == int(second["id"])
    assert boosted[0]["quality"] == "gold"
    # Gold rows are still retrieved by the ordinary lexical call: same status, same reach.
    assert {int(row["id"]) for row in store.search(query, schema_hash="h1", top_k=2, record=False)} == {
        int(first["id"]),
        int(second["id"]),
    }


def test_gold_bonus_is_scaled_to_the_best_match(store: ExperienceStore) -> None:
    first = _verified(store)
    second = _verified(store, question="六月华东区域订单金额合计是多少？", sql=SQL)
    store.set_grade(int(second["id"]), "gold")
    query = "2026年6月华东地区销售订单金额"
    base = {row["id"]: row["score"] for row in store.score(query, schema_hash="h1")}
    ceiling = max(base.values())
    boosted = {row["id"]: row["score"] for row in store.score(query, schema_hash="h1", gold_boost=0.2)}
    # The gold row gains exactly 0.2 * (best base score), whatever that best score was.
    assert boosted[int(second["id"])] == pytest.approx(
        round(base[int(second["id"])] + 0.2 * ceiling, 6)
    )
    assert boosted[int(first["id"])] == pytest.approx(base[int(first["id"])])


def test_grading_requires_a_verified_row(store: ExperienceStore) -> None:
    candidate = store.add(QUESTION, SQL, schema_hash="h1")
    with pytest.raises(ValueError, match="只有 verified"):
        store.set_grade(int(candidate["id"]), "gold")
    with pytest.raises(KeyError):
        store.set_grade(9999, "gold")
    with pytest.raises(ValueError):
        store.set_grade(int(_verified(store)["id"]), "candidate")


def test_deprecated_rows_leave_retrieval_but_stay_auditable(store: ExperienceStore) -> None:
    record = _verified(store)
    retired = store.deprecate(int(record["id"]), reason="口径作废")
    assert retired["status"] == "deprecated"
    assert retired["deprecate_reason"] == "口径作废"
    assert store.search(QUESTION, schema_hash="h1") == []
    assert store.score(QUESTION, schema_hash="h1") == []
    stats = store.stats()
    assert stats["verified"] == 0 and stats["deprecated"] == 1 and stats["total"] == 1
    assert store.list(status="deprecated")[0]["id"] == record["id"]

    # Re-verifying a retired row brings it back with the reason cleared.
    revived = store.add(QUESTION, SQL, schema_hash="h1", verified=True)
    assert revived["status"] == "verified" and revived["deprecated_at"] is None
    assert store.search(QUESTION, schema_hash="h1")


def test_legacy_hard_quality_is_migrated_on_open(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    store = ExperienceStore(path)
    record = store.add(QUESTION, SQL, schema_hash="h1", verified=True)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE experiences SET quality = 'hard' WHERE id = ?", (int(record["id"]),))
    reopened = ExperienceStore(path)
    assert reopened.list()[0]["quality"] == "verified"
    assert reopened.search(QUESTION, schema_hash="h1")[0]["quality"] == "verified"


def test_staleness_reports_other_hashes_without_writing(store: ExperienceStore) -> None:
    _verified(store)
    store.add(OTHER, OTHER_SQL, schema_hash="old-hash", verified=True)
    report = store.staleness("h1")
    assert report["active_rows"] == 1
    assert report["stale_rows"] == 1
    assert report["stale_hashes"] == ["old-hash"]
    # An old-hash row is already unreachable by retrieval, so the report is a backlog, not a bug.
    assert store.search(OTHER, schema_hash="h1") == []


def test_mark_stale_previews_then_retires(store: ExperienceStore) -> None:
    active = _verified(store)
    stale = store.add(OTHER, OTHER_SQL, schema_hash="old-hash", verified=True)

    preview = store.mark_stale("h1", dry_run=True)
    assert preview["affected"] == 1 and preview["ids"] == [int(stale["id"])]
    assert store.list()[0]["status"] != "deprecated"

    applied = store.mark_stale("h1", reason="schema_changed")
    assert applied["affected"] == 1
    rows = {int(row["id"]): row for row in store.list(limit=10)}
    assert rows[int(stale["id"])]["status"] == "deprecated"
    assert rows[int(stale["id"])]["deprecate_reason"] == "schema_changed"
    assert rows[int(active["id"])]["status"] == "verified"
    # Idempotent: running it again has nothing left to retire.
    assert store.mark_stale("h1")["affected"] == 0
