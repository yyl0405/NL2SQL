from __future__ import annotations

import builtins
import json
from dataclasses import replace
from typing import Any

import pytest

from nl2sql_agent.ast_guard import ast_available
from nl2sql_agent.catalog import Catalog
from nl2sql_agent.cli import _print_dialect_report, main
from nl2sql_agent.config import Settings
from nl2sql_agent.dialect_audit import (
    DIALECT_CASES,
    UNMEASURED,
    audit_dialect_case,
    run_dialect_audit,
)
from nl2sql_agent.evaluation import load_cases
from nl2sql_agent.guard import SQLGuard
from nl2sql_agent.guard_suite import load_case_file
from nl2sql_agent.runtime import Runtime
from nl2sql_agent.tools.db import MssqlTarget

# Verdicts measured with sqlglot on the synthetic demo catalog. Pinned on purpose: a
# transpiler upgrade that changes a result set, or a dialect adapter that finally
# catches a cross-dialect shape, has to show up here as a test diff instead of
# quietly moving the numbers in the report.
SILENT_DIVERGENCE = ["dialect_month_tsql", "dialect_concat_tsql", "dialect_round_tsql"]
LOUD_FAILURE = ["dialect_year_tsql", "dialect_year_pg", "dialect_year_mysql"]


def _corpus() -> list[dict[str, Any]]:
    return load_case_file(DIALECT_CASES)


def _guard_as(catalog: Catalog, dialect: str) -> SQLGuard:
    """Same tables, labelled as another dialect — how the audit scopes the guard."""
    return SQLGuard(replace(catalog, dialect=dialect, _link_index=None, _link_key=None))


def _audit(runtime: Runtime, *, with_ast: bool | None = None) -> dict[str, Any]:
    return run_dialect_audit(
        runtime.catalog, _corpus(), target=runtime.settings.db_path, with_ast=with_ast
    )


def _block_sqlglot(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "sqlglot" or name.startswith("sqlglot."):
            raise ImportError("No module named 'sqlglot'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr("nl2sql_agent.dialect_audit.ast_available", lambda: False)


def test_corpus_shape_axes_and_dialects() -> None:
    cases = _corpus()
    assert len(cases) == 22
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["intent"] for case in cases}) == 8
    assert {case["axis"] for case in cases} == {
        "row_limit",
        "paging",
        "date_format",
        "date_part",
        "string_concat",
        "identifier_quote",
        "null_fallback",
        "numeric_precision",
    }
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["dialect"]] = counts.get(case["dialect"], 0) + 1
        assert case["tags"] == ["dialect"], case["id"]
        assert "v1_baseline" not in case["tags"]
        assert case["status"] == "active"
        assert case["source"] == "synthetic"
        assert case["order_sensitive"] is True
        assert case["expected_tables"]
        # A dialect row is only meaningful when all three spellings of the intent exist.
        for field in ("gold_sql", "sqlite_sql", "wrong_dialect_sql"):
            assert case[field].strip(), f"{case['id']}: empty {field}"
        assert case["gold_sql"] != case["wrong_dialect_sql"], case["id"]
    assert counts == {"tsql": 8, "postgres": 7, "mysql": 4, "sqlite": 3}


def test_one_intent_is_one_question_and_one_reference() -> None:
    by_intent: dict[str, list[dict[str, Any]]] = {}
    for case in _corpus():
        by_intent.setdefault(case["intent"], []).append(case)
    for intent, rows in by_intent.items():
        assert len(rows) >= 2, intent
        assert len({row["question"] for row in rows}) == 1, intent
        assert len({row["sqlite_sql"] for row in rows}) == 1, intent
        assert len({row["dialect"] for row in rows}) == len(rows), intent


