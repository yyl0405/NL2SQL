"""Clarification policy: when to ask, what to offer, and how an answer becomes a query.

The plan for this layer named five triggers (指标歧义 / 时间范围缺失 / 实体多义 /
权限不足 / schema 无此指标). Measured against the frozen corpus they do not belong
to the same tier. Blocking on 实体多义 costs 0 of 123 answerable questions (P1-2
measured ``frozen.ambiguous == []``), while blocking on 指标歧义 and 时间范围缺失
costs real cases — those questions are answerable, they just do not state a 口径.
So only the first three may block; the other two answer and hand back the exact
sentences that would have made the question narrower.

The second failure mode this layer exists for is the empty clarification: the
model's 「请补充必要的条件后重新提问。」 is not askable, so every emitted
clarification is routed through :meth:`ClarificationPolicy.enrich`, which attaches
a code and concrete options — or, when no option can be derived, the name of the
missing slot instead of a shrug.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .conversation import Slots, extract_slots
from .value_align import MAX_CLARIFICATION_OPTIONS

CODE_METRIC = "metric_ambiguity"
CODE_TIME = "time_range_missing"
CODE_ENTITY = "ambiguous_entity"
CODE_PERMISSION = "permission_denied"
CODE_SCHEMA = "schema_gap"
CODE_MODEL = "model_clarification"

# These three mean "the answer would be a guess".
ASK_CODES = (CODE_ENTITY, CODE_PERMISSION, CODE_SCHEMA)
# These two mean "the answer depends on a 口径 the user may not care about".
ASSUME_CODES = (CODE_METRIC, CODE_TIME)
CLARIFY_CODES = ASK_CODES + ASSUME_CODES + (CODE_MODEL,)

GUARD_PERMISSION_CODES = frozenset({"permission_table", "permission_column"})

MODE_OFF = "off"
MODE_OPTIONS = "options"
MODE_ASK = "ask"
CLARIFY_MODES = (MODE_OFF, MODE_OPTIONS, MODE_ASK)

FALLBACK_AS_OF = "2026-07-01"


@dataclass(frozen=True)
class MetricCandidate:
    """One reading of an ambiguous noun, as the question that states it outright."""

    metric: str
    question: str
    qualifiers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "question": self.question, "qualifiers": list(self.qualifiers)}


@dataclass(frozen=True)
class MetricTerm:
    term: str
    candidates: tuple[MetricCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.term, "candidates": [item.to_dict() for item in self.candidates]}


# The follow-up path in ``rewriter`` and this policy must agree on which nouns are
# ambiguous, so the table lives here and both read it. The candidate questions are
# the ones the demo schema can actually answer.
DEFAULT_METRIC_TERMS: tuple[MetricTerm, ...] = (
    MetricTerm(
        "库存",
        (
            MetricCandidate("safety_stock", "库存低于安全库存的物料有哪些？", ("安全库存", "低于")),
            MetricCandidate("inventory_value", "各分类库存金额排名", ("金额", "价值")),
            MetricCandidate("available_stock", "整机设备的可用库存是多少？", ("可用",)),
        ),
    ),
    MetricTerm(
        "订单",
        (
            MetricCandidate("revenue", "销售订单总金额是多少？", ("金额", "总额", "销售", "卖了")),
            MetricCandidate("order_count", "各状态订单数量统计", ("数量", "多少", "状态")),
            MetricCandidate("overdue", "未发货的逾期订单有哪些？", ("逾期", "未发货", "发货")),
        ),
    ),
    MetricTerm(
        "采购",
        (
            MetricCandidate("purchase_received", "采购到货数量统计", ("到货", "收货")),
            MetricCandidate("in_transit", "各供应商在途采购量排名", ("在途", "供应商")),
        ),
    ),
)

# Metrics whose number moves with the period. A question without a date is still
# answerable — over all periods — but only under that reading.
PERIOD_METRICS = (
    "revenue",
    "order_count",
    "overdue",
    "purchase_received",
    "in_transit",
    "customer_orders",
)

PERIOD_WORDS = ("月", "季", "周", "年", "日期", "截至", "截止")


class MetricGlossary:
    """Ambiguous business nouns and the questions that settle each reading."""

    def __init__(self, terms: Iterable[MetricTerm]) -> None:
        self.terms = tuple(terms)
        if not self.terms:
            raise ValueError("指标歧义词表不能为空：空词表会让所有含这些名词的问法都自认为已消歧")
        for term in self.terms:
            if len(term.candidates) < 2:
                raise ValueError(f"“{term.term}”只有 {len(term.candidates)} 个候选口径，不构成歧义")
            metrics = [item.metric for item in term.candidates]
            if len(set(metrics)) != len(metrics):
                raise ValueError(f"“{term.term}”的候选口径重复: {metrics}")
            for item in term.candidates:
                if not item.question.strip():
                    raise ValueError(f"“{term.term}”的候选 {item.metric} 缺少可直接发送的问句")

    @classmethod
    def default(cls) -> "MetricGlossary":
        return cls(DEFAULT_METRIC_TERMS)

    @classmethod
    def load(cls, path: str | Path) -> "MetricGlossary":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")), source=str(path))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source: str = "") -> "MetricGlossary":
        terms: list[MetricTerm] = []
        for index, item in enumerate(raw.get("terms") or []):
            where = f"{source or '词表'}第 {index + 1} 条"
            term = str(item.get("term") or "").strip()
            if not term:
                raise ValueError(f"{where} 缺少 term")
            candidates: list[MetricCandidate] = []
            for candidate in item.get("candidates") or []:
                metric = str(candidate.get("metric") or "").strip()
                if not metric:
                    raise ValueError(f"{where} 的候选缺少 metric")
                candidates.append(
                    MetricCandidate(
                        metric=metric,
                        question=str(candidate.get("question") or "").strip(),
                        qualifiers=tuple(
                            str(word).strip()
                            for word in (candidate.get("qualifiers") or [])
                            if str(word).strip()
                        ),
                    )
                )
            terms.append(MetricTerm(term=term, candidates=tuple(candidates)))
        return cls(terms)

    def matched(self, question: str) -> list[tuple[MetricTerm, tuple[MetricCandidate, ...]]]:
        """Terms present in the question, with the candidates its own words rule in."""
        out: list[tuple[MetricTerm, tuple[MetricCandidate, ...]]] = []
        for term in self.terms:
            if term.term not in question:
                continue
            kept = tuple(
                item for item in term.candidates if any(word in question for word in item.qualifiers)
            )
            out.append((term, kept))
        return out

    def unresolved(self, question: str) -> list[tuple[MetricTerm, tuple[MetricCandidate, ...]]]:
        """Terms the question names but never narrows, paired with every reading."""
        return [(term, term.candidates) for term, kept in self.matched(question) if not kept]

    @property
    def ambiguous_nouns(self) -> dict[str, tuple[tuple[str, str], ...]]:
        return {
            term.term: tuple((item.metric, item.question) for item in term.candidates)
            for term in self.terms
        }

    def stats(self) -> dict[str, Any]:
        return {
            "terms": [term.term for term in self.terms],
            "candidates": sum(len(term.candidates) for term in self.terms),
        }


# The packaged noun table: built from constants, so a deployment that does not set
# NL2SQL_METRIC_GLOSSARY never touches the filesystem for it.
DEFAULT_METRIC_GLOSSARY = MetricGlossary.default()


@dataclass
class Clarification:
    """What the agent needs from the user before it can run a query."""

    reason: str
    question: str = ""
    options: list[str] = field(default_factory=list)
    missing_slots: list[str] = field(default_factory=list)
    code: str = CODE_MODEL
    tier: str = "ask"
    assumed: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "question": self.question,
            "options": list(self.options),
            "missing_slots": list(self.missing_slots),
            "code": self.code,
            "tier": self.tier,
            "assumed": self.assumed,
        }

    @property
    def actionable(self) -> bool:
        """Askable by picking, rather than by writing a new question from scratch."""
        return bool(self.options) or bool(self.missing_slots)


@dataclass
class Trigger:
    code: str
    tier: str
    reason: str
    term: str = ""
    options: list[str] = field(default_factory=list)
    missing_slots: list[str] = field(default_factory=list)
    assumed: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "tier": self.tier,
            "reason": self.reason,
            "term": self.term,
            "options": list(self.options),
            "missing_slots": list(self.missing_slots),
            "assumed": self.assumed,
        }

    def as_clarification(self) -> Clarification:
        return Clarification(
            reason=self.reason,
            question=self.reason,
            options=list(self.options),
            missing_slots=list(self.missing_slots),
            code=self.code,
            tier=self.tier,
            assumed=self.assumed,
        )


@dataclass
class ClarifyPlan:
    """What the policy decided about one question, before the tool loop runs."""

    question: str
    triggers: list[Trigger] = field(default_factory=list)
    mode: str = MODE_OPTIONS

    @property
    def blocking(self) -> list[Trigger]:
        return [item for item in self.triggers if item.tier == "ask"]

    @property
    def assumed(self) -> list[Trigger]:
        return [item for item in self.triggers if item.tier == "assume"]

    @property
    def codes(self) -> list[str]:
        return [item.code for item in self.triggers]

    @property
    def alternatives(self) -> list[str]:
        out: list[str] = []
        for item in self.triggers:
            out.extend(option for option in item.options if option not in out)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "codes": self.codes,
            "blocking": [item.code for item in self.blocking],
            "assume": [item.code for item in self.assumed],
            "alternatives": self.alternatives,
            "triggers": [item.to_dict() for item in self.triggers],
        }


class ClarificationPolicy:
    """Decides which trigger codes apply to a question and what to offer the user."""

    def __init__(
        self,
        glossary: MetricGlossary | None = None,
        *,
        as_of: str = "",
        mode: str = MODE_OPTIONS,
        max_options: int = MAX_CLARIFICATION_OPTIONS,
    ) -> None:
        self.glossary = glossary or MetricGlossary.default()
        self.as_of = _as_date(as_of)
        self.mode = (mode or MODE_OPTIONS).strip().lower()
        if self.mode not in CLARIFY_MODES:
            raise ValueError(f"未知的澄清策略模式 {mode!r}，可选 {list(CLARIFY_MODES)}")
        self.max_options = max(1, int(max_options))

    @property
    def enabled(self) -> bool:
        return self.mode != MODE_OFF

    def decide(self, question: str, slots: Slots | None = None) -> ClarifyPlan:
        """Scan the two 口径 triggers. In ``options`` mode nothing here may block."""
        text = (question or "").strip()
        plan = ClarifyPlan(question=text, mode=self.mode)
        if not text or not self.enabled:
            return plan
        state = slots if slots is not None else extract_slots(text)
        metric = self._metric_trigger(text, state)
        if metric is not None:
            if self.mode == MODE_ASK and not state.metric:
                # Only a question that names the noun and nothing measurable at all
                # is unanswerable; the extractor finding a metric means the user did
                # state which 口径 they want.
                metric.tier = "ask"
            plan.triggers.append(metric)
        period = self._time_trigger(text, state)
        if period is not None:
            plan.triggers.append(period)
        return plan

    def _metric_trigger(self, text: str, slots: Slots) -> Trigger | None:
        open_terms = self.glossary.unresolved(text)
        if not open_terms:
            return None
        term, candidates = open_terms[0]
        options = [item.question for item in candidates if item.metric != slots.metric]
        if not options:
            return None
        return Trigger(
            code=CODE_METRIC,
            tier="assume",
            reason=f"“{term.term}”在本库里对应 {len(candidates)} 种口径，问题没有说是哪一种",
            term=term.term,
            options=options[: self.max_options],
            missing_slots=[term.term],
            assumed=slots.metric,
        )

    def _time_trigger(self, text: str, slots: Slots) -> Trigger | None:
        if slots.month or slots.date or slots.metric not in PERIOD_METRICS:
            return None
        if any(word in text for word in PERIOD_WORDS):
            return None
        return Trigger(
            code=CODE_TIME,
            tier="assume",
            reason="问题没有说时间范围，已按全部数据统计",
            options=self.period_options(text),
            missing_slots=["month"],
            assumed="全部时间",
        )

    def period_options(self, text: str) -> list[str]:
        """The same question, narrowed to a period the catalog's as-of date defines.

        The absolute month travels inside the sentence because that is the only
        form the demo catalog can resolve: an option the agent cannot execute
        when the user sends it back is a decoy, not a choice.
        """
        stem = (text or "").rstrip("？?。")
        out = [
            f"{phrase}（{literal}）{stem}？" for phrase, literal in self.periods()[: self.max_options]
        ]
        return [item for item in out if item.strip()]

    def periods(self) -> tuple[tuple[str, str], ...]:
        first = self.as_of.replace(day=1)
        previous = (first - timedelta(days=1)).replace(day=1)
        earlier = (previous - timedelta(days=1)).replace(day=1)
        return tuple(
            (phrase, f"{month.year}年{month.month}月")
            for phrase, month in (("本月", first), ("上月", previous), ("上上月", earlier))
        )

    def scope_trigger(self, code: str, *, reason: str, missing: Iterable[str] = ()) -> Trigger:
        """A blocking trigger the caller already established (entity / permission / schema)."""
        if code not in ASK_CODES:
            raise ValueError(f"{code} 不是阻塞式澄清的触发码")
        return Trigger(
            code=code,
            tier="ask",
            reason=reason,
            options=self.example_questions(),
            missing_slots=[item for item in missing if item],
        )

    def example_questions(self, *, exclude: str = "") -> list[str]:
        """Known-answerable examples, one per ambiguous noun, for the codes with no slot."""
        out: list[str] = []
        for term in self.glossary.terms:
            if term.term == exclude:
                continue
            for candidate in term.candidates:
                if candidate.question not in out:
                    out.append(candidate.question)
                break
            if len(out) >= self.max_options:
                break
        return out[: self.max_options]

    def enrich(
        self,
        clarification: Clarification | None,
        *,
        question: str = "",
        plan: ClarifyPlan | None = None,
        guard_codes: Iterable[str] = (),
        keep_open: bool = False,
    ) -> tuple[Clarification | None, list[str]]:
        """Attach a code and askable options to whatever a layer produced.

        Returns the clarification plus the assume-tier alternatives the caller should
        expose next to an ``ok`` answer. Never called for a status that executed SQL,
        so a working answer is never rewritten.

        ``keep_open`` is set by the caller when the producing layer already said what
        is missing (a model explanation naming a column, a code, a mapping). Canned
        examples from the packaged noun table would then offer questions the current
        catalog cannot answer, in place of the specific ask the user just read.
        """
        alternatives = list(plan.alternatives) if plan else []
        if not self.enabled:
            return clarification, alternatives
        if clarification is None:
            reason = "这个问题还缺一个条件，直接猜会给出错的数字。"
            clarification = Clarification(reason=reason, question=reason)
        codes = set(guard_codes or ())
        if not clarification.code or clarification.code == CODE_MODEL:
            clarification.code = self._classify(clarification, question, codes, plan)
        clarification.tier = "ask" if clarification.code in ASK_CODES else "assume"
        if not clarification.options:
            if keep_open and clarification.code == CODE_MODEL:
                # Classification found no known gap in the question, so this ask is
                # entirely the model's own words - do not overwrite it with picks.
                clarification.options = []
            else:
                clarification.options = self._fill(clarification, question, plan)[: self.max_options]
        if not clarification.missing_slots and plan:
            clarification.missing_slots = [
                slot for item in plan.assumed for slot in item.missing_slots if slot not in clarification.missing_slots
            ]
        return clarification, alternatives

    def _classify(
        self,
        clarification: Clarification,
        question: str,
        codes: set[str],
        plan: ClarifyPlan | None = None,
    ) -> str:
        if codes & GUARD_PERMISSION_CODES:
            return CODE_PERMISSION
        if plan and plan.triggers:
            # The plan is deterministic evidence about the *question*; the reason text
            # is the model's guess about why it could not answer. When the policy
            # already found a 口径 gap, that is the gap the options hand back, so
            # labelling it schema_gap would blame the catalog for a question the
            # catalog can answer three different ways.
            return plan.triggers[0].code
        text = f"{clarification.reason} {clarification.question}"
        if "取值" in text or "指代" in text or "哪一个" in text:
            return CODE_ENTITY
        if any(word in text for word in ("没有覆盖", "不包含", "换一个", "可查询", "不支持")):
            return CODE_SCHEMA
        if any(word in text for word in ("时间范围", "哪个期间", "哪个月", "截止")):
            return CODE_TIME
        if self.glossary.unresolved(question):
            return CODE_METRIC
        return CODE_MODEL

    def _fill(self, clarification: Clarification, question: str, plan: ClarifyPlan | None) -> list[str]:
        if plan:
            wanted = {clarification.code, CODE_METRIC, CODE_TIME}
            for item in plan.triggers:
                if item.code in wanted and item.options:
                    return list(item.options)
        if clarification.code == CODE_TIME:
            return self.period_options(question)
        return self.example_questions()


def with_options(answer: str, options: Sequence[str]) -> str:
    """Keep whatever the producing layer said, then append what the user can send back.

    A clarification that only says 「请补充」 asks the user to write a new question;
    appending the candidate sentences turns it into a pick.
    """
    text = (answer or "").strip()
    if not options or "你可以直接这样问" in text:
        return text
    lines = [text or "这个问题需要再确认一下：", "你可以直接这样问："]
    lines.extend(f"{index}. {option}" for index, option in enumerate(options, 1))
    return "\n".join(lines)


def resolve_clarification_reply(reply: str, options: Sequence[str]) -> str:
    """Turn 「2」/「第二个」/ a fragment of an option into the full askable question.

    The options are complete questions by construction, so resolving a reply needs
    no model call and no context inheritance: the pick *is* the next question.
    """
    text = (reply or "").strip()
    if not text or not options:
        return ""
    index = _selector_index(text, len(options))
    if index is not None:
        return options[index]
    if len(text) < 4:
        # A two-character reply ("可用") is a keyword, not a question; matching it
        # against three long sentences would let a fragment pick an arbitrary口径.
        return ""
    lowered = text.lower()
    matches = [option for option in options if lowered in option.lower()]
    return matches[0] if len(matches) == 1 else ""


_SELECTOR = re.compile(r"^\s*(?:我选|选|要|第|来|就用)?\s*(\d+)\s*(?:个|条|项|号|种|种口径)?\s*[。.]?\s*$")
_CN_NUMBER = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5"}
_SELECTOR_CN = re.compile(r"^\s*第\s*([一二三四五])\s*(?:个|条|项|号|种)?\s*[。.]?\s*$")


def _selector_index(text: str, count: int) -> int | None:
    match = _SELECTOR.match(text)
    if not match:
        cn = _SELECTOR_CN.match(text)
        if not cn:
            return None
        value = int(_CN_NUMBER[cn.group(1)])
    else:
        value = int(match.group(1))
    return value - 1 if 1 <= value <= count else None


def _as_date(text: str) -> date:
    raw = (text or "").strip()[:10]
    for pattern in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return datetime.strptime(FALLBACK_AS_OF, "%Y-%m-%d").date()


def clarification_text(clarification: Clarification) -> str:
    """Render what the user sees: the reason, then the options they can send as-is."""
    parts = [f"这个问题需要再确认一下：{clarification.reason}"]
    if clarification.options:
        parts.append("你可以直接这样问：")
        parts.extend(f"{index}. {option}" for index, option in enumerate(clarification.options, 1))
    elif clarification.missing_slots:
        parts.append("还需要补充：" + "、".join(clarification.missing_slots) + "。")
    else:
        parts.append("请补充问题中的具体条件后再问一次。")
    return "\n".join(parts)
