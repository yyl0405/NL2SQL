"""Dialect fidelity audit: one intent, several dialects, one executable backend.

The frozen corpus only ever runs on SQLite, so a dialect mistake can never show up as
an evaluation failure there — it shows up in production as a query the real engine
rejects, or worse, as a wrong number that arrives without an error. This module turns
that invisible class into measured numbers without needing a second database:

* ``gold_sql`` is the intent written for its own dialect, and it is checked for parsing
  and for guard acceptance under that dialect;
* ``sqlite_sql`` is the same intent on the backend we can actually run, and it supplies
  the reference result set;
* non-SQLite gold is transpiled down to SQLite and EXECUTED, so the comparison is on
  result sets, not on SQL text — a transpile that returns different rows is a silent
  divergence, and one that raises is a loud one. The two must be counted separately
  because only one of them can be caught by retry-on-error logic.

Nothing here calls a model. Every verdict is either measured or marked ``unmeasured``
with the reason, and an absent ``sqlglot`` or a non-SQLite backend shrinks the measured
set instead of producing a clean-looking zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .ast_guard import ast_available, map_dialect
from .catalog import Catalog
from .guard import SQLGuard
from .result_compare import rows_equal
from .tools.db import SqliteTarget, Target, coerce_target
from .tools.sql_tools import run_readonly_query

DIALECT_CASES = Path(__file__).resolve().parent / "data" / "dialect_cases.jsonl"
EXECUTABLE_DIALECT = "sqlite"

UNMEASURED = "unmeasured"


@dataclass
class DialectRow:
    id: str
    intent: str
    axis: str
    dialect: str
    gold_parse: str = UNMEASURED
    gold_guard_regex: str = UNMEASURED
    gold_guard_ast: str = UNMEASURED
    gold_codes: list[str] = field(default_factory=list)
    exec_mode: str = UNMEASURED
    xlate: str = UNMEASURED
    ref_rows: int | None = None
    ran_rows: int | None = None
    error: str = ""
    wrong_guard_regex: str = UNMEASURED
    wrong_guard_ast: str = UNMEASURED
    wrong_codes: list[str] = field(default_factory=list)
    wrong_engine: str = UNMEASURED

    @property
    def gold_blocked(self) -> bool:
        return "blocked" in (self.gold_guard_regex, self.gold_guard_ast)

    @property
    def unguarded_wrong_shape(self) -> bool:
        return self.wrong_guard_regex == "valid" and self.wrong_guard_ast != "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "intent": self.intent,
            "axis": self.axis,
            "dialect": self.dialect,
            "gold_parse": self.gold_parse,
            "gold_guard_regex": self.gold_guard_regex,
            "gold_guard_ast": self.gold_guard_ast,
            "gold_codes": list(self.gold_codes),
            "exec_mode": self.exec_mode,
            "xlate": self.xlate,
            "ref_rows": self.ref_rows,
            "ran_rows": self.ran_rows,
            "error": self.error,
            "wrong_guard_regex": self.wrong_guard_regex,
            "wrong_guard_ast": self.wrong_guard_ast,
            "wrong_codes": list(self.wrong_codes),
            "wrong_engine": self.wrong_engine,
        }


def _guard_verdict(
    catalog: Catalog, dialect: str, sql: str, *, with_ast: bool
) -> tuple[str, list[str]]:
    scoped = replace(
        catalog, dialect=dialect, _link_index=None, _link_key=None
    ) if (catalog.dialect or "").lower() != dialect.lower() else catalog
    analyzer = None
    if with_ast:
        from .ast_guard import AstAnalyzer

        analyzer = AstAnalyzer(dialect)
    result = SQLGuard(scoped, analyzer=analyzer).check(sql)
    codes = sorted({issue.code for issue in result.errors})
    return ("valid" if result.valid else "blocked"), codes


def _transpile(sql: str, dialect: str) -> tuple[str, str]:
    try:
        import sqlglot
    except Exception as exc:  # noqa: BLE001 - optional dependency
        return "", f"sqlglot_unavailable:{type(exc).__name__}"
    try:
        return sqlglot.transpile(sql, read=map_dialect(dialect), write=EXECUTABLE_DIALECT)[0], ""
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:120]}"


def _parses(sql: str, dialect: str) -> str:
    try:
        import sqlglot
    except Exception:  # noqa: BLE001
        return UNMEASURED
    try:
        sqlglot.parse_one(sql, read=map_dialect(dialect))
    except Exception:  # noqa: BLE001
        return "failed"
    return "ok"


def audit_dialect_case(
    catalog: Catalog,
    case: dict[str, Any],
    *,
    target: Target | str | Path | None,
    with_ast: bool,
    max_rows: int = 200,
    timeout_ms: int = 10000,
) -> DialectRow:
    dialect = str(case.get("dialect") or EXECUTABLE_DIALECT).strip().lower()
    # Callers here pass a bare demo-library path as often as a target object.
    target = coerce_target(target) if target is not None else None
    row = DialectRow(
        id=str(case.get("id", "")),
        intent=str(case.get("intent", "")),
        axis=str(case.get("axis", "")),
        dialect=dialect,
    )
    gold = str(case.get("gold_sql") or "")
    reference = str(case.get("sqlite_sql") or "")
    wrong = str(case.get("wrong_dialect_sql") or "")

    row.gold_parse = _parses(gold, dialect)
    verdict, codes = _guard_verdict(catalog, dialect, gold, with_ast=False)
    row.gold_guard_regex, row.gold_codes = verdict, codes
    if with_ast:
        row.gold_guard_ast = _guard_verdict(catalog, dialect, gold, with_ast=True)[0]
        row.wrong_guard_ast = _guard_verdict(catalog, dialect, wrong, with_ast=True)[0]
    row.wrong_guard_regex, row.wrong_codes = _guard_verdict(
        catalog, dialect, wrong, with_ast=False
    )

    if target is None or not isinstance(target, SqliteTarget) or not reference:
        # Only SQLite can be executed offline; on any other backend the result-set
        # columns stay unmeasured rather than pretending the audit ran.
        row.exec_mode = UNMEASURED
        row.xlate = UNMEASURED
        row.wrong_engine = UNMEASURED
        return row

    ref = run_readonly_query(target, reference, max_rows=max_rows, timeout_ms=timeout_ms)
    if not ref.get("success"):
        row.exec_mode = "reference_failed"
        row.xlate = "exec_error"
        row.error = str(ref.get("error") or "")[:160]
        return row
    row.ref_rows = int(ref.get("row_count") or 0)

    probe = run_readonly_query(target, wrong, max_rows=max_rows, timeout_ms=timeout_ms)
    row.wrong_engine = "rejects" if not probe.get("success") else "runs"

    if dialect == EXECUTABLE_DIALECT:
        row.exec_mode = "native"
        ran = run_readonly_query(target, gold, max_rows=max_rows, timeout_ms=timeout_ms)
    else:
        if not ast_available():
            # Transpiling is the only way to compare a foreign-dialect gold offline,
            # so without sqlglot the honest answer is "not measured", not a failure.
            row.exec_mode = UNMEASURED
            row.xlate = UNMEASURED
            row.error = "sqlglot 未安装，无法把该方言转译到 SQLite 比对"
            return row
        row.exec_mode = "transpiled"
        translated, failure = _transpile(gold, dialect)
        if not translated:
            row.xlate = "transpile_error"
            row.error = failure
            ran = None
        else:
            ran = run_readonly_query(
                target, translated, max_rows=max_rows, timeout_ms=timeout_ms
            )
    if ran is None:
        return row
    if not ran.get("success"):
        row.xlate = "exec_error"
        row.ran_rows = None
        row.error = str(ran.get("error") or "")[:160]
        return row
    row.ran_rows = int(ran.get("row_count") or 0)
    same = rows_equal(
        [list(r) for r in ref.get("rows") or []],
        [list(r) for r in ran.get("rows") or []],
        order_sensitive=bool(case.get("order_sensitive", True)),
    )
    row.xlate = "match" if same else "divergence"
    return row


def run_dialect_audit(
    catalog: Catalog,
    cases: list[dict[str, Any]],
    *,
    target: Target | str | Path | None,
    with_ast: bool | None = True,
) -> dict[str, Any]:
    if with_ast is None:
        with_ast = ast_available()
    # A bare path is still "the demo library"; without this it would be reported as an
    # unmeasured backend instead of the SQLite target it is.
    target = coerce_target(target) if target is not None else None
    rows = [
        audit_dialect_case(catalog, case, target=target, with_ast=with_ast) for case in cases
    ]
    dialects: dict[str, int] = {}
    for row in rows:
        dialects[row.dialect] = dialects.get(row.dialect, 0) + 1

    xlate_counts = {
        key: sum(1 for row in rows if row.xlate == key)
        for key in ("match", "divergence", "exec_error", "transpile_error", UNMEASURED)
    }
    comparable = xlate_counts["match"] + xlate_counts["divergence"] + xlate_counts["exec_error"]
    transpiled_rows = [row for row in rows if row.exec_mode == "transpiled"]
    transpiled_comparable = [
        row for row in transpiled_rows if row.xlate in ("match", "divergence", "exec_error")
    ]
    transpiled_match = [row for row in transpiled_comparable if row.xlate == "match"]
    wrong_blocked_regex = [
        row.id for row in rows if row.wrong_guard_regex == "blocked"
    ]
    wrong_blocked_ast = [row.id for row in rows if row.wrong_guard_ast == "blocked"]

    if target is None:
        backend = UNMEASURED
    elif isinstance(target, SqliteTarget):
        backend = "sqlite"
    else:
        # The target says which server it is; falling back to "sqlite" here would print
        # a backend that was never used.
        backend = str(target.dialect)

    # Guard verdicts are only about the guard when the catalog actually contains what the
    # corpus queries. Against a real-ERP catalog the demo tables are absent, so every
    # gold comes back ``unknown_table`` — reporting that as "N golds falsely blocked"
    # would blame the guard for a catalog/corpus mismatch.
    corpus_tables = sorted({
        str(table) for case in cases for table in (case.get("expected_tables") or [])
    })
    missing_tables = [table for table in corpus_tables if catalog.table(table) is None]

    failures = {
        "gold_parse_failed": [row.id for row in rows if row.gold_parse == "failed"],
        "gold_blocked_by_guard": [row.id for row in rows if row.gold_blocked],
        "reference_failed": [row.id for row in rows if row.exec_mode == "reference_failed"],
        "empty_reference": [row.id for row in rows if row.ref_rows == 0],
    }
    return {
        "cases": len(rows),
        "intents": len({row.intent for row in rows}),
        "axes": sorted({row.axis for row in rows}),
        "dialects": dialects,
        "backend": backend,
        "catalog": {
            "dialect": catalog.dialect,
            "tables": len(catalog.tables),
            "corpus_tables": corpus_tables,
            "missing_tables": missing_tables,
        },
        "parser_measured": ast_available(),
        "gold_parse": {
            "ok": sum(1 for row in rows if row.gold_parse == "ok"),
            "failed": len(failures["gold_parse_failed"]),
            "unmeasured": sum(1 for row in rows if row.gold_parse == UNMEASURED),
            "failed_ids": failures["gold_parse_failed"],
        },
        "gold_guard": {
            "regex_blocked": len(failures["gold_blocked_by_guard"]),
            "blocked_ids": failures["gold_blocked_by_guard"],
            "ast_checked": with_ast,
        },
        "reference": {
            "executed": sum(1 for row in rows if row.ref_rows is not None),
            "failed": len(failures["reference_failed"]),
            "empty": len(failures["empty_reference"]),
            "failed_ids": failures["reference_failed"] + failures["empty_reference"],
        },
        "equivalence": {
            "comparable": comparable,
            "comparable_transpiled": len(transpiled_comparable),
            "transpiled_match": len(transpiled_match),
            "transpiled_match_rate": (
                round(len(transpiled_match) / len(transpiled_comparable), 4)
                if transpiled_comparable
                else None
            ),
            "native": sum(1 for row in rows if row.exec_mode == "native"),
            "transpiled": sum(1 for row in rows if row.exec_mode == "transpiled"),
            **xlate_counts,
            "match_rate": round(xlate_counts["match"] / comparable, 4) if comparable else None,
            "divergence_ids": [row.id for row in rows if row.xlate == "divergence"],
            "exec_error_ids": [
                row.id for row in rows if row.xlate in ("exec_error", "transpile_error")
            ],
        },
        "wrong_shapes": {
            "total": len(rows),
            "regex_blocked": len(wrong_blocked_regex),
            "ast_blocked": len(wrong_blocked_ast),
            "regex_blocked_ids": wrong_blocked_regex,
            "ast_only_blocked_ids": [
                row.id for row in rows if row.wrong_guard_ast == "blocked"
                and row.wrong_guard_regex != "blocked"
            ],
            "engine_rejects": sum(1 for row in rows if row.wrong_engine == "rejects"),
            "engine_runs": sum(1 for row in rows if row.wrong_engine == "runs"),
            "engine_unmeasured": sum(1 for row in rows if row.wrong_engine == UNMEASURED),
            "unguarded_ids": [row.id for row in rows if row.unguarded_wrong_shape],
        },
        "invariant_failures": failures,
        "rows": [row.to_dict() for row in rows],
    }
