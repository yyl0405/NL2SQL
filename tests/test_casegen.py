"""P0.5 AI case drafting: contract, execution check, dedupe, draft-only status."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nl2sql_agent.casegen import (
    CASEGEN_PROMPT_VERSION,
    build_user_prompt,
    generate_cases,
    schema_digest,
    seed_examples,
    write_cases,
)
from nl2sql_agent.evaluation import evaluate, load_cases
from nl2sql_agent.guard import SQLGuard
from nl2sql_agent.llm import LLMResponse

GOOD_SQL = "SELECT region, COUNT(*) AS orders FROM sales_order GROUP BY region"


class FakeModel:
    """One chat turn, replying with a fixed payload — the model is not under test."""

    def __init__(self, payload: Any, *, tokens: int = 120) -> None:
        self.payload = payload
        self.tokens = tokens
        self.calls = 0
        self.messages: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        self.calls += 1
        self.messages = messages
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return LLMResponse(content=content, usage={"total_tokens": self.tokens})


def draft_row(question: str, sql: str = GOOD_SQL, **overrides: Any) -> dict[str, Any]:
    row = {
        "question": question,
        "gold_sql": sql,
        "expected_tables": ["sales_order"],
        "category": "group_by",
        "difficulty": "easy",
    }
    row.update(overrides)
    return row


def cases_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"cases": rows}


def test_drafts_are_parked_as_draft_never_active(catalog, runtime) -> None:
    model = FakeModel(cases_payload([draft_row("各区域有多少销售订单？")]))
    result = generate_cases(
        model,
        catalog,
        db_path=runtime.settings.db_path,
        count=1,
        guard=runtime.guard,
    )
    assert result.generated == 1
    assert result.rejected == []
    assert result.tokens == 120
    draft = result.drafts[0]
    assert draft["status"] == "draft"
    assert draft["source"] == "ai_draft"
    assert draft["category"] == "group_by"
    assert draft["gold_row_count"] >= 1
    assert draft["tags"] == ["ai_draft", CASEGEN_PROMPT_VERSION]


def test_prompt_carries_schema_seeds_and_count(catalog, runtime) -> None:
    model = FakeModel(cases_payload([]))
    seeds = seed_examples(load_cases(Path("src/nl2sql_agent/data/demo_cases.jsonl")), limit=3)
    generate_cases(
        model,
        catalog,
        db_path=runtime.settings.db_path,
        count=5,
        seeds=seeds,
        avoid=["库存低于安全库存的物料有哪些？"],
    )
    user = json.loads(model.messages[-1]["content"])
    assert user["count"] == 5
    assert "sales_order(" in user["schema"]
    assert len(user["examples"]) == 3
    assert user["already_covered_questions"] == ["库存低于安全库存的物料有哪些？"]


def test_schema_digest_skips_disabled_columns(catalog) -> None:
    digest = schema_digest(catalog)
    assert "sales_order(" in digest
    for line in digest.splitlines():
        assert "password" not in line.lower()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("这不是 JSON", "unparseable_response"),
        ({"not_cases": []}, "missing_cases_array"),
    ],
)
def test_unusable_response_rejects_whole_batch(payload, reason, catalog, runtime) -> None:
    result = generate_cases(
        FakeModel(payload),
        catalog,
        db_path=runtime.settings.db_path,
        count=2,
    )
    assert result.drafts == []
    assert result.rejected == [{"question": "", "reason": reason}]


def _reject_reason(rows: list[dict[str, Any]], catalog, runtime, **kwargs: Any) -> str:
    result = generate_cases(
        FakeModel(cases_payload(rows)),
        catalog,
        db_path=runtime.settings.db_path,
        count=len(rows),
        **kwargs,
    )
    assert result.drafts == []
    return result.rejected[0]["reason"]


def test_duplicate_within_batch_and_against_frozen_set(catalog, runtime) -> None:
    row = draft_row("各区域有多少销售订单？")
    again = dict(row, question="各区域有多少 销售订单？")  # same after normalization
    batch = generate_cases(
        FakeModel(cases_payload([row, again])),
        catalog,
        db_path=runtime.settings.db_path,
        count=2,
    )
    assert [draft["question"] for draft in batch.drafts] == [row["question"]]
    assert batch.rejected == [{"question": again["question"], "reason": "duplicate_question"}]
    assert _reject_reason(
        [row],
        catalog,
        runtime,
        avoid=["各区域有多少销售订单？"],
    ).startswith("duplicate_question")


def test_missing_or_unknown_contract_fields(catalog, runtime) -> None:
    assert _reject_reason([dict(draft_row("问题"), gold_sql="")], catalog, runtime) == "missing_question_or_sql"
    assert _reject_reason([dict(draft_row("问题"), expected_tables=[])], catalog, runtime) == "missing_expected_tables"
    assert _reject_reason(
        [dict(draft_row("问题"), expected_tables=["nope_table"])],
        catalog,
        runtime,
    ).startswith("unknown_table")


def test_guard_rejects_a_write_disguised_as_a_case(catalog, runtime) -> None:
    reason = _reject_reason(
        [draft_row("删掉所有订单", sql="DELETE FROM sales_order")],
        catalog,
        runtime,
        guard=runtime.guard,
    )
    assert reason.startswith("guard_rejected")


def test_gold_sql_that_does_not_run_is_dropped(catalog, runtime) -> None:
    reason = _reject_reason(
        [draft_row("问题", sql="SELECT not_a_column FROM sales_order", expected_tables=["sales_order"])],
        catalog,
        runtime,
    )
    assert reason.startswith("gold_exec_failed")


def test_gold_that_returns_zero_rows_measures_nothing(catalog, runtime) -> None:
    reason = _reject_reason(
        [draft_row("问题", sql="SELECT * FROM sales_order WHERE region = '不存在区域'")],
        catalog,
        runtime,
    )
    assert reason == "gold_returned_no_rows"


def test_bogus_category_falls_back_to_the_rule_classifier(catalog, runtime) -> None:
    row = draft_row("库存低于安全库存的物料有哪些？", category="随便写的", difficulty="超难")
    result = generate_cases(
        FakeModel(cases_payload([row])),
        catalog,
        db_path=runtime.settings.db_path,
        count=1,
    )
    assert result.rejected == []
    assert result.drafts[0]["category"] != "随便写的"
    assert result.drafts[0]["difficulty"] == "medium"


def test_drafts_cannot_move_a_frozen_metric(catalog, runtime, tmp_path: Path) -> None:
    """The whole point of status=draft: the evaluator skips the row instead of running it."""
    model = FakeModel(cases_payload([draft_row("各区域有多少销售订单？")]))
    result = generate_cases(model, catalog, db_path=runtime.settings.db_path, count=1, guard=runtime.guard)
    out = tmp_path / "ai_draft_cases.jsonl"
    assert write_cases(out, result.drafts) == 1
    assert write_cases(out, result.drafts) == 1  # appends, so a human can diff the batch

    loaded = load_cases(out)
    assert [case.status for case in loaded] == ["draft", "draft"]
    report = evaluate(
        runtime.agent,
        loaded,
        db_path=runtime.settings.db_path,
        guard=SQLGuard(catalog),
        report_dir=None,
    )
    assert report.total == 0
    assert report.skipped == 2
    assert report.metrics["execution_accuracy"] == 0.0