def test_dialect_corpus_never_enters_the_gated_corpus() -> None:
    gated = {case.id for case in load_cases(Settings().eval_cases_path)}
    assert gated.isdisjoint({case["id"] for case in _corpus()})
    baseline = [
        case
        for case in load_cases(Settings().eval_cases_path)
        if "v1_baseline" in set(case.tags)
    ]
    assert len(baseline) == 40


def test_every_dialect_gold_is_accepted_by_its_own_dialect_guard(runtime: Runtime) -> None:
    """What the corpus is for: a dialect-native gold must not be a false rejection."""
    report = _audit(runtime)
    assert report["gold_guard"]["regex_blocked"] == 0
    assert report["gold_guard"]["blocked_ids"] == []
    assert report["invariant_failures"]["gold_blocked_by_guard"] == []
    assert report["gold_guard"]["ast_checked"] == ast_available()
    assert [row["id"] for row in report["rows"] if row["gold_guard_ast"] == "blocked"] == []
    if ast_available():
        assert report["gold_parse"]["ok"] == 22
        assert report["gold_parse"]["failed"] == 0


def test_function_keyword_from_is_not_read_as_a_table(
    catalog: Catalog,
) -> None:
    """`EXTRACT(YEAR FROM d)` hides a keyword, not a table reference.

    Reading it as a table turned a column name into an unknown table and blocked
    correct PostgreSQL / MySQL gold, so this is the regression the corpus bought.
    Scoped to PostgreSQL because these four spellings are PostgreSQL's: on a
    SQLite-labelled catalog the dialect rule is right to reject `EXTRACT(...)`
    outright, and that would test the wrong thing here.
    """
    guard = _guard_as(catalog, "postgres")
    for sql in (
        "SELECT EXTRACT(YEAR FROM o.order_date) AS y FROM sales_order o",
        "SELECT TRIM(BOTH ' ' FROM o.region) AS r FROM sales_order o",
        "SELECT SUBSTRING(o.region FROM 2 FOR 3) AS r FROM sales_order o",
        "SELECT POSITION('x' IN o.customer_name) AS p FROM sales_order o",
    ):
        result = guard.check(sql)
        assert result.valid, f"{sql} -> {[i.code for i in result.errors]}"
        assert result.tables == ["sales_order"], sql


def test_blanking_never_hides_a_real_table(guard: SQLGuard) -> None:
    """The fix must not become a bypass: a real FROM after the function still counts."""
    for sql, code in (
        ("SELECT TRIM(o.customer_name) AS c FROM internal_audit", "disabled_table"),
        ("SELECT audit_id FROM internal_audit", "disabled_table"),
        ("SELECT name FROM sqlite_master WHERE type = 'table'", "unknown_table"),
    ):
        result = guard.check(sql)
        assert not result.valid, sql
        assert code in {issue.code for issue in result.errors}, sql


def test_every_sqlite_reference_executes_and_returns_rows(runtime: Runtime) -> None:
    report = _audit(runtime)
    assert report["reference"]["executed"] == 22
    assert report["reference"]["failed"] == 0
    assert report["reference"]["empty"] == 0
    assert all(row["ref_rows"] and row["ref_rows"] > 0 for row in report["rows"])
    assert report["invariant_failures"]["reference_failed"] == []
    assert report["invariant_failures"]["empty_reference"] == []


def test_transpile_equivalence_verdicts_are_pinned(runtime: Runtime) -> None:
    if not ast_available():
        pytest.skip("sqlglot 未安装：只有原生 SQLite 行可比对")
    eq = _audit(runtime)["equivalence"]
    assert eq["comparable"] == 22
    assert (eq["native"], eq["transpiled"]) == (3, 19)
    assert eq["match"] == 16
    assert eq["transpile_error"] == 0
    assert eq["unmeasured"] == 0
    assert eq["match_rate"] == pytest.approx(0.7273)
    assert eq["comparable_transpiled"] == 19
    assert eq["transpiled_match"] == 13
    assert eq["transpiled_match_rate"] == pytest.approx(0.6842)
    assert eq["divergence_ids"] == SILENT_DIVERGENCE
    assert eq["exec_error_ids"] == LOUD_FAILURE
    # Silent and loud failures are different classes, and only one of them is catchable
    # by retry-on-error logic, so they must never be merged into one counter.
    assert set(eq["divergence_ids"]).isdisjoint(eq["exec_error_ids"])


