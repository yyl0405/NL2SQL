"""Small text utilities: CJK-aware tokenization, BM25, JSON extraction, table rendering."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

_WORD_RE = re.compile(r"[a-z_][a-z0-9_]*|\d+(?:\.\d+)?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Tokenize mixed Chinese/English text.

    Chinese has no word boundaries in the source questions we see, so we index
    CJK unigrams, bigrams and trigrams. This is deliberately dependency-free;
    production deployments can swap in jieba / a domain dictionary.
    """
    lowered = (text or "").lower()
    tokens = _WORD_RE.findall(lowered)
    for segment in _CJK_RE.findall(lowered):
        tokens.extend(segment)
        tokens.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        tokens.extend(segment[i : i + 3] for i in range(len(segment) - 2))
    return [t for t in tokens if t.strip()]


def normalize_question(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return _PUNCT_RE.sub("", text)


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def token_union(*texts: str) -> set[str]:
    """Distinct tokens across several fields: one row's fingerprint for redundancy checks.

    Question and SQL together means a repeat is caught either by wording or by shape —
    three near-identical phrasings of one query, and the same query written three ways
    around the same SQL, both collapse to one token set.
    """
    tokens: set[str] = set()
    for text in texts:
        tokens.update(tokenize(text or ""))
    return tokens


def mmr_select(
    scores: Sequence[float],
    token_sets: Sequence[set[str]],
    *,
    top_k: int,
    diversity: float = 0.0,
) -> list[int]:
    """Greedy max-marginal-relevance selection, returning indices into ``scores``.

    ``diversity = 0`` reproduces the plain top-k ordering exactly, so a library that
    never enabled it keeps its previous retrieval. Above 0 each candidate is charged
    its largest token overlap with what has already been chosen, which is what stops
    three near-identical phrasings from filling a three-slot few-shot budget. Scores
    are expected on a 0..1 scale because the relevance and redundancy terms are
    weighted by ``1-λ`` and ``λ`` directly; ties break on the lower index so the
    result never depends on set iteration order.
    """
    if top_k <= 0 or not scores:
        return []
    limit = min(int(top_k), len(scores))
    if diversity <= 0:
        return sorted(range(len(scores)), key=lambda i: (-float(scores[i]), i))[:limit]
    lambda_ = min(1.0, max(0.0, float(diversity)))
    remaining = set(range(len(scores)))
    chosen: list[int] = []
    while remaining and len(chosen) < limit:
        best: int | None = None
        best_value = float("-inf")
        for index in sorted(remaining):
            redundancy = max(
                (jaccard(token_sets[index], token_sets[other]) for other in chosen),
                default=0.0,
            )
            value = (1.0 - lambda_) * float(scores[index]) - lambda_ * redundancy
            if value > best_value:
                best, best_value = index, value
        assert best is not None
        chosen.append(best)
        remaining.discard(best)
    return chosen


@dataclass
class BM25:
    """Minimal BM25 implementation over tokenized documents."""

    documents: Sequence[Sequence[str]]
    k1: float = 1.5
    b: float = 0.75

    def __post_init__(self) -> None:
        self._doc_freqs: Counter[str] = Counter()
        self._doc_lengths: list[int] = []
        for doc in self.documents:
            self._doc_freqs.update(set(doc))
            self._doc_lengths.append(len(doc))
        self._avg_len = (sum(self._doc_lengths) / len(self._doc_lengths)) if self._doc_lengths else 0.0
        self._n_docs = len(self.documents)

    def scores(self, query: Sequence[str]) -> list[float]:
        if not self.documents or not query:
            return [0.0] * self._n_docs
        result: list[float] = []
        for idx, doc in enumerate(self.documents):
            freqs = Counter(doc)
            length = self._doc_lengths[idx] or 1
            score = 0.0
            for term in query:
                df = self._doc_freqs.get(term, 0)
                if not df:
                    continue
                idf = math.log(1 + (self._n_docs - df + 0.5) / (df + 0.5))
                tf = freqs.get(term, 0)
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self._avg_len or 1))
                if denom:
                    score += idf * (tf * (self.k1 + 1)) / denom
            result.append(score)
        return result


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a model response, tolerating code fences."""
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(cleaned[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def to_jsonable(value: Any) -> Any:
    """Convert sqlite / dataclass-ish values into JSON-safe primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        # SQL Server DECIMAL must stay numeric: str(Decimal("3754.00")) is
        # "3754.00" and str(Decimal("3754")) is "3754", which makes two equal
        # answer sets look different to the evaluator.
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        return to_jsonable(value.to_dict())
    return str(value)


def format_table(columns: Sequence[str], rows: Sequence[Sequence[Any]], max_width: int = 40) -> str:
    """Render an ASCII table without third-party dependencies."""
    if not columns:
        return "(no columns)"
    rendered: list[list[str]] = []
    for row in rows:
        rendered.append([_cell(value, max_width) for value in row])
    widths = [len(str(c)) for c in columns]
    for row in rendered:
        for idx, cell in enumerate(row[: len(widths)]):
            widths[idx] = min(max(widths[idx], len(cell)), max_width)

    def line(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def render_row(values: Sequence[str]) -> str:
        cells = [f" {v:<{widths[i]}}" for i, v in enumerate(values[: len(widths)])]
        return "│" + "│".join(cells) + "│"

    out = [line("┌", "┬", "┐"), render_row([str(c) for c in columns]), line("├", "┼", "┤")]
    out.extend(render_row(row) for row in rendered)
    out.append(line("└", "┴", "┘"))
    return "\n".join(out)


def _cell(value: Any, max_width: int) -> str:
    text = "NULL" if value is None else str(value)
    text = text.replace("\n", " ")
    if len(text) > max_width:
        return text[: max_width - 1] + "…"
    return text
