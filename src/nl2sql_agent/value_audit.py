"""Value alignment audit: does the business dictionary really pay for itself?

Alignment sits in front of generation, so a wrong dictionary entry is worse than no
dictionary at all — it rewrites the question the model sees. Three things can only be
checked against the attached catalog and its live rows, not against the dictionary file:

* a declared ``table.column`` that the catalog does not contain (the injected pair
  would name a column that does not exist);
* a ``canonical`` value that is not in the column any more (a stale dictionary
  injects a value that matches zero rows, which looks like a real zero);
* an alias that silently collides with a second stored value.

Then the corpus half asks the opposite question: does the alignment step actually
change the answer? That is measured as a paired A/B in one process — the same agent,
the same provider, two runtimes that differ only in ``value_alignment``, each with its
own experience store so a success recorded by one arm cannot become a few-shot
example for the other.

Exit codes are driven by invariants only. ``fixed``/``broken`` deltas are reported,
never gated: the alias corpus is assistant-authored measurement material, and a
difference on it is evidence, not a regression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .config import Settings
from .guard_suite import load_case_file
from .result_compare import rows_equal
from .runtime import create_runtime
from .tools.db import Target
from .tools.sql_tools import run_readonly_query
from .value_align import Alignment, ValueAligner, ValueDictionary

ALIAS_CASES = Path(__file__).resolve().parent / "data" / "alias_cases.jsonl"
UNMEASURED = "unmeasured"
# Unicode-aware on purpose: real ERP columns are often Chinese, and an audit that
# quietly skipped them would report "0 stale values" for the dictionary that matters.
# Brackets, quotes and spaces are still refused — they would end up inside SQL.
_IDENTIFIER = re.compile(r"[^\W\d]\w*")


@dataclass
class AliasRow:
    id: str
    question: str
    expectation: str
    mention: str = ""
    expect_canonical: str = ""
    align: str = UNMEASURED
    injected: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    off_status: str = UNMEASURED
    on_status: str = UNMEASURED
    off_pass: str = UNMEASURED
    on_pass: str = UNMEASURED
    delta: str = UNMEASURED
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "expectation": self.expectation,
            "mention": self.mention,
            "expect_canonical": self.expect_canonical,
            "align": self.align,
            "injected": list(self.injected),
            "options": list(self.options),
            "off_status": self.off_status,
            "on_status": self.on_status,
            "off_pass": self.off_pass,
            "on_pass": self.on_pass,
            "delta": self.delta,
            "error": self.error,
        }


def expectation_of(case: dict[str, Any]) -> str:
    tags = {str(tag) for tag in (case.get("tags") or [])}
    if case.get("expect_clarification") or "collision" in tags:
        return "collision"
    if "stated" in tags:
        return "stated"
    if "no_dictionary_mention" in tags:
        return "quiet"
    if "status_enum" in tags:
        return "status_enum"
    return "alias"


def _verdict(alignment: Alignment) -> str:
    if alignment.ambiguous:
        return "ambiguous"
    if alignment.resolved:
        return "injected"
    if alignment.verified:
        return "stated"
    return "unchanged"


def _quote(name: str, *, mssql: bool) -> str:
    return f"[{name}]" if mssql else f'"{name}"'


def dictionary_catalog_findings(catalog: Catalog, dictionary: ValueDictionary) -> dict[str, Any]:
    """Every declared column must exist in the catalog the agent will really use."""

    missing: list[str] = []
    uncheckable: list[str] = []
    for column in dictionary.columns:
        table, _, field_name = column.partition(".")
        if not (_IDENTIFIER.fullmatch(table) and _IDENTIFIER.fullmatch(field_name)):
            uncheckable.append(column)
            continue
        spec = catalog.table(table)
        if spec is None or spec.column(field_name) is None:
            missing.append(column)
    return {
        "columns": len(dictionary.columns),
        "missing": missing,
        "uncheckable": uncheckable,
    }


def dictionary_data_findings(
    dictionary: ValueDictionary,
    target: Target,
    *,
    mssql: bool,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Is each stored value still in the column? A stale value injects a zero-row filter."""

    distinct_cache: dict[str, set[str]] = {}
    stale: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for entry in dictionary.entries:
        column = entry.column
        if not (_IDENTIFIER.fullmatch(entry.table) and _IDENTIFIER.fullmatch(entry.field)):
            skipped.append(column)
            continue
        if column not in distinct_cache:
            sql = (
                f"SELECT DISTINCT {_quote(entry.field, mssql=mssql)} "
                f"FROM {_quote(entry.table, mssql=mssql)}"
            )
            ran = run_readonly_query(target, sql, max_rows=5000, timeout_ms=timeout_ms)
            if not ran.get("success"):
                errors.append(f"{column}: {str(ran.get('error') or '')[:100]}")
                distinct_cache[column] = set()
                continue
            distinct_cache[column] = {
                str(row[0]) for row in (ran.get("rows") or []) if row and row[0] is not None
            }
        values = distinct_cache[column]
        if not values:
            skipped.append(column)
        elif entry.canonical not in values:
            stale.append(f"{column}={entry.canonical}")
    return {
        "entries": len(dictionary.entries),
        "columns": len(distinct_cache),
        "stale": sorted(set(stale)),
        "skipped": sorted(set(skipped)),
        "errors": errors,
    }