def test_silent_divergence_rows_carry_no_error_while_loud_rows_do(runtime: Runtime) -> None:
    if not ast_available():
        pytest.skip("sqlglot 未安装：转译列不参与测量")
    rows = {row["id"]: row for row in _audit(runtime)["rows"]}
    for case_id in SILENT_DIVERGENCE:
        assert rows[case_id]["xlate"] == "divergence"
        assert rows[case_id]["error"] == ""
        assert rows[case_id]["ran_rows"] is not None
    for case_id in LOUD_FAILURE:
        assert rows[case_id]["xlate"] == "exec_error"
        assert rows[case_id]["error"]
        assert rows[case_id]["ran_rows"] is None


def test_regex_layer_blocks_every_cross_dialect_shape(runtime: Runtime) -> None:
    """P2-2 closed: the dialect rules run in the regex layer, for 22/22 wrong shapes.

    This was the measured hole (`regex_blocked == 0`, "the tokenizer reads blanked
    tokens, TOP 3 and LIMIT 3 are the same string to it"). The rules are keyed on the
    catalog's own dialect, so the same corpus also proves the other half: 0 of the 22
    native spellings are rejected. Engine behaviour is unchanged — 13 reject, 9 run —
    and every one of those 9 silent ones is now stopped before it reaches the database.
    """
    report = _audit(runtime)
    wrong = report["wrong_shapes"]
    assert wrong["total"] == 22
    assert wrong["regex_blocked"] == 22
    assert len(wrong["regex_blocked_ids"]) == 22
    assert report["gold_guard"]["regex_blocked"] == 0
    rows = {row["id"]: row for row in report["rows"]}
    silent = {case_id for case_id, row in rows.items() if row["wrong_engine"] == "runs"}
    assert len(silent) == 9
    assert silent <= set(wrong["regex_blocked_ids"])
    if ast_available():
        assert wrong["ast_blocked"] == 22
        assert wrong["ast_only_blocked_ids"] == []
        assert wrong["unguarded_ids"] == []
    else:
        assert wrong["ast_blocked"] == 0
    assert wrong["engine_rejects"] == 13
    assert wrong["engine_runs"] == 9
    assert wrong["engine_rejects"] + wrong["engine_runs"] + wrong["engine_unmeasured"] == 22


