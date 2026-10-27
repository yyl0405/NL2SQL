"""P2-5 成本与 token：单价算术、配置校验、事件汇总与 CLI。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nl2sql_agent.cli import main
from nl2sql_agent.config import Settings
from nl2sql_agent.costs import cost_rollup_lines, cost_summary, normalize_prices
from nl2sql_agent.llm import usage_breakdown
from nl2sql_agent.observability import summarize


def _events() -> list[dict]:
    return [
        {
            "event": "turn",
            "category": "sales",
            "principal": "alice",
            "tokens": 1000,
            "prompt_tokens": 700,
            "completion_tokens": 200,
            "cached_tokens": 100,
        },
        {
            "event": "turn",
            "category": "sales",
            "principal": "bob",
            "tokens": 500,
            "prompt_tokens": 300,
            "completion_tokens": 100,
            "cached_tokens": 50,
            "cached": True,
            "cache_saved_tokens": 400,
        },
        {"event": "tool", "tokens": 9999},
    ]


PRICES = {"input_per_1m": 2.0, "output_per_1m": 8.0, "cached_input_per_1m": 0.5}


def test_cost_summary_splits_tokens_and_prices_each_bucket() -> None:
    report = cost_summary(_events(), prices=PRICES)
    assert report["turns"] == 2
    assert report["tokens"] == {
        "total": 1500,
        "prompt": 1000,
        "completion": 300,
        "cached": 150,
        "unclassified": 200,
    }
    # Prompt 1000: 850 billable at 2 + 150 cached at 0.5; completion 300 at 8;
    # unclassified 200 at the input rate as an explicit lower bound.
    expected = 850 / 1_000_000 * 2 + 150 / 1_000_000 * 0.5 + 300 / 1_000_000 * 8 + 200 / 1_000_000 * 2
    assert report["estimated_cost"] == pytest.approx(expected)
    assert report["by_category"]["sales"]["turns"] == 2
    assert report["by_principal"]["alice"]["estimated_cost"] == pytest.approx(0.00305)
    assert report["by_principal"]["bob"]["estimated_cost"] == pytest.approx(0.001525)
    assert report["cache"] == {"hits": 1, "hit_rate": 0.5, "saved_tokens": 400}
    assert report["pricing"] == {"configured": True, **PRICES, "currency": "USD"}
    lines = list(cost_rollup_lines(report))
    assert any("估算费用" in line for line in lines)
    assert not any("费用未配置" in line for line in lines)


def test_cached_tokens_fall_back_to_the_input_price() -> None:
    event = {
        "event": "turn",
        "tokens": 100,
        "prompt_tokens": 100,
        "completion_tokens": 0,
        "cached_tokens": 50,
    }
    priced = cost_summary([event], prices={"input_per_1m": 2.0, "output_per_1m": 8.0})
    discounted = cost_summary([event], prices={**PRICES})
    assert priced["estimated_cost"] == pytest.approx(100 / 1_000_000 * 2)
    assert discounted["estimated_cost"] == pytest.approx((50 * 2 + 50 * 0.5) / 1_000_000)


def test_no_prices_means_no_confident_zero() -> None:
    report = cost_summary(_events())
    assert report["estimated_cost"] is None
    assert report["by_category"]["sales"]["estimated_cost"] is None
    assert report["pricing"]["configured"] is False
    assert "未配置" in report["pricing"]["note"]
    lines = list(cost_rollup_lines(report))
    assert any("费用未配置" in line for line in lines)


def test_half_a_price_table_raises() -> None:
    with pytest.raises(ValueError):
        normalize_prices({"input_per_1m": 2.0})
    with pytest.raises(ValueError):
        normalize_prices({"output_per_1m": 8.0})
    with pytest.raises(ValueError):
        normalize_prices({**PRICES, "input_per_1m": -1})


def test_empty_log_reports_zero_turns_without_a_cost() -> None:
    report = cost_summary([])
    assert report["turns"] == 0
    assert report["tokens"]["total"] == 0
    assert report["cache"]["hit_rate"] is None
    assert report["estimated_cost"] is None
    lines = list(cost_rollup_lines(report))
    assert any("没有 turn 事件" in line for line in lines)


def test_usage_breakdown_keeps_cached_within_prompt() -> None:
    assert usage_breakdown(
        {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 80, "total_tokens": 120}
    ) == {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 80, "total_tokens": 120}
    assert usage_breakdown({"prompt_tokens": 10, "cached_tokens": 99})["cached_tokens"] == 10
    assert usage_breakdown(None) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
    }


def test_settings_reject_half_a_price_table_and_negative_prices() -> None:
    with pytest.raises(ValueError):
        Settings(price_input_per_1m=2.0)
    with pytest.raises(ValueError):
        Settings(price_output_per_1m=8.0)
    with pytest.raises(ValueError):
        Settings(price_input_per_1m=2.0, price_output_per_1m=8.0, price_cached_input_per_1m=-0.1)
    table = Settings(
        price_input_per_1m=2.0, price_output_per_1m=8.0, price_cached_input_per_1m=0.5
    ).price_table
    assert table == {
        "input_per_1m": 2.0,
        "output_per_1m": 8.0,
        "cached_input_per_1m": 0.5,
        "currency": "USD",
    }
    assert Settings().price_table is None


def test_price_and_cache_knobs_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NL2SQL_RESULT_CACHE_TTL_SEC", "60")
    monkeypatch.setenv("NL2SQL_RESULT_CACHE_MAX", "4")
    monkeypatch.setenv("NL2SQL_PRICE_INPUT_PER_1M", "2")
    monkeypatch.setenv("NL2SQL_PRICE_OUTPUT_PER_1M", "8")
    monkeypatch.setenv("NL2SQL_PRICE_CACHED_INPUT_PER_1M", "0.5")
    monkeypatch.setenv("NL2SQL_PRICE_CURRENCY", "cny")
    loaded = Settings.load(home=tmp_path)
    assert loaded.result_cache_ttl_sec == 60.0
    assert loaded.result_cache_max_entries == 4
    assert loaded.price_currency == "CNY"
    assert loaded.price_table == {
        "input_per_1m": 2.0,
        "output_per_1m": 8.0,
        "cached_input_per_1m": 0.5,
        "currency": "CNY",
    }


def test_result_cache_knobs_are_clamped() -> None:
    settings = Settings(result_cache_ttl_sec=-5, result_cache_max_entries=0)
    assert settings.result_cache_ttl_sec == 0.0
    assert settings.result_cache_max_entries == 1


def test_summarize_reports_token_split_and_cache() -> None:
    metrics = summarize(_events())
    assert metrics["token_usage"] == {
        "prompt_tokens": 1000,
        "completion_tokens": 300,
        "cached_tokens": 150,
        "unclassified_tokens": 200,
    }
    assert metrics["cache"] == {"hits": 1, "hit_rate": 0.5, "saved_tokens": 400}
    assert metrics["total_tokens"] == 1500


def test_cli_cost_prints_json_and_human_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps(_events()[0], ensure_ascii=False) + "\n", encoding="utf-8")
    code = main(
        [
            "cost",
            "--log-path",
            str(log),
            "--price-input",
            "2",
            "--price-output",
            "8",
            "--price-cached-input",
            "0.5",
            "--json",
            "--home",
            str(tmp_path),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["turns"] == 1
    assert payload["report"]["estimated_cost"] == pytest.approx(0.00305)

    code = main(
        [
            "cost",
            "--log-path",
            str(log),
            "--price-input",
            "2",
            "--price-output",
            "8",
            "--price-cached-input",
            "0.5",
            "--home",
            str(tmp_path),
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "估算费用  0.003050" in output
    assert "费用未配置" not in output

    code = main(["cost", "--log-path", str(log), "--home", str(tmp_path)])
    assert code == 0
    output = capsys.readouterr().out
    assert "费用未配置" in output
    assert "按业务域" in output


def test_cli_cost_refuses_half_a_price_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps(_events()[0]) + "\n", encoding="utf-8")
    code = main(["cost", "--log-path", str(log), "--price-input", "2", "--home", str(tmp_path)])
    assert code == 2
    assert "必须同时给" in capsys.readouterr().err


def test_cli_cost_reports_a_missing_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["cost", "--log-path", str(tmp_path / "absent.jsonl"), "--home", str(tmp_path)])
    assert code == 2
    assert "事件日志不存在" in capsys.readouterr().err