DIM_COLUMNS = ("lookup_type", "lookup_code", "lookup_name")


def dim_enum_findings(
    dictionary: ValueDictionary,
    target: Target,
    catalog: Catalog,
    *,
    mssql: bool,
    dim_table: str = "dim_lookup",
) -> dict[str, Any]:
    """A status alias that claims to come from the dimension table must really be there.

    Entries say so through ``note = "dim_lookup:<lookup_type>"``; the rest (工单状态、
    采购状态 in the demo) have no registered dictionary at all, so they are reported as
    unchecked rather than invented.
    """

    status_entries = [entry for entry in dictionary.entries if entry.kind == "status"]
    declared = [
        entry for entry in status_entries if str(entry.note).startswith("dim_lookup:")
    ]
    unchecked = [
        f"{entry.column}={entry.canonical}" for entry in status_entries if entry not in declared
    ]
    out: dict[str, Any] = {
        "present": False,
        "status_entries": len(status_entries),
        "declared": len(declared),
        "labels": 0,
        "missing_code": [],
        "missing_label": [],
        "unchecked": unchecked,
    }
    spec = catalog.table(dim_table)
    if spec is None:
        out["missing_code"] = [f"{entry.column}={entry.canonical}" for entry in declared]
        out["absent_table"] = True
        return out
    if any(spec.column(name) is None for name in DIM_COLUMNS):
        out["shape"] = "unexpected"
        return out
    select = ", ".join(_quote(name, mssql=mssql) for name in DIM_COLUMNS)
    ran = run_readonly_query(
        target,
        f"SELECT DISTINCT {select} FROM {_quote(dim_table, mssql=mssql)}",
        max_rows=5000,
        timeout_ms=5000,
    )
    if not ran.get("success"):
        out["error"] = str(ran.get("error") or "")[:120]
        return out
    names: dict[tuple[str, str], str] = {}
    for row in ran.get("rows") or []:
        cells = list(row)
        if len(cells) < 3 or cells[0] is None or cells[1] is None:
            continue
        names[(str(cells[0]), str(cells[1]))] = str(cells[2])
    out["present"] = True
    out["codes"] = len(names)
    for entry in declared:
        lookup_type = str(entry.note).split(":", 1)[1].strip()
        label = names.get((lookup_type, entry.canonical))
        if label is None:
            out["missing_code"].append(f"{entry.column}={entry.canonical}")
            continue
        if label not in entry.mentions:
            out["missing_label"].append(f"{entry.column}={entry.canonical}({lookup_type}:{label})")
        else:
            out["labels"] += 1
    return out


def align_rows(
    cases: list[dict[str, Any]],
    aligner: ValueAligner,
) -> list[AliasRow]:
    rows: list[AliasRow] = []
    for case in cases:
        row = AliasRow(
            id=str(case.get("id") or ""),
            question=str(case.get("question") or ""),
            expectation=expectation_of(case),
            mention=str(case.get("alias_mention") or ""),
            expect_canonical=str(case.get("expect_canonical") or ""),
        )
        alignment = aligner.align(row.question)
        row.align = _verdict(alignment)
        row.injected = [f"{item.column}={item.canonical}" for item in alignment.resolved]
        row.options = list(alignment.options)
        rows.append(row)
    return rows


