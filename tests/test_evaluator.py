from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from nl2sql_agent.agent import AGENT_PROMPT_VERSION, NL2SQLAgent
from nl2sql_agent.cli import main
from nl2sql_agent.config import Settings
from nl2sql_agent.ast_guard import AstAnalyzer
from nl2sql_agent.evaluation import evaluate, load_cases
from nl2sql_agent.evaluation.evaluator import (
    FAILURE_LAYERS,
    GENERATION_SUBTYPES,
    EvalCase,
    attribute_failure,
    classify_generation_subtype,
    render_markdown,
    trace_stages,
)
from nl2sql_agent.guard import SQLGuard
from nl2sql_agent.guard_suite import load_principals
from nl2sql_agent.result_compare import describe_diff, rows_equal
from nl2sql_agent.runtime import Runtime
from nl2sql_agent.text import to_jsonable
from nl2sql_agent.voting import SecondOpinion


def test_rows_equal_ignores_order_by_default() -> None:
    assert rows_equal([[1, "a"], [2, "b"]], [[2, "b"], [1, "a"]])
    assert not rows_equal([[1], [2]], [[1]], order_sensitive=False)


def test_rows_equal_respects_order_sensitive_flag() -> None:
    assert not rows_equal([[1], [2]], [[2], [1]], order_sensitive=True)


def test_rows_equal_normalizes_numbers_and_strings() -> None:
    assert rows_equal([[62280.00004, " 华东 "]], [[62280.0, "华东"]])
    assert not rows_equal([[1]], [[2]])


def test_rows_equal_normalizes_decimal_from_sql_server() -> None:
    # pyodbc returns SQL Server DECIMAL as Decimal, not float. A ratio computed
    # with CAST has more decimals than the ROUND(..., 4) gold and is still the
    # same business answer.
    assert rows_equal([[Decimal("0.999300")]], [[Decimal("0.9992898096")]])
    assert rows_equal([[Decimal("23.830000")]], [[23.83]])
    assert not rows_equal([[Decimal("1.0001")]], [[Decimal("1.0002")]])


def test_to_jsonable_keeps_decimal_numeric() -> None:
    # str(Decimal("3754.00")) != str(Decimal("3754")); keeping the value numeric
    # lets the evaluator normalize both to the same answer.
    assert to_jsonable(Decimal("3754.00")) == 3754.0
    assert to_jsonable(Decimal("0.999300")) == 0.9993
    assert isinstance(to_jsonable(Decimal("1.5")), float)


def test_load_cases_contains_refusal() -> None:
    cases = load_cases(Settings().eval_cases_path)
    assert len(cases) == 123
    refusal = [case for case in cases if case.expect_refusal]
    assert len(refusal) == 10
    assert all(case.category == "scope" for case in refusal)
    assert all(case.gold_sql for case in cases if not case.expect_refusal)
    assert all(case.expected_tables for case in cases if not case.expect_refusal)
    assert all(case.difficulty in {"easy", "medium", "hard"} for case in cases)
    assert all(case.status == "active" for case in cases)


def test_corpus_meets_the_expansion_targets() -> None:
    """P0-3 扩容验收：≥100 条、难例 ≥30%、每类 ≥10 条、v1 基线子集可用 tag 复原。"""
    cases = load_cases(Settings().eval_cases_path)
    assert len({case.id for case in cases}) == len(cases)
    assert len({case.question for case in cases}) == len(cases)
    hard = sum(1 for case in cases if case.difficulty == "hard")
    assert hard / len(cases) >= 0.3
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.category] = counts.get(case.category, 0) + 1
    assert min(counts.values()) >= 10
    assert sum(1 for case in cases if "v1_baseline" in case.tags) == 40


def test_eval_case_requires_gold_sql() -> None:
    with pytest.raises(ValueError, match="gold_sql"):
        EvalCase.from_dict({"id": "missing_gold", "question": "问题", "category": "metric"})


def test_draft_cases_are_skipped(runtime: Runtime) -> None:
    report = evaluate(
        runtime.agent,
        [
            {
                "id": "draft_case",
                "question": "2026年6月销售总额是多少？",
                "category": "metric",
                "gold_sql": "SELECT 1",
                "status": "draft",
            }
        ],
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=None,
    )
    assert report.total == 0
    assert report.skipped == 1


