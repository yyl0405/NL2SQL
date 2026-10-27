"""Business value alignment: map what the user *says* to what the database *stores*.

A question that says "杭智" cannot be answered by a query on
`sales_order.customer_name = '杭智'` — the row simply does not exist, and the
answer comes back as an empty result that looks like a real zero. This module
holds the alias dictionary (客户 / 物料 / 供应商 / 区域 / 状态枚举) and the
resolution step that turns a mention into the stored value before generation.

Two rules shape the design, and both come from failures elsewhere in the
project:

* **Never guess.** When one mention maps to more than one stored value
  ("精工" is both a customer and a supplier), the aligner reports ambiguity and
  the agent asks — inventing a candidate would produce a confident wrong number.
* **Silence must be identical to no-op.** When nothing is resolved the question
  is returned verbatim, so the frozen corpus cannot drift because a dictionary
  happened to exist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_KINDS = frozenset({"customer", "material", "supplier", "region", "status", "other"})
# A prompt should not grow without bound because a question mentioned a dozen
# entities; the rest are reported as `dropped` instead of silently injected.
MAX_ALIGNED_VALUES = 8
MAX_CLARIFICATION_OPTIONS = 3
# Words that name a value's *category* rather than the value itself. "物料轴承"
# is not a guess about 轴承 — the user already said which column to look in, so
# a collision against a supplier name is not real ambiguity.
KIND_QUALIFIERS: dict[str, tuple[str, ...]] = {
    "material": ("物料", "原料"),
    "customer": ("客户",),
    "supplier": ("供应商",),
    "region": ("地区", "区域"),
}


@dataclass(frozen=True)
class DictionaryEntry:
    """One stored value plus the ways people refer to it."""

    kind: str
    column: str
    canonical: str
    aliases: tuple[str, ...] = ()
    note: str = ""

    @property
    def table(self) -> str:
        return self.column.split(".", 1)[0]

    @property
    def field(self) -> str:
        return self.column.split(".", 1)[1]

    @property
    def mentions(self) -> tuple[str, ...]:
        return (self.canonical,) + self.aliases

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "column": self.column,
            "canonical": self.canonical,
            "aliases": list(self.aliases),
            "note": self.note,
        }


@dataclass(frozen=True)
class Resolved:
    mention: str
    kind: str
    column: str
    canonical: str
    already_stated: bool = False
    qualified_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mention": self.mention,
            "kind": self.kind,
            "column": self.column,
            "canonical": self.canonical,
            "already_stated": self.already_stated,
            "qualified_by": self.qualified_by,
        }


@dataclass(frozen=True)
class Ambiguous:
    mention: str
    candidates: tuple[Resolved, ...]

    @property
    def options(self) -> tuple[str, ...]:
        return tuple(f"{item.canonical}（{item.column}）" for item in self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mention": self.mention,
            "candidates": [item.to_dict() for item in self.candidates],
            "options": list(self.options),
        }


@dataclass
class Alignment:
    """Resolution result for one question."""

    question: str
    effective: str
    source: str = "none"
    resolved: list[Resolved] = field(default_factory=list)
    verified: list[Resolved] = field(default_factory=list)
    ambiguous: list[Ambiguous] = field(default_factory=list)
    dropped: list[Resolved] = field(default_factory=list)
    suppressed: list[Resolved] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.effective != self.question

    @property
    def options(self) -> list[str]:
        out: list[str] = []
        for item in self.ambiguous:
            out.extend(option for option in item.options if option not in out)
        return out[:MAX_CLARIFICATION_OPTIONS]

    @classmethod
    def passthrough(cls, question: str, source: str = "off") -> "Alignment":
        return Alignment(question=question, effective=question, source=source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "changed": self.changed,
            "resolved": [item.to_dict() for item in self.resolved],
            "verified": [item.to_dict() for item in self.verified],
            "ambiguous": [item.to_dict() for item in self.ambiguous],
            "dropped": [item.to_dict() for item in self.dropped],
            "suppressed": [item.to_dict() for item in self.suppressed],
            "options": self.options,
        }


class ValueDictionary:
    """Validated alias set. A malformed dictionary is a config error, not a warning."""

    def __init__(self, entries: list[DictionaryEntry] | tuple[DictionaryEntry, ...]) -> None:
        self.entries = tuple(entries)
        self._mention_index: dict[str, tuple[DictionaryEntry, ...]] = {}
        seen: dict[tuple[str, str], None] = {}
        for entry in self.entries:
            for mention in entry.mentions:
                key = (mention.strip().lower(), entry.column, entry.canonical)
                if key in seen:
                    raise ValueError(f"词典里 {entry.column} 的 {mention} → {entry.canonical} 重复声明")
                seen[key] = None
                bucket = self._mention_index.setdefault(mention.strip().lower(), [])
                # One entry per distinct stored value: the same literal living in two
                # columns (work_order.status and sales_order.status both say "pending")
                # is not a collision — the value is unique, only its column is not, and
                # a duplicated pair in the prompt would buy nothing.
                if not any(item.canonical == entry.canonical for item in bucket):
                    bucket.append(entry)
        if not self.entries:
            raise ValueError("业务词典不能为空：空词典会让所有问法都自认为已对齐")

    @classmethod
    def load(cls, path: str | Path) -> "ValueDictionary":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw, source=str(path))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source: str = "") -> "ValueDictionary":
        entries: list[DictionaryEntry] = []
        for index, item in enumerate(raw.get("entries") or []):
            entries.append(cls._entry(item, source, index))
        return cls(entries)

    @staticmethod
    def _entry(item: dict[str, Any], source: str, index: int) -> DictionaryEntry:
        where = f"{source or '词典'}第 {index + 1} 条"
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"{where} 的 kind 未知: {kind!r}，可选 {sorted(ALLOWED_KINDS)}")
        column = str(item.get("column") or "").strip()
        if "." not in column:
            raise ValueError(f"{where} 的 column 必须是 表.列，实际是 {column!r}")
        canonical = str(item.get("canonical") or "").strip()
        if not canonical:
            raise ValueError(f"{where} 缺少 canonical")
        aliases: list[str] = []
        for alias in item.get("aliases") or []:
            text = str(alias).strip()
            if not text:
                raise ValueError(f"{where} 的别名不能是空串")
            if text.lower() == canonical.lower():
                raise ValueError(f"{where} 把正式取值 {canonical} 自己声明成了别名")
            if text not in aliases:
                aliases.append(text)
        return DictionaryEntry(
            kind=kind,
            column=column,
            canonical=canonical,
            aliases=tuple(aliases),
            note=str(item.get("note") or ""),
        )

    def candidates(self, mention: str) -> tuple[DictionaryEntry, ...]:
        return tuple(self._mention_index.get(mention.strip().lower(), ()))

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(sorted({item.column for item in self.entries}))

    @property
    def collision_mentions(self) -> tuple[str, ...]:
        """Mentions that legitimately point at more than one stored value."""
        return tuple(
            sorted(
                mention
                for mention, bucket in self._mention_index.items()
                if len({item.canonical for item in bucket}) > 1
            )
        )

    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self.entries),
            "kinds": sorted({item.kind for item in self.entries}),
            "columns": list(self.columns),
            "aliases": sum(len(item.aliases) for item in self.entries),
            "mentions": len(self._mention_index),
            "collisions": list(self.collision_mentions),
        }


class ValueAligner:
    """Longest-mention-first scan; overlapping short forms never double-fire."""

    def __init__(self, dictionary: ValueDictionary) -> None:
        self.dictionary = dictionary
        self._mentions = sorted(
            {mention for entry in dictionary.entries for mention in entry.mentions},
            key=lambda text: (-len(text), text.lower()),
        )

    def align(self, question: str) -> Alignment:
        text = question or ""
        lowered = text.lower()
        occupied = [False] * len(text)
        resolved: list[Resolved] = []
        ambiguous: list[Ambiguous] = []
        for mention in self._mentions:
            needle = mention.lower()
            if not needle:
                continue
            start = lowered.find(needle)
            while start != -1:
                end = start + len(needle)
                if not any(occupied[start:end]):
                    self._classify(text, text[start:end], mention, resolved, ambiguous)
                    for position in range(start, end):
                        occupied[position] = True
                    break  # one mention per question is enough evidence
                start = lowered.find(needle, end)
        ambiguous.sort(key=lambda item: item.mention)
        resolved.sort(key=lambda item: (item.kind, item.column, item.canonical))
        if ambiguous:
            # Ambiguity outranks injection: a guessed value is the failure mode
            # this component exists to prevent. Values that did resolve are kept
            # as `suppressed` so the report can show what the clarification cost.
            return Alignment(
                question=text,
                effective=text,
                source="dictionary",
                ambiguous=ambiguous,
                suppressed=resolved,
            )
        needed = [item for item in resolved if not item.already_stated]
        injected = needed[:MAX_ALIGNED_VALUES]
        alignment = Alignment(
            question=text,
            effective=_append_block(text, injected) if injected else text,
            source="dictionary",
            resolved=injected,
            verified=[item for item in resolved if item.already_stated],
            dropped=needed[MAX_ALIGNED_VALUES:],
        )
        return alignment

    def _classify(
        self,
        text: str,
        matched_text: str,
        mention: str,
        resolved: list[Resolved],
        ambiguous: list[Ambiguous],
    ) -> None:
        bucket = self.dictionary.candidates(mention)
        if not bucket:
            return
        distinct = {item.canonical for item in bucket}
        qualifier = ""
        if len(distinct) > 1:
            # The question may name the category itself ("物料轴承"), which is
            # evidence from the user rather than a guess made on their behalf.
            keeper = _qualified(bucket, text)
            if keeper is not None:
                bucket = [keeper]
                distinct = {keeper.canonical}
                qualifier = keeper.kind
        if len(distinct) > 1:
            candidates = tuple(
                sorted(
                    (
                        Resolved(
                            mention=matched_text,
                            kind=item.kind,
                            column=item.column,
                            canonical=item.canonical,
                        )
                        for item in bucket
                    ),
                    key=lambda item: (item.canonical, item.column),
                )
            )
            ambiguous.append(Ambiguous(mention=matched_text, candidates=candidates))
            return
        entry = bucket[0]
        resolved.append(
            Resolved(
                mention=matched_text,
                kind=entry.kind,
                column=entry.column,
                canonical=entry.canonical,
                already_stated=matched_text.lower() == entry.canonical.lower(),
                qualified_by=qualifier,
            )
        )


def _qualified(bucket: list[DictionaryEntry], text: str) -> DictionaryEntry | None:
    """Return the only candidate whose value kind the question names outright."""

    kept: list[DictionaryEntry] = []
    for entry in bucket:
        if any(word in text for word in KIND_QUALIFIERS.get(entry.kind, ())):
            kept.append(entry)
    return kept[0] if len(kept) == 1 else None


def _append_block(question: str, injected: list[Resolved]) -> str:
    pairs = " | ".join(f"{item.column}={item.canonical}" for item in injected)
    return f"{question}\n\n[值对齐] {pairs}"
