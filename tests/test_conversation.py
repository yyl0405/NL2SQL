from __future__ import annotations

from nl2sql_agent.conversation import (
    Conversation,
    Slots,
    Turn,
    as_turns,
    extract_slots,
)


def test_extract_slots_reads_every_follow_up_shape() -> None:
    slots = extract_slots("2026年6月华东地区销售额最高的前3个客户，按区域排名从高到低")
    assert slots.month == "2026-06"
    assert slots.region == "华东"
    assert slots.limit == 3
    assert slots.metric == "revenue"
    assert slots.direction == "desc"


def test_bare_month_inherits_year_from_hint() -> None:
    assert extract_slots("那5月呢？", year_hint="2026").month == "2026-05"
    assert extract_slots("那5月呢？").month == ""


def test_full_date_and_iso_date_land_in_the_same_slot() -> None:
    assert extract_slots("截至2026年6月15日仍未发货的订单").date == "2026-06-15"
    assert extract_slots("截至 2026-06-15 的逾期订单").date == "2026-06-15"


def test_filled_by_overrides_only_slots_the_new_turn_states() -> None:
    prior = Slots(region="华东", month="2026-06", metric="revenue")
    current = Slots(region="华南")
    merged = prior.filled_by(current)
    assert merged.region == "华南"
    assert merged.month == "2026-06"
    assert merged.metric == "revenue"
    assert prior.region == "华东"


def test_turn_resolved_prefers_the_effective_question() -> None:
    assert Turn(question="那华南呢？", effective_question="2026年6月华南销售额").resolved == (
        "2026年6月华南销售额"
    )
    assert Turn(question="独立问题").resolved == "独立问题"


def test_conversation_context_skips_unanswered_turns() -> None:
    conversation = Conversation()
    conversation.add(Turn(question="q1", status="ok", slots=Slots(region="华东", metric="revenue")))
    conversation.add(Turn(question="q2", status="needs_clarification", slots=Slots(region="华南")))
    assert conversation.pending_slots().region == "华东"
    assert len(conversation) == 2
    assert conversation.render() == "问：q1"


def test_as_turns_accepts_conversation_and_turn_list() -> None:
    turns = [Turn(question="q1", status="ok")]
    assert as_turns(None) == []
    assert as_turns(turns) == turns
    assert as_turns(Conversation(turns)) == turns
