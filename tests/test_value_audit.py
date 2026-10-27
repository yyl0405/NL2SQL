"""Tests for the value-alignment audit (``nl2sql-agent alias-check``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.cli import main
from nl2sql_agent.config import Settings
from nl2sql_agent.demo import ensure_demo_db
from nl2sql_agent.guard_suite import load_case_file
from nl2sql_agent.value_align import ValueAligner, ValueDictionary
from nl2sql_agent.value_audit import (
    ALIAS_CASES,
    align_rows,
    corpus_findings,
    dictionary_catalog_findings,
    dictionary_data_findings,
    dim_enum_findings,
    expectation_of,
    frozen_footprint,
    run_alias_audit,
)

PACKAGED = Path(__file__).resolve().parents[1] / "src" / "nl2sql_agent" / "data" / (
    "value_dictionary.json"
)


@pytest.fixture()
def dictionary() -> ValueDictionary:
    return ValueDictionary.load(PACKAGED)


@pytest.fixture()
def demo_settings() -> Settings:
    settings = Settings(provider="mock")
    settings.ensure_dirs()
    ensure_demo_db(settings.db_path)
    return settings


def mutate(match: str, **overrides: Any) -> ValueDictionary:
    """The packaged dictionary with one entry changed, keeping every other entry intact."""

    raw = json.loads(PACKAGED.read_text(encoding="utf-8"))
    index = next(i for i, item in enumerate(raw["entries"]) if item["canonical"] == match)
    raw["entries"][index].update(overrides)
    return ValueDictionary.from_dict(raw, source="mutated")


def test_the_packaged_dictionary_fits_the_packaged_catalog_and_data(
    catalog: Catalog, dictionary: ValueDictionary, demo_settings: Settings
) -> None:
    cat = dictionary_catalog_findings(catalog, dictionary)
    data = dictionary_data_findings(dictionary, demo_settings.target, mssql=False)

    assert cat == {"columns": 7, "missing": [], "uncheckable": []}
    assert data["stale"] == [] and data["errors"] == [] and data["skipped"] == []
    assert data["columns"] == 7


def test_a_column_the_catalog_does_not_have_is_reported_not_guessed(
    catalog: Catalog, dictionary: ValueDictionary
) -> None:
    mutated = ValueDictionary.from_dict(
        {
            "entries": [
                {"kind": "customer", "column": "sales_order.customer_name", "canonical": "甲"},
                {"kind": "customer", "column": "no_such_table.customer_name", "canonical": "乙"},
                {"kind": "customer", "column": "sales_order.没有这列", "canonical": "丙"},
                {"kind": "customer", "column": "sales_order.[带引号的列]", "canonical": "丁"},
            ]
        },
        source="test",
    )
    findings = dictionary_catalog_findings(catalog, mutated)

    assert "sales_order.customer_name" not in findings["missing"]
    assert set(findings["missing"]) == {
        "no_such_table.customer_name",
        "sales_order.没有这列",
    }
    assert findings["uncheckable"] == ["sales_order.[带引号的列]"]
    # the packaged dictionary is all plain identifiers, so nothing escapes the check
    assert dictionary_catalog_findings(catalog, dictionary)["uncheckable"] == []


def test_a_value_that_left_the_data_is_stale(demo_settings: Settings) -> None:
    stale = mutate("杭州智造科技有限公司", canonical="杭州智造科技有限公司改名")
    data = dictionary_data_findings(stale, demo_settings.target, mssql=False)

    assert data["stale"] == ["sales_order.customer_name=杭州智造科技有限公司改名"]


def test_status_aliases_are_cross_checked_against_the_dimension_table(
    catalog: Catalog, dictionary: ValueDictionary, demo_settings: Settings
) -> None:
    dim = dim_enum_findings(dictionary, demo_settings.target, catalog, mssql=False)

    assert dim["present"] is True
    assert dim["declared"] == 3 and dim["labels"] == 3
    assert dim["missing_code"] == [] and dim["missing_label"] == []
    assert len(dim["unchecked"]) == 5, "工单/采购状态在演示库里没有字典"

    unlabelled = mutate("pending", aliases=["待处理"])
    mutated = dim_enum_findings(unlabelled, demo_settings.target, catalog, mssql=False)
    assert mutated["missing_label"] == ["sales_order.status=pending(order_status:待发货)"]


def test_expectation_of_reads_the_corpus_tags() -> None:
    assert expectation_of({"tags": ["alias"]}) == "alias"
    assert expectation_of({"tags": ["alias", "stated"]}) == "stated"
    assert expectation_of({"tags": ["alias", "status_enum"]}) == "status_enum"
    assert expectation_of({"tags": ["alias", "collision"], "expect_clarification": True}) == (
        "collision"
    )
    assert expectation_of({"tags": ["alias", "no_dictionary_mention"]}) == "quiet"


def test_every_alias_case_aligns_to_the_value_it_claims_to_use() -> None:
    cases = load_case_file(ALIAS_CASES)
    rows = align_rows(cases, ValueAligner(ValueDictionary.load(PACKAGED)))

    assert corpus_findings(rows, cases) == {}
    assert {row.align for row in rows} == {"injected", "stated", "ambiguous", "unchanged"}


def test_an_alias_case_the_dictionary_cannot_align_is_a_finding() -> None:
    case = {"id": "x", "question": "这个月一共开了多少张工单？", "tags": ["alias"]}
    rows = align_rows([case], ValueAligner(ValueDictionary.load(PACKAGED)))

    assert rows[0].align == "unchanged"
    assert corpus_findings(rows, [case])["not_aligned"] == ["x"]


def test_an_alias_that_aligns_to_the_wrong_value_is_a_finding() -> None:
    case = {
        "id": "x",
        "question": "客户杭智的订单明细",
        "tags": ["alias"],
        "expect_canonical": "北京华创机器有限公司",
    }
    rows = align_rows([case], ValueAligner(ValueDictionary.load(PACKAGED)))

    assert corpus_findings(rows, [case])["wrong_value"] == ["x"]


def test_a_stated_control_that_gets_injected_is_a_finding() -> None:
    case = {
        "id": "c",
        "question": "客户杭智的订单明细",
        "tags": ["alias", "stated"],
        "expect_canonical": "杭州智造科技有限公司",
    }
    rows = align_rows([case], ValueAligner(ValueDictionary.load(PACKAGED)))

    assert corpus_findings(rows, [case])["changed_stated"] == ["c"]


def test_the_dictionary_never_turns_a_frozen_question_into_a_clarification(
    demo_settings: Settings,
) -> None:
    aligner = ValueAligner(ValueDictionary.load(PACKAGED))
    frozen = load_case_file(demo_settings.eval_cases_path)
    footprint = frozen_footprint(frozen, aligner)

    assert footprint["cases"] == len(frozen)
    assert footprint["ambiguous"] == []
    # the dictionary touches status wording, not every question
    assert 0 < footprint["changed"] < footprint["cases"]


def test_the_full_audit_passes_its_own_invariants_without_calling_a_model(
    catalog: Catalog, demo_settings: Settings
) -> None:
    cases = load_case_file(ALIAS_CASES)
    frozen = load_case_file(demo_settings.eval_cases_path)
    report = run_alias_audit(
        demo_settings, catalog, cases, frozen, dictionary_path=PACKAGED, with_ab=False
    )

    assert report["findings"] == {}
    assert report["ab"] == {"measured": False}
    assert report["expectations"]["collision"] == 2
    assert report["align"]["stated"] == 3 and report["align"]["unchanged"] == 1


def test_a_stale_dictionary_value_fails_the_audit(
    catalog: Catalog, demo_settings: Settings, tmp_path: Path
) -> None:
    raw = json.loads(PACKAGED.read_text(encoding="utf-8"))
    for entry in raw["entries"]:
        if entry["canonical"] == "伺服电机":
            entry["canonical"] = "伺服电机（已改名）"
    stale_path = tmp_path / "dictionary.json"
    stale_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    report = run_alias_audit(
        demo_settings,
        catalog,
        load_case_file(ALIAS_CASES),
        [],
        dictionary_path=stale_path,
        with_ab=False,
    )

    assert report["findings"]["stale_value"] == ["material.material_name=伺服电机（已改名）"]


def test_cli_alias_check_passes_the_invariants_without_the_ab(tmp_path, capsys) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    capsys.readouterr()

    assert main(["alias-check", "--home", str(tmp_path), "--no-ab"]) == 0
    out = capsys.readouterr().out
    assert "不变量  全部通过" in out
    assert "配对 A/B  本次未测量" in out


def test_cli_alias_check_measures_the_paired_ab(tmp_path, capsys) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    # The gold comes from the frozen set verbatim: the A/B only votes when both arms
    # can be compared against the same result set, so a hand-written projection here
    # would measure a mismatch between the test and the mock, not the alignment.
    frozen = next(c for c in load_case_file(Settings().eval_cases_path) if c["id"] == "customer_orders")
    case = {
        "id": "cli_alias",
        "question": "客户杭智的订单明细",
        "gold_sql": frozen["gold_sql"],
        "expected_tables": frozen["expected_tables"],
        "order_sensitive": frozen["order_sensitive"],
        "category": frozen["category"],
        "difficulty": frozen["difficulty"],
        "tags": ["alias"],
        "alias_mention": "杭智",
        "expect_canonical": "杭州智造科技有限公司",
    }
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["alias-check", "--home", str(tmp_path), "--cases", str(cases)]) == 0
    out = capsys.readouterr().out
    assert "关闭对齐通过 0，开启对齐通过 1" in out
    assert "修好 1、改坏 0" in out


def test_cli_alias_check_refuses_without_a_demo_database(tmp_path, capsys) -> None:
    assert main(["alias-check", "--home", str(tmp_path)]) == 2
    assert "演示库不存在" in capsys.readouterr().err


def test_cli_alias_check_refuses_a_dictionary_that_does_not_fit_the_catalog(
    tmp_path, capsys
) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    dictionary = tmp_path / "dictionary.json"
    dictionary.write_text(
        json.dumps(
            {"entries": [{"kind": "customer", "column": "no_such_table.name", "canonical": "甲"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    capsys.readouterr()

    assert main(
        ["alias-check", "--home", str(tmp_path), "--dictionary", str(dictionary), "--no-ab"]
    ) == 2
    assert "目录与词典不匹配" in capsys.readouterr().err


def test_cli_alias_check_fails_on_a_stale_value(tmp_path, capsys) -> None:
    assert main(["init", "--home", str(tmp_path)]) == 0
    raw = json.loads(PACKAGED.read_text(encoding="utf-8"))
    for entry in raw["entries"]:
        if entry["canonical"] == "宁波轴承集团":
            entry["canonical"] = "宁波轴承集团有限公司"
    dictionary = tmp_path / "dictionary.json"
    dictionary.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()

    assert main(
        ["alias-check", "--home", str(tmp_path), "--dictionary", str(dictionary), "--no-ab"]
    ) == 1
    assert "stale_value" in capsys.readouterr().err
