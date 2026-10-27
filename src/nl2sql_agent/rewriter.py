"""Follow-up question rewriting (anaphora + ellipsis resolution).

Two interchangeable channels share one contract, ``Rewrite``:

* :class:`RuleBasedRewriter` — deterministic slot inheritance plus phrase
  substitution. It is the offline channel and the fallback of last resort.
* :class:`LLMQuestionRewriter` — a real model rewriting the follow-up into a
  standalone question under a JSON contract. Any invalid or failed answer
  degrades to the rule result instead of guessing.

Both produce a *standalone question string*, so the tool loop never learns that
the turn came from a conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from .clarify import DEFAULT_METRIC_GLOSSARY
from .conversation import (
    SLOT_FIELDS,
    Slots,
    Turn,
    as_turns,
    extract_slots,
)
from .demo.seed import CUSTOMER_NAMES, MATERIAL_NAMES, REGIONS
from .llm import ChatModel
from .text import parse_json_object

REWRITE_PROMPT_VERSION = "rewrite-v1"
MAX_REWRITTEN_CHARS = 200

VALUE_FIELDS = ("region", "customer", "material", "order_id", "month", "date", "limit")
SHAPE_FIELDS = ("metric", "dimension", "direction")

ANAPHORA_MARKERS = (
    "那",
    "它",
    "这个",
    "这些",
    "其中",
    "同样",
    "一样",
    "同上",
    "呢",
    "再看",
    "还是",
    "拆开",
    "分开",
    "换成",
    "改成",
    "改为",
    "换个",
)

# Metrics that cannot be templated without one specific value.
REQUIRED_SLOT_HINT: dict[str, tuple[str, str]] = {
    "kitting": ("order_id", "订单号（例如 ORD-1012）"),
    "bom": ("material", "物料名称"),
    "available_stock": ("material", "物料名称"),
    "customer_orders": ("customer", "客户名称"),
}

# A noun that maps to several supported metrics: inherit silently and the agent
# answers a question the user never asked. Same table the clarification policy
# reads, so the follow-up path and the first-turn path cannot drift apart.
AMBIGUOUS_NOUNS = DEFAULT_METRIC_GLOSSARY.ambiguous_nouns


@dataclass
class Rewrite:
    """Resolution result for one follow-up turn."""

    question: str
    slots: Slots = field(default_factory=Slots)
    source: str = "none"
    ambiguous: bool = False
    reason: str = ""
    options: list[str] = field(default_factory=list)
    inherited: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    value_alignment: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "source": self.source,
            "ambiguous": self.ambiguous,
            "reason": self.reason,
            "options": list(self.options),
            "inherited": list(self.inherited),
            "changed": list(self.changed),
            "missing": list(self.missing),
            "slots": self.slots.to_dict(),
            "value_alignment": self.value_alignment,
        }


class QuestionRewriter(Protocol):
    """Shared contract for both rewriting channels."""

    def rewrite(self, question: str, history: Any = None) -> Rewrite:
        ...


# A follow-up often only carries a relative time word ("那去年的呢？"). The slot
# model reads absolute dates, so without a textual swap that turn looks like "no
# change" and used to fall through to the question templates - which speak the
# synthetic demo vocabulary and silently re-topic a attached catalog question.
_RELATIVE_TIME_RE = re.compile(r"前年|去年|今年|明年|上个月|这个月|下个月|上月|本月|下月")
_TIME_RE = re.compile(r"\d{4}\s*年(?:\s*\d{1,2}\s*月)?|\d{4}-\d{2}(?:-\d{2})?|\d{1,2}\s*月")


def _swap_time_surface(base: str, follow_up: str) -> str:
    """Replace the previous question's time phrase with the user's own words."""
    new = _RELATIVE_TIME_RE.search(follow_up or "")
    if not new:
        return ""
    old = _RELATIVE_TIME_RE.search(base or "")
    if old:
        return "" if old.group(0) == new.group(0) else base[: old.start()] + new.group(0) + base[old.end() :]
    return f"{new.group(0)}{base}" if not _TIME_RE.search(base) else ""


def _template_round_trips(prior: Turn) -> bool:
    """Templates may only re-word a question they can also reproduce.

    When the previous question is not one of the shapes these templates emit,
    it is talking about tables this catalog's demo vocabulary never had;
    rendering it anyway would answer a different question.
    """
    rendered = _render(prior.slots)
    return bool(rendered) and _normalize(rendered) == _normalize(prior.resolved)


class RuleBasedRewriter:
    """Resolve follow-ups by inheriting slots and editing the previous question."""

    def rewrite(self, question: str, history: Any = None) -> Rewrite:
        turns = as_turns(history)
        prior = _latest_answered(turns)
        if prior is None:
            current = extract_slots(question)
            return Rewrite(question=question, slots=current, source="rule")

        hint = prior.slots.month[:4]
        current = extract_slots(question, year_hint=hint)
        if not _needs_context(question, current):
            return Rewrite(question=question, slots=current, source="rule")

        prior_slots = prior.slots
        merged = prior_slots.filled_by(current)
        inherited = current.missing_relative_to(prior_slots)
        changed = merged.changed_relative_to(prior_slots)
        templates_safe = _template_round_trips(prior)

        noun_options = _ambiguous_noun_options(question, current)
        if noun_options:
            return Rewrite(
                question=question,
                slots=merged,
                source="rule",
                ambiguous=True,
                reason="这句话对应多个业务口径，需要确认问的是哪一种",
                options=noun_options if templates_safe else [prior.resolved],
                inherited=inherited,
                changed=changed,
            )

        if not changed:
            swapped = _swap_time_surface(prior.resolved, question)
            if swapped:
                return Rewrite(
                    question=swapped,
                    slots=merged,
                    source="rule",
                    inherited=inherited,
                    reason="追问只给了时间词，按上一句原样替换时间",
                )

        templates_safe = _template_round_trips(prior)
        resolved = ""
        if [name for name in changed if name in SHAPE_FIELDS] and templates_safe:
            resolved = _render(merged)
        if not resolved:
            resolved = _substitute(prior.resolved, prior_slots, merged)
        if not resolved and templates_safe:
            resolved = _render(merged)

        if not resolved or _normalize(resolved) == _normalize(prior.resolved):
            return _clarification(question, merged, inherited, changed, prior.resolved, templates_safe)

        return Rewrite(
            question=resolved,
            slots=merged,
            source="rule",
            inherited=inherited,
            changed=changed,
        )


def _clarification(
    question: str,
    merged: Slots,
    inherited: list[str],
    changed: list[str],
    prior_text: str = "",
    templates_safe: bool = True,
) -> Rewrite:
    referents = merged.entities()
    hint = REQUIRED_SLOT_HINT.get(merged.metric)
    missing: list[str] = []
    if hint and not getattr(merged, hint[0]):
        reason = f"改问这个指标还缺少{hint[1]}"
        missing = [hint[0]]
        options: list[str] = []
    elif not templates_safe:
        # Templated candidates would name demo objects the conversation never had.
        reason = "没有识别出要修改的查询条件"
        options = []
    elif len(referents) > 1:
        reason = "这句话可能指向上文里的多个对象，需要确认指代"
        options = _entity_options(merged, prior_text)
    else:
        reason = "没有识别出要修改的查询条件"
        options = _dimension_options(merged, prior_text)
    if not options and prior_text and not missing:
        options = [prior_text]
    return Rewrite(
        question=question,
        slots=merged,
        source="rule",
        ambiguous=True,
        reason=reason,
        options=options[:3],
        inherited=inherited,
        changed=changed,
        missing=missing,
    )


class LLMQuestionRewriter:
    """Ask the model to rewrite the follow-up into one standalone question."""

    def __init__(
        self,
        llm: ChatModel,
        fallback: RuleBasedRewriter | None = None,
        *,
        history_turns: int = 3,
        max_chars: int = MAX_REWRITTEN_CHARS,
    ) -> None:
        self.llm = llm
        self.fallback = fallback or RuleBasedRewriter()
        self.history_turns = max(1, history_turns)
        self.max_chars = max_chars
        self.prompt_version = REWRITE_PROMPT_VERSION

    def rewrite(self, question: str, history: Any = None) -> Rewrite:
        base = self.fallback.rewrite(question, history)
        turns = [turn for turn in as_turns(history) if turn.status == "ok"]
        if not turns:
            return base

        transcript = "\n".join(
            f"{index}. 问：{turn.resolved}\n   答：{(turn.answer or '')[:120]}"
            for index, turn in enumerate(turns[-self.history_turns :], 1)
        )
        messages = [
            {"role": "system", "content": self._prompt()},
            {
                "role": "user",
                "content": f"历史对话：\n{transcript}\n\n最新问题：{question}",
            },
        ]
        try:
            response = self.llm.chat(messages)
        except Exception as exc:  # noqa: BLE001 - rewriting must never break the turn
            base.source = "llm-error"
            base.reason = f"改写模型调用失败，沿用规则改写：{exc}"[:300]
            return base

        payload = parse_json_object(response.content)
        resolved = str(payload.get("question") or "").strip() if payload else ""
        if not payload or not self._acceptable(resolved, question, turns):
            base.source = "llm-invalid"
            base.reason = "改写结果不符合契约，沿用规则改写"
            return base

        ambiguous = bool(payload.get("ambiguous"))
        options = [str(item).strip() for item in (payload.get("options") or []) if str(item).strip()]
        if ambiguous and not options:
            base.source = "llm-invalid"
            base.reason = "模型声明歧义但没有给出可选项，沿用规则改写"
            return base
        return Rewrite(
            question=resolved,
            slots=base.slots,
            source="llm",
            ambiguous=ambiguous,
            reason=str(payload.get("reason") or "").strip(),
            options=options[:3],
            inherited=[
                name
                for name in (payload.get("inherited_slots") or [])
                if str(name) in Slots().to_dict()
            ]
            or base.inherited,
            changed=base.changed,
        )

    def _acceptable(self, resolved: str, question: str, turns: Sequence[Turn]) -> bool:
        text = _normalize(resolved)
        if len(text) < 2 or len(text) > self.max_chars:
            return False
        if "\n" in resolved or "```" in resolved:
            return False
        if re.search(r"\b(select|with|from|where)\b", resolved, re.IGNORECASE):
            return False
        anchors = _normalize(question) + _normalize(
            "".join(turn.resolved for turn in turns[-2:])
        )
        if not any(token and token in anchors for token in _tokens(text)):
            return False
        return not self._fabricated(resolved, anchors)

    @staticmethod
    def _fabricated(resolved: str, anchors: str) -> bool:
        """Reject a rewrite that introduces an entity the conversation never had."""
        for value in (*REGIONS, *CUSTOMER_NAMES, *MATERIAL_NAMES):
            if value in resolved and value not in anchors:
                return True
        upper = anchors.upper()
        return any(
            match.group(0) not in upper
            for match in re.finditer(r"ORD-\d+", resolved.upper())
        )

    @staticmethod
    def _prompt() -> str:
        return f"""你要把多轮问数对话里的最新问题改写成一个**独立、可直接问数**的中文问题。

