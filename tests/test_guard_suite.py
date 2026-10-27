from __future__ import annotations

import pytest

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.config import Settings
from nl2sql_agent.evaluation import load_cases
from nl2sql_agent.guard_suite import (
    ADVERSARIAL_CASES,
    FANOUT_CASES,
    PERMISSION_CASES,
    _catalog_for,
    load_case_file,
    load_principals,
    run_adversarial,
    run_all,
)


@pytest.fixture(scope="module")
def suite_report() -> dict:
    catalog = Catalog.from_path(Settings().schema_path)
    gold = [
        {"id": case.id, "gold_sql": case.gold_sql}
        for case in load_cases(Settings().eval_cases_path)
    ]
    return run_all(
        catalog,
        adversarial=load_case_file(ADVERSARIAL_CASES),
        permission=load_case_file(PERMISSION_CASES),
        fanout=load_case_file(FANOUT_CASES),
        gold=gold,
        principals=load_principals(Settings().principals_path),
    )


@pytest.fixture(scope="module")
def no_ast_report() -> dict:
    """The same bench with the AST layer force-disabled, as on a machine without sqlglot."""
    catalog = Catalog.from_path(Settings().schema_path)
    gold = [
        {"id": case.id, "gold_sql": case.gold_sql}
        for case in load_cases(Settings().eval_cases_path)
    ]
    return run_all(
        catalog,
        adversarial=load_case_file(ADVERSARIAL_CASES),
        permission=load_case_file(PERMISSION_CASES),
        fanout=load_case_file(FANOUT_CASES),
        gold=gold,
        principals=load_principals(Settings().principals_path),
        with_ast=False,
    )


def test_bench_has_no_failures(suite_report: dict) -> None:
    assert suite_report["failures"] == {key: 0 for key in suite_report["failures"]}


def test_suite_sizes_are_pinned(suite_report: dict) -> None:
    adversarial = suite_report["adversarial"]
    assert (
        adversarial["total"],
        adversarial["blocking"],
        adversarial["pass_through"],
        adversarial["tagged_ast"],
    ) == (41, 31, 10, 6)
    assert suite_report["permission"]["total"] == 23
    assert suite_report["fanout"]["total"] == 11


def test_gold_corpus_is_clean(suite_report: dict) -> None:
    gold = suite_report["gold_sql"]["regex"]
    assert gold["total"] == 113
    assert gold["skipped"] == 10
    assert gold["errors"] == 0
    assert gold["warnings"] == 0


def test_regex_layer_has_no_holes_in_its_own_contract(suite_report: dict) -> None:
    adversarial = suite_report["adversarial"]
    assert adversarial["regex_missed"] == []
    assert adversarial["false_positive_ast"] == []
    assert adversarial["regex_blocked"] == adversarial["regex_blocking_scope"] == 25


def test_permission_suite_blocks_and_never_leaks_through_the_view(suite_report: dict) -> None:
    permission = suite_report["permission"]
    assert len(permission["principals"]) == 5
    # `perm_rep_ast_combo` (`FROM[internal_audit]`) used to be blocked only by the AST
    # layer, which made this count depend on whether sqlglot was installed. The dialect
    # quoting rule catches the shape in the regex layer, so 17 is now the number in both
    # install shapes — and that independence is what the assertion below pins.
    assert permission["blocked"] == 17
    assert permission["view_leaks"] == []


def test_unmeasured_ast_is_flagged_rather_than_reported_as_clean(no_ast_report: dict) -> None:
    assert no_ast_report["catalog"]["ast_measured"] is False
    # 17 with and without the parser: `perm_rep_ast_combo` used to be visible only to
    # sqlglot (`FROM[internal_audit]`), and the dialect quoting rule closed that gap.
    assert no_ast_report["permission"]["blocked"] == 17
    assert no_ast_report["permission"]["view_leaks"] == []
    assert no_ast_report["adversarial"]["ast_blocked"] == 0
    assert no_ast_report["adversarial"]["ast_only_blocked"] == []
    # Without the flag an auditor reading `gold_sql.ast` would see a 0/0 pass.
    assert no_ast_report["gold_sql"]["ast"]["measured"] is False
    assert no_ast_report["gold_sql"]["ast"]["errors"] == 0
    assert no_ast_report["gold_sql"]["regex"]["measured"] is True
    assert no_ast_report["failures"] == {key: 0 for key in no_ast_report["failures"]}


def test_ast_contribution_is_measured_not_assumed(suite_report: dict) -> None:
    if not suite_report["catalog"]["ast_measured"]:
        pytest.skip("sqlglot 未安装：AST 层不参与测量")
    adversarial = suite_report["adversarial"]
    assert adversarial["ast_blocked"] == adversarial["blocking"]
    # Three shapes are still parser-only (function escalation, a write without INSERT,
    # xp_cmdshell). `adv_nospace_from` left this list when the dialect quoting rule made
    # `[internal_audit]` visible to the regex layer, and it moved into the misdiagnosed
    # group below: blocked in both layers, but only the AST names `metadata_access` /
    # `disabled_table` for the right reason.
    assert adversarial["ast_only_blocked"] == [
        "adv_randomblob",
        "adv_select_into",
        "adv_xp_cmdshell",
    ]
    assert adversarial["regex_also_blocked_ast_tagged"] == [
        "adv_information_schema",
        "adv_nospace_from",
        "adv_sqlite_master",
    ]
    assert suite_report["gold_sql"]["ast"]["errors"] == 0
    assert suite_report["gold_sql"]["ast"]["warnings"] == 0


def test_regex_only_run_executes_but_does_not_assert_ast_cases(catalog: Catalog) -> None:
    outcomes = run_adversarial(catalog, load_case_file(ADVERSARIAL_CASES), with_ast=False)
    by_id = {row.id: row for row in outcomes}
    assert len(outcomes) == 41
    assert by_id["adv_insert"].asserted is True
    assert by_id["adv_nospace_from"].asserted is False
    assert by_id["adv_nospace_from"].passed is True
    # Executed, and since the quoting rule it is also blocked by the regex layer — but
    # the run still does not score it, because its declared expectation belongs to the
    # AST contract (`disabled_table`), not to `dialect_bracket_quote`.
    assert by_id["adv_nospace_from"].actual_valid is False
    assert "dialect_bracket_quote" in by_id["adv_nospace_from"].codes


def test_ast_run_asserts_every_case(catalog: Catalog) -> None:
    pytest.importorskip("sqlglot")
    outcomes = run_adversarial(catalog, load_case_file(ADVERSARIAL_CASES), with_ast=True)
    assert all(row.asserted for row in outcomes)
    assert all(row.passed for row in outcomes)


def test_dialect_switch_does_not_mutate_the_catalog(catalog: Catalog) -> None:
    mssql = _catalog_for(catalog, "mssql")
    assert mssql.dialect == "mssql"
    assert catalog.dialect == "sqlite"
    assert _catalog_for(mssql, "mssql") is mssql


def test_missing_case_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_case_file(tmp_path / "nope.jsonl")


def test_unknown_principal_in_a_case_is_reported_not_silently_skipped(catalog: Catalog) -> None:
    from nl2sql_agent.guard_suite import run_permission

    outcomes, leaks = run_permission(
        catalog,
        [{"id": "x", "principal": "ghost", "sql": "SELECT 1", "expect_valid": False}],
        {},
    )
    assert outcomes[0].passed is False
    assert "未定义的 principal" in outcomes[0].detail
    assert leaks == []
