"""Human-labeled calibration for the AI judge."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools.sql_tools import run_readonly_query
from .judge import (
    JUDGE_PROMPT_VERSION,
    JudgeInput,
    JudgeModel,
    JudgeVerdict,
    build_schema_excerpt,
    truncate_result,
)

_LABEL_VALUES = {
    "sql_equivalent": {"yes", "no", "unknown"},
    "answer_faithful": {"yes", "no", "unknown"},
    "refusal_appropriate": {"yes", "no", "unknown", "not_applicable"},
}
_LABEL_DEFAULTS = {
    "sql_equivalent": "unknown",
    "answer_faithful": "unknown",
    "refusal_appropriate": "not_applicable",
}


@dataclass(frozen=True)
class CalibrationCase:
    id: str
    question: str
    gold_sql: str
    candidate_sql: str
    candidate_answer: str
    human_labels: dict[str, str] = field(default_factory=dict)
    expected_tables: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CalibrationCase":
        case_id = str(raw.get("id") or "").strip()
        question = str(raw.get("question") or "").strip()
        gold_sql = str(raw.get("gold_sql") or "").strip()
        candidate_sql = str(raw.get("candidate_sql") or "").strip()
        if not case_id or not question or not gold_sql or not candidate_sql:
            raise ValueError("校准样本必须包含 id/question/gold_sql/candidate_sql")
        labels: dict[str, str] = {}
        raw_labels = raw.get("human_labels") or {}
        for name, allowed in _LABEL_VALUES.items():
            value = str(raw_labels.get(name, _LABEL_DEFAULTS[name])).strip().lower()
            if value not in allowed:
                raise ValueError(f"校准样本 {case_id} 的 {name} 标签非法: {value}")
            labels[name] = value
        return cls(
            id=case_id,
            question=question,
            gold_sql=gold_sql,
            candidate_sql=candidate_sql,
            candidate_answer=str(raw.get("candidate_answer") or ""),
            human_labels=labels,
            expected_tables=tuple(
                str(item) for item in raw.get("expected_tables") or [] if str(item).strip()
            ),
        )


@dataclass
class CalibrationResult:
    total: int
    evaluated: int
    unknown: int
    agreement: float
    field_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    model: str = ""
    prompt_version: str = JUDGE_PROMPT_VERSION
    verdicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "evaluated": self.evaluated,
            "unknown": self.unknown,
            "agreement": self.agreement,
            "field_metrics": self.field_metrics,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "verdicts": self.verdicts,
        }


def load_calibration(path: str | Path) -> list[CalibrationCase]:
    cases: list[CalibrationCase] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                cases.append(CalibrationCase.from_dict(json.loads(line)))
    return cases


def cohen_kappa(pairs: list[tuple[str, str]]) -> float:
    if not pairs:
        return 0.0
    total = len(pairs)
    observed = sum(1 for predicted, human in pairs if predicted == human) / total
    predicted_counts = Counter(predicted for predicted, _ in pairs)
    human_counts = Counter(human for _, human in pairs)
    expected = sum(
        (predicted_counts[label] / total) * (human_counts[label] / total)
        for label in set(predicted_counts) | set(human_counts)
    )
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return round((observed - expected) / (1.0 - expected), 4)


def calibrate(
    judge_model: JudgeModel,
    cases: list[CalibrationCase],
    *,
    db_path: str | Path,
    catalog: Any,
    max_rows: int = 20,
) -> CalibrationResult:
    verdicts: list[JudgeVerdict] = []
    comparisons: dict[str, list[tuple[str, str]]] = {name: [] for name in _LABEL_VALUES}
    sample_matches = 0
    sample_considered = 0

    for case in cases:
        gold = run_readonly_query(db_path, case.gold_sql, max_rows=1000, timeout_ms=10000)
        candidate = run_readonly_query(db_path, case.candidate_sql, max_rows=1000, timeout_ms=10000)
        gold_columns, gold_rows, gold_truncated = truncate_result(
            list(gold.get("columns") or []),
            list(gold.get("rows") or []),
            max_rows=max_rows,
        )
        candidate_columns, candidate_rows, candidate_truncated = truncate_result(
            list(candidate.get("columns") or []),
            list(candidate.get("rows") or []),
            max_rows=max_rows,
        )
        item = JudgeInput(
            case_id=case.id,
            question=case.question,
            gold_sql=case.gold_sql,
            gold_columns=gold_columns,
            gold_rows=gold_rows,
            candidate_status="ok" if candidate.get("success") else "failed",
            candidate_sql=case.candidate_sql,
            candidate_columns=candidate_columns,
            candidate_rows=candidate_rows,
            candidate_answer=case.candidate_answer,
            schema_excerpt=build_schema_excerpt(catalog, list(case.expected_tables)),
            truncated=gold_truncated or candidate_truncated,
        )
        verdict = judge_model.judge(item)
        verdicts.append(verdict)

        considered = False
        matched = True
        for name, human_label in case.human_labels.items():
            if human_label == "not_applicable":
                continue
            predicted = str(getattr(verdict, name, "unknown"))
            comparisons[name].append((predicted, human_label))
            considered = True
            if predicted != human_label:
                matched = False
        if considered:
            sample_considered += 1
            if matched:
                sample_matches += 1

    field_metrics = {
        name: {
            "considered": len(pairs),
            "agreement": _ratio(
                sum(1 for predicted, human in pairs if predicted == human),
                len(pairs),
            ),
            "kappa": cohen_kappa(pairs),
        }
        for name, pairs in comparisons.items()
        if pairs
    }
    return CalibrationResult(
        total=len(cases),
        evaluated=len(verdicts),
        unknown=sum(1 for verdict in verdicts if verdict.is_unknown),
        agreement=_ratio(sample_matches, sample_considered),
        field_metrics=field_metrics,
        model=str(getattr(judge_model, "model", "") or ""),
        prompt_version=str(
            getattr(judge_model, "prompt_version", "") or JUDGE_PROMPT_VERSION
        ),
        verdicts=[verdict.to_dict() for verdict in verdicts],
    )


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0
