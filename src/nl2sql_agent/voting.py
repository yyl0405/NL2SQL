"""Second-opinion generation for the dual-channel vote.

One question, two independent SQL-generation paths, both executed for real. If
they return the same result set the answer is worth more confidence; if they
return different result sets the answer is worth a human, not a coin flip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .catalog import Catalog
from .config import Settings
from .dialects import get as get_dialect
from .llm import ChatModel, LLMError, usage_tokens
from .mock_llm import RulePlanner
from .text import parse_json_object

MAX_PROMPT_TABLES = 6
_SELECT_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


@dataclass
class SecondOpinion:
    """What the second channel produced — a candidate SQL, or why it has none."""

    sql: str = ""
    source: str = ""
    error: str = ""
    tokens: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.sql) and not self.error


class SecondOpinionGenerator(Protocol):
    source: str

    def generate(self, question: str, tables: list[str]) -> SecondOpinion:
        ...


class RuleBasedSecondOpinion:
    """Offline channel: the rule templates write SQL directly, skipping the tool loop.

    This is a genuinely independent path for the synthetic demo catalog — the
    primary answer came from a tool-calling loop, this one from a planner that
    only sees the question. It cannot express SQL for a real ERP catalog, so on
    a non-demo catalog it reports "no SQL" and the vote simply does not happen.
    """

    source = "rule"

    def __init__(self, catalog: Catalog, today: str = "2026-07-01") -> None:
        self.planner = RulePlanner(catalog, today)

    def generate(self, question: str, tables: list[str]) -> SecondOpinion:
        plan = self.planner.plan(question)
        if not plan.sql:
            return SecondOpinion(
                source=self.source,
                error=plan.reason or "规则通道未能生成 SQL",
            )
        return SecondOpinion(sql=plan.sql, source=self.source)


class LLMSecondOpinion:
    """Real-model channel: one tool-free call, contract ``{"sql": "..."}``.

    No tools on purpose: a second loop would re-use the same retrieval the first
    channel already trusted, so agreement would prove nothing about the SQL.
    """

    source = "llm"

    def __init__(self, llm: ChatModel, catalog: Catalog, settings: Settings) -> None:
        self.llm = llm
        self.catalog = catalog
        self.settings = settings

    def generate(self, question: str, tables: list[str]) -> SecondOpinion:
        names = self._candidate_tables(question, tables)
        if not names:
            return SecondOpinion(source=self.source, error="第二通道找不到可参考的表")
        try:
            response = self.llm.chat(
                [
                    {"role": "system", "content": self._prompt(names)},
                    {"role": "user", "content": question},
                ]
            )
        except LLMError as exc:
            return SecondOpinion(source=self.source, error=f"第二通道模型调用失败: {exc}")
        tokens = usage_tokens(response.usage)
        sql = extract_sql(response.content)
        if not sql:
            return SecondOpinion(
                source=self.source,
                error="第二通道没有返回 SQL",
                tokens=tokens,
            )
        return SecondOpinion(sql=sql, source=self.source, tokens=tokens)

    def _candidate_tables(self, question: str, tables: list[str]) -> list[str]:
        known = [name for name in dict.fromkeys(tables) if self.catalog.table(name) is not None]
        if known:
            return known[:MAX_PROMPT_TABLES]
        return [hit.table.name for hit in self.catalog.search_tables(question, top_k=MAX_PROMPT_TABLES)]

    def _prompt(self, names: list[str]) -> str:
        profile = get_dialect(self.catalog.dialect)
        dialect = profile.channel_label
        blocks: list[str] = []
        for name in names:
            table = self.catalog.table(name)
            if table is None or table.disabled:
                continue
            columns = ", ".join(
                f"{column.name} {column.type}"
                for column in table.columns
                if not column.disabled and not column.sensitive
            )
            blocks.append(f"- {table.name}: {table.description}\n  可用字段: {columns}")
        schema = "\n".join(blocks)
        as_of = self.catalog.as_of_date or self.settings.demo_today
        row_limit = profile.row_limit_hint
        return f"""你是独立复核通道。针对用户问题，自己写出一条 {dialect} 只读 SELECT 并返回结果。

基准日期: {as_of}

可参考的表（字段名只能来自这里）:
{schema}

硬性要求:
- 只输出单条 SELECT/WITH 查询；禁止 SELECT *、写操作、多语句、调用不存在的字段。
- 相对时间按基准日期换算（今年=所在自然年，本月=所在月，上月=前一个月）。
- 金额保留 2 位，比率保留 4 位；{row_limit}。
- 只输出 JSON，不要解释: {{"sql": "单条 SQL"}}
"""


def extract_sql(content: str) -> str:
    """Accept the JSON contract and a bare statement; reject anything else."""
    payload = parse_json_object(content) or {}
    candidate = str(payload.get("sql") or "").strip()
    if not candidate:
        candidate = re.sub(r"^\s*```(?:sql)?|```\s*$", "", str(content or "").strip(), flags=re.IGNORECASE)
        candidate = candidate.strip().strip("`").strip()
    candidate = candidate.rstrip(";").strip()
    if not _SELECT_START.match(candidate):
        return ""
    return candidate


def should_verify(
    settings: Settings,
    *,
    status: str,
    confidence: float,
    category: str,
    executed: bool,
) -> bool:
    """Ask a second channel only where an error would cost something.

    Voting doubles model calls and query load, so it triggers on the two cases
    where a wrong number is expensive or likely: low confidence, and the
    aggregation-heavy categories (JOIN fan-out and recursive BOM expansion).
    """
    if not settings.dual_channel or status != "ok" or not executed:
        return False
    if confidence < settings.dual_channel_confidence_threshold:
        return True
    return category in {item for item in settings.dual_channel_categories if item}
