from __future__ import annotations

import json
from pathlib import Path

import pytest

from nl2sql_agent.config import Settings
from nl2sql_agent.evaluation import evaluate, load_cases
from nl2sql_agent.evaluation.evaluator import EvalCase, rows_equal
from nl2sql_agent.runtime import Runtime


def test_rows_equal_ignores_order_by_default() -> None:
    assert rows_equal([[1, "a"], [2, "b"]], [[2, "b"], [1, "a"]])
    assert not rows_equal([[1], [2]], [[1]], order_sensitive=False)


def test_rows_equal_respects_order_sensitive_flag() -> None:
    assert not rows_equal([[1], [2]], [[2], [1]], order_sensitive=True)


def test_rows_equal_normalizes_numbers_and_strings() -> None:
    assert rows_equal([[62280.00004, " 华东 "]], [[62280.0, "华东"]])
    assert not rows_equal([[1]], [[2]])


def test_load_cases_contains_refusal() -> None:
    cases = load_cases(Settings().eval_cases_path)
    assert len(cases) == 40
    refusal = [case for case in cases if case.expect_refusal]
    assert len(refusal) == 3
    assert all(case.category == "scope" for case in refusal)
    assert all(case.gold_sql for case in cases if not case.expect_refusal)
    assert all(case.expected_tables for case in cases if not case.expect_refusal)
    assert all(case.difficulty in {"easy", "medium", "hard"} for case in cases)
    assert all(case.status == "active" for case in cases)


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
    cases = load_cases(runtime.settings.eval_cases_path)
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
    assert report.total_tokens == 0
    assert report.by_category
    assert report.judge["enabled"] is False
    assert report.prompt_version == "agent-v2"

    reports = sorted((tmp_path / "reports").glob("eval_*.json"))
    markdown = sorted((tmp_path / "reports").glob("eval_*.md"))
    assert len(reports) == 1
    assert len(markdown) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["passed"] == 40
    assert len(payload["cases"]) == 40
    assert payload["metadata"]["prompt_version"] == "agent-v2"
    assert payload["metadata"]["provider"] == "mock"
    assert payload["metadata"]["model"] == "RuleBasedLLM"
    assert payload["judge"]["enabled"] is False
    assert payload["cases"][0]["table_recall_at_5"] is not None
    assert "Execution accuracy" in markdown[0].read_text(encoding="utf-8")
    assert "AI Judge" in markdown[0].read_text(encoding="utf-8")
