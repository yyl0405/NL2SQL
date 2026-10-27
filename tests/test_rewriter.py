from __future__ import annotations

import json

import pytest

from nl2sql_agent.conversation import Conversation, Turn, extract_slots
from nl2sql_agent.llm import LLMError, LLMResponse
from nl2sql_agent.rewriter import (
    LLMQuestionRewriter,
    RuleBasedRewriter,
)


def session(*questions: str) -> Conversation:
    """A conversation whose turns were answered on their own wording."""
    conversation = Conversation()
    for question in questions:
        conversation.add(
            Turn(
                question=question,
                effective_question=question,
                status="ok",
                slots=extract_slots(question),
                answer="查询结果：1.0",
                sql="SELECT 1",
            )
        )
    return conversation


REWRITER = RuleBasedRewriter()


@pytest.mark.parametrize(
    ("prior", "follow_up", "resolved"),
    [
        (
            "2026年6月华东地区销售订单总金额是多少？",
            "那华南呢？",
            "2026年6月华南地区销售订单总金额是多少？",
        ),
        (
            "2026年6月华东地区销售订单总金额是多少？",
            "2026年5月呢？",
            "2026年5月华东地区销售订单总金额是多少？",
        ),
        (
            "2026年6月华东地区销售订单总金额是多少？",
            "那5月华南呢？",
            "2026年5月华南地区销售订单总金额是多少？",
        ),
        (
            "2026年6月各地区销售金额排名",
            "按客户拆开看排名前3",
            "2026年6月销售额最高的前3个客户是谁？",
        ),
        ("销售额最高的前3个客户是谁？", "那前5个呢？", "销售额最高的前5个客户是谁？"),
        (
            "整机设备的BOM所有子件及单台总用量是多少？",
            "换成伺服电机",
            "伺服电机的BOM所有子件及单台总用量是多少？",
        ),
        (
            "订单ORD-1005的物料齐套率是多少？",
            "那ORD-1002呢？",
            "订单ORD-1002的物料齐套率是多少？",
        ),
        (
            "截至2026年7月1日仍未发货的订单有哪些？",
            "截至2026年6月15日呢？",
            "截至2026年6月15日仍未发货的订单有哪些？",
        ),
        (
            "截至2026-06-15仍未发货的订单有哪些？",
            "截至2026年7月1日呢？",
            "截至2026-07-01仍未发货的订单有哪些？",
        ),
    ],
)
def test_rule_rewriter_resolves_follow_ups(prior: str, follow_up: str, resolved: str) -> None:
    rewrite = REWRITER.rewrite(follow_up, session(prior))
    assert rewrite.source == "rule"
    assert rewrite.ambiguous is False
    assert rewrite.question == resolved


@pytest.mark.parametrize(
    ("prior", "follow_up"),
    [
        ("2026年6月华东地区销售订单总金额是多少？", "库存低于安全库存的物料有哪些？"),
        ("2026年6月华东地区销售订单总金额是多少？", "订单ORD-1012的物料齐套率是多少？"),
    ],
)
def test_standalone_follow_up_is_not_rewritten(prior: str, follow_up: str) -> None:
    rewrite = REWRITER.rewrite(follow_up, session(prior))
    assert rewrite.question == follow_up
    assert rewrite.inherited == []


def test_first_turn_needs_no_context() -> None:
    assert REWRITER.rewrite("那华南呢？").question == "那华南呢？"
    assert REWRITER.rewrite("那华南呢？", session()).question == "那华南呢？"


def test_ambiguous_noun_becomes_clarification_with_options() -> None:
    rewrite = REWRITER.rewrite(
        "那库存情况怎么样？", session("2026年6月华东地区销售订单总金额是多少？")
    )
    assert rewrite.ambiguous is True
    assert rewrite.question == "那库存情况怎么样？"
    assert len(rewrite.options) == 3
    assert all(option != "那库存情况怎么样？" for option in rewrite.options)


def test_metric_that_needs_a_missing_slot_clarifies_without_fake_options() -> None:
    rewrite = REWRITER.rewrite("那齐套率呢？", session("库存低于安全库存的物料有哪些？"))
    assert rewrite.ambiguous is True
    assert rewrite.missing == ["order_id"]
    assert "订单号" in rewrite.reason
    assert rewrite.options == []


def test_unresolvable_value_change_clarifies_instead_of_repeating() -> None:
    rewrite = REWRITER.rewrite("那华南呢？", session("2026年6月各地区销售金额排名"))
    assert rewrite.ambiguous is True
    assert rewrite.question == "那华南呢？"
    for option in rewrite.options:
        assert option != "2026年6月各地区销售金额排名"


@pytest.mark.parametrize(
    ("prior", "follow_up", "resolved"),
    [
        ("今年签订的销售合同总金额是多少？", "那去年的呢？", "去年签订的销售合同总金额是多少？"),
        ("去年签订的销售合同总金额是多少？", "那今年呢？", "今年签订的销售合同总金额是多少？"),
        ("各客户的合同金额排名，取前10名", "那去年呢？", "去年各客户的合同金额排名，取前10名"),
    ],
)
def test_relative_time_follow_up_swaps_only_the_time_phrase(
    prior: str, follow_up: str, resolved: str
) -> None:
    """The slot model reads absolute dates; 「去年」 must still resolve.

    A relative time word is the one change the slot diff cannot see, so without
    a textual swap the turn looks unchanged and used to fall through to the
    question templates.
    """
    rewrite = REWRITER.rewrite(follow_up, session(prior))
    assert rewrite.question == resolved
    assert rewrite.ambiguous is False


