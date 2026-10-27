"""Tests for the clarification policy (P1-6)."""

from __future__ import annotations

import pytest

from nl2sql_agent.clarify import (
    ASK_CODES,
    CODE_ENTITY,
    CODE_METRIC,
    CODE_MODEL,
    CODE_PERMISSION,
    CODE_SCHEMA,
    CODE_TIME,
    MODE_ASK,
    MODE_OFF,
    MODE_OPTIONS,
    Clarification,
    ClarificationPolicy,
    MetricCandidate,
    MetricGlossary,
    MetricTerm,
    Trigger,
    clarification_text,
    resolve_clarification_reply,
    with_options,
)

VAGUE = "库存有多少？"
NARROWED = "库存低于安全库存的物料的清单"
PERIODLESS = "销售额是多少？"
DATED = "2026年6月销售额是多少？"


def policy(mode: str = MODE_OPTIONS, **kwargs: object) -> ClarificationPolicy:
    return ClarificationPolicy(as_of="2026-07-01", mode=mode, **kwargs)  # type: ignore[arg-type]


def test_a_noun_with_several_readings_is_reported_with_every_reading() -> None:
    plan = policy().decide(VAGUE)
    assert plan.codes == [CODE_METRIC]
    assert len(plan.alternatives) == 3
    assert plan.assumed and not plan.blocking


def test_the_question_narrowing_the_noun_itself_is_left_alone() -> None:
    assert policy().decide(NARROWED).codes == []


def test_a_period_metric_without_a_period_offers_periods_not_a_question() -> None:
    plan = policy().decide(PERIODLESS)
    assert plan.codes == [CODE_TIME]
    assert [item.assumed for item in plan.assumed] == ["全部时间"]
    assert all("？" in option for option in plan.alternatives)


@pytest.mark.parametrize(
    "question",
    [DATED, "最近三个月的销售额是多少？", "各状态订单数量统计（按季度）"],
)
def test_any_stated_period_counts(question: str) -> None:
    assert policy().decide(question).codes == []


def test_period_options_carry_the_absolute_month_the_catalog_can_resolve() -> None:
    periods = policy().periods()
    assert [phrase for phrase, _ in periods] == ["本月", "上月", "上上月"]
    assert [literal for _, literal in periods] == ["2026年7月", "2026年6月", "2026年5月"]
    options = policy().period_options(PERIODLESS)
    assert options[1] == "上月（2026年6月）销售额是多少？"


def test_max_options_caps_both_layers() -> None:
    plan = policy(max_options=1).decide(VAGUE)
    assert len(plan.alternatives) == 1


def test_the_default_mode_never_blocks_on_a_口径_gap() -> None:
    for question in (VAGUE, PERIODLESS, "订单情况怎么样？"):
        plan = policy().decide(question)
        assert plan.blocking == []


def test_ask_mode_promotes_only_an_unmeasured_noun() -> None:
    assert policy(MODE_ASK).decide(VAGUE).blocking[0].code == CODE_METRIC
    # 期间缺失在 ask 档仍然只是假设：全库合计是一个真实答案，不是一个猜测。
    assert policy(MODE_ASK).decide(PERIODLESS).blocking == []


def test_ask_mode_keeps_a_stated_metric_answerable() -> None:
    assert policy(MODE_ASK).decide("库存金额排名").blocking == []


def test_off_disables_the_layer_entirely() -> None:
    quiet = policy(MODE_OFF)
    assert quiet.decide(VAGUE).codes == []
    assert quiet.enabled is False
    clarification, alternatives = quiet.enrich(None, question=VAGUE)
    assert alternatives == [] and clarification is None


def test_an_unknown_mode_is_a_config_error() -> None:
    with pytest.raises(ValueError):
        ClarificationPolicy(mode="sometimes")


def test_scope_trigger_refuses_an_assume_code() -> None:
    with pytest.raises(ValueError):
        policy().scope_trigger(CODE_TIME, reason="x")


def test_a_bare_clarification_comes_back_actionable() -> None:
    bare = Clarification(reason="请补充必要的条件后重新提问。", question="请补充必要的条件后重新提问。")
    enriched, _ = policy().enrich(bare, question="各客户的毛利率是多少？")
    assert enriched is not None
    # Nothing in the text names a catalog gap, so the honest label is the fallback —
    # but the user still gets questions they can send.
    assert enriched.code == CODE_MODEL
    assert enriched.actionable and len(enriched.options) == 3


def test_a_reason_that_blames_the_catalog_is_labelled_schema_gap() -> None:
    reason = "当前演示库只覆盖订单、库存等业务指标，请补充或换一个可查询的指标。"
    enriched, _ = policy().enrich(Clarification(reason=reason), question="各客户的毛利率是多少？")
    assert enriched is not None and enriched.code == CODE_SCHEMA


def test_the_plan_labels_before_the_reason_text_guesses() -> None:
    # The model blamed the catalog, but the question's problem is that 库存 has three
    # readings — and the options handed back are those three readings.
    plan = policy().decide(VAGUE)
    enriched, _ = policy().enrich(
        Clarification(reason="当前演示库没有匹配到支持的业务指标"), question=VAGUE, plan=plan
    )
    assert enriched is not None
    assert enriched.code == CODE_METRIC
    assert enriched.options == plan.alternatives


def test_a_guard_permission_code_outranks_everything_else() -> None:
    enriched, _ = policy().enrich(
        Clarification(reason="请补充条件"), question=VAGUE, guard_codes={"permission_table"}
    )
    assert enriched is not None and enriched.code == CODE_PERMISSION


