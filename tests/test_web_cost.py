"""Web 成本看板：健康检查开关、无日志时的显式说明、事件汇总接口。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="Web 控制台需要 pip install -e '.[web]'")
pytest.importorskip("httpx", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

from nl2sql_agent.config import Settings  # noqa: E402
from nl2sql_agent.runtime import create_runtime  # noqa: E402
from nl2sql_agent.web.app import create_app  # noqa: E402


def test_health_reports_the_cache_and_pricing_switches(settings: Settings) -> None:
    with TestClient(create_app(create_runtime(settings))) as client:
        payload = client.get("/api/health").json()
    assert payload["result_cache"] == {"enabled": False, "ttl_sec": 0.0}
    assert payload["pricing"] == {"configured": False, "currency": "USD"}


def test_cost_endpoint_is_explicit_when_no_log_is_configured(settings: Settings) -> None:
    with TestClient(create_app(create_runtime(settings))) as client:
        payload = client.get("/api/cost").json()
    assert payload["enabled"] is False
    assert "NL2SQL_LOG_PATH" in payload["reason"]
    assert payload["report"] is None


def test_cost_endpoint_rolls_up_the_event_log(settings: Settings, tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text(
        json.dumps(
            {
                "event": "turn",
                "category": "sales",
                "principal": "rep_east",
                "tokens": 1000,
                "prompt_tokens": 700,
                "completion_tokens": 200,
                "cached_tokens": 100,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    priced = replace(settings, log_path=log, price_input_per_1m=2.0, price_output_per_1m=8.0)
    with TestClient(create_app(create_runtime(priced))) as client:
        health = client.get("/api/health").json()
        payload = client.get("/api/cost").json()
    assert health["result_cache"] == {"enabled": False, "ttl_sec": 0.0}
    assert health["pricing"] == {"configured": True, "currency": "USD"}
    assert payload["enabled"] is True
    report = payload["report"]
    assert report["turns"] == 1
    assert report["pricing"]["configured"] is True
    # No cached-input price configured, so cached tokens fall back to the input rate:
    # 600 billable prompt + 100 cached at 2 + 200 completion at 8 + 100 unclassified at 2.
    assert report["estimated_cost"] == pytest.approx(0.0032)
    assert report["by_principal"]["rep_east"]["turns"] == 1