def test_refusal_metrics_use_explicit_flag(runtime: Runtime, tmp_path: Path) -> None:
    cases = [
        {
            "id": "refusal_custom_category",
            "question": "今天杭州天气怎么样？",
            "category": "out_of_scope",
            "expect_refusal": True,
        }
    ]
    report = evaluate(
        runtime.agent,
        cases,
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=None,
    )
    assert report.refusals == 1
    assert report.answerable == 0
    assert report.passed == 1
    assert report.metrics["refusal_accuracy"] == 1.0


def test_full_offline_evaluation(runtime: Runtime, tmp_path: Path) -> None:
    cases = [
        case
        for case in load_cases(runtime.settings.eval_cases_path)
        if "v1_baseline" in case.tags
    ]
    report = evaluate(
        runtime.agent,
        cases,
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=tmp_path / "reports",
    )
    assert report.total == 40
    assert report.answerable == 37
    assert report.refusals == 3
    assert report.skipped == 0
    assert report.passed == 40
    assert report.metrics["execution_accuracy"] == 1.0
    assert report.metrics["valid_sql_rate"] == 1.0
    assert report.metrics["refusal_accuracy"] == 1.0
    assert 0.0 <= report.metrics["table_recall_at_5"] <= 1.0
    assert report.metrics["avg_tokens"] == 0.0
    assert report.metrics["dual_channel_cases"] == 0.0
    assert report.metrics["dual_channel_agreement_rate"] == 0.0
    assert all(case.dual_channel_ran is False for case in report.cases)
    assert report.total_tokens == 0
    assert report.by_category
    assert set(report.by_difficulty) <= {"easy", "medium", "hard"}
    assert sum(item["cases"] for item in report.by_difficulty.values()) == report.total
    assert abs(sum(item["share"] for item in report.by_difficulty.values()) - 1.0) < 1e-9
    assert report.judge["enabled"] is False
    assert report.prompt_version == AGENT_PROMPT_VERSION

    reports = sorted((tmp_path / "reports").glob("eval_*.json"))
    markdown = sorted((tmp_path / "reports").glob("eval_*.md"))
    assert len(reports) == 1
    assert len(markdown) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["passed"] == 40
    assert len(payload["cases"]) == 40
    assert payload["metadata"]["prompt_version"] == AGENT_PROMPT_VERSION
    assert payload["metadata"]["provider"] == "mock"
    assert payload["metadata"]["model"] == "RuleBasedLLM"
    assert payload["judge"]["enabled"] is False
    assert payload["cases"][0]["table_recall_at_5"] is not None
    assert "Execution accuracy" in markdown[0].read_text(encoding="utf-8")
    assert "AI Judge" in markdown[0].read_text(encoding="utf-8")

    mixed_report = evaluate(
        runtime.agent,
        load_cases(runtime.settings.eval_cases_path),
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=None,
    )
    assert mixed_report.total == 123
    assert mixed_report.refusals == 10
    assert mixed_report.passed >= 61
    assert mixed_report.metrics["refusal_accuracy"] >= 0.9
    assert mixed_report.metrics["table_recall_at_5"] >= 0.9
    assert mixed_report.by_difficulty["hard"]["cases"] == 37
    assert mixed_report.by_difficulty["hard"]["share"] >= 0.3


