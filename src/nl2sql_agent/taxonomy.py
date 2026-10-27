"""Rule-based question category taxonomy for recall weighting and triage.

Deliberately keyword-based: deterministic, offline, and inspectable. The
vocabulary was induced from the synthetic eval set, so accuracy on real
business phrasing is unverified — `classify` therefore reports the matched
keywords and a score instead of pretending to be certain, and an unmatched
question returns `unknown` rather than the nearest business category.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

UNKNOWN = "unknown"
OUT_OF_SCOPE = "scope"

# Business categories the agent can actually answer on the demo schema.
BUSINESS_CATEGORIES = (
    "inventory",
    "purchase",
    "production",
    "bom",
    "kitting",
    "detail",
    "group_by",
    "ranking",
    "time_filter",
    "metric",
)

# Domain nouns score higher than answer-shape words: 「各…库存金额排名」 is an
# inventory question, not a group_by question.
DOMAIN_WEIGHT = 1.0
SHAPE_WEIGHT = 0.6
SPECIFIC_WEIGHT = 0.8

DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "kitting": ("齐套", "缺料", "欠料", "配套率", "齐货", "备料情况"),
    "bom": ("bom", "物料清单", "子件", "母件", "父项", "层级", "展开", "单机用量", "单台用量"),
    "inventory": ("库存", "在库", "结存", "可用量", "安全库存", "锁定量", "呆滞"),
    "purchase": ("采购", "在途", "到货", "供应商", "请购", "收货", "po"),
    "production": ("工单", "完工", "投产", "在制", "制单", "产线", "工时", "产能"),
}

SHAPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "detail": ("明细", "清单", "逐单", "逐行"),
    "group_by": ("各", "按", "分组", "分布", "统计", "排名榜", "分别", "不同", "各自"),
    "ranking": ("排名", "排行", "最高", "最低", "最大", "最小", "前", "top", "倒数"),
    "time_filter": (
        "截至",
        "截止",
        "年",
        "月",
        "日",
        "季度",
        "本周",
        "上周",
        "本月",
        "上月",
        "最近",
        "逾期",
        "过期",
    ),
    "metric": ("多少", "总额", "总金额", "金额", "合计", "总量", "数量", "率", "占比", "卖了"),
}

# Keywords that must not be matched as bare substrings. ``po`` is a table
# column and 前 hides inside 之前/当前/提前, so both need a bounded pattern.
PATTERNS: dict[str, re.Pattern[str]] = {
    "po": re.compile(r"\bpo\b"),
    "前": re.compile(r"(?<![之以提当目面])前\s*\d"),
}

# Tie-break order: domain-specific first, then shape. A date qualifier is the
# weakest signal of all — 「2026年6月…总金额是多少」 asks for a metric, not for a
# time filter.
PRIORITY = (
    "kitting",
    "bom",
    "inventory",
    "purchase",
    "production",
    "detail",
    "group_by",
    "ranking",
    "metric",
    "time_filter",
)


@dataclass
class Classification:
    category: str
    score: float = 0.0
    matched: list[str] = field(default_factory=list)

    @property
    def is_business(self) -> bool:
        return self.category in BUSINESS_CATEGORIES

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "score": self.score,
            "matched": list(self.matched),
            "is_business": self.is_business,
        }


def classify(question: str) -> Classification:
    text = (question or "").strip().lower()
    if not text:
        return Classification(UNKNOWN)

    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}
    for table, weight in (
        (DOMAIN_KEYWORDS, DOMAIN_WEIGHT),
        (SHAPE_KEYWORDS, SHAPE_WEIGHT),
    ):
        for category, keywords in table.items():
            for keyword in keywords:
                pattern = PATTERNS.get(keyword)
                matched = pattern.search(text) if pattern else keyword in text
                if matched:
                    scores[category] = max(scores.get(category, 0.0), weight)
                    hits.setdefault(category, []).append(keyword)

    if "明细" in text or "清单" in text:
        scores["detail"] = max(scores.get("detail", 0.0), SPECIFIC_WEIGHT)

    if not scores:
        return Classification(UNKNOWN)

    best = max(scores.values())
    contenders = [category for category in PRIORITY if scores.get(category, 0.0) >= best - 1e-9]
    return Classification(
        category=contenders[0] if contenders else UNKNOWN,
        score=round(best, 4),
        matched=sorted({keyword for words in hits.values() for keyword in words})[:8],
    )


def classify_category(question: str) -> str:
    return classify(question).category