def corpus_findings(rows: list[AliasRow], cases: list[dict[str, Any]]) -> dict[str, list[str]]:
    by_id = {str(case.get("id") or ""): case for case in cases}
    out = {
        "not_aligned": [],
        "wrong_value": [],
        "changed_stated": [],
        "changed_quiet": [],
        "not_ambiguous": [],
        "missing_options": [],
        "dangling_alias": [],
    }
    for row in rows:
        case = by_id.get(row.id, {})
        if row.expectation in ("alias", "status_enum"):
            if row.align != "injected":
                out["not_aligned"].append(row.id)
            elif row.expect_canonical and row.expect_canonical not in {
                item.split("=", 1)[1] for item in row.injected
            }:
                out["wrong_value"].append(row.id)
        elif row.expectation == "collision":
            if row.align != "ambiguous":
                out["not_ambiguous"].append(row.id)
            elif len(row.options) < int(case.get("expect_options") or 2):
                out["missing_options"].append(row.id)
        elif row.expectation == "stated" and row.align in ("injected", "ambiguous"):
            out["changed_stated"].append(row.id)
        elif row.expectation == "quiet" and row.align != "unchanged":
            out["changed_quiet"].append(row.id)
        if row.expectation in ("alias", "status_enum") and row.mention:
            if row.mention == row.expect_canonical:
                out["dangling_alias"].append(row.id)
    return {key: sorted(values) for key, values in out.items() if values}


def frozen_footprint(frozen_cases: list[dict[str, Any]], aligner: ValueAligner) -> dict[str, Any]:
    """How much of the frozen corpus does the dictionary touch at all?

    A frozen question is the shared ruler for every other stage, so alignment may
    append values to it but must never turn it into a clarification: that would move
    a gold case out of the measurable set without any model being involved.
    """

    changed: list[str] = []
    ambiguous: list[str] = []
    injected: dict[str, int] = {}
    for case in frozen_cases:
        alignment = aligner.align(str(case.get("question") or ""))
        if alignment.ambiguous:
            ambiguous.append(str(case.get("id") or ""))
        if alignment.changed:
            changed.append(str(case.get("id") or ""))
            for item in alignment.resolved:
                key = f"{item.column}={item.canonical}"
                injected[key] = injected.get(key, 0) + 1
    return {
        "cases": len(frozen_cases),
        "changed": len(changed),
        "changed_ids": changed,
        "ambiguous": ambiguous,
        "injected_values": dict(sorted(injected.items(), key=lambda kv: -kv[1])),
    }


def _arm_results(settings: Settings, questions: list[str], experience: Path) -> list[Any]:
    runtime = create_runtime(replace(settings, experience_path=experience))
    return [runtime.agent.run(question) for question in questions]


