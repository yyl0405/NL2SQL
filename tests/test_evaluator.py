from __future__ import annotations

import json
from pathlib import Path

from nl2sql_agent.config import Settings
from nl2sql_agent.evaluation import evaluate, load_cases
from nl2sql_agent.evaluation.evaluator import rows_equal
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
    assert len(cases) == 16
    refusal = [case for case in cases if case.get("expect_refusal")]
    assert len(refusal) == 1
    assert refusal[0]["category"] == "scope"
    assert all("gold_sql" in case for case in cases if not case.get("expect_refusal"))


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
    assert report.total == 16
    assert report.answerable == 15
    assert report.refusals == 1
    assert report.passed == 16
    assert report.metrics["execution_accuracy"] == 1.0
    assert report.metrics["valid_sql_rate"] == 1.0
    assert report.metrics["refusal_accuracy"] == 1.0

    reports = sorted((tmp_path / "reports").glob("eval_*.json"))
    markdown = sorted((tmp_path / "reports").glob("eval_*.md"))
    assert len(reports) == 1
    assert len(markdown) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["passed"] == 16
    assert len(payload["cases"]) == 16
    assert "Execution accuracy" in markdown[0].read_text(encoding="utf-8")
