"""Prompt building for the deterministic question-to-answer pipeline.

Shipped NL2SQL products do not let the model wander over the catalog: they
retrieve a bounded schema slice, generate one query, run it, and phrase the
result. This module holds only the prompts and the schema rendering that path
needs - the orchestration lives in ``agent.NL2SQLAgent._run_pipeline``.

The prompt is deliberately narrower than the agent prompt: no tools are
available, so the model is told exactly which tables and columns exist and is
given an explicit escape hatch (``{"sql": ""}``) when that slice is not enough.
An empty answer escalates to the tool loop instead of guessing a column name.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .catalog import Catalog
from .config import Settings
from .dialects import get as get_dialect
from .text import parse_json_object

#: Tables whose columns are handed to the generation call. Beyond this the prompt
#: stops helping (the model picks a column from a longer list it never read) and
#: starts costing tokens.
MAX_PIPELINE_TABLES = 6
#: Rows the phrasing call may quote. A top-N answer fits; a 200-row dump would be
#: paid for twice (once to send, once for the model to re-read it back at us).
MAX_PHRASE_ROWS = 20
#: Value probes per turn. A `LIKE '%x%'` scan is the one pipeline stage that can be
#: slow on a real ERP table, so the budget is explicit instead of "every text column".
MAX_VALUE_PROBES = 8
#: Code-like mentions probed at most per turn.
MAX_VALUE_MENTIONS = 2

#: A value the user typed as a code: letters plus digits (M7, AB12, 2.15.08). Pure
#: letter runs are skipped because they are indistinguishable from an identifier,
#: and Chinese mentions have no token boundary to cut on - the business dictionary
#: covers those.
_CODE_MENTION = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*\d[A-Za-z0-9_.\-]*|\d+(?:\.\d+)+")
#: Column types a value probe is worth running: numeric and date columns cannot
#: hold the 机型/客户 wording a code maps to.
_TEXT_TYPE = re.compile(r"(char|text|nstr|clob|string)", re.IGNORECASE)


def value_mentions(question: str, known_names: Iterable[str], *, limit: int = MAX_VALUE_MENTIONS) -> list[str]:
    """Code-like tokens in the question that are not a table or column name.

    "物料编码表里 M7 机型" has to be read as the stored value `M7机型`, not as
    `Code = 'M7'`; the first step is noticing that M7 is a value mention.
    """
    skip = {str(name).lower() for name in known_names}
    found: list[str] = []
    for token in _CODE_MENTION.findall(question or ""):
        cleaned = token.strip("._-")
        if len(cleaned) < 2 or cleaned.lower() in skip or cleaned in found:
            continue
        found.append(cleaned)
    return found[: max(1, int(limit))]


def probe_candidates(catalog: Catalog, names: list[str], *, limit: int = MAX_VALUE_PROBES) -> list[tuple[str, str]]:
    """(table, column) pairs a value probe is allowed to scan, cheapest scope first."""
    pairs: list[tuple[str, str]] = []
    for name in names:
        table = catalog.table(name)
        if table is None or table.disabled:
            continue
        for column in table.columns:
            if column.disabled or column.sensitive:
                continue
            if not _TEXT_TYPE.search(column.type or ""):
                continue
            if (table.name, column.name) not in pairs:
                pairs.append((table.name, column.name))
            if len(pairs) >= limit:
                return pairs
    return pairs


def value_probe_block(hits: list[dict[str, Any]]) -> str:
    """Render probed values for the generation prompt; "" when nothing was found."""
    lines = []
    for hit in hits:
        values = [str(item) for item in (hit.get("values") or []) if str(item).strip()]
        if not values:
            continue
        lines.append(
            f"- {hit.get('table')}.{hit.get('column')}（{hit.get('mention')}）: "
            + ", ".join(values[:5])
        )
    if not lines:
        return ""
    return (
        "运行时探测到的真实取值（问题里的代码/简称在这些列里有对应值，"
        "等值匹配请用下面的完整值，不要拿简称片段去比）:\n" + "\n".join(lines)
    )


def _filter_line(filters: tuple[dict[str, Any], ...], *, kind: str) -> str:
    parts = []
    for item in filters:
        column = str(item.get("column") or "").strip()
        value = str(item.get("value") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not column:
            continue
        predicate = f"{column} = '{value}'" if value else column
        parts.append(f"{predicate}" + (f"（{reason}）" if reason else ""))
    if not parts:
        return ""
    label = "必要 JOIN/过滤条件" if kind == "required" else "默认口径"
    return f"  {label}: " + "; ".join(parts)


def schema_block(catalog: Catalog, names: list[str]) -> str:
    """Render the tables the generation call is allowed to use.

    Disabled and sensitive columns are left out rather than marked: this prompt
    has no tool round in which the model could react to a marker, and the guard
    rejects either kind, so a rejected column is a wasted turn.
    """
    lines: list[str] = []
    for name in names:
        table = catalog.table(name)
        if table is None or table.disabled:
            continue
        columns = ", ".join(
            f"{column.name} {column.type}"
            + (f"[{column.description}]" if column.description else "")
            for column in table.columns
            if not column.disabled and not column.sensitive
        )
        block = [f"- {table.name}: {table.description}", f"  可用字段: {columns}"]
        if table.primary_key:
            block.append(f"  主键: {', '.join(table.primary_key)}")
        for kind, filters in (
            ("required", table.required_filters),
            ("default", table.default_filters),
        ):
            line = _filter_line(filters, kind=kind)
            if line:
                block.append(line)
        lines.append("\n".join(block))
    return "\n".join(lines)


def relationship_block(catalog: Catalog, names: list[str]) -> str:
    rows = catalog.relationships_for(names)
    if not rows:
        return "（无）"
    return "\n".join(
        f"- {item.get('left')} = {item.get('right')}"
        + (f"（{item.get('type')}）" if item.get("type") else "")
        for item in rows
    )


def generation_prompt(
    catalog: Catalog,
    settings: Settings,
    *,
    names: list[str],
    examples: str = "",
    carry: str = "",
    values: str = "",
) -> str:
    dialect = get_dialect(catalog.dialect)
    rules = "\n".join(f"- {rule}" for rule in catalog.business_rules) or "（无）"
    value_block = f"\n{values}\n" if values else ""
    few_shot = (
        f"""