规则:
1. 补全上文省略的条件（时间、地区、客户、物料、订单号、Top N、分组维度、指标口径），不改变用户本轮明确表达的修改意图。
2. 只做改写：不编造上文没有出现过的实体值，不输出 SQL，不回答问题本身。
3. 如果指代对象或指标口径无法确定（例如上文中同时存在多个候选对象），把 ambiguous 设为 true，并给出最多 3 个可以直接追问的完整问法作为 options。
4. 改写后的问题必须自包含，脱离历史也能被理解，长度不超过 {MAX_REWRITTEN_CHARS} 字符。

只输出一个 JSON 对象，不要输出 Markdown:
{{
  "question": "改写后的独立问题",
  "ambiguous": false,
  "reason": "歧义原因，无歧义时为空字符串",
  "options": ["可选的澄清问法"],
  "inherited_slots": ["从上文继承的条件名"]
}}
[{REWRITE_PROMPT_VERSION}]
"""


def _latest_answered(turns: Sequence[Turn]) -> Turn | None:
    for turn in reversed(list(turns)):
        if turn.status == "ok":
            return turn
    return None


def _needs_context(question: str, current: Slots) -> bool:
    """Only an explicit anaphora marker or a bare value fragment may inherit."""
    text = _normalize(question)
    if not text:
        return False
    if any(marker in text for marker in ANAPHORA_MARKERS):
        return True
    if len(text) > 8 or current.metric:
        return False
    return bool(
        current.region
        or current.month
        or current.customer
        or current.material
        or current.order_id
        or current.limit
        or current.dimension
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _tokens(text: str) -> list[str]:
    """CJK runs and ASCII words of the rewritten question, used as anchors."""
    return re.findall(r"[一-鿿]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}|\d{3,}", text or "")


def _ambiguous_noun_options(question: str, current: Slots) -> list[str]:
    if current.metric or current.dimension:
        return []
    text = _normalize(question)
    for noun, candidates in AMBIGUOUS_NOUNS.items():
        if noun in text:
            return [option for _, option in candidates[:3]]
    return []


def _dimension_options(slots: Slots, exclude: str = "") -> list[str]:
    options: list[str] = []
    for dimension in ("", "region", "customer", "category"):
        candidate = _render(
            Slots(
                **{
                    **slots.to_dict(),
                    "dimension": dimension,
                    "metric": slots.metric or "revenue",
                }
            )
        )
        if candidate and candidate != exclude and candidate not in options:
            options.append(candidate)
    return options[:3]


def _entity_options(slots: Slots, exclude: str = "") -> list[str]:
    """One concrete question per candidate referent, so the user can just pick."""
    options: list[str] = []
    for name, value in slots.entities():
        for metric, dimension in ((slots.metric, slots.dimension), _entity_fallback(name)):
            candidate = Slots(
                **{
                    **slots.to_dict(),
                    "region": "",
                    "customer": "",
                    "material": "",
                    "order_id": "",
                    "metric": metric,
                    "dimension": dimension,
                }
            )
            setattr(candidate, name, value)
            rendered = _render(candidate)
            # A shape that drops the entity did not resolve the reference.
            if rendered and str(value) in rendered and rendered != exclude:
                if rendered not in options:
                    options.append(rendered)
                break
    return options[:3]


def _entity_fallback(field: str) -> tuple[str, str]:
    """A metric that can actually consume this entity type."""
    return {
        "customer": ("customer_orders", ""),
        "material": ("available_stock", ""),
        "order_id": ("kitting", ""),
    }.get(field, ("", ""))


def _substitute(base: str, prior: Slots, merged: Slots) -> str:
    text = base
    applied = 0
    for name in VALUE_FIELDS:
        old = getattr(prior, name)
        new = getattr(merged, name)
        if not new or old == new:
            continue
        # A value that only appears in this turn has nowhere to land in the
        # previous wording, so the shape templates must handle it instead.
        if not old:
            return ""
        replaced = _replace_value(text, name, old, new)
        if replaced is None:
            return ""
        text = replaced
        applied += 1
    return text if applied else ""


def _replace_value(text: str, name: str, old: Any, new: Any) -> str | None:
    if name == "month":
        pattern = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月|(?<![\d-])\d{4}-\d{2}(?![\d-])")
        year, month = str(new).split("-")
        replacement = f"{year}年{int(month)}月"
    elif name == "date":
        pattern = re.compile(
            r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]"
            r"|(?<![\d-])\d{4}-\d{2}-\d{2}(?![\d-])"
        )
        match = pattern.search(text)
        if not match:
            return None
        year, month, day = str(new).split("-")
        # Keep the date style the previous wording used so the resolved question
        # stays a shape the frozen single-turn set already covers.
        replacement = (
            f"{year}年{int(month)}月{int(day)}日" if "年" in match.group(0) else str(new)
        )
    elif name == "limit":
        pattern = re.compile(r"(?:前|top)\s*\d+|\d+\s*(?:个|家|位|名)")
        replacement = f"前{int(new)}"
    elif name == "region":
        pattern = re.compile(f"{re.escape(str(old))}地区|{re.escape(str(old))}")
        replacement = f"{new}地区"
    elif name in ("order_id",):
        pattern = re.compile(r"ORD-\d+", re.IGNORECASE)
        replacement = str(new)
    else:
        pattern = re.compile(re.escape(str(old)))
        replacement = str(new)
    if not pattern.search(text):
        return None
    return pattern.sub(replacement, text, count=1)


def _render(slots: Slots) -> str:
    """Template a merged slot state into one of the question shapes the schema
    supports. Returns "" when no shape matches, so callers can fall back."""
    if not slots.metric:
        return ""
    metric = slots.metric
    month = slots.month_phrase()
    region = f"{slots.region}地区" if slots.region else ""
    prefix = f"{month}{region}"

    if metric == "revenue":
        if slots.dimension == "region":
            return f"{month}各地区销售金额排名"
        if slots.dimension == "customer":
            return f"{prefix}销售额最高的前{slots.limit or 3}个客户是谁？"
        if slots.dimension == "category":
            return f"{prefix}各分类销售金额排名"
        if slots.dimension:
            return ""
        return f"{prefix}销售订单总金额是多少？"

    if metric == "customer_orders":
        if not slots.customer:
            return ""
        return f"{slots.customer}的订单明细"
    if metric == "kitting":
        return f"{slots.order_id}的物料齐套率是多少？" if slots.order_id else ""
    if metric == "bom":
        return f"{slots.material}的BOM所有子件及单台总用量是多少？" if slots.material else ""
    if metric == "available_stock":
        return f"{slots.material}的可用库存是多少？" if slots.material else ""
    if metric == "safety_stock":
        return "库存低于安全库存的物料有哪些？"
    if metric == "inventory_value":
        return "各分类库存金额排名"
    if metric == "in_transit":
        return "各供应商在途采购量排名"
    if metric == "purchase_received":
        return f"{month}采购到货数量统计"
    if metric == "completion_rate":
        return f"{month}工单完工率" if month else "工单完工率明细"
    if metric == "low_completion":
        return "完工数量低于计划80%的工单有哪些？"
    if metric == "overdue":
        return f"截至{slots.date}未发货的逾期订单有哪些？" if slots.date else "未发货的逾期订单有哪些？"
    if metric == "order_count":
        return "各状态订单数量统计"
    return ""
