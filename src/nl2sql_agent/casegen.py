"""AI-drafted evaluation cases: generate, execute-check, dedupe, park as drafts.

The frozen set is hand-written and hand-reviewed, which is exactly why it can be
the ground truth. A model can draft candidates, but a draft must never be able to
move a metric: every row is written with ``status="draft"``, and the evaluator
counts non-active statuses as ``skipped`` instead of running them. Promotion to
the frozen set is a human edit, not a flag.

Three checks gate a draft, cheapest first, so a broken batch costs one call and no
database work:

* the contract — question + gold SQL present, tables real, no duplicate of an
  existing question after normalization;
* the guard — a draft that only parses as a write or an escape is junk, not a
  hard case;
* execution — gold SQL must actually run **and return rows**. A gold that answers
  zero rows grades any empty result as correct, so it measures nothing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from .catalog import Catalog
from .llm import usage_tokens
from .taxonomy import BUSINESS_CATEGORIES, classify_category
from .text import normalize_question, parse_json_object
from .tools.sql_tools import run_readonly_query

if TYPE_CHECKING:  # pragma: no cover
    from .guard import SQLGuard
    from .llm import ChatModel
    from .tools.db import Target

CASEGEN_PROMPT_VERSION = "casegen-v1"
DEFAULT_SOURCE = "ai_draft"

SYSTEM_PROMPT = (
    "你是 NL2SQL 评测用例生成器。只输出一个 JSON 对象，不要解释、不要代码块围栏。\n"
    "格式：{\"cases\": [{\"question\": str, \"gold_sql\": str, "
    "\"expected_tables\": [str], \"category\": str, \"difficulty\": \"easy|medium|hard\"}]}\n"
    "约束：gold_sql 必须是只读 SELECT（禁止 INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA、"
    "禁止读取系统表或用户表），只能用给定目录里真实存在的表和列；"
    "question 用中文业务问法，必须能被这条 gold_sql 唯一回答；"
    "category 从给定类别里选；expected_tables 填 gold_sql 真正引用的表。"
)


@dataclass
class DraftResult:
    """What one generation call produced, kept separate from what was rejected."""

    drafts: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    tokens: int = 0
    prompt_version: str = CASEGEN_PROMPT_VERSION

    @property
    def generated(self) -> int:
        return len(self.drafts) + len(self.rejected)


def schema_digest(catalog: Catalog, *, max_tables: int = 12) -> str:
    """Table/column lines the model may write against — the same view the agent gets."""
    lines: list[str] = []
    for table in catalog.enabled_tables()[:max_tables]:
        columns = ", ".join(
            f"{column.name}:{column.type}"
            for column in table.columns
            if not column.disabled
        )
        lines.append(f"{table.name}({columns})")
    return "\n".join(lines)


def seed_examples(cases: Iterable[Any], *, limit: int = 3) -> list[dict[str, Any]]:
    """A few frozen cases as shape anchors — question wording, not answers to copy."""
    seeds: list[dict[str, Any]] = []
    for case in list(cases)[:limit]:
        if is_dataclass(case):
            data = asdict(case)
        elif hasattr(case, "to_dict"):
            data = case.to_dict()
        else:
            data = dict(case)
        seeds.append(
            {
                "question": str(data.get("question", "")),
                "gold_sql": str(data.get("gold_sql", "")),
                "category": str(data.get("category", "")),
                "expected_tables": list(data.get("expected_tables") or []),
            }
        )
    return [seed for seed in seeds if seed["question"] and seed["gold_sql"]]


def build_user_prompt(
    catalog: Catalog,
    seeds: Sequence[dict[str, Any]],
    *,
    count: int,
    avoid: Sequence[str] = (),
) -> str:
    payload: dict[str, Any] = {
        "count": count,
        "schema": schema_digest(catalog),
        "categories": list(BUSINESS_CATEGORIES),
        "examples": list(seeds),
    }
    if avoid:
        payload["already_covered_questions"] = list(avoid)[:40]
    return json.dumps(payload, ensure_ascii=False)


def _row_error(
    row: Any,
    catalog: Catalog,
    seen: set[str],
    guard: "SQLGuard | None",
) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(row, dict):
        return "not_an_object", {}
    question = str(row.get("question") or "").strip()
    gold_sql = str(row.get("gold_sql") or "").strip()
    if not question or not gold_sql:
        return "missing_question_or_sql", {}
    key = normalize_question(question)
    if not key or key in seen:
        return "duplicate_question", {}
    tables = [str(item).strip().lower() for item in (row.get("expected_tables") or []) if str(item).strip()]
    if not tables:
        return "missing_expected_tables", {}
    unknown = [name for name in tables if catalog.table(name) is None]
    if unknown:
        return f"unknown_table:{unknown[0]}", {}
    if guard is not None:
        checked = guard.check(gold_sql)
        if not checked.valid:
            return f"guard_rejected:{','.join(issue.code for issue in checked.errors) or 'invalid'}", {}
    return None, {"question": question, "gold_sql": gold_sql, "tables": tables, "row": row}


def generate_cases(
    model: "ChatModel",
    catalog: Catalog,
    *,
    db_path: "Target | str | Path",
    count: int = 10,
    seeds: Sequence[dict[str, Any]] = (),
    avoid: Sequence[str] = (),
    guard: "SQLGuard | None" = None,
    max_rows: int = 200,
    timeout_ms: int = 5000,
) -> DraftResult:
    """Draft ``count`` cases, validate them, and return drafts plus rejection reasons."""
    result = DraftResult()
    if count <= 0:
        return result
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(catalog, seeds, count=count, avoid=avoid)},
    ]
    response = model.chat(messages)
    result.tokens = usage_tokens(getattr(response, "usage", None))
    content = str(getattr(response, "content", "") or "")
    payload = parse_json_object(content)
    if payload is None:
        result.rejected.append({"question": "", "reason": "unparseable_response"})
        return result
    rows = payload.get("cases")
    if not isinstance(rows, list):
        result.rejected.append({"question": "", "reason": "missing_cases_array"})
        return result

    seen = {normalize_question(text) for text in avoid if normalize_question(text)}
    for index, row in enumerate(rows):
        error, prepared = _row_error(row, catalog, seen, guard)
        if error:
            result.rejected.append(
                {"question": str((row or {}).get("question") if isinstance(row, dict) else "")[:120], "reason": error}
            )
            continue
        executed = run_readonly_query(
            db_path,
            prepared["gold_sql"],
            max_rows=max_rows,
            timeout_ms=timeout_ms,
        )
        if not executed.get("success"):
            result.rejected.append(
                {"question": prepared["question"][:120], "reason": f"gold_exec_failed:{executed.get('error')}"}
            )
            continue
        if not executed.get("rows"):
            result.rejected.append({"question": prepared["question"][:120], "reason": "gold_returned_no_rows"})
            continue
        seen.add(normalize_question(prepared["question"]))
        category = str(prepared["row"].get("category") or "").strip()
        if category not in BUSINESS_CATEGORIES:
            category = classify_category(prepared["question"])
        difficulty = str(prepared["row"].get("difficulty") or "").strip().lower()
        if difficulty not in {"easy", "medium", "hard"}:
            difficulty = "medium"
        result.drafts.append(
            {
                "id": f"ai_{index:03d}",
                "question": prepared["question"],
                "gold_sql": prepared["gold_sql"],
                "expected_tables": prepared["tables"],
                "category": category,
                "difficulty": difficulty,
                "source": DEFAULT_SOURCE,
                "status": "draft",
                "tags": ["ai_draft", CASEGEN_PROMPT_VERSION],
                "gold_row_count": int(executed.get("row_count") or len(executed.get("rows") or [])),
            }
        )
    return result


def write_cases(path: "str | Path", drafts: Sequence[dict[str, Any]]) -> int:
    """Append drafts as JSONL — one row per line, human-editable before promotion."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for draft in drafts:
            handle.write(json.dumps(draft, ensure_ascii=False) + "\n")
    return len(drafts)
