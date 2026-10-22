"""Frozen regression set + execution accuracy evaluation."""

from __future__ import annotations

import json
import random
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agent import AGENT_PROMPT_VERSION, NL2SQLAgent
from ..guard import SQLGuard
from ..tools.sql_tools import run_readonly_query
from .judge import (
    JUDGE_PROMPT_VERSION,
    JudgeInput,
    JudgeModel,
    JudgeVerdict,
    build_schema_excerpt,
    truncate_result,
)

TABLE_RECALL_K = 5
_ACTIVE_CASE_STATUS = "active"
_SKIPPED_CASE_STATUSES = {"draft", "retired"}


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    category: str
    gold_sql: str = ""
    expect_refusal: bool = False
    order_sensitive: bool = False
    expected_tables: tuple[str, ...] = ()
    difficulty: str = ""
    source: str = ""
    status: str = "active"
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvalCase":
        case_id = str(raw.get("id") or raw.get("question", "")).strip()[:80]
        question = str(raw.get("question", "")).strip()
        if not case_id or not question:
            raise ValueError("评测用例必须包含 id 与 question")
        expect_refusal = bool(raw.get("expect_refusal", False))
        gold_sql = str(raw.get("gold_sql") or "").strip()
        if not expect_refusal and not gold_sql:
            raise ValueError(f"用例 {case_id} 缺少 gold_sql")
        status = str(raw.get("status") or _ACTIVE_CASE_STATUS).strip().lower()
        if status != _ACTIVE_CASE_STATUS and status not in _SKIPPED_CASE_STATUSES:
            raise ValueError(f"用例 {case_id} 的 status 非法: {status}")
        expected_tables = raw.get("expected_tables") or []
        tags = raw.get("tags") or []
        return cls(
            id=case_id,
            question=question,
            category=str(raw.get("category") or "uncategorized").strip(),
            gold_sql=gold_sql,
            expect_refusal=expect_refusal,
            order_sensitive=bool(raw.get("order_sensitive", False)),
            expected_tables=tuple(str(item) for item in expected_tables if str(item).strip()),
            difficulty=str(raw.get("difficulty") or "").strip(),
            source=str(raw.get("source") or "").strip(),
            status=status,
            tags=tuple(str(item) for item in tags if str(item).strip()),
        )


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
    tokens: int = 0
    error: str = ""
    gold_rows: int = 0
    expect_refusal: bool = False
    table_recall_at_5: float | None = None
    expected_tables: list[str] = field(default_factory=list)


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
    model: str = ""
    prompt_version: str = AGENT_PROMPT_VERSION
    git_sha: str = ""
    skipped: int = 0
    total_tokens: int = 0
    by_category: dict[str, dict[str, Any]] = field(default_factory=dict)
    judge: dict[str, Any] = field(default_factory=lambda: _judge_placeholder())

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "provider": self.provider,
            "metadata": {
                "provider": self.provider,
                "model": self.model,
                "prompt_version": self.prompt_version,
                "git_sha": self.git_sha,
                "run_at": self.generated_at,
                "case_count": self.total,
            },
            "total": self.total,
            "answerable": self.answerable,
            "refusals": self.refusals,
            "skipped": self.skipped,
            "passed": self.passed,
            "total_tokens": self.total_tokens,
            "metrics": self.metrics,
            "by_category": self.by_category,
            "judge": self.judge,
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
                    "tokens": case.tokens,
                    "row_count": case.row_count,
                    "gold_rows": case.gold_rows,
                    "expect_refusal": case.expect_refusal,
                    "expected_tables": case.expected_tables,
                    "table_recall_at_5": case.table_recall_at_5,
                    "error": case.error,
                }
                for case in self.cases
            ],
        }


