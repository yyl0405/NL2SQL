"""Conversation state: the query slots a follow-up question inherits.

Multi-turn support is deliberately built on explicit slots rather than on
prompting the model with the raw transcript: the resolved question is a
standalone string, so the single-turn tool loop, the guard, the deterministic
evaluator and the failure queue all keep working unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .demo.seed import CUSTOMER_NAMES, MATERIAL_NAMES, REGIONS

SLOT_FIELDS = (
    "region",
    "customer",
    "material",
    "order_id",
    "month",
    "date",
    "limit",
    "dimension",
    "metric",
    "direction",
)

# Entity slots anaphora can point at ("那个呢" -> which of these?).
ENTITY_FIELDS = ("region", "customer", "material", "order_id")

_MONTH = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
_MONTH_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})(?![\d-])")
_MONTH_BARE = re.compile(r"(?<![\d年-])(\d{1,2})\s*月")
_FULL_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
# \b does not work here: CJK characters count as word characters, so an ISO
# value glued to Chinese ("截至2026-06-15未") would never match.
_DATE_ISO = re.compile(r"(?<![\d-])(\d{4}-\d{2}-\d{2})(?![\d-])")
_ORDER_ID = re.compile(r"ORD-\d+", re.IGNORECASE)
_LIMIT = re.compile(r"(?:前|top)\s*(\d+)|(\d+)\s*(?:个|家|位|名)", re.IGNORECASE)

DIMENSION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("region", r"各地区|各区域|分地区|按地区|按区域|地区排名|区域排名"),
    ("customer", r"各客户|按客户|每个客户|客户排名"),
    ("category", r"各分类|按分类|分类统计|按类别|各类别|分类金额"),
    ("supplier", r"各供应商|按供应商|供应商排名"),
    ("status", r"各状态|按状态|状态统计|状态分布"),
    ("material", r"各物料|按物料|物料排名"),
)

# First match wins, so narrower business metrics are listed before the generic
# revenue catch-all.
METRIC_PATTERNS: tuple[tuple[str, str], ...] = (
    ("kitting", r"齐套|缺料|欠料|配套率"),
    ("bom", r"bom|物料清单|子件|母件"),
    ("safety_stock", r"安全库存"),
    ("in_transit", r"在途"),
    ("available_stock", r"可用库存|可用量|可用数"),
    ("inventory_value", r"库存金额|库存价值"),
    ("completion_rate", r"完工率|完工进度"),
    ("low_completion", r"完工数量"),
    ("purchase_received", r"采购到货|已到货|到货数量|收货"),
    ("overdue", r"逾期|未发货"),
    ("order_count", r"状态.{0,6}统计|订单数量|订单数|多少单"),
    ("customer_orders", r"订单明细|订单列表"),
    ("revenue", r"销售|销售额|金额|总额|营收|卖了"),
)

ASC_PATTERN = re.compile(r"最低|最小|倒数|升序|从低到高|由低到高")
DESC_PATTERN = re.compile(r"最高|最大|排名|排行|降序|从高到低|由大到小|前\s*\d")

# Words that only make sense when something came before.
FOLLOWUP_MARKERS = (
    "那",
    "呢",
    "同样",
    "一样",
    "同上",
    "再看",
    "换成",
    "改成",
    "改为",
    "换个",
    "拆开",
    "分开看",
    "它",
    "这个",
    "这些",
    "其中",
    "按",
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


# 「客户」「物料」后跟的词只有短名词才可能是实体名，否则捕到的是疑问词尾巴。
_ENTITY_NOISE = re.compile(
    r"多少|怎样|怎么|如何|哪些|哪个|什么|情况|满意度|评分|是谁|排名|明细|列表|状态|名称|名字"
)


def _derived_entity(text: str, noun: str) -> str:
    match = re.search(rf"{noun}[\"“」']?([^，。？?、,;；\s\"”」]+)", text)
    if match is None:
        return ""
    raw = match.group(1).strip("\"“」'")
    if not 2 <= len(raw) <= 6:
        return ""
    if raw[0] in "呢吗的了吧啊咋怎哪这那它某每是谁有哪为":
        return ""
    return "" if _ENTITY_NOISE.search(raw) else raw


@dataclass
class Slots:
    """The filter/shape state of a question. Empty means "not stated"."""

    region: str = ""
    customer: str = ""
    material: str = ""
    order_id: str = ""
    month: str = ""
    date: str = ""
    limit: int = 0
    dimension: str = ""
    metric: str = ""
    direction: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in SLOT_FIELDS}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Slots":
        raw = raw or {}
        return cls(
            **{
                name: (int(raw.get("limit") or 0) if name == "limit" else str(raw.get(name) or ""))
                for name in SLOT_FIELDS
            }
        )

    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in SLOT_FIELDS)

    def filled_by(self, newer: "Slots") -> "Slots":
        """Return a copy where ``newer`` overrides only the slots it states."""
        merged = Slots()
        for name in SLOT_FIELDS:
            current = getattr(newer, name)
            setattr(merged, name, current if current else getattr(self, name))
        return merged

    def missing_relative_to(self, other: "Slots") -> list[str]:
        return [
            name
            for name in SLOT_FIELDS
            if getattr(other, name) and not getattr(self, name)
        ]

    def changed_relative_to(self, other: "Slots") -> list[str]:
        return [
            name
            for name in SLOT_FIELDS
            if getattr(self, name)
            and getattr(other, name)
            and getattr(self, name) != getattr(other, name)
        ]

    def entities(self) -> list[tuple[str, str]]:
        return [(name, str(getattr(self, name))) for name in ENTITY_FIELDS if getattr(self, name)]

    def month_phrase(self) -> str:
        if not self.month:
            return ""
        year, month = self.month.split("-")
        return f"{year}年{int(month)}月"

    def summary(self) -> str:
        parts = [f"{name}={getattr(self, name)}" for name in SLOT_FIELDS if getattr(self, name)]
        return " ".join(parts) if parts else "(no slots)"


def extract_slots(question: str, *, year_hint: str = "") -> Slots:
    """Read the query slots a follow-up can inherit.

    ``year_hint`` resolves a bare 「5月」 against the year already in play.
    """
    text = _normalize(question)
    slots = Slots()
    if not text:
        return slots

    for region in REGIONS:
        if region in text:
            slots.region = region
            break

    for name in sorted(CUSTOMER_NAMES, key=len, reverse=True):
        if name in text:
            slots.customer = name
            break
    if not slots.customer:
        slots.customer = _derived_entity(text, "客户")

    for name in sorted(MATERIAL_NAMES, key=len, reverse=True):
        if name in text:
            slots.material = name
            break
    if not slots.material:
        slots.material = _derived_entity(text, "物料")

    order_match = _ORDER_ID.search(text)
    slots.order_id = order_match.group(0).upper() if order_match else ""

    date_match = _FULL_DATE.search(text)
    if date_match:
        year, month, day = (int(part) for part in date_match.groups())
        slots.date = f"{year:04d}-{month:02d}-{day:02d}"
    else:
        iso_date = _DATE_ISO.search(text)
        slots.date = iso_date.group(1) if iso_date else ""

    # A full date fills exactly one slot. Reading a month out of it as well
    # would make a follow-up that changes the day look like it changed the
    # month, and the substitution pass would then reject the phrase.
    month_match = None if slots.date else (_MONTH.search(text) or _MONTH_ISO.search(text))
    if month_match:
        year, month = int(month_match.group(1)), int(month_match.group(2))
        if 1 <= month <= 12:
            slots.month = f"{year:04d}-{month:02d}"
    elif year_hint and re.fullmatch(r"\d{4}", str(year_hint)):
        bare = _MONTH_BARE.search(text)
        if bare and 1 <= int(bare.group(1)) <= 12:
            slots.month = f"{year_hint}-{int(bare.group(1)):02d}"

    limit_match = _LIMIT.search(text)
    if limit_match:
        value = limit_match.group(1) or limit_match.group(2)
        slots.limit = max(1, min(20, int(value)))

    for name, pattern in DIMENSION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            slots.dimension = name
            break

    for name, pattern in METRIC_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            slots.metric = name
            break

    if ASC_PATTERN.search(text):
        slots.direction = "asc"
    elif DESC_PATTERN.search(text):
        slots.direction = "desc"

    return slots


def is_followup(question: str) -> bool:
    text = _normalize(question).lower()
    return any(marker in text for marker in FOLLOWUP_MARKERS)


@dataclass
class Turn:
    """One user question plus the state the agent resolved it to."""

    question: str
    effective_question: str = ""
    status: str = ""
    answer: str = ""
    sql: str = ""
    row_count: int = 0
    slots: Slots = field(default_factory=Slots)
    rewrite_source: str = "none"
    inherited: list[str] = field(default_factory=list)
    clarification: dict[str, Any] | None = None
    trace_id: str = ""

    @property
    def resolved(self) -> str:
        return self.effective_question or self.question

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "effective_question": self.resolved,
            "status": self.status,
            "sql": self.sql,
            "row_count": self.row_count,
            "slots": self.slots.to_dict(),
            "rewrite_source": self.rewrite_source,
            "inherited": list(self.inherited),
            "clarification": self.clarification,
            "trace_id": self.trace_id,
        }


@dataclass
class Conversation:
    """Turn history. Each turn stores the *merged* slot state, so the last
    answered turn alone is enough to resolve the next follow-up."""

    turns: list[Turn] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.turns)

    def add(self, turn: Turn) -> Turn:
        self.turns.append(turn)
        return turn

    def extend(self, turns: Iterable[Turn]) -> "Conversation":
        self.turns.extend(turns)
        return self

    @property
    def last(self) -> Turn | None:
        return self.turns[-1] if self.turns else None

    def latest_answered(self) -> Turn | None:
        for turn in reversed(self.turns):
            if turn.status == "ok":
                return turn
        return None

    def pending_slots(self) -> Slots:
        turn = self.latest_answered()
        return turn.slots if turn else Slots()

    def render(self, max_turns: int = 3) -> str:
        answered = [turn for turn in self.turns if turn.status == "ok"][-max_turns:]
        lines: list[str] = []
        for turn in answered:
            lines.append(f"问：{turn.resolved}")
            if turn.sql:
                lines.append(f"SQL：{turn.sql}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"turns": [turn.to_dict() for turn in self.turns]}


def as_turns(history: Conversation | Sequence[Turn] | None) -> list[Turn]:
    if history is None:
        return []
    if isinstance(history, Conversation):
        return list(history.turns)
    return list(history)
