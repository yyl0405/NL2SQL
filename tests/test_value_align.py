"""Tests for the value-alignment step (P1-2 business dictionary)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nl2sql_agent.config import Settings
from nl2sql_agent.runtime import create_runtime
from nl2sql_agent.value_align import (
    Alignment,
    DictionaryEntry,
    ValueAligner,
    ValueDictionary,
)

DICTIONARY = Path(__file__).resolve().parents[1] / "src" / "nl2sql_agent" / "data" / (
    "value_dictionary.json"
)


def build(raw: dict[str, object]) -> ValueDictionary:
    return ValueDictionary.from_dict(raw, source="test")


def dictionary_of(*entries: DictionaryEntry) -> ValueDictionary:
    return ValueDictionary(list(entries))


def test_packaged_dictionary_is_self_consistent() -> None:
    dictionary = ValueDictionary.load(DICTIONARY)
    stats = dictionary.stats()
    assert stats["entries"] == 30
    assert stats["collisions"] == ["精工", "轴承"]
    assert {"customer", "material", "region", "status", "supplier"} <= set(stats["kinds"])
    # every declared column must be table.field, the guard for the audit's SQL
    for column in dictionary.columns:
        table, _, field_name = column.partition(".")
        assert table and field_name and "." not in field_name


def test_unmatched_question_is_returned_byte_identical() -> None:
    aligner = ValueAligner(ValueDictionary.load(DICTIONARY))
    question = "2026年6月各地区的销售订单金额排名"
    alignment = aligner.align(question)

    assert alignment.effective == question
    assert alignment.changed is False
    assert alignment.resolved == []
    assert alignment.ambiguous == []


def test_alias_appends_the_stored_value_instead_of_editing_the_wording() -> None:
    aligner = ValueAligner(ValueDictionary.load(DICTIONARY))
    alignment = aligner.align("客户杭智的订单明细")

    assert alignment.effective == (
        "客户杭智的订单明细\n\n[值对齐] sales_order.customer_name=杭州智造科技有限公司"
    )
    assert alignment.resolved[0].mention == "杭智"
    assert alignment.resolved[0].already_stated is False


def test_a_stated_value_is_verified_not_reinjected() -> None:
    aligner = ValueAligner(ValueDictionary.load(DICTIONARY))
    alignment = aligner.align("物料伺服电机的可用库存是多少？")

    assert alignment.changed is False
    assert [item.canonical for item in alignment.verified] == ["伺服电机"]
    assert alignment.verified[0].already_stated is True


def test_longest_mention_wins_so_a_full_name_is_not_a_collision() -> None:
    aligner = ValueAligner(ValueDictionary.load(DICTIONARY))
    alignment = aligner.align("客户苏州精工的订单明细")

    assert alignment.ambiguous == []
    assert alignment.resolved[0].canonical == "苏州精工自动化有限公司"


def test_a_collision_asks_instead_of_guessing() -> None:
    aligner = ValueAligner(ValueDictionary.load(DICTIONARY))
    alignment = aligner.align("精工还有多少在途采购")

    assert alignment.changed is False, "an unresolved collision must not inject anything"
    assert [item.mention for item in alignment.ambiguous] == ["精工"]
    assert alignment.options == [
        "上海精工电机（purchase_order.supplier_name）",
        "苏州精工自动化有限公司（sales_order.customer_name）",
    ]
    # the value that did resolve is kept visible as a cost of the clarification
    assert [item.column for item in alignment.suppressed] == ["purchase_order.status"]


def test_a_category_word_in_the_question_resolves_a_collision() -> None:
    aligner = ValueAligner(ValueDictionary.load(DICTIONARY))
    material = aligner.align("物料轴承的可用库存是多少？")
    unqualified = aligner.align("轴承的库存金额是多少？")

    assert material.ambiguous == []
    assert material.changed is False, "轴承 is already the stored material name"
    assert material.verified[0].kind == "material"
    assert material.verified[0].qualified_by == "material"
    assert [item.mention for item in unqualified.ambiguous] == ["轴承"]


def test_status_enum_maps_chinese_wording_to_the_stored_code() -> None:
    aligner = ValueAligner(ValueDictionary.load(DICTIONARY))
    alignment = aligner.align("待发货订单有多少")

    assert alignment.effective.endswith("[值对齐] sales_order.status=pending")


def test_more_values_than_the_cap_are_reported_as_dropped() -> None:
    entries = [
        DictionaryEntry(
            kind="other",
            column=f"t.c{i}",
            canonical=f"v{i}",
            aliases=(f"简称{i}",),
        )
        for i in range(10)
    ]
    aligner = ValueAligner(dictionary_of(*entries))
    alignment = aligner.align(" ".join(f"简称{i}" for i in range(10)))

    assert len(alignment.resolved) == 8
    assert [item.column for item in alignment.dropped] == ["t.c8", "t.c9"]


def test_off_mode_is_a_passthrough_and_carries_no_alignment_block() -> None:
    alignment = Alignment.passthrough("客户杭智的订单明细")

    assert alignment.source == "off"
    assert alignment.effective == alignment.question
    assert alignment.changed is False
    assert alignment.to_dict()["resolved"] == []


def test_malformed_dictionary_is_an_error_not_a_degradation() -> None:
    with pytest.raises(ValueError, match="kind 未知"):
        build({"entries": [{"kind": "banana", "column": "t.c", "canonical": "x"}]})
    with pytest.raises(ValueError, match="表.列"):
        build({"entries": [{"kind": "customer", "column": "customer", "canonical": "x"}]})
    with pytest.raises(ValueError, match="缺少 canonical"):
        build({"entries": [{"kind": "customer", "column": "t.c", "canonical": ""}]})
    with pytest.raises(ValueError, match="重复声明"):
        build(
            {
                "entries": [
                    {"kind": "customer", "column": "t.c", "canonical": "甲", "aliases": ["小甲"]},
                    {"kind": "customer", "column": "t.c", "canonical": "甲", "aliases": ["小甲"]},
                ]
            }
        )
    with pytest.raises(ValueError, match="不能为空"):
        build({"entries": []})


def test_the_same_literal_in_two_columns_is_not_a_duplicate() -> None:
    dictionary = build(
        {
            "entries": [
                {"kind": "status", "column": "so.status", "canonical": "pending"},
                {"kind": "status", "column": "wo.status", "canonical": "pending"},
            ]
        }
    )
    bucket = dictionary.candidates("pending")

    assert len(dictionary.entries) == 2
    assert dictionary.collision_mentions == ()
    # one entry per distinct stored value: the literal is unique, only its column is not
    assert [entry.column for entry in bucket] == ["so.status"]


def test_alignment_json_keeps_both_arms_comparable() -> None:
    payload = ValueAligner(ValueDictionary.load(DICTIONARY)).align("客户杭智的订单明细").to_dict()

    assert json.loads(json.dumps(payload))["resolved"][0]["column"] == (
        "sales_order.customer_name"
    )
    assert payload["suppressed"] == []


def test_agent_publishes_the_alignment_it_used_and_the_effective_question(
    settings: Settings,
) -> None:
    result = create_runtime(settings).agent.run("客户杭智的订单明细")

    assert result.value_alignment is not None
    assert result.value_alignment["changed"] is True
    assert "杭州智造科技有限公司" in result.effective_question
    assert result.to_dict()["value_alignment"] == result.value_alignment


def test_agent_turns_a_collision_into_a_clarification_with_options(settings: Settings) -> None:
    result = create_runtime(settings).agent.run("精工还有多少在途采购")

    assert result.status == "needs_clarification"
    assert result.sql is None
    assert result.clarification is not None
    assert len(result.clarification.options) == 2
    assert result.clarification.missing_slots == ["精工"]


def test_alignment_off_leaves_the_agent_result_empty_handed(tmp_path: Path) -> None:
    on = create_runtime(Settings(home=tmp_path / "on", provider="mock")).agent.run(
        "客户杭智的订单明细"
    )
    off = create_runtime(
        Settings(home=tmp_path / "off", provider="mock", value_alignment="off")
    ).agent.run("客户杭智的订单明细")

    assert on.value_alignment is not None
    assert off.value_alignment is None
    assert off.effective_question == off.question
