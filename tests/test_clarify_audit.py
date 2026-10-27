"""Tests for the clarification audit (``nl2sql-agent clarify-check``).

The corpus is assistant-authored, so these tests gate invariants — an ``ask``-tier
trigger must not come from the two 口径 codes, a clarification must be pickable, an
option must be sendable back — and never the counts for their own sake. The frozen-set
numbers are pinned because a change there means the tier split moved.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from typing import Any

import pytest

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.clarify import (
    CODE_ENTITY,
    CODE_METRIC,
    CODE_TIME,
    MODE_ASK,
    MODE_OFF,
    MODE_OPTIONS,
    ClarificationPolicy,
    MetricGlossary,
)
from nl2sql_agent.clarify_audit import (
    CLARIFY_CASES,
    KINDS,
    ClarifyRow,
    bare_clarification_count,
    corpus_findings,
    frozen_footprint,
    measure_rows,
    observations,
    paired_ab,
    plan_of,
    run_clarify_audit,
    validate_cases,
)
from nl2sql_agent.cli import main
from nl2sql_agent.config import Settings
from nl2sql_agent.guard_suite import load_case_file
from nl2sql_agent.runtime import Runtime, create_runtime

VAGUE = "库存有多少？"
PERIODLESS = "销售额是多少？"
NARROWED = "2026年6月华东地区销售订单总金额是多少？"


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    return load_case_file(CLARIFY_CASES)


@pytest.fixture(scope="module")
def frozen() -> list[dict[str, Any]]:
    return load_case_file(Settings(provider="mock").eval_cases_path)


@pytest.fixture(scope="module")
def glossary() -> MetricGlossary:
    return MetricGlossary.default()


@pytest.fixture(scope="module")
def audit_runtime(tmp_path_factory) -> Runtime:
    return create_runtime(Settings(home=tmp_path_factory.mktemp("clarify_audit"), provider="mock"))


@pytest.fixture(scope="module")
def measured(audit_runtime: Runtime, cases: list[dict[str, Any]], glossary: MetricGlossary):
    """One full pass over the packaged corpus, shared by the corpus tests."""

    policy = ClarificationPolicy(
        glossary, as_of=audit_runtime.settings.demo_today, mode=MODE_OPTIONS
    )
    return measure_rows(audit_runtime.agent, policy, cases)


def footprint(frozen: list[dict[str, Any]], glossary: MetricGlossary) -> dict[str, Any]:
    return frozen_footprint(frozen, Settings(provider="mock"), glossary, None)


def test_the_packaged_corpus_is_well_formed(cases: list[dict[str, Any]]) -> None:
    validate_cases(cases)

    assert {kind: sum(1 for case in cases if case["kind"] == kind) for kind in KINDS} == {
        "trigger": 8,
        "quiet": 5,
        "enrich": 3,
        "followup": 6,
    }
    assert len({case["id"] for case in cases}) == len(cases)
    # Four followups re-ask a trigger case's question on purpose: the second turn is
    # what is under test, so the first turn has to be a known-vague one.
    assert len({case["question"] for case in cases}) == len(cases) - 4


@pytest.mark.parametrize(
    "patch,message",
    [
        ({"id": ""}, "缺少 id"),
        ({"question": ""}, "缺少 question"),
        ({"kind": "vague"}, "kind 未知"),
        ({"expect_codes": "metric_ambiguity"}, "必须是数组"),
        ({"expect_codes": ["no_such_code"]}, "未知触发码"),
        ({"kind": "followup", "reply": ""}, "缺少 reply"),
    ],
)
def test_a_malformed_case_refuses_the_whole_run(
    cases: list[dict[str, Any]], patch: dict[str, Any], message: str
) -> None:
    broken = [dict(case) for case in cases]
    broken[0].update({"id": "x", "kind": "trigger", "reply": "1", **patch})
    with pytest.raises(ValueError, match=message):
        validate_cases(broken)


def test_a_duplicated_id_refuses_the_whole_run(cases: list[dict[str, Any]]) -> None:
    broken = [dict(case) for case in cases]
    broken[1]["id"] = broken[0]["id"]
    with pytest.raises(ValueError, match="重复"):
        validate_cases(broken)


def test_the_shipped_mode_blocks_no_frozen_question(
    frozen: list[dict[str, Any]], glossary: MetricGlossary
) -> None:
    """The tier split exists for exactly this number: a 口径 gap may not eat a gold."""

    report = footprint(frozen, glossary)

    assert report["cases"] == 123
    assert report["blocking_options"] == []
    assert (report["metric"], report["time"], report["quiet"]) == (10, 39, 80)
    assert report["options_offered"] == 147


def test_the_ask_mode_costs_four_answerable_frozen_questions(
    frozen: list[dict[str, Any]], glossary: MetricGlossary
) -> None:
    report = footprint(frozen, glossary)

    assert report["blocking_ask"] == 4
    assert report["blocking_ask_ids"] == [
        "d_inventory_m005",
        "i_locked_share",
        "t_due_july",
        "t_last30_days",
    ]
    assert report["entity_blocking"] == []


def test_the_two_policy_codes_block_only_in_ask_mode(glossary: MetricGlossary) -> None:
    settings = Settings(provider="mock")
    options = ClarificationPolicy(glossary, as_of=settings.demo_today, mode=MODE_OPTIONS)
    ask = ClarificationPolicy(glossary, as_of=settings.demo_today, mode=MODE_ASK)

    plan = plan_of(options, VAGUE)
    assert plan["codes"] == [CODE_METRIC] and plan["blocking"] == []
    assert len(plan["alternatives"]) == 3
    assert plan_of(ask, VAGUE)["blocking"] == [CODE_METRIC]
    # A question that already names a metric did answer the 口径 question, so even the
    # blocking tier has nothing to ask it.
    assert plan_of(ask, "各分类库存金额排名")["blocking"] == []
    assert plan_of(options, NARROWED)["codes"] == []


def test_period_options_carry_an_absolute_month_the_catalog_can_resolve(
    glossary: MetricGlossary,
) -> None:
    settings = Settings(provider="mock")
    plan = plan_of(
        ClarificationPolicy(glossary, as_of=settings.demo_today, mode=MODE_OPTIONS), PERIODLESS
    )

    assert [item[:2] for item in plan["alternatives"]] == ["本月", "上月", "上上"]
    assert all("2026年" in item for item in plan["alternatives"])


def test_the_corpus_produces_no_bare_clarification(measured) -> None:
    assert bare_clarification_count(measured) == 0
    assert [row.id for row in measured if row.actionable == "no"] == []


def test_every_option_and_every_second_turn_works(measured) -> None:
    assert sum(row.reask_total for row in measured) == 20
    assert [row.id for row in measured if row.reask_failed] == []

    followups = [row for row in measured if row.kind == "followup"]
    assert len(followups) == 6
    assert [row.id for row in followups if row.rewrite_source != "clarify_reply"] == []
    assert [row.id for row in followups if row.final_status != "ok"] == []
    assert all(row.expect_effective in row.resolved_question for row in followups)
    assert corpus_findings(measured) == {}


def test_an_assume_tier_trigger_that_blocks_is_a_finding() -> None:
    row = ClarifyRow(id="x", kind="trigger", question=VAGUE, plan_blocking=[CODE_METRIC])

    assert corpus_findings([row]) == {"assume_tier_blocking": ["x"]}


def test_a_clarification_without_options_or_slots_is_a_finding() -> None:
    row = ClarifyRow(
        id="x", kind="trigger", question=VAGUE, status="needs_clarification", actionable="no"
    )

    assert corpus_findings([row])["unactionable_clarification"] == ["x"]


def test_a_second_turn_asking_the_same_entity_again_is_a_finding() -> None:
    row = ClarifyRow(
        id="x",
        kind="followup",
        question="精工的销售订单总金额是多少？",
        reply="1",
        code=CODE_ENTITY,
        rewrite_source="clarify_reply",
        final_code=CODE_ENTITY,
        final_status="needs_clarification",
        expect_final_status="ok",
    )

    findings = corpus_findings([row])
    assert findings["clarification_loop"] == ["x"]
    assert findings["followup_not_answered"] == ["x"]


def test_an_option_that_is_not_a_question_is_a_finding() -> None:
    row = ClarifyRow(
        id="x",
        kind="trigger",
        question=VAGUE,
        reask=True,
        reask_total=3,
        reask_ok=2,
        reask_failed=["轴承（needs_human）"],
    )

    assert corpus_findings([row])["option_not_sendable"] == ["x"]


def test_a_period_option_that_changes_nothing_is_an_observation_not_a_finding() -> None:
    # 本月 and 上月 returning the same rows is a fact about the seed, not a broken
    # policy; gating it would let a data refresh fail CI for the wrong reason.
    row = ClarifyRow(
        id="x",
        kind="trigger",
        question=PERIODLESS,
        plan_codes=[CODE_TIME],
        reask=True,
        reask_total=3,
        reask_ok=3,
        reask_distinct=1,
    )

    assert corpus_findings([row]) == {}
    assert observations([row]) == {"option_period_inert": ["x（1 种结果）"]}


def test_a_quiet_case_that_loses_its_answer_is_an_observation() -> None:
    row = ClarifyRow(id="x", kind="quiet", question=NARROWED, status="needs_human")

    assert observations([row])["quiet_not_answered"] == ["x（needs_human）"]
    assert corpus_findings([row]) == {}


def test_paired_ab_trades_bare_clarifications_for_pickable_options(
    audit_runtime: Runtime, cases: list[dict[str, Any]], glossary: MetricGlossary
) -> None:
    ab = paired_ab(audit_runtime.settings, cases, glossary)

    assert ab["measured"] is True
    assert ab["asked"] == {MODE_OFF: 13, MODE_OPTIONS: 13}
    assert ab["bare"][MODE_OFF] == 9 and ab["bare"][MODE_OPTIONS] == 0
    assert ab["actionable"][MODE_OFF] < ab["actionable"][MODE_OPTIONS]
    assert ab["options_offered"][MODE_OFF] < ab["options_offered"][MODE_OPTIONS]
    assert ab["regressed"] == []


def test_the_audit_measures_the_packaged_corpus_end_to_end(
    audit_runtime: Runtime, catalog: Catalog, cases: list[dict[str, Any]], frozen: list[dict[str, Any]]
) -> None:
    report = run_clarify_audit(audit_runtime.settings, catalog, cases, frozen, with_ab=False)

    assert report["findings"] == {}
    assert report["policy"]["mode"] == MODE_OPTIONS
    assert report["catalog"] == {
        "dialect": catalog.dialect,
        "tables": len(catalog.tables),
        "backend": "sqlite",
        "schema": str(Settings(provider="mock").schema_path),
        "packaged_demo": True,
    }
    assert report["ask_quality"]["clarified"] == 13
    assert report["ask_quality"]["bare"] == 0
    assert report["followup"] == {"cases": 6, "resolved": 6, "answered": 6, "looped": 0}
    assert report["ab"] == {"measured": False}
    assert json.dumps(report, ensure_ascii=False)


def test_a_catalog_that_is_not_the_packaged_demo_is_flagged(
    audit_runtime: Runtime, catalog: Catalog, frozen: list[dict[str, Any]], tmp_path
) -> None:
    other = tmp_path / "real_schema.json"
    shutil.copyfile(Settings(provider="mock").schema_path, other)

    report = run_clarify_audit(
        replace(audit_runtime.settings, schema_override=other), catalog, [], frozen, with_ab=False
    )

    assert report["catalog"]["packaged_demo"] is False
    assert report["cases"] == 0


def test_cli_clarify_check_passes_the_invariants(tmp_path, capsys) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    capsys.readouterr()

    assert main(["clarify-check", "--home", str(tmp_path), "--no-ab"]) == 0
    out = capsys.readouterr().out
    assert "目录  backend=sqlite" in out
    assert "不变量  全部通过" in out
    assert "配对 A/B  本次未测量" in out


def test_cli_clarify_check_refuses_the_packaged_corpus_on_a_real_catalog(
    tmp_path, monkeypatch, capsys
) -> None:
    # The corpus, the noun table and every expected value are written against the demo
    # seed. On a deployment catalog the reask fails because the tables differ, and the
    # findings would read like a policy bug.
    other = tmp_path / "erp_schema.json"
    shutil.copyfile(Settings(provider="mock").schema_path, other)
    monkeypatch.setenv("NL2SQL_SCHEMA_PATH", str(other))
    assert main(["init", "--home", str(tmp_path)]) == 0
    capsys.readouterr()

    assert main(["clarify-check", "--home", str(tmp_path), "--no-ab"]) == 2
    assert "目录与语料不匹配" in capsys.readouterr().err


def test_cli_clarify_check_accepts_an_operator_supplied_corpus(tmp_path, monkeypatch, capsys) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    other = tmp_path / "erp_schema.json"
    shutil.copyfile(Settings(provider="mock").schema_path, other)
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {
                "id": "cli_quiet",
                "kind": "quiet",
                "question": NARROWED,
                "expect_codes": [],
                "expect_final_status": "ok",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NL2SQL_SCHEMA_PATH", str(other))
    capsys.readouterr()

    assert main(["clarify-check", "--home", str(tmp_path), "--cases", str(cases), "--no-ab"]) == 0
    out = capsys.readouterr().out
    assert "⚠ 非打包演示库" in out
    assert "不变量  全部通过" in out


def test_cli_clarify_check_fails_when_a_policy_code_blocks(tmp_path, capsys) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {"id": "cli_quiet", "kind": "quiet", "question": VAGUE, "expect_codes": []},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    capsys.readouterr()

    assert main(["clarify-check", "--home", str(tmp_path), "--cases", str(cases), "--no-ab"]) == 1
    assert "fired_when_quiet" in capsys.readouterr().err
