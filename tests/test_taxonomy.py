from __future__ import annotations

import json
from pathlib import Path

import pytest

from nl2sql_agent.config import Settings
from nl2sql_agent.taxonomy import (
    BUSINESS_CATEGORIES,
    UNKNOWN,
    classify,
    classify_category,
)


def _frozen_cases() -> list[dict]:
    path = Path(Settings().eval_cases_path)
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("2026年6月华东地区销售订单总金额是多少？", "metric"),
        ("按状态统计订单数量", "group_by"),
        ("销售额最高的前3个客户是谁？", "ranking"),
        ("截至2026年7月1日仍未发货的订单有哪些？", "time_filter"),
        ("库存低于安全库存的物料有哪些？", "inventory"),
        ("各供应商在途采购数量是多少？", "purchase"),
        ("2026年6月计划完工的工单完工率", "production"),
        ("整机设备的BOM所有子件及单台总用量是多少？", "bom"),
        ("订单ORD-1012的物料齐套率是多少？", "kitting"),
        ("客户杭州智造科技有限公司的订单明细", "detail"),
    ],
)
def test_classify_each_category(question: str, expected: str) -> None:
    assert classify_category(question) == expected


def test_domain_category_beats_answer_shape_words() -> None:
    # 「各 … 排名」 是形状词，「供应商/在途」 是业务域，业务域必须赢。
    assert classify_category("各供应商在途采购数量排名") == "purchase"
    assert classify_category("库存金额最高的物料分类") == "inventory"


def test_time_words_do_not_override_metric_intent() -> None:
    assert classify_category("2026年6月销售总额是多少？") == "metric"


def test_unmatched_question_is_unknown_not_guessed() -> None:
    result = classify("今天杭州天气怎么样？")
    assert result.category == UNKNOWN
    assert result.is_business is False
    assert result.score == 0.0


def test_empty_and_none_input() -> None:
    assert classify("").category == UNKNOWN
    assert classify(None).category == UNKNOWN


def test_classification_dict_shape() -> None:
    payload = classify("轴承还剩多少库存").to_dict()
    assert payload["category"] == "inventory"
    assert payload["is_business"] is True
    assert "库存" in payload["matched"]


def test_categories_are_the_declared_taxonomy() -> None:
    for question in [
        "各状态订单数量统计",
        "工单完工率",
        "缺料情况",
        "供应商送货在途多少",
    ]:
        assert classify_category(question) in BUSINESS_CATEGORIES


def test_paraphrases_outside_the_eval_set() -> None:
    held_out = {
        "轴承还剩多少库存": "inventory",
        "ORD-1001 缺料情况": "kitting",
        "物料清单展开到第三层": "bom",
        "工单完工进度": "production",
        "供应商送货在途多少": "purchase",
        "上个月华东卖了多少钱": "metric",
        "不同状态的单子数量": "group_by",
        "销量最好的客户排名": "ranking",
        "客户深圳光年的订单明细": "detail",
    }
    wrong = {q: (e, classify_category(q)) for q, e in held_out.items() if classify_category(q) != e}
    assert not wrong, f"misclassified paraphrases: {wrong}"


def test_classifier_matches_labels_on_the_v1_baseline_subset() -> None:
    """Regression guard, not a capability claim.

    The keyword vocabulary was induced from this same synthetic set, so 100%
    here measures stability of the rules, not real-world classification skill.
    """
    cases = _frozen_cases()
    baseline = [case for case in cases if "v1_baseline" in case.get("tags", [])]
    answerable = [case for case in baseline if not case.get("expect_refusal")]
    assert len(answerable) == 37
    wrong = [
        (case["id"], case["category"], classify_category(case["question"]))
        for case in answerable
        if classify_category(case["question"]) != case["category"]
    ]
    assert not wrong


# Accepted disagreements on the expanded corpus. ``DOMAIN_WEIGHT`` lets a domain
# noun outrank the answer shape (「库存金额最高的前2个仓库」 is read as inventory, not
# ranking), and the cross-domain hard cases ask about two domains at once, so
# both answers are defensible. Pinned by id: a new disagreement has to be added
# here on purpose, and a resolved one has to be deleted here to fail the test.
ACCEPTED_DISAGREEMENTS = {
    "m_june_line_count": "detail",
    "m_june_avg_line": "detail",
    "g_region_month": "detail",
    "r_top_warehouses": "inventory",
    "r_top_supplier": "purchase",
    "t_q2_order_count": "metric",
    "t_may_june_compare": "group_by",
    "t_wo_late": "production",
    "t_po_july": "purchase",
    "d_inventory_m005": "inventory",
    "h_net_gap": "inventory",
    "h_cover_ratio": "inventory",
    "h_region_demand_supply": "inventory",
    "h_arrival_covers_safety": "inventory",
    "h_supplier_supply_state": "inventory",
}


def test_classifier_coverage_on_the_expanded_corpus() -> None:
    answerable = [case for case in _frozen_cases() if not case.get("expect_refusal")]
    assert len(answerable) == 113
    wrong = {
        case["id"]: classify_category(case["question"])
        for case in answerable
        if classify_category(case["question"]) != case["category"]
    }
    assert wrong == ACCEPTED_DISAGREEMENTS
    agreement = (len(answerable) - len(wrong)) / len(answerable)
    assert agreement >= 0.85
    # Every in-scope question gets a business category: an `unknown` here would
    # leave the few-shot picker without a category hint.
    assert [case_id for case_id, got in wrong.items() if got == "unknown"] == []