def load_cases(path: str | Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            cases.append(EvalCase.from_dict(json.loads(line)))
    return cases


def evaluate(
    agent: NL2SQLAgent,
    cases: list[EvalCase | dict[str, Any]],
    *,
    db_path: str | Path,
    guard: SQLGuard,
    provider: str = "mock",
    report_dir: str | Path | None = "reports",
    max_gold_rows: int = 1000,
    model: str = "",
    prompt_version: str = AGENT_PROMPT_VERSION,
    git_sha: str | None = None,
    judge_model: JudgeModel | None = None,
    judge_on_fail: bool = False,
    judge_sample: int | None = None,
    judge_seed: int = 20261021,
    judge_max_calls: int = 100,
    judge_max_rows: int = 20,
) -> EvalReport:
    started = time.monotonic()
    normalized = [case if isinstance(case, EvalCase) else EvalCase.from_dict(case) for case in cases]
    skipped = sum(1 for case in normalized if case.status != _ACTIVE_CASE_STATUS)
    outcomes: list[CaseOutcome] = []
    runs: list[tuple[EvalCase, Any, dict[str, Any] | None]] = []

    for case in normalized:
        if case.status != _ACTIVE_CASE_STATUS:
            continue
        table_recall = _table_recall(agent, case)
        result = agent.run(case.question)

        if case.expect_refusal:
            passed = result.status in {"needs_clarification", "refused"} and not result.sql
            outcomes.append(_outcome(case, result, table_recall, passed=passed))
            runs.append((case, result, None))
            continue

        gold = run_readonly_query(
            db_path,
            case.gold_sql,
            max_rows=max_gold_rows,
            timeout_ms=10000,
        )
        if not gold.get("success"):
            outcomes.append(
                _outcome(
                    case,
                    result,
                    table_recall,
                    passed=False,
                    error=f"gold_sql_failed: {gold.get('error')}",
                )
            )
            runs.append((case, result, None))
            continue

        passed = (
            result.status == "ok"
            and rows_equal(
                result.rows,
                gold.get("rows") or [],
                order_sensitive=case.order_sensitive,
            )
        )
        outcomes.append(
            _outcome(
                case,
                result,
                table_recall,
                passed=passed,
                gold_rows=int(gold.get("row_count") or 0),
            )
        )
        runs.append((case, result, gold))

    answerable = [c for c in outcomes if not c.expect_refusal]
    refusals = [c for c in outcomes if c.expect_refusal]
    passed_answerable = sum(1 for c in answerable if c.passed)
    passed_refusals = sum(1 for c in refusals if c.passed)
    valid_sql = sum(1 for c in answerable if c.sql and guard.check(c.sql).valid)
    execution_success = sum(1 for c in answerable if c.status == "ok")
    recall_values = [c.table_recall_at_5 for c in outcomes if c.table_recall_at_5 is not None]
    total_tokens = sum(c.tokens for c in outcomes)
    metrics = {
        "execution_accuracy": _ratio(passed_answerable, len(answerable)),
        "overall_accuracy": _ratio(passed_answerable + passed_refusals, len(outcomes)),
        "valid_sql_rate": _ratio(valid_sql, len(answerable)),
        "execution_success_rate": _ratio(execution_success, len(answerable)),
        "refusal_accuracy": _ratio(passed_refusals, len(refusals)),
        "table_recall_at_5": round(_mean(recall_values), 4),
        "avg_steps": _mean([c.steps for c in outcomes]),
        "avg_latency_ms": _mean([c.elapsed_ms for c in outcomes]),
        "avg_tokens": _mean([c.tokens for c in outcomes]),
        "total_tokens": float(total_tokens),
    }
    settings = getattr(agent, "settings", None)
    judge_report = _judge_placeholder()
    if judge_model is not None:
        selected = _select_judge_runs(
            runs,
            outcomes,
            on_fail=judge_on_fail,
            sample=judge_sample,
            seed=judge_seed,
        )
        verdicts: list[JudgeVerdict] = []
        limit_reached = False
        for case, result, gold in selected:
            if len(verdicts) >= judge_max_calls:
                limit_reached = True
                break
            item = _build_judge_input(case, result, gold, agent, max_rows=judge_max_rows)
            try:
                verdict = judge_model.judge(item)
            except Exception as exc:  # noqa: BLE001 - one judge failure must not abort the run
                verdict = JudgeVerdict(
                    case_id=case.id,
                    parse_error=f"judge_error: {exc}"[:500],
                )
            verdicts.append(verdict)
        judge_report = _build_judge_report(
            verdicts,
            total=len(outcomes),
            sampled=len(selected),
            limit_reached=limit_reached,
            refusal_ids={case.id for case in normalized if case.expect_refusal},
            model_name=str(
                getattr(judge_model, "model", "")
                or getattr(settings, "judge_model", "")
                or ""
            ),
            prompt_version=str(
                getattr(judge_model, "prompt_version", "")
                or getattr(settings, "judge_prompt_version", "")
                or JUDGE_PROMPT_VERSION
            ),
        )

    report = EvalReport(
        provider=provider,
        total=len(outcomes),
        answerable=len(answerable),
        refusals=len(refusals),
        passed=passed_answerable + passed_refusals,
        metrics=metrics,
        cases=outcomes,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        model=model
        or (
            type(agent.llm).__name__
            if provider == "mock"
            else str(getattr(settings, "model", "") or "")
        ),
        prompt_version=prompt_version,
        git_sha=_git_sha() if git_sha is None else git_sha,
        skipped=skipped,
        total_tokens=total_tokens,
        by_category=_category_metrics(outcomes),
        judge=judge_report,
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
        f"- Model: `{report.model or 'n/a'}`",
        f"- Agent prompt: `{report.prompt_version}`",
        f"- Git SHA: `{report.git_sha or 'n/a'}`",
        f"- Cases: {report.total} (answerable {report.answerable}, refusals {report.refusals}, skipped {report.skipped})",
        f"- Passed: {report.passed}/{report.total}",
        f"- Execution accuracy: {metrics['execution_accuracy']:.2%}",
        f"- Overall accuracy: {metrics['overall_accuracy']:.2%}",
        f"- Valid SQL rate: {metrics['valid_sql_rate']:.2%}",
        f"- Execution success rate: {metrics['execution_success_rate']:.2%}",
        f"- Refusal accuracy: {metrics['refusal_accuracy']:.2%}",
        f"- Table recall@{TABLE_RECALL_K}: {metrics['table_recall_at_5']:.2%}",
        f"- Avg steps: {metrics['avg_steps']:.2f}",
        f"- Avg latency: {metrics['avg_latency_ms']:.2f} ms",
        f"- Tokens: {report.total_tokens} total / {metrics['avg_tokens']:.2f} avg",
        f"- Wall clock: {elapsed_ms:.0f} ms",
        "",
        "## By category",
        "",
        "| category | cases | passed | accuracy | avg ms |",
        "|---|---|---|---|---|",
    ]
    for category, item in report.by_category.items():
        lines.append(
            f"| {category} | {item['cases']} | {item['passed']} | "
            f"{item['accuracy']:.2%} | {item['avg_latency_ms']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## AI Judge",
            "",
            _judge_markdown(report.judge),
            "",
            f"| id | category | status | pass | rows | gold | steps | ms | tokens | recall@{TABLE_RECALL_K} |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for case in report.cases:
        recall = "-" if case.table_recall_at_5 is None else f"{case.table_recall_at_5:.2f}"
        lines.append(
            f"| {case.id} | {case.category} | {case.status} | "
            f"{'✅' if case.passed else '❌'} | {case.row_count} | {case.gold_rows} | "
            f"{case.steps} | {case.elapsed_ms:.1f} | {case.tokens} | {recall} |"
        )
    failures = [case for case in report.cases if not case.passed]
    if failures:
        lines.extend(["", "## Failures", ""])
        for case in failures:
            lines.append(f"- **{case.id}**: {case.status} — {case.error or 'result mismatch'}")
            if case.sql:
                lines.append(f"  ```sql\n  {case.sql}\n  ```")
    return "\n".join(lines) + "\n"


def _outcome(
    case: EvalCase,
    result: Any,
    table_recall: float | None,
    *,
    passed: bool,
    gold_rows: int = 0,
    error: str = "",
) -> CaseOutcome:
    return CaseOutcome(
        id=case.id,
        question=case.question,
        category=case.category,
        status=result.status,
        passed=passed,
        sql=result.sql,
        steps=result.steps,
        elapsed_ms=result.elapsed_ms,
        row_count=result.row_count,
        tokens=result.tokens,
        error=error or result.error,
        gold_rows=gold_rows,
        expect_refusal=case.expect_refusal,
        table_recall_at_5=table_recall,
        expected_tables=list(case.expected_tables),
    )


def _table_recall(agent: NL2SQLAgent, case: EvalCase) -> float | None:
    if not case.expected_tables:
        return None
    hits = {hit.table.name for hit in agent.catalog.search_tables(case.question, top_k=TABLE_RECALL_K)}
    expected = set(case.expected_tables)
    return round(len(hits & expected) / len(expected), 4)


def _category_metrics(outcomes: list[CaseOutcome]) -> dict[str, dict[str, Any]]:
    categories = sorted({case.category or "uncategorized" for case in outcomes})
    result: dict[str, dict[str, Any]] = {}
    for category in categories:
        subset = [case for case in outcomes if (case.category or "uncategorized") == category]
        passed = sum(1 for case in subset if case.passed)
        result[category] = {
            "cases": len(subset),
            "passed": passed,
            "accuracy": _ratio(passed, len(subset)),
            "avg_latency_ms": _mean([case.elapsed_ms for case in subset]),
        }
    return result


def _select_judge_runs(
    runs: list[tuple[EvalCase, Any, dict[str, Any] | None]],
    outcomes: list[CaseOutcome],
    *,
    on_fail: bool,
    sample: int | None,
    seed: int,
) -> list[tuple[EvalCase, Any, dict[str, Any] | None]]:
    selected = list(runs)
    if on_fail:
        selected = [run for index, run in enumerate(selected) if not outcomes[index].passed]
    if sample is not None and 0 < sample < len(selected):
        rng = random.Random(seed)
        picked = set(rng.sample(range(len(selected)), sample))
        selected = [run for index, run in enumerate(selected) if index in picked]
    return selected


def _build_judge_input(
    case: EvalCase,
    result: Any,
    gold: dict[str, Any] | None,
    agent: NL2SQLAgent,
    *,
    max_rows: int,
) -> JudgeInput:
    gold_columns = list((gold or {}).get("columns") or [])
    gold_rows = list((gold or {}).get("rows") or [])
    gold_columns, gold_rows, gold_truncated = truncate_result(
        gold_columns,
        gold_rows,
        max_rows=max_rows,
    )
    candidate_columns, candidate_rows, candidate_truncated = truncate_result(
        list(result.columns or []),
        list(result.rows or []),
        max_rows=max_rows,
    )
    return JudgeInput(
        case_id=case.id,
        question=case.question,
        category=case.category,
        gold_sql=case.gold_sql,
        gold_columns=gold_columns,
        gold_rows=gold_rows,
        candidate_status=result.status,
        candidate_sql=result.sql,
        candidate_columns=candidate_columns,
        candidate_rows=candidate_rows,
        candidate_answer=result.answer,
        candidate_confidence=result.confidence,
        expect_refusal=case.expect_refusal,
        schema_excerpt=build_schema_excerpt(agent.catalog, list(case.expected_tables)),
        truncated=gold_truncated or candidate_truncated,
    )


def _build_judge_report(
    verdicts: list[JudgeVerdict],
    *,
    total: int,
    sampled: int,
    limit_reached: bool,
    refusal_ids: set[str],
    model_name: str,
    prompt_version: str,
) -> dict[str, Any]:
    non_unknown = [verdict for verdict in verdicts if not verdict.is_unknown]
    refusal_verdicts = [verdict for verdict in verdicts if verdict.case_id in refusal_ids]
    issue_counts = Counter(issue for verdict in verdicts for issue in verdict.issues)
    cache_hits = sum(1 for verdict in verdicts if verdict.cached)
    tokens = sum(verdict.tokens for verdict in verdicts)
    metrics = {
        "judge_coverage": _ratio(len(non_unknown), total),
        "judge_sql_equivalent_rate": _verdict_rate(verdicts, "sql_equivalent"),
        "judge_result_equivalent_rate": _verdict_rate(verdicts, "result_equivalent"),
        "judge_answer_faithful_rate": _verdict_rate(verdicts, "answer_faithful"),
        "judge_answer_complete_rate": _verdict_rate(verdicts, "answer_complete"),
        "judge_refusal_appropriate_rate": _verdict_rate(refusal_verdicts, "refusal_appropriate"),
        "judge_unknown_rate": _ratio(len(verdicts) - len(non_unknown), len(verdicts)),
        "judge_cache_hits": float(cache_hits),
        "judge_tokens": float(tokens),
    }
    if not verdicts:
        status = "no_cases"
    elif limit_reached:
        status = "partial"
    else:
        status = "completed"
    return {
        "enabled": True,
        "status": status,
        "auxiliary": True,
        "model": model_name,
        "prompt_version": prompt_version,
        "judged_cases": len(verdicts),
        "sample_size": sampled,
        "unknown_cases": len(verdicts) - len(non_unknown),
        "cache_hits": cache_hits,
        "tokens": tokens,
        "max_calls_reached": limit_reached,
        "metrics": metrics,
        "issue_counts": dict(issue_counts),
        "verdicts": [verdict.to_dict() for verdict in verdicts],
        "disclaimer": "AI Judge 为辅助信号，不替代 execution_accuracy。",
    }


def _verdict_rate(verdicts: list[JudgeVerdict], field: str) -> float:
    values = [getattr(verdict, field) for verdict in verdicts]
    considered = [value for value in values if value in {"yes", "no"}]
    return _ratio(sum(1 for value in considered if value == "yes"), len(considered))


def _judge_placeholder() -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "not_run",
        "auxiliary": True,
        "model": "",
        "prompt_version": JUDGE_PROMPT_VERSION,
        "judged_cases": 0,
        "sample_size": 0,
        "unknown_cases": 0,
        "cache_hits": 0,
        "tokens": 0,
        "max_calls_reached": False,
        "metrics": {},
        "issue_counts": {},
        "verdicts": [],
        "disclaimer": "AI Judge 为辅助信号，不替代 execution_accuracy。",
        "message": "AI Judge 未启用；当前结论仅来自确定性 gold SQL 结果比对。",
    }


def _judge_markdown(judge: dict[str, Any]) -> str:
    if not judge.get("enabled"):
        return f"未启用（{judge.get('message', 'judge not run')}）。"
    lines = [
        f"> {judge.get('disclaimer', '')}",
        "",
        f"- Model: `{judge.get('model', '')}`",
        f"- Prompt: `{judge.get('prompt_version', '')}`",
        f"- Status: `{judge.get('status', '')}`",
        f"- Judged: {judge.get('judged_cases', 0)} / sampled {judge.get('sample_size', 0)}",
        f"- Unknown: {judge.get('unknown_cases', 0)}",
        f"- Cache hits: {judge.get('cache_hits', 0)}",
        f"- Judge tokens: {judge.get('tokens', 0)}",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for key, value in (judge.get("metrics") or {}).items():
        rendered = f"{value:.2%}" if key.endswith("_rate") or key == "judge_coverage" else value
        lines.append(f"| {key} | {rendered} |")
    issue_counts = judge.get("issue_counts") or {}
    if issue_counts:
        lines.extend(["", "### Issues", ""])
        for tag, count in sorted(issue_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{tag}`: {count}")
    verdicts = judge.get("verdicts") or []
    if verdicts:
        lines.extend(
            [
                "",
                "### Verdicts",
                "",
                "| case | sql | result | faithful | complete | refusal | severity | cached |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for verdict in verdicts:
            lines.append(
                f"| {verdict.get('case_id', '')} | {verdict.get('sql_equivalent', '')} | "
                f"{verdict.get('result_equivalent', '')} | {verdict.get('answer_faithful', '')} | "
                f"{verdict.get('answer_complete', '')} | {verdict.get('refusal_appropriate', '')} | "
                f"{verdict.get('severity', '')} | {'yes' if verdict.get('cached') else 'no'} |"
            )
    return "\n".join(lines)


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


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