def paired_ab(
    settings: Settings,
    rows: list[AliasRow],
    cases: list[dict[str, Any]],
    target: Target,
    *,
    mssql: bool,
) -> dict[str, Any]:
    by_id = {str(case.get("id") or ""): case for case in cases}
    gold: dict[str, dict[str, Any]] = {}
    for row in rows:
        case = by_id.get(row.id) or {}
        sql = str(case.get("gold_sql") or "").strip()
        if sql:
            gold[row.id] = run_readonly_query(
                target, sql, max_rows=settings.max_rows, timeout_ms=settings.query_timeout_ms
            )

    questions = [row.question for row in rows]
    root = Path(settings.home or Path(".")) / "var" / "alias_ab"
    root.mkdir(parents=True, exist_ok=True)
    off = _arm_results(replace(settings, value_alignment="off"), questions, root / "off.db")
    on = _arm_results(replace(settings, value_alignment="on"), questions, root / "on.db")

    def arm_pass(row: AliasRow, result: Any) -> str:
        case = by_id.get(row.id) or {}
        reference = gold.get(row.id) or {}
        if not reference.get("success"):
            return UNMEASURED
        if result.status != "ok":
            return "fail"
        same = rows_equal(
            [list(ref_row) for ref_row in (reference.get("rows") or [])],
            [list(res_row) for res_row in (result.rows or [])],
            order_sensitive=bool(case.get("order_sensitive", False)),
        )
        return "pass" if same else "fail"

    for row, off_result, on_result in zip(rows, off, on):
        row.off_status, row.on_status = off_result.status, on_result.status
        if row.expectation == "collision":
            # A clarification is not a result set, so it cannot vote in the EX delta.
            # It is measured next to it: did the agent ask, and did it hand the user
            # the candidate values to pick from.
            row.off_pass = row.on_pass = UNMEASURED
            row.delta = "clarification"
            continue
        row.off_pass, row.on_pass = arm_pass(row, off_result), arm_pass(row, on_result)
        if row.off_pass == UNMEASURED or row.on_pass == UNMEASURED:
            row.delta = UNMEASURED
            continue
        if row.off_pass != row.on_pass:
            row.delta = "fixed" if row.on_pass == "pass" else "broken"
        else:
            row.delta = "both_pass" if row.on_pass == "pass" else "both_fail"

    scored = [row for row in rows if row.delta != "clarification"]
    asked = [row for row in rows if row.expectation == "collision"]

    def count(key: str, value: str) -> int:
        return sum(1 for row in scored if getattr(row, key) == value)

    return {
        "measured": True,
        "provider": settings.provider,
        "cases": len(rows),
        "scored": len(scored),
        "gold_executed": sum(1 for ran in gold.values() if ran.get("success")),
        "gold_failed": sorted(
            row_id for row_id, ran in gold.items() if not ran.get("success")
        ),
        "off_pass": count("off_pass", "pass"),
        "on_pass": count("on_pass", "pass"),
        "fixed": sorted(row.id for row in rows if row.delta == "fixed"),
        "broken": sorted(row.id for row in rows if row.delta == "broken"),
        "both_pass": count("delta", "both_pass"),
        "both_fail": count("delta", "both_fail"),
        "unmeasured": count("delta", UNMEASURED),
        "clarification": {
            "cases": len(asked),
            "asked_on": sum(1 for row in asked if row.on_status == "needs_clarification"),
            "asked_off": sum(1 for row in asked if row.off_status == "needs_clarification"),
            "with_options": sum(1 for row in asked if len(row.options) >= 2),
        },
    }


def run_alias_audit(
    settings: Settings,
    catalog: Catalog,
    cases: list[dict[str, Any]],
    frozen_cases: list[dict[str, Any]],
    *,
    dictionary_path: str | Path | None = None,
    with_ab: bool = True,
) -> dict[str, Any]:
    if dictionary_path is not None:
        settings = replace(settings, value_dictionary_override=Path(dictionary_path))
    dictionary = ValueDictionary.load(settings.value_dictionary_path)
    mssql = settings.is_mssql
    aligner = ValueAligner(dictionary)
    rows = align_rows(cases, aligner)
    stats = dictionary.stats()
    cat = dictionary_catalog_findings(catalog, dictionary)
    data = dictionary_data_findings(dictionary, settings.target, mssql=mssql)
    dim = dim_enum_findings(dictionary, settings.target, catalog, mssql=mssql)
    footprint = frozen_footprint(frozen_cases, aligner)

    findings = corpus_findings(rows, cases)
    findings["stale_value"] = data["stale"]
    findings["dictionary_column_absent"] = cat["missing"]
    findings["frozen_turned_clarification"] = footprint["ambiguous"]
    if dim["missing_code"]:
        findings["dim_code_absent"] = dim["missing_code"]
    if dim["missing_label"]:
        findings["dim_label_absent"] = dim["missing_label"]
    findings = {name: ids for name, ids in findings.items() if ids}

    ab = (
        paired_ab(settings, rows, cases, settings.target, mssql=mssql)
        if with_ab
        else {"measured": False}
    )
    return {
        "cases": len(rows),
        "dictionary": {
            "entries": stats["entries"],
            "kinds": stats["kinds"],
            "aliases": stats["aliases"],
            "mentions": stats["mentions"],
            "collisions": stats["collisions"],
            "path": str(settings.value_dictionary_path),
        },
        "catalog": {
            "dialect": catalog.dialect,
            "tables": len(catalog.tables),
            **cat,
        },
        "data": data,
        "dim_lookup": dim,
        "frozen": footprint,
        "expectations": {
            key: sum(1 for row in rows if row.expectation == key)
            for key in ("alias", "stated", "collision", "quiet", "status_enum")
        },
        "align": {
            key: sum(1 for row in rows if row.align == key)
            for key in ("injected", "stated", "ambiguous", "unchanged", UNMEASURED)
        },
        "ab": ab,
        "findings": findings,
        "rows": [row.to_dict() for row in rows],
    }
