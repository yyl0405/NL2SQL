from __future__ import annotations

import json

import pytest

from nl2sql_agent.cli import main
from nl2sql_agent.config import Settings
from nl2sql_agent.evaluation import evaluate, load_cases
from nl2sql_agent.evaluation.evaluator import (
    CaseOutcome,
    _paraphrase_metrics,
    render_markdown,
)
from nl2sql_agent.runtime import Runtime


def _frozen():
    return {case.id: case for case in load_cases(Settings().eval_cases_path)}


def _rewrites():
    return load_cases(Settings().paraphrase_cases_path)


def test_paraphrase_corpus_changes_only_the_question() -> None:
    """The measurement is only attributable to wording if nothing else moved."""
    frozen = _frozen()
    rewrites = _rewrites()
    assert len(rewrites) >= 30
    assert len({case.id for case in rewrites}) == len(rewrites)
    assert len({case.question for case in rewrites}) == len(rewrites)
    for case in rewrites:
        source = frozen.get(case.paraphrase_of)
        assert source is not None, f"{case.id} points at a missing case"
        assert case.gold_sql == source.gold_sql
        assert case.expected_tables == source.expected_tables
        assert case.category == source.category
        assert case.difficulty == source.difficulty
        assert case.order_sensitive == source.order_sensitive
        assert case.question != source.question
        assert case.status == "active"
        assert not case.turns
        assert not case.expect_refusal and not case.expect_clarification


def test_paraphrase_corpus_stays_out_of_the_ci_gate_subset() -> None:
    """`eval --only-tag v1_baseline` must keep meaning "those 40 cases"."""
    combined = load_cases(Settings().eval_cases_path) + _rewrites()
    assert all("v1_baseline" not in case.tags for case in _rewrites())
    assert all("paraphrase" in case.tags for case in _rewrites())
    subset = [case for case in combined if "v1_baseline" in set(case.tags)]
    assert len(subset) == 40
    assert not any(case.paraphrase_of for case in subset)


def test_pairs_split_by_verdict_kind() -> None:
    def outcome(case_id: str, passed: bool, *, of: str = "", status: str = "ok", rows: int = 1) -> CaseOutcome:
        return CaseOutcome(
            id=case_id,
            question=case_id,
            category="metric",
            status=status,
            passed=passed,
            sql="SELECT 1",
            steps=1,
            elapsed_ms=1.0,
            row_count=rows,
            table_recall_at_5=1.0,
            paraphrase_of=of,
        )

    block = _paraphrase_metrics(
        [
            outcome("a", True),
            outcome("a__para", True, of="a"),
            outcome("b", False),
            outcome("b__para", False, of="b"),
            outcome("c", True),
            outcome("c__para", False, of="c"),
            outcome("d", False),
            outcome("d__para", True, of="d"),
            outcome("e__para", False, of="e"),
            outcome("f", True),
            outcome("f__para", False, of="f", status="error", rows=0),
        ]
    )
    assert block["rewrites"] == 6
    assert block["pairs"] == 5
    assert block["unmatched"] == ["e__para"]
    assert block["both_pass"] == 1 and block["both_fail"] == 1
    assert block["brittle_ids"] == ["c__para", "f__para"]
    assert block["reverse_ids"] == ["d__para"]
    assert block["verdict_agreement_rate"] == pytest.approx(0.4)
    assert block["source_accuracy"] == pytest.approx(0.6)
    assert block["rewrite_accuracy"] == pytest.approx(0.4)
    # f's source executed and the rewrite did not, so that pair leaves the shape count.
    assert block["row_count_comparable"] == 4
    assert block["mean_table_recall_source"] == 1.0


def test_metrics_block_is_absent_without_rewrites(runtime: Runtime) -> None:
    report = evaluate(
        runtime.agent,
        [_frozen()["rev_june_east"]],
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=None,
    )
    assert report.paraphrase == {}
    assert report.to_dict()["paraphrase"] == {}


def test_evaluate_pairs_a_rewrite_with_its_source(runtime: Runtime) -> None:
    frozen = _frozen()["available_stock_m001"]
    rewrite = next(case for case in _rewrites() if case.paraphrase_of == frozen.id)
    report = evaluate(
        runtime.agent,
        [frozen, rewrite],
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=None,
    )
    assert report.total == 2
    block = report.paraphrase
    assert block["pairs"] == 1 and block["unmatched"] == []
    assert block["verdict_agreement_rate"] in {0.0, 1.0}
    assert block["rewrites"] == 1

    markdown = render_markdown(report, 12.0)
    assert "## Paraphrase pairs" in markdown
    assert "Verdict agreement" in markdown
    assert "Paraphrase agreement" in markdown
    assert "A rewrite copies gold SQL" in markdown


def test_unpaired_run_says_nothing_was_measured(runtime: Runtime) -> None:
    rewrite = _rewrites()[0]
    report = evaluate(
        runtime.agent,
        [rewrite],
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=None,
    )
    assert report.paraphrase["pairs"] == 0
    assert report.paraphrase["unmatched"] == [rewrite.id]
    markdown = render_markdown(report, 12.0)
    assert "n/a (0 pairs)" in markdown
    assert "No pairs in this run" in markdown


def test_cli_paraphrase_extends_the_corpus(tmp_path, capsys) -> None:
    code = main(
        [
            "eval",
            "--home",
            str(tmp_path),
            "--provider",
            "mock",
            "--report-dir",
            str(tmp_path / "r"),
            "--paraphrase",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1  # the corpus is not expected to pass
    assert payload["total"] == 155
    assert payload["paraphrase"]["pairs"] == 32
    assert payload["paraphrase"]["rewrites"] == 32
    assert payload["paraphrase"]["source_accuracy"] > payload["paraphrase"]["rewrite_accuracy"]


def test_regression_gate_refuses_a_paraphrase_run_against_the_frozen_baseline(tmp_path, capsys) -> None:
    baseline_path = tmp_path / "ex_baseline.json"
    common = ["eval", "--home", str(tmp_path), "--provider", "mock", "--report-dir", str(tmp_path / "r")]
    assert main([*common, "--write-baseline", str(baseline_path)]) == 1
    capsys.readouterr()

    code = main(
        [
            *common,
            "--paraphrase",
            "--compare",
            str(baseline_path),
            "--fail-on-regression",
        ]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "语料规模不同" in err