def test_parser_absence_reports_unmeasured_rather_than_a_clean_zero(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _block_sqlglot(monkeypatch)
    report = run_dialect_audit(
        runtime.catalog, _corpus(), target=runtime.settings.db_path, with_ast=None
    )
    assert report["parser_measured"] is False
    assert report["gold_parse"] == {
        "ok": 0,
        "failed": 0,
        "unmeasured": 22,
        "failed_ids": [],
    }
    eq = report["equivalence"]
    # Only the three native SQLite rows are still comparable; the rest say "not
    # measured" instead of being counted as transpile failures.
    assert eq["comparable"] == 3
    assert eq["match"] == 3
    assert eq["transpile_error"] == 0
    assert eq["exec_error"] == 0
    assert eq["unmeasured"] == 19
    assert all(row["gold_guard_regex"] == "valid" for row in report["rows"])
    assert all(row["exec_mode"] == UNMEASURED for row in report["rows"] if row["dialect"] != "sqlite")
    assert report["gold_guard"]["ast_checked"] is False
    assert report["invariant_failures"] == {
        "gold_parse_failed": [],
        "gold_blocked_by_guard": [],
        "reference_failed": [],
        "empty_reference": [],
    }
    _print_dialect_report(report)
    out = capsys.readouterr().out
    assert "未安装 sqlglot" in out
    assert "不是 0 命中" in out
    # A rate computed over 3 of 22 rows must be printed with its denominator, or the
    # 100% here reads like a result.
    assert "可比 3/22 条" in out


def test_a_blocked_gold_is_a_corpus_failure_not_a_silent_pass(runtime: Runtime) -> None:
    """Anti-vacuity: the invariant that keeps dialect gold honest really fires."""
    cases = [
        {
            "id": "fake_blocked_gold",
            "intent": "audit_rows",
            "axis": "row_limit",
            "dialect": "tsql",
            "gold_sql": "SELECT TOP 3 audit_id FROM internal_audit",
            "sqlite_sql": "SELECT audit_id FROM internal_audit LIMIT 3",
            "wrong_dialect_sql": "SELECT order_id FROM sales_order LIMIT 3",
            "order_sensitive": True,
        }
    ]
    report = run_dialect_audit(
        runtime.catalog, cases, target=runtime.settings.db_path, with_ast=ast_available()
    )
    assert report["invariant_failures"]["gold_blocked_by_guard"] == ["fake_blocked_gold"]
    assert report["gold_guard"]["blocked_ids"] == ["fake_blocked_gold"]
    assert report["gold_guard"]["regex_blocked"] == 1


def test_mssql_backend_leaves_only_the_guard_columns_measured(catalog: Catalog) -> None:
    target = MssqlTarget(
        server="localhost",
        port=1433,
        database="none",
        username="none",
        password="none",
    )
    report = run_dialect_audit(
        catalog, _corpus(), target=target, with_ast=ast_available()
    )
    assert report["backend"] == "mssql"
    assert report["reference"]["executed"] == 0
    assert report["equivalence"]["comparable"] == 0
    assert report["equivalence"]["match_rate"] is None
    assert report["equivalence"]["unmeasured"] == 22
    assert report["wrong_shapes"]["engine_unmeasured"] == 22
    assert report["wrong_shapes"]["engine_rejects"] == 0
    # Guard and parser verdicts need no connection, so they stay measured either way.
    assert report["gold_guard"]["regex_blocked"] == 0
    if ast_available():
        assert report["gold_parse"]["ok"] == 22


def test_no_target_at_all_is_unmeasured(catalog: Catalog) -> None:
    report = run_dialect_audit(catalog, _corpus(), target=None, with_ast=False)
    assert report["backend"] == UNMEASURED
    assert all(row["xlate"] == UNMEASURED for row in report["rows"])
    assert all(row["exec_mode"] == UNMEASURED for row in report["rows"])
    assert report["wrong_shapes"]["engine_unmeasured"] == 22


def test_audit_case_reports_both_guard_layers_separately(runtime: Runtime) -> None:
    case = next(row for row in _corpus() if row["id"] == "dialect_limit_sqlite")
    row = audit_dialect_case(
        runtime.catalog, case, target=runtime.settings.db_path, with_ast=ast_available()
    )
    assert row.gold_guard_regex == "valid"
    # `SELECT TOP 3 …` sent to a SQLite catalog: the dialect rule sees it without help.
    assert row.wrong_guard_regex == "blocked"
    assert "dialect_top" in row.wrong_codes
    assert row.gold_blocked is False
    assert row.unguarded_wrong_shape is False
    if ast_available():
        assert row.wrong_guard_ast == "blocked"
    off = audit_dialect_case(
        runtime.catalog, case, target=runtime.settings.db_path, with_ast=False
    )
    assert off.gold_guard_ast == UNMEASURED
    assert off.wrong_guard_ast == UNMEASURED
    assert off.wrong_guard_regex == "blocked"
    assert off.unguarded_wrong_shape is False


def test_cli_dialect_check_passes_and_prints_the_findings(tmp_path, capsys) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["dialect-check", "--home", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "方言基准  backend=sqlite cases=22 intents=8 axes=8" in out
    assert "gold 护栏  正则层拦截 0 条" in out
    assert "跨方言错误形状" in out
    assert "正则层拦 22" in out
    if ast_available():
        assert "静默偏离" in out
        # Nothing is left unguarded, so the line that used to list them is not printed.
        assert "两层护栏都没拦" not in out


def test_cli_dialect_check_json_is_the_report(tmp_path, capsys) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    capsys.readouterr()
    code = main(["dialect-check", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["cases"] == 22
    assert len(payload["rows"]) == 22
    assert payload["backend"] == "sqlite"
    assert not any(payload["invariant_failures"].values())


def test_cli_dialect_check_no_ast_skips_only_the_ast_layer(tmp_path, capsys) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    capsys.readouterr()
    code = main(["dialect-check", "--home", str(tmp_path), "--no-ast", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["gold_guard"]["ast_checked"] is False
    assert payload["wrong_shapes"]["ast_blocked"] == 0
    assert all(row["gold_guard_ast"] == UNMEASURED for row in payload["rows"])
    if payload["parser_measured"]:
        assert payload["equivalence"]["comparable"] == 22


def test_cli_dialect_check_refuses_without_a_demo_database(tmp_path, capsys) -> None:
    code = main(["dialect-check", "--home", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "演示库不存在" in err


def test_catalog_without_the_corpus_tables_is_reported_as_mismatch(
    runtime: Runtime
) -> None:
    """A guard number is only about the guard when the catalog can answer the corpus.

    Measured by accident, then pinned on purpose: pointing the audit at a catalog that
    lacks the demo tables made it print "正则层拦截 22 条" and exit 1 — a guard
    false-positive alarm caused by the catalog, not by the guard.
    """
    from dataclasses import replace

    from nl2sql_agent.catalog import Column, Table

    foreign = replace(
        runtime.catalog,
        tables={
            "widget": Table(
                name="widget",
                description="unrelated",
                synonyms=(),
                primary_key=("id",),
                columns=(
                    Column(
                        name="id", type="INTEGER", description="",
                        synonyms=(), sensitive=False, disabled=False,
                    ),
                ),
                required_filters=(),
                disabled=False,
            )
        },
        _link_index=None,
        _link_key=None,
    )
    report = run_dialect_audit(foreign, _corpus(), target=None, with_ast=False)
    assert "sales_order" in report["catalog"]["missing_tables"]
    assert report["catalog"]["corpus_tables"]
    assert set(report["catalog"]["missing_tables"]) == set(report["catalog"]["corpus_tables"])
    # The misattribution this precondition exists to prevent: against a catalog without
    # the corpus tables, every single gold now looks like a guard false-positive. Before
    # this stage the backquoted gold slipped through that alarm — the regex layer read no
    # tables at all from ``FROM `internal_audit` `` (a bypass, fixed in ``mask_sql``), so
    # the mismatch used to hide one of its own cases.
    assert report["gold_guard"]["regex_blocked"] == report["cases"]
    assert set(report["gold_guard"]["blocked_ids"]) == {row["id"] for row in report["rows"]}


def test_cli_dialect_check_refuses_a_catalog_that_lacks_the_corpus_tables(
    tmp_path, monkeypatch, capsys
) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    capsys.readouterr()
    schema = tmp_path / "other_schema.json"
    schema.write_text(
        json.dumps(
            {
                "domain": "other",
                "dialect": "sqlite",
                "as_of_date": "2026-01-01",
                "tables": [
                    {"name": "widget", "primary_key": "id", "columns": [{"name": "id", "type": "INTEGER"}]}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NL2SQL_SCHEMA_PATH", str(schema))
    code = main(["dialect-check", "--home", str(tmp_path)])
    out = capsys.readouterr()
    assert code == 2
    assert "目录与语料不匹配" in out.err
    assert "目录与语料不匹配" in out.out