def test_a_code_set_upstream_is_not_rewritten() -> None:
    entity = Clarification(reason="简称对应多个取值", code=CODE_ENTITY, tier="ask")
    enriched, _ = policy().enrich(entity, question="精工的订单情况怎么样？")
    assert enriched is not None and enriched.code == CODE_ENTITY


def test_tier_follows_the_code() -> None:
    assert policy().enrich(Clarification(reason="x"), question=VAGUE)[0].tier == "assume"
    assert (
        policy().enrich(Clarification(reason="没有覆盖这个指标"), question="毛利率呢")[0].tier == "ask"
    )
    assert set(ASK_CODES) == {CODE_ENTITY, CODE_PERMISSION, CODE_SCHEMA}


def test_options_and_missing_slots_are_both_actionable() -> None:
    assert Clarification(reason="x", options=["a"]).actionable
    assert Clarification(reason="x", missing_slots=["month"]).actionable
    assert not Clarification(reason="x").actionable


def test_with_options_appends_once_and_never_eats_the_original_reason() -> None:
    text = with_options("这个指标有两种口径。", ["按金额", "按数量"])
    assert text.startswith("这个指标有两种口径。")
    assert "1. 按金额" in text and "2. 按数量" in text
    assert with_options(text, ["按金额", "按数量"]) == text
    assert with_options("不需要确认。", []) == "不需要确认。"


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("1", "库存低于安全库存的物料有哪些？"),
        ("我选 2", "各分类库存金额排名"),
        ("第三个", "整机设备的可用库存是多少？"),
        ("可用", ""),
        ("库存金额", "各分类库存金额排名"),
        ("5", ""),
        ("", ""),
    ],
)
def test_reply_resolution(reply: str, expected: str) -> None:
    options = policy().decide(VAGUE).alternatives
    assert resolve_clarification_reply(reply, options) == expected


def test_an_ambiguous_fragment_resolves_to_nothing() -> None:
    options = ["库存低于安全库存的物料有哪些？", "各分类库存金额排名"]
    assert resolve_clarification_reply("库存", options) == ""


def test_resolution_without_options_is_still_no_resolution() -> None:
    assert resolve_clarification_reply("1", []) == ""


def test_rendered_text_always_tells_the_user_what_to_do() -> None:
    picked = clarification_text(Trigger(code=CODE_METRIC, tier="assume", reason="r", options=["甲"]).as_clarification())
    assert "你可以直接这样问" in picked and "甲" in picked
    slots = clarification_text(Clarification(reason="r", missing_slots=["month"]))
    assert "还需要补充：month" in slots
    shrug = clarification_text(Clarification(reason="r"))
    assert "请补充问题中的具体条件" in shrug


def test_a_clarification_with_neither_options_nor_slots_would_be_reported() -> None:
    plan = policy().decide(VAGUE)
    assert plan.to_dict()["blocking"] == []
    assert plan.to_dict()["assume"] == [CODE_METRIC]


def test_glossary_rejects_a_table_that_cannot_produce_a_choice() -> None:
    with pytest.raises(ValueError):
        MetricGlossary([])
    with pytest.raises(ValueError):
        MetricGlossary([MetricTerm("库存", (MetricCandidate("safety_stock", "问句"),))])
    with pytest.raises(ValueError):
        MetricGlossary(
            [
                MetricTerm(
                    "库存",
                    (
                        MetricCandidate("same", "问句甲"),
                        MetricCandidate("same", "问句乙"),
                    ),
                )
            ]
        )
    with pytest.raises(ValueError):
        MetricGlossary(
            [MetricTerm("库存", (MetricCandidate("a", "  "), MetricCandidate("b", "问句")))]
        )


def test_glossary_round_trips_through_a_dictionary() -> None:
    raw = {"terms": [{"term": "库存", "candidates": [
        {"metric": "a", "question": "甲？", "qualifiers": ["安全"]},
        {"metric": "b", "question": "乙？"},
    ]}]}
    loaded = MetricGlossary.from_dict(raw, source="test")
    assert loaded.stats() == {"terms": ["库存"], "candidates": 2}
    assert loaded.unresolved("库存有多少？") and not loaded.unresolved("库存安全吗？")


def test_the_packaged_noun_table_is_the_one_the_rewriter_uses() -> None:
    from nl2sql_agent.rewriter import AMBIGUOUS_NOUNS

    glossary = MetricGlossary.default()
    assert set(AMBIGUOUS_NOUNS) == set(glossary.ambiguous_nouns)
    assert CODE_MODEL not in ASK_CODES


def test_a_model_explained_gap_is_not_decorated_with_canned_examples() -> None:
    """The model named the missing value; demo-shaped picks contradict it."""
    policy = ClarificationPolicy(MetricGlossary.default(), as_of="2026-07-01")
    reason = "「华东」在 Area 与 Province 两列都对不上，需要给出确切的区域编码"
    clarified, _ = policy.enrich(
        Clarification(reason=reason, question=reason, code=CODE_MODEL),
        question="只看华东地区的客户",
        keep_open=True,
    )
    assert clarified.code == CODE_MODEL
    assert clarified.options == []


def test_an_ambiguous_noun_still_gets_askable_examples() -> None:
    policy = ClarificationPolicy(MetricGlossary.default(), as_of="2026-07-01")
    clarified, _ = policy.enrich(
        Clarification(reason="", question="", code=CODE_METRIC),
        question="库存情况怎么样？",
    )
    assert clarified.options
