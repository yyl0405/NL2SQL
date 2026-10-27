"""Tests for the clarification layer as the agent actually uses it (P1-6)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from nl2sql_agent.agent import _kind_word
from nl2sql_agent.clarify import CODE_ENTITY, CODE_METRIC, CODE_SCHEMA, CODE_TIME
from nl2sql_agent.config import Settings
from nl2sql_agent.runtime import Runtime, create_runtime

VAGUE = "库存有多少？"
COLLISION = "精工的销售订单总金额是多少？"
SELF_SAME_COLLISION = "轴承的可用库存是多少？"
PERIODLESS = "销售额是多少？"
NARROWED = "2026年6月华东地区销售订单总金额是多少？"


def agent_in(runtime: Runtime, mode: str) -> Runtime:
    return create_runtime(replace(runtime.settings, clarify=mode))


def test_a_vague_noun_is_asked_with_the_three_readings(runtime: Runtime) -> None:
    result = runtime.agent.run(VAGUE)
    assert result.status == "needs_clarification"
    assert result.clarification is not None
    assert result.clarification.code == CODE_METRIC
    assert len(result.clarification.options) == 3
    assert "你可以直接这样问" in result.answer
    assert result.clarify and result.clarify["codes"] == [CODE_METRIC]


def test_picking_a_number_is_enough_to_get_an_answer(runtime: Runtime) -> None:
    first = runtime.agent.run(VAGUE)
    second = runtime.agent.run("2", history=[first.to_turn()])
    assert second.rewrite_source == "clarify_reply"
    assert second.effective_question == first.clarification.options[1]
    assert second.status == "ok"


def test_an_ambiguous_short_name_blocks_and_hands_back_both_values(runtime: Runtime) -> None:
    result = runtime.agent.run(COLLISION)
    assert result.status == "needs_clarification"
    assert result.clarification.code == CODE_ENTITY
    assert result.sql is None
    assert [item.split("（")[0] for item in result.clarification.options] == [
        "上海精工电机",
        "苏州精工自动化有限公司",
    ]


def test_a_pick_replaces_the_mention_with_the_stored_value(runtime: Runtime) -> None:
    first = runtime.agent.run(COLLISION)
    second = runtime.agent.run("2", history=[first.to_turn()])
    assert second.status == "ok"
    assert "苏州精工自动化有限公司" in second.effective_question
    assert "精工的销售" not in second.effective_question


def test_a_stored_value_that_equals_the_short_name_does_not_loop(runtime: Runtime) -> None:
    # 轴承 is a material literally named 轴承: replacing it changes nothing, so the
    # pick has to travel as the category word or turn two asks the same question back.
    first = runtime.agent.run(SELF_SAME_COLLISION)
    assert first.clarification.code == CODE_ENTITY
    second = runtime.agent.run("2", history=[first.to_turn()])
    assert second.effective_question == "物料轴承的可用库存是多少？"
    assert second.status == "ok"
    assert (second.clarification or None) is None


def test_the_other_reading_of_the_same_name_resolves_by_replacement(runtime: Runtime) -> None:
    first = runtime.agent.run(SELF_SAME_COLLISION)
    second = runtime.agent.run("1", history=[first.to_turn()])
    assert "宁波轴承集团" in second.effective_question
    assert second.status == "ok"


def test_a_working_answer_keeps_its_answer_and_adds_the_narrower_questions(
    runtime: Runtime,
) -> None:
    result = runtime.agent.run(PERIODLESS)
    assert result.status == "ok"
    assert result.clarification is None
    assert [CODE_TIME] == result.clarify["codes"]
    assert len(result.alternatives) == 3
    # The narrower readings travel as a structured list, never glued onto the answer:
    # the Judge and the eval read ``answer``, and a number line with sentences appended
    # to it is a number line they can no longer verify.
    assert "上月（2026年6月）销售额是多少？" in result.alternatives
    assert "你可以直接这样问" not in result.answer


@pytest.mark.parametrize("question", [NARROWED, "库存低于安全库存的物料有哪些？"])
def test_a_fully_stated_question_is_not_touched(runtime: Runtime, question: str) -> None:
    result = runtime.agent.run(question)
    assert result.status == "ok"
    assert result.alternatives == []
    assert result.clarify is None
    assert "你可以直接这样问" not in result.answer


def test_off_mode_leaves_the_answers_exactly_as_they_were(runtime: Runtime) -> None:
    quiet = agent_in(runtime, "off")
    for question in (VAGUE, PERIODLESS, NARROWED):
        before = runtime.agent.run(question)
        after = quiet.agent.run(question)
        assert before.status == after.status
        assert before.rows == after.rows
        assert after.alternatives == []
        assert after.clarify is None


def test_the_default_mode_never_blocks_a_question_that_has_an_answer(
    runtime: Runtime,
) -> None:
    assert runtime.agent.run(PERIODLESS).status == "ok"
    assert runtime.agent.run(VAGUE).clarification.tier == "assume"


def test_an_out_of_scope_metric_gets_examples_instead_of_a_shrug(runtime: Runtime) -> None:
    result = runtime.agent.run("各客户的毛利率是多少？")
    assert result.status == "needs_clarification"
    assert result.clarification.code == CODE_SCHEMA
    assert result.clarification.actionable
    assert len(result.clarification.options) == 3


def test_kind_word_reads_the_dictionary_not_a_hard_coded_table(runtime: Runtime) -> None:
    aligner = runtime.agent.aligner
    assert _kind_word(aligner, "material.material_name") == "物料"
    assert _kind_word(aligner, "sales_order.customer_name") == "客户"
    assert _kind_word(aligner, "no_such.column") == ""
    assert _kind_word(None, "material.material_name") == ""


def test_result_payload_carries_the_plan_for_the_web_layer(runtime: Runtime) -> None:
    payload = runtime.agent.run(PERIODLESS).to_dict()
    assert payload["alternatives"] and payload["clarify"]["mode"] == "options"
    assert payload["clarification"] is None


def test_a_blank_settings_mode_falls_back_to_options() -> None:
    assert Settings(provider="mock").clarify == "options"
    assert create_runtime(Settings(provider="mock", clarify="")).agent.clarifier.mode == "options"
