"""LLM-as-judge protocol, prompt construction and response parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings
from ..text import parse_json_object

JUDGE_PROMPT_VERSION = "judge-v2"
MAX_ROWS = 20
MAX_COLS = 12
MAX_CELL_CHARS = 80
MAX_INPUT_CHARS = 12000
MAX_ISSUES = 8
MAX_REASON_CHARS = 500
MAX_RAW_CHARS = 2000

SQL_EQUIVALENCE_VALUES = {"yes", "no", "unknown", "not_applicable"}
ANSWER_VALUES = {"yes", "no", "unknown"}
REFUSAL_VALUES = {"yes", "no", "unknown", "not_applicable"}
SEVERITY_VALUES = {"none", "minor", "major", "unknown"}
ISSUE_TAGS = frozenset(
    {
        "wrong_table",
        "wrong_filter",
        "wrong_join",
        "fanout",
        "wrong_metric",
        "hallucinated_number",
        "missing_filter",
        "unsafe_sql",
    }
)


class JudgeError(RuntimeError):
    pass


@dataclass
class JudgeInput:
    case_id: str
    question: str
    category: str = ""
    gold_sql: str = ""
    gold_columns: list[str] = field(default_factory=list)
    gold_rows: list[list[Any]] = field(default_factory=list)
    candidate_status: str = ""
    candidate_sql: str | None = None
    candidate_columns: list[str] = field(default_factory=list)
    candidate_rows: list[list[Any]] = field(default_factory=list)
    candidate_answer: str = ""
    candidate_confidence: float = 0.0
    expect_refusal: bool = False
    schema_excerpt: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "category": self.category,
            "gold_sql": self.gold_sql,
            "gold_columns": self.gold_columns,
            "gold_rows": self.gold_rows,
            "candidate_status": self.candidate_status,
            "candidate_sql": self.candidate_sql,
            "candidate_columns": self.candidate_columns,
            "candidate_rows": self.candidate_rows,
            "candidate_answer": self.candidate_answer,
            "candidate_confidence": self.candidate_confidence,
            "expect_refusal": self.expect_refusal,
            "schema_excerpt": self.schema_excerpt,
            "truncated": self.truncated,
        }

    def cache_payload(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "gold_sql": self.gold_sql,
            "candidate_sql": self.candidate_sql or "",
            "candidate_answer": self.candidate_answer,
            "candidate_status": self.candidate_status,
            "expect_refusal": self.expect_refusal,
        }


@dataclass
class JudgeVerdict:
    case_id: str
    sql_equivalent: str = "unknown"
    result_equivalent: str = "unknown"
    answer_faithful: str = "unknown"
    answer_complete: str = "unknown"
    refusal_appropriate: str = "unknown"
    severity: str = "unknown"
    issues: list[str] = field(default_factory=list)
    reason: str = ""
    model: str = ""
    prompt_version: str = JUDGE_PROMPT_VERSION
    cached: bool = False
    tokens: int = 0
    parse_error: str = ""
    raw: str = ""

    @property
    def is_unknown(self) -> bool:
        fields = [
            self.sql_equivalent,
            self.result_equivalent,
            self.answer_faithful,
            self.answer_complete,
        ]
        if self.refusal_appropriate != "not_applicable":
            fields.append(self.refusal_appropriate)
        return all(value == "unknown" for value in fields)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "sql_equivalent": self.sql_equivalent,
            "result_equivalent": self.result_equivalent,
            "answer_faithful": self.answer_faithful,
            "answer_complete": self.answer_complete,
            "refusal_appropriate": self.refusal_appropriate,
            "severity": self.severity,
            "issues": self.issues,
            "reason": self.reason,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "cached": self.cached,
            "tokens": self.tokens,
            "parse_error": self.parse_error,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JudgeVerdict":
        return cls(
            case_id=str(raw.get("case_id", "")),
            sql_equivalent=str(raw.get("sql_equivalent", "unknown")),
            result_equivalent=str(raw.get("result_equivalent", "unknown")),
            answer_faithful=str(raw.get("answer_faithful", "unknown")),
            answer_complete=str(raw.get("answer_complete", "unknown")),
            refusal_appropriate=str(raw.get("refusal_appropriate", "unknown")),
            severity=str(raw.get("severity", "unknown")),
            issues=[str(item) for item in raw.get("issues") or []],
            reason=str(raw.get("reason", "")),
            model=str(raw.get("model", "")),
            prompt_version=str(raw.get("prompt_version", JUDGE_PROMPT_VERSION)),
            cached=bool(raw.get("cached", False)),
            tokens=int(raw.get("tokens") or 0),
            parse_error=str(raw.get("parse_error", "")),
            raw=str(raw.get("raw", "")),
        )


class JudgeModel(Protocol):
    def judge(self, item: JudgeInput) -> JudgeVerdict:
        ...


def load_judge_prompt() -> str:
    path = Path(__file__).resolve().parents[1] / "data" / "judge_prompt.txt"
    return path.read_text(encoding="utf-8")


def truncate_result(
    columns: list[str],
    rows: list[list[Any]],
    *,
    max_rows: int = MAX_ROWS,
    max_cols: int = MAX_COLS,
    max_cell_chars: int = MAX_CELL_CHARS,
) -> tuple[list[str], list[list[Any]], bool]:
    truncated = len(columns) > max_cols or len(rows) > max_rows
    kept_columns = [str(column) for column in columns[:max_cols]]
    kept_rows: list[list[Any]] = []
    for row in rows[:max_rows]:
        if len(row) > max_cols:
            truncated = True
        values: list[Any] = []
        for value in row[:max_cols]:
            if isinstance(value, str) and len(value) > max_cell_chars:
                truncated = True
                values.append(value[: max_cell_chars - 3] + "...")
            else:
                values.append(value)
        kept_rows.append(values)
    return kept_columns, kept_rows, truncated


def build_judge_user_prompt(item: JudgeInput) -> str:
    sections = [
        ("question", item.question),
        ("category", item.category),
        ("expect_refusal", "true" if item.expect_refusal else "false"),
        ("schema_excerpt", item.schema_excerpt or "(not provided)"),
        ("gold_sql", item.gold_sql or "(not provided)"),
        ("gold_result", _format_result(item.gold_columns, item.gold_rows)),
        ("candidate_status", item.candidate_status or "(empty)"),
        ("candidate_sql", item.candidate_sql or "(none)"),
        ("candidate_result", _format_result(item.candidate_columns, item.candidate_rows)),
        ("candidate_answer", item.candidate_answer or "(empty)"),
        ("truncated", "true" if item.truncated else "false"),
    ]
    text = "\n".join(f"<{name}>{value}</{name}>" for name, value in sections)
    if len(text) > MAX_INPUT_CHARS:
        item.truncated = True
        text = text[: MAX_INPUT_CHARS - 18] + "\n... truncated ..."
    return text


def parse_verdict(
    content: str,
    *,
    case_id: str,
    model: str = "",
    prompt_version: str = JUDGE_PROMPT_VERSION,
) -> JudgeVerdict:
    verdict = JudgeVerdict(case_id=case_id, model=model, prompt_version=prompt_version)
    verdict.raw = (content or "")[:MAX_RAW_CHARS]
    payload = parse_json_object(content or "")
    if not isinstance(payload, dict):
        verdict.parse_error = "invalid_json"
        return verdict
    verdict.sql_equivalent = _enum(payload.get("sql_equivalent"), SQL_EQUIVALENCE_VALUES)
    verdict.result_equivalent = _enum(payload.get("result_equivalent"), SQL_EQUIVALENCE_VALUES)
    verdict.answer_faithful = _enum(payload.get("answer_faithful"), ANSWER_VALUES)
    verdict.answer_complete = _enum(payload.get("answer_complete"), ANSWER_VALUES)
    verdict.refusal_appropriate = _enum(payload.get("refusal_appropriate"), REFUSAL_VALUES)
    verdict.severity = _enum(payload.get("severity"), SEVERITY_VALUES)
    verdict.issues = _issues(payload.get("issues"))
    reason = str(payload.get("reason") or "").strip()
    verdict.reason = reason[:MAX_REASON_CHARS]
    return verdict


def build_schema_excerpt(catalog: Any, table_names: list[str], *, max_chars: int = 2000) -> str:
    lines: list[str] = []
    for name in table_names:
        table = catalog.table(name)
        if table is None or getattr(table, "disabled", False):
            continue
        lines.append(f"TABLE {table.name}: {table.description}")
        for column in table.columns:
            if getattr(column, "sensitive", False) or getattr(column, "disabled", False):
                continue
            detail = f"  {column.name} {column.type}"
            if column.description:
                detail += f" -- {column.description}"
            lines.append(detail)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 18] + "\n... truncated ..."
    return text


class LLMJudge:
    """OpenAI-compatible judge client. Failures degrade to unknown verdicts."""

    def __init__(self, settings: Settings) -> None:
        if settings.judge_provider != "openai":
            raise JudgeError(f"不支持的 Judge provider: {settings.judge_provider}")
        if not settings.judge_api_key:
            raise JudgeError("未配置 Judge API Key，请设置 NL2SQL_JUDGE_API_KEY 或复用 NL2SQL_API_KEY。")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise JudgeError("缺少 openai 依赖，请执行: pip install -e '.[llm]'") from exc
        self.settings = settings
        self.model = settings.judge_model
        self.prompt_version = settings.judge_prompt_version
        self.system_prompt = load_judge_prompt()
        self.client = OpenAI(
            api_key=settings.judge_api_key,
            base_url=settings.judge_base_url or settings.base_url,
            timeout=settings.request_timeout_sec,
        )
        self.calls = 0
        self.total_tokens = 0

    def judge(self, item: JudgeInput) -> JudgeVerdict:
        self.calls += 1
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": build_judge_user_prompt(item)},
            ],
            "temperature": 0.0,
            "max_tokens": self.settings.judge_max_tokens,
        }
        if self.settings.disable_thinking:
            kwargs["extra_body"] = {"enable_thinking": False}
        try:
            response = self._create(kwargs, json_mode=True)
        except Exception:
            try:
                response = self._create(kwargs, json_mode=False)
            except Exception as exc:  # noqa: BLE001 - judge failure must not abort evaluation
                return JudgeVerdict(
                    case_id=item.case_id,
                    model=self.model,
                    prompt_version=self.prompt_version,
                    parse_error=f"judge_call_failed: {exc}"[:MAX_REASON_CHARS],
                )
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage is not None else 0
        self.total_tokens += tokens
        if not response.choices:
            return JudgeVerdict(
                case_id=item.case_id,
                model=self.model,
                prompt_version=self.prompt_version,
                tokens=tokens,
                parse_error="judge_empty_choices",
            )
        content = response.choices[0].message.content or ""
        verdict = parse_verdict(
            content,
            case_id=item.case_id,
            model=self.model,
            prompt_version=self.prompt_version,
        )
        verdict.tokens = tokens
        return redact_verdict(
            verdict,
            [self.settings.judge_api_key, self.settings.api_key],
        )

    def _create(self, kwargs: dict[str, Any], *, json_mode: bool) -> Any:
        payload = dict(kwargs)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return self.client.chat.completions.create(**payload)


def redact_verdict(verdict: JudgeVerdict, secrets: list[str]) -> JudgeVerdict:
    for secret in secrets:
        if not secret:
            continue
        verdict.reason = verdict.reason.replace(secret, "***")
        verdict.raw = verdict.raw.replace(secret, "***")
    return verdict


def _format_result(columns: list[str], rows: list[list[Any]]) -> str:
    return json.dumps(
        {"columns": columns, "rows": rows},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _enum(value: Any, allowed: set[str]) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else "unknown"


def _issues(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        tag = str(item or "").strip().lower()
        if tag in ISSUE_TAGS and tag not in result:
            result.append(tag)
        if len(result) >= MAX_ISSUES:
            break
    return result
