"""Model-free, database-free guard bench: adversarial, permission and fan-out suites.

The agent evaluation measures whether the model asks the right question. This
bench measures whether the guard stops the wrong SQL — and, just as importantly,
whether it leaves the right SQL alone. Both numbers matter: a guard that blocks
everything is safe and useless.

Three suites, all declarative JSONL so a reviewer can add a case without
touching code:

* ``adversarial_cases.jsonl`` — expected block/pass with expected error codes,
  tagged ``regex`` (the zero-dependency layer must catch it) or ``ast`` (only
  the parser can block it, or only it names the right reason — an introspection
  query rejected as ``unknown_table`` is blocked but misdiagnosed). Running
  both layers is how the AST's actual contribution is measured instead of
  assumed: an ``ast`` case is *executed* under the regex layer but not asserted,
  so the regex column reports holes in the regex layer rather than the designed
  division of labour, and ``ast_only_blocked`` counts what regex genuinely let
  through.
* ``permission_cases.jsonl`` — a principal, a SQL, an expected verdict, plus
  ``view_forbids``: identifiers that must not even appear in the filtered
  catalog the prompt is built from.
* ``fanout_cases.jsonl`` — JOIN fan-out warnings, including the negative cases
  that keep the warning honest.

Finally ``audit_gold_sql`` runs the guard over the evaluation corpus: the gold
SQL is by definition correct, so an error there is a guard bug and a warning is
noise worth counting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .ast_guard import AstAnalyzer, ast_available
from .catalog import Catalog
from .guard import GuardResult, SQLGuard
from .principal import Principal, principal_from_payload

ADVERSARIAL_SUITE = "adversarial"
PERMISSION_SUITE = "permission"
FANOUT_SUITE = "fanout"

DATA_DIR = Path(__file__).resolve().parent / "data"
ADVERSARIAL_CASES = DATA_DIR / "adversarial_cases.jsonl"
PERMISSION_CASES = DATA_DIR / "permission_cases.jsonl"
FANOUT_CASES = DATA_DIR / "fanout_cases.jsonl"


@dataclass
class CaseOutcome:
    id: str
    suite: str
    passed: bool
    expect_valid: bool
    actual_valid: bool
    codes: list[str] = field(default_factory=list)
    missing_codes: list[str] = field(default_factory=list)
    unexpected_codes: list[str] = field(default_factory=list)
    layer: str = "regex"
    why: str = ""
    detail: str = ""
    asserted: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "suite": self.suite,
            "passed": self.passed,
            "expect_valid": self.expect_valid,
            "actual_valid": self.actual_valid,
            "codes": self.codes,
            "missing_codes": self.missing_codes,
            "unexpected_codes": self.unexpected_codes,
            "layer": self.layer,
            "why": self.why,
            "detail": self.detail,
            "asserted": self.asserted,
        }


def load_case_file(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_principals(path: str | Path) -> dict[str, Principal]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload.get("principals") if isinstance(payload, dict) else payload
    return {
        str(raw.get("id", "")): principal_from_payload(raw)
        for raw in items or []
        if isinstance(raw, dict)
    }


def _catalog_for(catalog: Catalog, dialect: str) -> Catalog:
    wanted = (dialect or catalog.dialect or "sqlite").strip().lower()
    if wanted == (catalog.dialect or "").strip().lower():
        return catalog
    return replace(catalog, dialect=wanted, _link_index=None, _link_key=None)


def _verdict(
    case: dict[str, Any], result: GuardResult, suite: str, *, asserted: bool = True
) -> CaseOutcome:
    expect_valid = bool(case.get("expect_valid", True))
    codes = sorted({issue.code for issue in result.errors})
    missing = [code for code in (case.get("expect_codes") or []) if code not in codes]
    passed = (expect_valid == result.valid and not missing) if asserted else True
    return CaseOutcome(
        id=str(case.get("id", "")),
        suite=suite,
        passed=passed,
        expect_valid=expect_valid,
        actual_valid=result.valid,
        codes=codes,
        missing_codes=missing if asserted else [],
        unexpected_codes=[] if expect_valid or not asserted else codes,
        layer=str(case.get("layer", "regex")),
        why=str(case.get("why", "")),
        detail="; ".join(issue.message for issue in result.errors)[:200],
        asserted=asserted,
    )


def run_adversarial(
    catalog: Catalog,
    cases: list[dict[str, Any]],
    *,
    with_ast: bool,
) -> list[CaseOutcome]:
    outcomes: list[CaseOutcome] = []
    for case in cases:
        # A case tagged ``ast`` asserts what the parser layer is for. Under the
        # regex-only run it is still *executed* — that is how "the regex layer
        # has a hole" is distinguished from "the regex layer blocked it for a
        # different reason" — but its verdict is recorded, not asserted, so the
        # regex column measures the regex layer's own contract.
        asserted = with_ast or str(case.get("layer", "regex")) != "ast"
        dialect = str(case.get("dialect", catalog.dialect or "sqlite"))
        scoped = _catalog_for(catalog, dialect)
        analyzer = AstAnalyzer(dialect=dialect) if with_ast else None
        guard = SQLGuard(scoped, analyzer=analyzer)
        result = guard.check(str(case.get("sql", "")))
        outcomes.append(_verdict(case, result, ADVERSARIAL_SUITE, asserted=asserted))
    return outcomes


def run_permission(
    catalog: Catalog,
    cases: list[dict[str, Any]],
    principals: dict[str, Principal],
    *,
    with_ast: bool = False,
) -> tuple[list[CaseOutcome], list[str]]:
    outcomes: list[CaseOutcome] = []
    leaks: list[str] = []
    for case in cases:
        principal = principals.get(str(case.get("principal", "")))
        if principal is None:
            outcomes.append(
                CaseOutcome(
                    id=str(case.get("id", "")),
                    suite=PERMISSION_SUITE,
                    passed=False,
                    expect_valid=bool(case.get("expect_valid", True)),
                    actual_valid=False,
                    layer="principal",
                    detail=f"未定义的 principal: {case.get('principal')}",
                )
            )
            continue
        guard = SQLGuard(
            catalog,
            principal=principal,
            analyzer=AstAnalyzer(catalog.dialect) if with_ast else None,
        )
        # ``ast_only`` cases assert that a table the tokenizer cannot even see
        # still hits the permission and catalogue checks. Without the parser
        # installed the premise does not exist, so the case is recorded, not
        # asserted — otherwise "optional dependency absent" reads as "bug".
        outcome = _verdict(
            case,
            guard.check(str(case.get("sql", ""))),
            PERMISSION_SUITE,
            asserted=with_ast or not case.get("ast_only"),
        )
        outcome.layer = "principal"
        outcomes.append(outcome)
        view = principal.view(catalog)
        for token in case.get("view_forbids", []) or []:
            table_name, _, column_name = str(token).partition(".")
            if not column_name:
                if view.table(table_name) is not None:
                    leaks.append(f"{case.get('id')}: 视图仍包含表 {token}")
                continue
            table = view.table(table_name)
            if table is not None and table.column(column_name) is not None:
                leaks.append(f"{case.get('id')}: 视图仍包含字段 {token}")
    return outcomes, leaks


def run_fanout(catalog: Catalog, cases: list[dict[str, Any]]) -> list[CaseOutcome]:
    outcomes: list[CaseOutcome] = []
    for case in cases:
        dialect = str(case.get("dialect", catalog.dialect or "sqlite"))
        guard = SQLGuard(_catalog_for(catalog, dialect))
        result = guard.check(str(case.get("sql", "")))
        warned = any(issue.code == "join_fanout" for issue in result.warnings)
        expect_warn = bool(case.get("expect_warn", True))
        codes = sorted({issue.code for issue in result.issues})
        outcomes.append(
            CaseOutcome(
                id=str(case.get("id", "")),
                suite=FANOUT_SUITE,
                passed=warned == expect_warn and result.valid,
                expect_valid=expect_warn,
                actual_valid=warned,
                codes=codes,
                missing_codes=["join_fanout"] if expect_warn and not warned else [],
                unexpected_codes=[] if expect_warn or not warned else ["join_fanout"],
                layer="regex",
                why=str(case.get("why", "")),
                detail="; ".join(issue.message for issue in result.warnings)[:200],
            )
        )
    return outcomes


@dataclass
class GoldAudit:
    total: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    # Without sqlglot the AST block is a placeholder, not a clean pass: `measured`
    # is the only thing in the JSON that says so.
    measured: bool = True

    @property
    def error_rate(self) -> float:
        return len(self.errors) / self.total if self.total else 0.0

    @property
    def warning_rate(self) -> float:
        return len(self.warnings) / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "measured": self.measured,
            "total": self.total,
            "skipped": self.skipped,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "error_rate": round(self.error_rate, 4),
            "warning_rate": round(self.warning_rate, 4),
            "error_cases": [row["id"] for row in self.errors],
            "warning_cases": [row["id"] for row in self.warnings],
        }


def audit_gold_sql(
    catalog: Catalog,
    cases: list[dict[str, Any]],
    *,
    with_ast: bool,
    principals: dict[str, Principal] | None = None,
) -> GoldAudit:
    """Correct SQL must never be blocked: an error here is a guard bug."""
    # Refusal cases carry no gold SQL on purpose — the right answer is "no query".
    # Auditing them as SQL would report a phantom guard bug on every run.
    executable = [case for case in cases if str(case.get("gold_sql", "")).strip()]
    audit = GoldAudit(total=len(executable), skipped=len(cases) - len(executable))
    analyzer = AstAnalyzer(catalog.dialect) if with_ast else None
    for case in executable:
        sql = str(case.get("gold_sql", ""))
        guard = SQLGuard(catalog, analyzer=analyzer)
        result = guard.check(sql)
        row = {
            "id": str(case.get("id", "")),
            "codes": sorted({issue.code for issue in result.issues}),
            "messages": [issue.message for issue in result.errors][:2],
        }
        if result.errors:
            audit.errors.append(row)
        elif result.warnings:
            audit.warnings.append(row)
    if principals:
        for principal in principals.values():
            for case in executable:
                sql = str(case.get("gold_sql", ""))
                guard = SQLGuard(catalog, principal=principal)
                result = guard.check(sql)
                blocked = [i for i in result.errors if i.code in {"permission_table", "permission_column"}]
                if blocked:
                    audit.errors.append(
                        {
                            "id": f"{case.get('id')}@{principal.id}",
                            "codes": sorted({i.code for i in blocked}),
                            "messages": [i.message for i in blocked][:2],
                        }
                    )
    return audit


def run_all(
    catalog: Catalog,
    *,
    adversarial: list[dict[str, Any]],
    permission: list[dict[str, Any]],
    fanout: list[dict[str, Any]],
    gold: list[dict[str, Any]],
    principals: dict[str, Principal],
    with_ast: bool | None = None,
) -> dict[str, Any]:
    """One bench: regex layer alone, regex + AST, permissions, fan-out, gold SQL."""
    # ``with_ast=None`` follows the machine: without sqlglot installed the AST
    # columns are dropped rather than reporting every case as blocked, which is
    # what fail-closed would look like and is not a measurement of anything.
    measure_ast = ast_available() if with_ast is None else with_ast
    regex = run_adversarial(catalog, adversarial, with_ast=False)
    ast = run_adversarial(catalog, adversarial, with_ast=True) if measure_ast else []
    perm_outcomes, leaks = run_permission(catalog, permission, principals, with_ast=measure_ast)
    fanout_outcomes = run_fanout(catalog, fanout)
    gold_regex = audit_gold_sql(catalog, gold, with_ast=False)
    gold_ast = (
        audit_gold_sql(catalog, gold, with_ast=True)
        if measure_ast
        else GoldAudit(total=gold_regex.total, skipped=gold_regex.skipped, measured=False)
    )

    blocked_regex = {row.id for row in regex if not row.actual_valid}
    ast_only = sorted({row.id for row in ast if not row.actual_valid} - blocked_regex)
    regex_also_blocked = sorted(
        row.id
        for row in regex
        if row.layer == "ast" and not row.expect_valid and not row.actual_valid
    )
    failed_regex = [row for row in regex if not row.passed]
    failed_ast = [row for row in ast if not row.passed]
    failed_perm = [row for row in perm_outcomes if not row.passed]
    failed_fanout = [row for row in fanout_outcomes if not row.passed]
    return {
        "catalog": {
            "dialect": catalog.dialect,
            "tables": len(catalog.enabled_tables()),
            "ast_available": ast_available(),
            "ast_measured": measure_ast,
        },
        "adversarial": {
            "total": len(adversarial),
            "blocking": sum(1 for case in adversarial if not case.get("expect_valid", True)),
            "pass_through": sum(1 for case in adversarial if case.get("expect_valid", True)),
            "tagged_ast": sum(1 for case in adversarial if str(case.get("layer")) == "ast"),
            "regex_scope": sum(1 for row in regex if row.asserted),
            "regex_blocking_scope": sum(
                1 for row in regex if row.asserted and not row.expect_valid
            ),
            "regex_blocked": sum(
                1 for row in regex if row.asserted and not row.expect_valid and not row.actual_valid
            ),
            "ast_blocked": sum(1 for row in ast if not row.expect_valid and not row.actual_valid),
            "ast_only_blocked": ast_only,
            "regex_also_blocked_ast_tagged": regex_also_blocked,
            "regex_missed": sorted(
                row.id
                for row in regex
                if row.asserted and not row.expect_valid and row.actual_valid
            ),
            "false_positive_ast": sorted(
                row.id for row in ast if row.expect_valid and not row.actual_valid
            ),
            "failed_regex": [row.to_dict() for row in failed_regex],
            "failed_ast": [row.to_dict() for row in failed_ast],
        },
        "permission": {
            "total": len(permission),
            "principals": sorted(principals),
            "blocked": sum(1 for row in perm_outcomes if not row.actual_valid),
            "view_leaks": leaks,
            "failed": [row.to_dict() for row in failed_perm],
        },
        "fanout": {
            "total": len(fanout),
            "warned": sum(1 for row in fanout_outcomes if row.actual_valid),
            "failed": [row.to_dict() for row in failed_fanout],
        },
        "gold_sql": {"regex": gold_regex.to_dict(), "ast": gold_ast.to_dict()},
        "failures": {
            "adversarial_regex": len(failed_regex),
            "adversarial_ast": len(failed_ast),
            "permission": len(failed_perm),
            "fanout": len(failed_fanout),
            "view_leaks": len(leaks),
            "gold_errors_regex": len(gold_regex.errors),
            "gold_errors_ast": len(gold_ast.errors) if measure_ast else 0,
        },
    }