def test_templates_never_retalk_a_question_they_cannot_reproduce() -> None:
    rewrite = REWRITER.rewrite("那华南呢？", session("今年签订的销售合同总金额是多少？"))
    assert "销售订单" not in rewrite.question
    assert all("销售订单" not in option for option in rewrite.options)
    assert rewrite.options == ["今年签订的销售合同总金额是多少？"]


def test_demo_shaped_prior_still_gets_templated_options() -> None:
    rewrite = REWRITER.rewrite("那华南呢？", session("2026年6月各地区销售金额排名"))
    assert rewrite.options and all("销售" in option for option in rewrite.options)


class FakeLLM:
    def __init__(self, payload: str = "", *, error: str = "") -> None:
        self.payload = payload
        self.error = error
        self.calls = 0
        self.messages: list[list[dict[str, object]]] = []

    def chat(self, messages, tools=None) -> LLMResponse:
        self.calls += 1
        self.messages.append(list(messages))
        if self.error:
            raise LLMError(self.error)
        return LLMResponse(content=self.payload)


def _json(question: str, **extra: object) -> str:
    return json.dumps({"question": question, **extra}, ensure_ascii=False)


def test_llm_rewriter_uses_valid_model_output() -> None:
    llm = FakeLLM(_json("2026年6月华南地区销售订单总金额是多少？", inherited_slots=["month"]))
    rewriter = LLMQuestionRewriter(llm)
    rewrite = rewriter.rewrite("那华南呢？", session("2026年6月华东地区销售订单总金额是多少？"))
    assert rewrite.source == "llm"
    assert rewrite.question == "2026年6月华南地区销售订单总金额是多少？"
    assert rewrite.inherited == ["month"]
    assert "[rewrite-v1]" in llm.messages[0][0]["content"]
    assert "2026年6月华东地区销售订单总金额是多少？" in llm.messages[0][1]["content"]


def test_llm_rewriter_rejects_sql_and_falls_back_to_rule() -> None:
    llm = FakeLLM(_json("SELECT region FROM sales_order WHERE region = '华南'"))
    rewrite = LLMQuestionRewriter(llm).rewrite(
        "那华南呢？", session("2026年6月华东地区销售订单总金额是多少？")
    )
    assert rewrite.source == "llm-invalid"
    assert rewrite.question == "2026年6月华南地区销售订单总金额是多少？"


def test_llm_rewriter_rejects_invented_entity_and_falls_back() -> None:
    llm = FakeLLM(_json("2026年6月华北地区销售订单总金额是多少？"))
    rewrite = LLMQuestionRewriter(llm).rewrite(
        "那华南呢？", session("2026年6月华东地区销售订单总金额是多少？")
    )
    assert rewrite.source == "llm-invalid"
    assert "华北" not in rewrite.question
    assert rewrite.question == "2026年6月华南地区销售订单总金额是多少？"


def test_llm_rewriter_rejects_invented_order_id() -> None:
    llm = FakeLLM(_json("订单ORD-9999的物料齐套率是多少？"))
    rewrite = LLMQuestionRewriter(llm).rewrite(
        "那ORD-1002呢？", session("订单ORD-1005的物料齐套率是多少？")
    )
    assert rewrite.source == "llm-invalid"
    assert rewrite.question == "订单ORD-1002的物料齐套率是多少？"


def test_llm_rewriter_survives_model_error() -> None:
    llm = FakeLLM(error="connection reset")
    rewrite = LLMQuestionRewriter(llm).rewrite(
        "那华南呢？", session("2026年6月华东地区销售订单总金额是多少？")
    )
    assert rewrite.source == "llm-error"
    assert rewrite.question == "2026年6月华南地区销售订单总金额是多少？"


def test_llm_rewriter_keeps_model_clarification() -> None:
    llm = FakeLLM(
        _json(
            "那库存情况怎么样？",
            ambiguous=True,
            reason="库存口径不止一种",
            options=["库存低于安全库存的物料有哪些？"],
        )
    )
    rewrite = LLMQuestionRewriter(llm).rewrite(
        "那库存情况怎么样？", session("2026年6月华东地区销售订单总金额是多少？")
    )
    assert rewrite.source == "llm"
    assert rewrite.ambiguous is True
    assert rewrite.options == ["库存低于安全库存的物料有哪些？"]


def test_llm_rewriter_degrades_when_ambiguity_has_no_options() -> None:
    llm = FakeLLM(_json("那库存情况怎么样？", ambiguous=True, reason="口径不明"))
    rewrite = LLMQuestionRewriter(llm).rewrite(
        "那库存情况怎么样？", session("2026年6月华东地区销售订单总金额是多少？")
    )
    assert rewrite.source == "llm-invalid"


def test_llm_rewriter_does_not_call_the_model_without_history() -> None:
    llm = FakeLLM(_json("2026年6月华南销售额"))
    rewrite = LLMQuestionRewriter(llm).rewrite("那华南呢？")
    assert llm.calls == 0
    assert rewrite.source == "rule"
    assert rewrite.question == "那华南呢？"
