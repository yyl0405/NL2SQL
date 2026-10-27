from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nl2sql_agent.cli import main
from nl2sql_agent.evaluation.regression import (
    BASELINE_KIND,
    GATED_METRICS,
    build_baseline,
    check_regression,
    format_regression,
)


def _report(metrics: dict[str, float] | None = None, **overrides: Any) -> dict[str, Any]:
    base_metrics = {
        "execution_accuracy": 0.46,
        "overall_accuracy": 0.55,
        "valid_sql_rate": 0.85,
        "execution_success_rate": 0.9,
        "refusal_accuracy": 0.9,
        "clarification_accuracy": 1.0,
        "table_recall_at_5": 0.94,
        "avg_latency_ms": 2.5,
        "avg_steps": 4.88,
        "avg_tokens": 0.0,
    }
    report: dict[str, Any] = {
        "generated_at": "2026-10-22T00:00:00+00:00",
        "provider": "mock",
        "metadata": {"model": "", "prompt_version": "v3", "git_sha": "abc1234", "guard": {"ast": "off"}},
        "total": 123,
        "answerable": 113,
        "refusals": 10,
        "clarifications": 0,
        "skipped": 0,
        "passed": 61,
        "metrics": dict(base_metrics, **(metrics or {})),
        "cases": [{"id": "case_1", "sql": "SELECT 1"}],
    }
    report.update(overrides)
    return report


def test_a_baseline_holds_numbers_and_never_the_corpus(tmp_path: Path) -> None:
    baseline = build_baseline(_report())

    assert baseline["kind"] == BASELINE_KIND
    assert baseline["provider"] == "mock"
    assert baseline["corpus"] == {"total": 123, "answerable": 113, "refusals": 10, "clarifications": 0, "skipped": 0}
    assert baseline["guard"] == {"ast": "off"}
    assert set(baseline["metrics"]) == set(GATED_METRICS)
    # Questions, generated SQL and per-case ids are the corpus, not the metric — a
    # baseline built from a real database must stay safe to commit.
    assert "cases" not in baseline
    assert "by_category" not in baseline


def test_an_unchanged_run_passes_the_gate() -> None:
    report = _report()
    verdict = check_regression(build_baseline(report), report, 0.02)

    assert verdict["ok"] is True
    assert verdict["regressions"] == []
    assert len(verdict["checked"]) == len(GATED_METRICS)
    assert all(item["delta"] == 0.0 for item in verdict["checked"])


def test_a_drop_larger_than_the_threshold_fails_and_the_boundary_does_not() -> None:
    report = _report()
    baseline = build_baseline(report)

    failing = check_regression(baseline, _report(metrics={"execution_accuracy": 0.46 - 0.0201}), 0.02)
    assert failing["ok"] is False
    assert [item["metric"] for item in failing["regressions"]] == ["execution_accuracy"]
    assert failing["regressions"][0]["delta_pp"] == pytest.approx(-2.01)

    at_the_limit = check_regression(baseline, _report(metrics={"execution_accuracy": 0.44}), 0.02)
    assert at_the_limit["ok"] is True


def test_latency_blow_up_is_observed_but_never_gates() -> None:
    report = _report()
    baseline = build_baseline(report)

    verdict = check_regression(
        baseline,
        _report(metrics={"avg_latency_ms": 5000.0, "avg_steps": 40.0}),
        0.02,
    )

    assert verdict["ok"] is True
    assert {item["metric"] for item in verdict["checked"]} == set(GATED_METRICS)


def test_a_different_provider_or_corpus_is_refused_rather_than_subtracted() -> None:
    report = _report()
    baseline = build_baseline(report)

    with pytest.raises(ValueError, match="provider"):
        check_regression(baseline, _report(provider="openai"), 0.02)
    with pytest.raises(ValueError, match="语料规模不同"):
        check_regression(baseline, _report(total=40, answerable=37), 0.02)
    with pytest.raises(ValueError, match="provider"):
        check_regression({"metrics": {"execution_accuracy": 0.4}}, report, 0.02)
    with pytest.raises(ValueError, match="corpus"):
        check_regression({"provider": "mock", "metrics": {"execution_accuracy": 0.46}}, report, 0.02)
    with pytest.raises(ValueError, match="write-baseline"):
        check_regression({"provider": "mock", "corpus": {"total": 123, "answerable": 113}}, report, 0.02)


