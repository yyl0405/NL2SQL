"""Frozen regression set + execution accuracy evaluation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agent import NL2SQLAgent
from ..guard import SQLGuard
from ..tools.sql_tools import run_readonly_query


@dataclass
class CaseOutcome:
    id: str
    question: str
    category: str
    status: str
    passed: bool
    sql: str | None
    steps: int
    elapsed_ms: float
    row_count: int
    error: str = ""
    gold_rows: int = 0
    expect_refusal: bool = False


@dataclass
class EvalReport:
    provider: str
    total: int
    answerable: int
    refusals: int
    passed: int
    metrics: dict[str, float] = field(default_factory=dict)
    cases: list[CaseOutcome] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "provider": self.provider,
            "total": self.total,
            "answerable": self.answerable,
            "refusals": self.refusals,
            "passed": self.passed,
            "metrics": self.metrics,
            "cases": [
                {
                    "id": case.id,
                    "question": case.question,
                    "category": case.category,
                    "status": case.status,
                    "passed": case.passed,
                    "sql": case.sql,
                    "steps": case.steps,
                    "elapsed_ms": case.elapsed_ms,
                    "row_count": case.row_count,
                    "gold_rows": case.gold_rows,
                    "expect_refusal": case.expect_refusal,
                    "error": case.error,
                }
                for case in self.cases
            ],
        }


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    return cases


def evaluate(
    agent: NL2SQLAgent,
    cases: list[dict[str, Any]],
    *,
    db_path: str | Path,
    guard: SQLGuard,
    provider: str = "mock",
    report_dir: str | Path | None = "reports",
    max_gold_rows: int = 1000,
) -> EvalReport:
    started = time.monotonic()
    outcomes: list[CaseOutcome] = []
    for case in cases:
        case_id = str(case.get("id") or case.get("question", ""))[:80]
        question = str(case.get("question", ""))
        category = str(case.get("category", ""))
        expect_refusal = bool(case.get("expect_refusal", False))
        result = agent.run(question)

        if expect_refusal:
            passed = result.status in {"needs_clarification", "refused"} and not result.sql
            outcomes.append(
                CaseOutcome(
                    id=case_id,
                    question=question,
                    category=category,
                    status=result.status,
                    passed=passed,
                    sql=result.sql,
                    steps=result.steps,
                    elapsed_ms=result.elapsed_ms,
                    row_count=result.row_count,
                    error=result.error,
                    expect_refusal=True,
                )
            )
            continue

        gold = run_readonly_query(
            db_path,
            str(case.get("gold_sql", "")),
            max_rows=max_gold_rows,
            timeout_ms=10000,
        )
        if not gold.get("success"):
            outcomes.append(
                CaseOutcome(
                    id=case_id,
                    question=question,
                    category=category,
                    status=result.status,
                    passed=False,
                    sql=result.sql,
                    steps=result.steps,
                    elapsed_ms=result.elapsed_ms,
                    row_count=result.row_count,
                    error=f"gold_sql_failed: {gold.get('error')}",
                )
            )
            continue

        passed = (
            result.status == "ok"
            and rows_equal(
                result.rows,
                gold.get("rows") or [],
                order_sensitive=bool(case.get("order_sensitive", False)),
            )
        )
        outcomes.append(
            CaseOutcome(
                id=case_id,
                question=question,
                category=category,
                status=result.status,
                passed=passed,
                sql=result.sql,
                steps=result.steps,
                elapsed_ms=result.elapsed_ms,
                row_count=result.row_count,
                gold_rows=int(gold.get("row_count") or 0),
                error=result.error,
            )
        )

    answerable = [c for c in outcomes if not c.expect_refusal]
    refusals = [c for c in outcomes if c.expect_refusal]
    passed_answerable = sum(1 for c in answerable if c.passed)
    passed_refusals = sum(1 for c in refusals if c.passed)
    valid_sql = sum(
        1
        for c in answerable
        if c.sql and guard.check(c.sql).valid
    )
    execution_success = sum(1 for c in answerable if c.status == "ok")
    metrics = {
        "execution_accuracy": _ratio(passed_answerable, len(answerable)),
        "overall_accuracy": _ratio(passed_answerable + passed_refusals, len(outcomes)),
        "valid_sql_rate": _ratio(valid_sql, len(answerable)),
        "execution_success_rate": _ratio(execution_success, len(answerable)),
        "refusal_accuracy": _ratio(passed_refusals, len(refusals)),
        "avg_steps": _mean([c.steps for c in outcomes]),
        "avg_latency_ms": _mean([c.elapsed_ms for c in outcomes]),
    }
    report = EvalReport(
        provider=provider,
        total=len(outcomes),
        answerable=len(answerable),
        refusals=len(refusals),
        passed=passed_answerable + passed_refusals,
        metrics=metrics,
        cases=outcomes,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if report_dir is not None:
        write_report(report, report_dir, elapsed_ms=(time.monotonic() - started) * 1000)
    return report


def rows_equal(
    left: list[list[Any]],
    right: list[list[Any]],
    *,
    order_sensitive: bool = False,
) -> bool:
    normalized_left = [_normalize_row(row) for row in left]
    normalized_right = [_normalize_row(row) for row in right]
    if len(normalized_left) != len(normalized_right):
        return False
    if order_sensitive:
        return normalized_left == normalized_right
    return sorted(normalized_left, key=repr) == sorted(normalized_right, key=repr)


def write_report(report: EvalReport, report_dir: str | Path, *, elapsed_ms: float) -> tuple[Path, Path]:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = directory / f"eval_{stamp}.json"
    md_path = directory / f"eval_{stamp}.md"
    payload = report.to_dict()
    payload["wall_clock_ms"] = round(elapsed_ms, 2)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report, elapsed_ms), encoding="utf-8")
    return json_path, md_path


def render_markdown(report: EvalReport, elapsed_ms: float) -> str:
    metrics = report.metrics
    lines = [
        "# NL2SQL Agent Evaluation",
        "",
        f"- Generated: {report.generated_at}",
        f"- Provider: `{report.provider}`",
        f"- Cases: {report.total} (answerable {report.answerable}, refusals {report.refusals})",
        f"- Passed: {report.passed}/{report.total}",
        f"- Execution accuracy: {metrics['execution_accuracy']:.2%}",
        f"- Overall accuracy: {metrics['overall_accuracy']:.2%}",
        f"- Valid SQL rate: {metrics['valid_sql_rate']:.2%}",
        f"- Execution success rate: {metrics['execution_success_rate']:.2%}",
        f"- Refusal accuracy: {metrics['refusal_accuracy']:.2%}",
        f"- Avg steps: {metrics['avg_steps']:.2f}",
        f"- Avg latency: {metrics['avg_latency_ms']:.2f} ms",
        f"- Wall clock: {elapsed_ms:.0f} ms",
        "",
        "| id | category | status | pass | rows | gold | steps | ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case in report.cases:
        lines.append(
            f"| {case.id} | {case.category} | {case.status} | "
            f"{'✅' if case.passed else '❌'} | {case.row_count} | {case.gold_rows} | "
            f"{case.steps} | {case.elapsed_ms:.1f} |"
        )
    failures = [case for case in report.cases if not case.passed]
    if failures:
        lines.extend(["", "## Failures", ""])
        for case in failures:
            lines.append(f"- **{case.id}**: {case.status} — {case.error or 'result mismatch'}")
            if case.sql:
                lines.append(f"  ```sql\n  {case.sql}\n  ```")
    return "\n".join(lines) + "\n"


def _normalize_row(row: list[Any]) -> tuple[Any, ...]:
    normalized: list[Any] = []
    for value in row:
        if isinstance(value, bool):
            normalized.append(value)
        elif isinstance(value, (int, float)):
            normalized.append(round(float(value), 4))
        elif isinstance(value, str):
            normalized.append(value.strip())
        else:
            normalized.append(value)
    return tuple(normalized)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0