参考经验（人工验证过的相似问法，只作 SQL 写法参考，表名与字段仍只能取上面列出的）:
{examples}
"""
        if examples
        else ""
    )
    row_limit = dialect.row_limit_hint
    as_of = catalog.as_of_date or settings.demo_today
    return f"""你是面向离散制造 ERP 的 NL2SQL 生成通道。这一轮没有工具：可用的表和字段只有下面列出的这些。

数据库: {dialect.label}（只读）
今天/基准日期: {as_of}

可参考的表:
{schema_block(catalog, names)}
{value_block}
表关系:
{relationship_block(catalog, names)}

业务口径:
{rules}
{few_shot}
硬性要求（违反任何一条都会被护栏拒绝）:
- 只输出一条 {dialect.label} SELECT 或 WITH ... SELECT；禁止写操作、禁止多语句、禁止 SELECT *。
- 表名和字段名只能来自上面列出的表；不确定的中文取值改用 LIKE，不要凭空猜一个精确值。
- 过滤条件按问题的字面口径落地：说"不为空"就只写 IS NOT NULL，不要自己再追加 0 或空串这类哨兵条件。
- 按某个度量排序或比较时，把那个度量一起写进 SELECT 并起别名——用户问"最高的前10个"也要看到那个数；只有问题明确只要名单时才只给标识列。
- 相对时间按基准日期换算（今年=所在自然年，本月=所在月，上月=前一个月）。
- 金额保留 2 位，比率保留 4 位；{row_limit}。
- 上面这些表不足以回答时，输出空 SQL 而不是猜字段：{{"sql": ""}}。

只输出 JSON，不要解释: {{"sql": "单条 SQL", "assumptions": ["用到的口径说明"]}}
{carry}"""


def guard_repair_prompt(errors: list[str]) -> str:
    listing = "\n".join(f"- {item}" for item in errors if item)
    return (
        "上一条 SQL 没有通过护栏，不能执行。请按下面的问题逐条修正，字段与表仍然只能"
        "用之前列出的那些；如果无法在给定表上修正，就输出空 SQL：\n"
        f"{listing}\n"
        "仍然只输出 JSON: {\"sql\": \"修正后的单条 SQL\", \"assumptions\": [...]}"
    )


def execution_repair_prompt(error: str) -> str:
    return (
        "上一条 SQL 通过了护栏但数据库报错："
        f"{(error or '')[:600]}\n"
        "请只针对这个报错做最小改动（例如去掉结果里不存在的时间差计算、改用已列出的字段），"
        "不要改变问题口径；无法修复就输出空 SQL。\n"
        "仍然只输出 JSON: {\"sql\": \"修正后的单条 SQL\", \"assumptions\": [...]}"
    )


def phrasing_messages(question: str, sql: str, execution: dict[str, Any]) -> list[dict[str, str]]:
    columns = list(execution.get("columns") or [])
    rows = list(execution.get("rows") or [])[:MAX_PHRASE_ROWS]
    payload = json.dumps(
        {"columns": columns, "rows": rows, "row_count": execution.get("row_count", len(rows))},
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    truncated = (
        f"（结果共 {execution.get('row_count')} 行，这里只给出前 {len(rows)} 行）"
        if execution.get("truncated")
        else ""
    )
    return [
        {
            "role": "system",
            "content": (
                "你是问数结果解读助手。只能引用给出的查询结果，不得补充、推算或编造任何"
                "结果里没有的数字；结果为空就直说没有查到。用中文一句话回答，需要列举时"
                "按给出的顺序。\n只输出 JSON: {\"answer\": \"一句话结论\"}"
            ),
        },
        {
            "role": "user",
            "content": f"问题: {question}\n执行的 SQL: {sql}\n查询结果{truncated}:\n{payload}",
        },
    ]


def answer_from(content: str) -> str:
    """The phrasing contract, or the raw text when the model ignored the contract."""
    payload = parse_json_object(content) or {}
    answer = str(payload.get("answer") or "").strip()
    if answer:
        return answer
    return str(content or "").strip()


def assumptions_from(content: str) -> list[str]:
    payload = parse_json_object(content) or {}
    raw = payload.get("assumptions")
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return []
