from __future__ import annotations

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.guard import SQLGuard

PARENT_COUNT = (
    "SELECT o.region, COUNT(o.order_id) AS c FROM sales_order o "
    "JOIN order_item i ON i.order_id = o.order_id GROUP BY o.region"
)
PARENT_SUM = (
    "SELECT m.category, SUM(m.safety_stock) AS s FROM material m "
    "JOIN order_item i ON i.material_id = m.material_id GROUP BY m.category"
)
CHILD_SUM = (
    "SELECT o.region, SUM(i.qty * i.unit_price) AS amount FROM sales_order o "
    "JOIN order_item i ON i.order_id = o.order_id GROUP BY o.region"
)
MIXED_SUM = (
    "SELECT m.category, ROUND(SUM(i.on_hand_qty * m.standard_cost), 2) AS inventory_value "
    "FROM material m JOIN inventory i ON i.material_id = m.material_id GROUP BY m.category"
)


def _fanout(result) -> list[str]:
    return [issue.message for issue in result.warnings if issue.code == "join_fanout"]


def test_parent_side_aggregate_over_one_to_many_is_flagged(catalog: Catalog) -> None:
    guard = SQLGuard(catalog)
    for sql in (PARENT_COUNT, PARENT_SUM):
        result = guard.check(sql)
        assert _fanout(result), sql
        assert result.valid


def test_child_side_and_mixed_aggregates_stay_quiet(catalog: Catalog) -> None:
    guard = SQLGuard(catalog)
    assert _fanout(guard.check(CHILD_SUM)) == []
    assert _fanout(guard.check(MIXED_SUM)) == []


def test_count_distinct_is_not_noise(catalog: Catalog) -> None:
    result = SQLGuard(catalog).check(
        "SELECT o.region, COUNT(DISTINCT o.order_id) AS c FROM sales_order o "
        "JOIN order_item i ON i.order_id = o.order_id GROUP BY o.region"
    )
    assert _fanout(result) == []


def test_pre_aggregated_child_is_not_noise(catalog: Catalog) -> None:
    result = SQLGuard(catalog).check(
        "SELECT o.order_id, x.total_qty FROM sales_order o "
        "JOIN (SELECT i.order_id, SUM(i.qty) AS total_qty FROM order_item i GROUP BY i.order_id) x "
        "ON x.order_id = o.order_id"
    )
    assert _fanout(result) == []


def test_plain_listing_without_aggregate_is_not_flagged(catalog: Catalog) -> None:
    result = SQLGuard(catalog).check(
        "SELECT o.order_id, i.qty FROM sales_order o JOIN order_item i ON i.order_id = o.order_id"
    )
    assert _fanout(result) == []


def test_cartesian_shapes_are_flagged(catalog: Catalog) -> None:
    guard = SQLGuard(catalog)
    assert _fanout(guard.check("SELECT o.order_id, m.material_name FROM sales_order o CROSS JOIN material m"))
    assert _fanout(
        guard.check("SELECT o.order_id FROM sales_order o LEFT JOIN order_item i WHERE i.qty > 0")
    )
    assert _fanout(guard.check("SELECT o.order_id, m.material_name FROM sales_order o, material m"))


def test_max_and_min_cannot_be_inflated_by_fanout(catalog: Catalog) -> None:
    # MAX/MIN over duplicated parent rows return the same value, so the roll-up
    # idiom MAX(parent.col) beside a child-side SUM is not a wrong number.
    result = SQLGuard(catalog).check(
        "SELECT i.material_id, SUM(i.on_hand_qty - i.locked_qty) AS available_qty, "
        "MAX(m.safety_stock) AS safety_stock FROM inventory i "
        "JOIN material m ON m.material_id = i.material_id GROUP BY i.material_id"
    )
    assert _fanout(result) == []


def test_clause_commas_are_not_read_as_comma_joins(catalog: Catalog) -> None:
    guard = SQLGuard(catalog)
    assert (
        _fanout(
            guard.check(
                "SELECT wo.status, COUNT(*) AS c FROM work_order wo "
                "GROUP BY wo.status ORDER BY c DESC, wo.status"
            )
        )
        == []
    )
    assert _fanout(guard.check("SELECT o.order_id FROM sales_order o ORDER BY o.due_date, o.order_id")) == []
    assert _fanout(guard.check("SELECT o.order_id, m.material_name FROM sales_order AS o, material AS m"))


def test_join_at_end_of_statement_is_not_missed(catalog: Catalog) -> None:
    # The terminator of the JOIN pattern is end-of-statement here; an earlier
    # version required whitespace before `$` and matched nothing.
    assert _fanout(SQLGuard(catalog).check("SELECT o.order_id FROM sales_order o JOIN material m"))


def test_unrelated_join_key_is_not_flagged(catalog: Catalog) -> None:
    result = SQLGuard(catalog).check(
        "SELECT o.region, COUNT(o.order_id) AS c FROM sales_order o "
        "JOIN order_item i ON i.unit_price = o.order_id GROUP BY o.region"
    )
    assert _fanout(result) == []


def test_two_parent_side_joins_both_warn(catalog: Catalog) -> None:
    result = SQLGuard(catalog).check(
        "SELECT o.region, COUNT(o.order_id) AS c FROM sales_order o "
        "JOIN work_order w ON w.order_id = o.order_id "
        "JOIN shipment s ON s.order_id = o.order_id GROUP BY o.region"
    )
    messages = _fanout(result)
    assert len(messages) == 2
    assert any("work_order" in message for message in messages)
    assert any("shipment" in message for message in messages)


def test_fanout_is_never_an_error(catalog: Catalog) -> None:
    for sql in (
        PARENT_COUNT,
        PARENT_SUM,
        "SELECT o.order_id, m.material_name FROM sales_order o CROSS JOIN material m",
        "SELECT o.order_id, m.material_name FROM sales_order o, material m",
    ):
        result = SQLGuard(catalog).check(sql)
        assert result.valid, sql
        assert all(issue.level == "warning" for issue in result.warnings)


def test_gold_corpus_stays_warning_free(catalog: Catalog) -> None:
    from nl2sql_agent.config import Settings
    from nl2sql_agent.evaluation import load_cases
    from nl2sql_agent.guard_suite import audit_gold_sql

    gold = [
        {"id": case.id, "gold_sql": case.gold_sql}
        for case in load_cases(Settings().eval_cases_path)
    ]
    audit = audit_gold_sql(catalog, gold, with_ast=False)
    assert [row["id"] for row in audit.errors] == []
    assert [row["id"] for row in audit.warnings] == []
    assert audit.total < len(gold)
    assert audit.skipped == len(gold) - audit.total