def test_cli_only_tag_runs_the_baseline_subset(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "eval",
            "--home",
            str(tmp_path),
            "--only-tag",
            "v1_baseline",
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Cases: 40 | Passed: 40" in out


def test_cli_only_tag_rejects_a_tag_with_no_cases(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "eval",
            "--home",
            str(tmp_path),
            "--only-tag",
            "no_such_tag",
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert code == 2
    assert (tmp_path / "reports").exists() is False


def test_multi_turn_suite_is_frozen_and_replays_sessions(runtime: Runtime) -> None:
    cases = load_cases(Settings().multi_turn_cases_path)
    assert len(cases) == 10
    assert all(case.turns for case in cases)
    assert sum(1 for case in cases if case.expect_clarification) == 2
    assert all(case.kind == "multi" for case in cases)

    report = evaluate(
        runtime.agent,
        cases,
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=None,
    )
    assert report.total == 10
    assert report.passed == 10
    assert report.metrics["multi_turn_accuracy"] == 1.0
    assert report.metrics["clarification_accuracy"] == 1.0
    assert report.clarifications == 2
    assert report.by_kind["multi"]["avg_turns"] == 2.0
    assert report.answerable == 8

    by_id = {outcome.id: outcome for outcome in report.cases}
    assert by_id["mt_region_june_south"].effective_question == "2026年6月华南地区销售订单总金额是多少？"
    assert by_id["mt_region_june_south"].rewrite_source == "rule"
    assert by_id["mt_clarify_stock_scope"].status == "needs_clarification"
    assert by_id["mt_clarify_stock_scope"].clarification_options == 3
    assert by_id["mt_clarify_kitting_missing_order"].sql is None


def test_mixed_report_splits_metrics_by_kind(runtime: Runtime) -> None:
    cases = load_cases(Settings().eval_cases_path) + load_cases(
        Settings().multi_turn_cases_path
    )
    report = evaluate(
        runtime.agent,
        cases,
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=None,
    )
    assert report.total == 133
    assert report.passed == 71
    assert report.by_kind["single"]["cases"] == 123
    assert report.by_kind["multi"]["cases"] == 10


EAST_SQL = (
    "SELECT ROUND(SUM(oi.qty * oi.unit_price * (1 - oi.discount)), 2) AS total_amount "
    "FROM sales_order o JOIN order_item oi ON oi.order_id = o.order_id "
    "WHERE o.region = '华东' AND o.order_date >= '2026-06-01' AND o.order_date < '2026-07-01'"
)
EAST_AGREE_SQL = EAST_SQL.replace("sales_order o", "sales_order AS so").replace(" o.", " so.")


class PerQuestionSecondOpinion:
    """Second channel pinned per question: one agreement, one inconclusive vote."""

    source = "static"

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers

    def generate(self, question: str, tables: list[str]) -> SecondOpinion:
        sql = self.answers.get(question, "")
        if not sql:
            return SecondOpinion(source=self.source, error="规则通道未能生成 SQL")
        return SecondOpinion(sql=sql, source=self.source)


def test_dual_channel_agreement_rate_ignores_inconclusive_votes(runtime: Runtime, tmp_path: Path) -> None:
    agent = NL2SQLAgent(
        runtime.catalog,
        runtime.agent.llm,
        runtime.agent.tools,
        replace(runtime.settings, dual_channel=True, dual_channel_confidence_threshold=1.0),
        store=runtime.store,
        schema_hash=runtime.agent.schema_hash,
        second_opinion=PerQuestionSecondOpinion({
            "2026年6月华东地区销售订单总金额是多少？": EAST_AGREE_SQL,
            "2026年6月华南地区销售订单总金额是多少？": "",
        }),
        guard=runtime.guard,
    )
    report = evaluate(
        agent,
        [
            {
                "id": "vote_agree",
                "question": "2026年6月华东地区销售订单总金额是多少？",
                "category": "metric",
                "expected_tables": ["sales_order", "order_item"],
                "gold_sql": EAST_SQL,
            },
            {
                "id": "vote_inconclusive",
                "question": "2026年6月华南地区销售订单总金额是多少？",
                "category": "metric",
                "expected_tables": ["sales_order", "order_item"],
                "gold_sql": EAST_SQL.replace("华东", "华南"),
            },
            {
                "id": "vote_refusal",
                "question": "今天杭州天气怎么样？",
                "category": "scope",
                "expect_refusal": True,
            },
        ],
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=tmp_path / "reports",
    )

    assert report.answerable == 2
    assert report.passed == 3
    by_id = {outcome.id: outcome for outcome in report.cases}
    assert by_id["vote_agree"].dual_channel_ran is True
    assert by_id["vote_agree"].dual_channel_agreement is True
    assert by_id["vote_inconclusive"].dual_channel_ran is True
    assert by_id["vote_inconclusive"].dual_channel_agreement is None
    assert by_id["vote_refusal"].dual_channel_ran is False
    # 两条用例跑了投票，分母只算能判定的一致/不一致：1/1 而不是 1/2
    assert report.metrics["dual_channel_cases"] == 2.0
    assert report.metrics["dual_channel_agreement_rate"] == 1.0

    payload = json.loads(sorted((tmp_path / "reports").glob("eval_*.json"))[0].read_text(encoding="utf-8"))
    assert payload["metrics"]["dual_channel_agreement_rate"] == 1.0
    assert [case["dual_channel_ran"] for case in payload["cases"]] == [True, True, False]
    markdown = sorted((tmp_path / "reports").glob("eval_*.md"))[0].read_text(encoding="utf-8")
    assert "Dual-channel agreement: 100.00% (2 cases voted)" in markdown


def _layer(**overrides: object) -> str:
    args: dict[str, object] = {
        "expect_refusal": False,
        "expect_clarification": False,
        "status": "ok",
        "has_sql": True,
        "row_count": 3,
        "gold_rows": 3,
        "table_recall": 1.0,
        "guard_rejected": False,
        "execution_failed": False,
    }
    args.update(overrides)
    return attribute_failure(**args)  # type: ignore[arg-type]


def test_failure_layer_never_blames_a_layer_the_evidence_cannot_support() -> None:
    assert _layer() == "generation"
    assert _layer(guard_rejected=True, has_sql=False) == "guard"
    assert _layer(execution_failed=True, has_sql=False) == "execution"
    assert _layer(table_recall=0.5, has_sql=False) == "retrieval"
    assert _layer(has_sql=False) == "stalled"
    # 中途被护栏拒过一次，但循环后来产出并执行了 SQL：不能拿那条告警定罪
    assert _layer(guard_rejected=True, execution_failed=True) == "generation"
    assert _layer(guard_rejected=True, table_recall=0.0, has_sql=False) == "guard"
    # 澄清与拒答是路径判定，优先于 SQL 层面的猜测
    assert _layer(expect_clarification=True, has_sql=False) == "clarification"
    assert _layer(status="needs_clarification") == "clarification"
    assert _layer(expect_refusal=True) == "triage"
    assert _layer(expect_refusal=True, has_sql=False) == "stalled"
    # gold 自己返回 0 行而 agent 有行：先怀疑标注而不是 agent
    assert _layer(gold_rows=0, row_count=5) == "label"
    # gold SQL 自己跑不通：这是评测集的问题，不能算到模型头上
    assert _layer(gold_rows=0, row_count=0, gold_failed=True) == "label"
    assert _layer(expect_refusal=True, gold_failed=True) == "label"


def _subtype(**overrides: object) -> str:
    args: dict[str, object] = {
        "candidate_rows": [[1, 5]],
        "gold_rows": [[1, 2]],
        "order_sensitive": False,
    }
    args.update(overrides)
    return classify_generation_subtype(**args)  # type: ignore[arg-type]


def test_generation_subtype_only_claims_what_result_shapes_can_prove() -> None:
    # 形状相同、数值不同：口径或聚合函数选错
    assert _subtype() == "value_mismatch"
    assert _subtype(candidate_rows=[], gold_rows=[[1, 2]]) == "empty_result"
    assert (
        _subtype(candidate_rows=[[1, 2], [3, 4]], gold_rows=[[3, 4], [1, 2]], order_sensitive=True)
        == "order_only"
    )
    # 无序比较下行集合相同就是通过了，不该被标成行序问题
    assert _subtype(candidate_rows=[[3, 4], [1, 2]], gold_rows=[[1, 2], [3, 4]]) == "value_mismatch"
    assert _subtype(candidate_rows=[[1]], gold_rows=[[1, 2]]) == "column_shape"
    # 扇出放大、漏聚合、过滤缺失在行数上长得一样，所以合成一个口径而不是猜
    assert _subtype(candidate_rows=[[1], [2]], gold_rows=[[1]]) == "grain_or_scope"


def test_generation_subtype_is_a_split_of_the_generation_layer(
    runtime: Runtime, tmp_path: Path
) -> None:
    report = evaluate(
        runtime.agent,
        load_cases(runtime.settings.eval_cases_path),
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=tmp_path / "reports",
    )
    generation = [case for case in report.cases if case.failure_layer == "generation"]
    assert generation
    assert all(case.generation_subtype in GENERATION_SUBTYPES for case in generation)
    assert all(case.generation_subtype == "" for case in report.cases if case.failure_layer != "generation")
    assert (
        sum(item["cases"] for item in report.by_generation_subtype.values()) == len(generation)
    )
    assert abs(sum(item["share"] for item in report.by_generation_subtype.values()) - 1.0) < 1e-3
    for item in report.by_generation_subtype.values():
        assert 0 <= item["full_recall"] <= item["cases"]
    assert (
        report.by_generation_subtype["grain_or_scope"]["full_recall"]
        <= report.by_failure_layer["generation"]["full_recall"]
    )

    payload = json.loads(
        sorted((tmp_path / "reports").glob("eval_*.json"))[0].read_text(encoding="utf-8")
    )
    assert payload["by_generation_subtype"] == report.by_generation_subtype
    assert all(
        case["generation_subtype"]
        for case in payload["cases"]
        if case["failure_layer"] == "generation"
    )


def test_trace_stages_splits_guard_from_execution() -> None:
    assert trace_stages(
        [{"tool": "validate_sql", "stage": "guard"}, {"tool": "execute_sql", "stage": "execution"}]
    ) == (True, True)
    assert trace_stages([{"tool": "search_tables", "stage": ""}]) == (False, False)
    assert trace_stages(None) == (False, False)


def test_failed_cases_are_attributed_in_the_report(runtime: Runtime, tmp_path: Path) -> None:
    report = evaluate(
        runtime.agent,
        load_cases(runtime.settings.eval_cases_path),
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=tmp_path / "reports",
    )
    failures = [case for case in report.cases if not case.passed]
    assert failures
    assert all(case.failure_layer in FAILURE_LAYERS for case in failures)
    assert all(case.failure_layer == "" for case in report.cases if case.passed)
    assert sum(item["cases"] for item in report.by_failure_layer.values()) == len(failures)
    # `_ratio` 四舍五入到 4 位，所以 share 之和只应接近 1
    assert abs(sum(item["share"] for item in report.by_failure_layer.values()) - 1.0) < 1e-3
    for item in report.by_failure_layer.values():
        assert 0 <= item["full_recall"] <= item["cases"]
    # mock 的多数失败是满召回后仍答错，检索不是挡路的那一层
    assert report.by_failure_layer["generation"]["cases"] > 0
    assert report.by_failure_layer["generation"]["full_recall"] > 0

    payload = json.loads(sorted((tmp_path / "reports").glob("eval_*.json"))[0].read_text(encoding="utf-8"))
    assert payload["by_failure_layer"] == report.by_failure_layer
    assert all(case["failure_layer"] for case in payload["cases"] if not case["passed"])
    markdown = sorted((tmp_path / "reports").glob("eval_*.md"))[0].read_text(encoding="utf-8")
    assert "## By failure layer" in markdown


def test_report_states_the_guard_configuration_that_produced_it(
    runtime: Runtime, monkeypatch
) -> None:
    cases = [
        {
            "id": "guard_config_case",
            "question": "2026年6月销售总额是多少？",
            "category": "metric",
            "gold_sql": "SELECT 1",
        }
    ]

    def run(guard: SQLGuard):
        return evaluate(
            runtime.agent,
            cases,
            db_path=runtime.settings.db_path,
            guard=guard,
            provider="mock",
            report_dir=None,
        )

    default = run(runtime.guard).to_dict()["metadata"]["guard"]
    assert default == {
        "ast": "off",
        "ast_available": default["ast_available"],
        "fail_closed": False,
        "principal": None,
        "allow_sensitive": False,
    }

    scoped = SQLGuard(
        runtime.guard.catalog,
        allow_sensitive=True,
        principal=load_principals(runtime.settings.principals_path)["rep_east"],
        analyzer=AstAnalyzer(runtime.guard.catalog.dialect),
    )
    report = run(scoped)
    config = report.to_dict()["metadata"]["guard"]
    assert config["ast"] == "on"
    assert config["principal"] == "rep_east"
    assert config["allow_sensitive"] is True
    line = next(x for x in render_markdown(report, 0.0).splitlines() if x.startswith("- Guard:"))
    assert "AST `on`" in line and "principal `rep_east`" in line

    # AST on without sqlglot fails closed, so the run's numbers mean nothing
    # unless the report says every statement was rejected for that reason.
    monkeypatch.setattr("nl2sql_agent.evaluation.evaluator.ast_available", lambda: False)
    degraded = run(scoped)
    assert degraded.to_dict()["metadata"]["guard"]["fail_closed"] is True
    assert "FAIL-CLOSED" in render_markdown(degraded, 0.0)