def test_metrics_missing_on_either_side_are_skipped_not_counted_as_zero() -> None:
    report = _report()
    report["metrics"].pop("clarification_accuracy")
    baseline = build_baseline(_report())

    verdict = check_regression(baseline, report, 0.02)

    assert verdict["skipped"] == ["clarification_accuracy"]
    assert len(verdict["checked"]) == len(GATED_METRICS) - 1

    with pytest.raises(ValueError, match="重叠"):
        check_regression(
            {"provider": "mock", "corpus": {"total": 123, "answerable": 113}, "metrics": {"unrelated": 1.0}},
            report,
            0.02,
        )


def test_formatting_shows_the_verdict_and_the_deltas() -> None:
    report = _report()
    verdict = check_regression(
        build_baseline(report),
        _report(metrics={"execution_accuracy": 0.40, "table_recall_at_5": 0.96}),
        0.02,
    )
    lines = format_regression(verdict)

    assert lines[0] == "EX 回归门禁: 失败（阈值 2.00pp）"
    assert "  execution_accuracy: 46.00% -> 40.00% (-6.00pp)" in lines
    assert any("table_recall_at_5" in line and "上升" in line for line in lines)


def test_cli_writes_a_baseline_and_the_gate_passes_on_a_rerun(tmp_path: Path, capsys) -> None:
    baseline_path = tmp_path / "benchmarks" / "ex_baseline.json"
    common = ["eval", "--home", str(tmp_path), "--provider", "mock", "--report-dir", str(tmp_path / "r")]

    write_code = main([*common, "--write-baseline", str(baseline_path)])
    captured = capsys.readouterr()
    assert write_code == 1  # mock fails part of the full corpus by design
    assert "Baseline written to" in captured.err

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["corpus"]["total"] == 123
    assert "cases" not in baseline

    gate_code = main([*common, "--compare", str(baseline_path), "--fail-on-regression", "--json"])
    gated = json.loads(capsys.readouterr().out)
    assert gate_code == 0
    assert gated["regression"]["ok"] is True
    assert gated["regression"]["max_drop"] == 0.02
    assert len(gated["regression"]["checked"]) == len(GATED_METRICS)
    # mock fails part of the 123-case corpus on purpose; the gate says so instead of
    # letting the known failures read as a regression.
    assert gated["total"] == 123


def test_cli_fails_the_gate_when_the_baseline_is_better(tmp_path: Path, capsys) -> None:
    baseline_path = tmp_path / "ex_baseline.json"
    common = ["eval", "--home", str(tmp_path), "--provider", "mock", "--report-dir", str(tmp_path / "r")]
    assert main([*common, "--write-baseline", str(baseline_path)]) == 1
    capsys.readouterr()

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["metrics"]["execution_accuracy"] += 0.05
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    assert main([*common, "--compare", str(baseline_path), "--fail-on-regression"]) == 1
    out = capsys.readouterr().out
    assert "EX 回归门禁: 失败" in out
    assert "execution_accuracy" in out


def test_cli_refuses_to_gate_against_an_incomparable_baseline(tmp_path: Path, capsys) -> None:
    subset_baseline = tmp_path / "subset.json"
    assert main(
        [
            "eval",
            "--home",
            str(tmp_path),
            "--provider",
            "mock",
            "--report-dir",
            str(tmp_path / "r"),
            "--only-tag",
            "v1_baseline",
            "--write-baseline",
            str(subset_baseline),
        ]
    ) == 0
    capsys.readouterr()

    code = main(
        [
            "eval",
            "--home",
            str(tmp_path),
            "--provider",
            "mock",
            "--report-dir",
            str(tmp_path / "r"),
            "--compare",
            str(subset_baseline),
            "--fail-on-regression",
        ]
    )
    err = capsys.readouterr().err

    assert code == 2
    assert "语料规模不同" in err


def test_cli_prints_the_verdict_in_human_mode(tmp_path: Path, capsys) -> None:
    baseline_path = tmp_path / "ex_baseline.json"
    subset = [
        "eval",
        "--home",
        str(tmp_path),
        "--provider",
        "mock",
        "--report-dir",
        str(tmp_path / "r"),
        "--only-tag",
        "v1_baseline",
    ]

    assert main([*subset, "--write-baseline", str(baseline_path)]) == 0
    capsys.readouterr()
    assert main([*subset, "--compare", str(baseline_path), "--fail-on-regression"]) == 0
    out = capsys.readouterr().out

    assert "EX 回归门禁: 通过（阈值 2.00pp" in out
    assert "Baseline:" in out  # the informational --compare diff is still printed


def test_cli_requires_a_baseline_for_the_gate(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "eval",
            "--home",
            str(tmp_path),
            "--provider",
            "mock",
            "--report-dir",
            str(tmp_path / "r"),
            "--only-tag",
            "v1_baseline",
            "--fail-on-regression",
        ]
    )

    assert code == 2
    assert "--compare" in capsys.readouterr().err
