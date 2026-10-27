"""P2-1 可观测性：trace_id 贯穿、事件脱敏、指标折线。

这一层的契约不在结果里而在日志里，所以断言写在文件上：跑一轮问数，读回 JSONL，
检查"同一轮"是否真的成立、业务原文是否默认留在盘外、指标的分母是否诚实。
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from nl2sql_agent import observability
from nl2sql_agent.agent import AGENT_PROMPT_VERSION, NL2SQLAgent
from nl2sql_agent.cli import _print_result, main
from nl2sql_agent.config import Settings
from nl2sql_agent.evaluation import evaluate
from nl2sql_agent.evaluation.evaluator import render_markdown
from nl2sql_agent.llm import LLMResponse, ToolCall
from nl2sql_agent.observability import (
    EVENT_API,
    EVENT_DB,
    EVENT_TOOL,
    EVENT_TURN,
    TEXT_CAP,
    TurnObservation,
    clean_payload,
    configure_logging,
    fingerprint,
    percentile,
    read_events,
    redact,
    reset_logging,
    summarize,
)
from nl2sql_agent.runtime import create_runtime

REVENUE = "2026年6月华东地区销售订单总金额是多少？"


class ScriptedLLM:
    """A model that replays a fixed script, so a test can force one tool call."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def chat(self, messages, tools=None) -> LLMResponse:
        self.calls += 1
        if not self.responses:
            raise AssertionError("scripted model ran out of responses")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _no_handler_leak():
    yield
    reset_logging()


def _settings(tmp_path: Path, **overrides) -> Settings:
    return replace(Settings(home=tmp_path, provider="mock"), **overrides)


def _events(path: Path) -> list[dict]:
    return read_events(path)["events"]


def _of(events: list[dict], name: str) -> list[dict]:
    return [item for item in events if item.get("event") == name]


def _our_handlers() -> list[logging.Handler]:
    """只看本模块自己装上的 sink：pytest 的日志插件也会往同名 logger 上挂 handler。"""
    return [
        handler
        for handler in logging.getLogger(observability.LOGGER_NAME).handlers
        if getattr(handler, "nl2sql_channel", "")
    ]


# ----------------------------------------------------------------------- 脱敏


def test_redact_hides_credentials_in_every_shape_we_store_them() -> None:
    samples = [
        "Login timeout expired for SERVER=10.0.0.7;DATABASE=erp;UID=svc;PWD=s3cr3t",
        "api_key: sk-abcdef123456",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "DRIVER={ODBC Driver 17 for SQL Server};SERVER=10.0.0.7,1433;DATABASE=erp;UID=ro;PWD=hunter2",
    ]
    for text in samples:
        out = redact(text)
        assert "s3cr3t" not in out, text
        assert "hunter2" not in out, text
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out, text
        assert "sk-abcdef123456" not in out, text
        assert "***" in out, text
        assert redact(out) == out  # 幂等：日志行不会被二次脱敏改坏


def test_clean_payload_never_writes_result_sets_or_keys() -> None:
    cleaned = clean_payload(
        {
            "rows": [[1, "secret-value"]],
            "api_key": "sk-abcdef123456",
            "arguments": {"sql": "SELECT 1"},
            "result": {"rows": [[2]], "columns": ["a"]},
            "channels": [{"sql": "SELECT leaked", "role": "second"}],
            "guard_codes": [],
            "n": 3,
            "ok": True,
        }
    )
    dumped = json.dumps(cleaned, ensure_ascii=False)
    assert "secret-value" not in dumped
    assert "SELECT" not in dumped
    assert "sk-abcdef123456" not in dumped
    # 被扣下的整段容器不留空壳，只剩非敏感字段的那部分照常写出。
    assert cleaned == {"result": {"columns": ["a"]}, "channels": [{"role": "second"}], "n": 3, "ok": True}


def test_long_free_text_is_capped_not_dumped() -> None:
    cleaned = clean_payload({"error": "boom " * 200}, include_text=True)
    assert len(cleaned["error"]) == TEXT_CAP + 1
    assert cleaned["error"].endswith("…")


def test_business_text_is_withheld_until_include_text(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log))
    runtime.agent.run(REVENUE, trace_id=observability.new_trace_id())
    written = log.read_text(encoding="utf-8")
    assert "2026年6月华东" not in written
    assert "SELECT" not in written
    turn = _of(_events(log), EVENT_TURN)[0]
    assert "question" not in turn and "sql" not in turn and "answer" not in turn
    assert turn["question_fingerprint"] == fingerprint(REVENUE)

    reset_logging()
    log2 = tmp_path / "with_text.jsonl"
    runtime2 = create_runtime(_settings(tmp_path, log_path=log2, log_include_text=True))
    result2 = runtime2.agent.run(REVENUE, trace_id=observability.new_trace_id())
    turn2 = _of(_events(log2), EVENT_TURN)[0]
    assert turn2["question"] == REVENUE
    assert turn2["sql"] == result2.sql


# ------------------------------------------------------------------ trace 贯穿


def test_one_turn_shares_one_trace_id_from_agent_to_database(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log))
    trace_id = observability.new_trace_id()
    result = runtime.agent.run(REVENUE, trace_id=trace_id)

    events = _events(log)
    assert result.trace_id == trace_id
    # > 0 而不是 >= 0：计时必须落在能看见它的时钟上，否则 P95 永远读到 0.0。
    assert result.elapsed_ms > 0
    assert events, "配置了日志路径却没有写任何事件"
    assert {item["trace_id"] for item in events} == {trace_id}, "有事件行丢了链路"
    assert _of(events, EVENT_TURN)[0]["status"] == "ok"
    assert [item["tool"] for item in _of(events, EVENT_TOOL)] == [
        item["tool"] for item in result.tool_trace
    ]
    assert all(item["elapsed_ms"] >= 0 for item in events)
    db = _of(events, EVENT_DB)[0]
    assert db["dialect"] == "sqlite"
    assert db["row_count"] == result.row_count
    assert db["sql_fingerprint"] == fingerprint(result.sql or "")


def test_two_runs_get_two_trace_ids(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log))
    first = runtime.agent.run(REVENUE, trace_id=observability.new_trace_id())
    second = runtime.agent.run(REVENUE, trace_id=observability.new_trace_id())
    assert first.trace_id != second.trace_id
    assert len({item["trace_id"] for item in _of(_events(log), EVENT_TURN)}) == 2


def test_bind_trace_id_isolates_threads(tmp_path: Path) -> None:
    # 并发问数时一条链不能串到另一条上：contextvar 的绑定必须是每线程独立的。
    log = tmp_path / "threads.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log))
    questions = [REVENUE, "库存低于安全库存的物料有哪些？"] * 4

    def one(question: str) -> str:
        with observability.bind_trace_id(observability.new_trace_id()):
            return runtime.agent.run(question).trace_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(one, questions))

    assert len(set(ids)) == len(ids), "两条并发链共用了同一个 trace_id"
    turns = _of(_events(log), EVENT_TURN)
    assert len(turns) == len(ids)
    assert {item["trace_id"] for item in turns} == set(ids)


def test_a_library_call_without_a_boundary_stays_deterministic(tmp_path: Path) -> None:
    # id 只在请求边界铸造：库内只继承。没有边界时 trace_id 为空，结果本身一字不变。
    runtime = create_runtime(Settings(home=tmp_path, provider="mock"))
    result = runtime.agent.run(REVENUE)
    assert result.trace_id == ""
    assert result.status == "ok"


def test_turn_event_carries_the_stamps_needed_to_reproduce_it(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log))
    result = runtime.agent.run(REVENUE, trace_id=observability.new_trace_id())
    turn = _of(_events(log), EVENT_TURN)[0]
    assert turn["provider"] == "mock"
    assert turn["model"] == "RuleBasedLLM"
    assert turn["prompt_version"] == AGENT_PROMPT_VERSION
    assert turn["prompt_hash"] == result.prompt_hash
    assert len(turn["prompt_hash"]) == 16
    assert len(turn["schema_hash"]) == 16
    assert turn["few_shot"]["considered"] is True
    assert turn["repair_calls"] == result.repair_calls == 0
    assert turn["dual_channel"] == {"ran": False}
    assert turn["value_alignment"]["source"] in {"dictionary", ""}


def test_provider_usage_reaches_the_result_and_the_turn_row(tmp_path: Path) -> None:
    # 只有真实 provider 才带 usage 块，mock 恒为 0，所以这条必须用 scripted model 走 openai 档。
    log = tmp_path / "tokens.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log))
    llm = ScriptedLLM(
        [
            LLMResponse(
                content=json.dumps(
                    {"status": "ok", "answer": "62280 元", "sql": "SELECT 1", "confidence": 0.9},
                    ensure_ascii=False,
                ),
                usage={"prompt_tokens": 900, "completion_tokens": 60, "total_tokens": 960},
            ),
            # A claimed answer with no tool call is nudged once to open the catalog, so
            # the turn's usage is the sum of both calls, not of the first one.
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_clarification", "answer": "请补充统计范围"},
                    ensure_ascii=False,
                ),
                usage={"prompt_tokens": 1200, "completion_tokens": 40, "total_tokens": 1240},
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog,
        llm,
        runtime.agent.tools,
        replace(runtime.settings, provider="openai"),
        store=runtime.store,
        schema_hash=runtime.agent.schema_hash,
    )
    result = agent.run(REVENUE, trace_id=observability.new_trace_id())
    assert llm.calls == 2
    assert result.tokens == 2200
    turn = _of(_events(log), EVENT_TURN)[0]
    assert turn["tokens"] == 2200
    assert summarize(_events(log))["total_tokens"] == 2200


def test_prompt_hash_moves_with_the_question_but_not_with_the_tool_loop(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log, few_shot_enabled=False))
    one = runtime.agent.run(REVENUE, trace_id=observability.new_trace_id())
    two = runtime.agent.run("库存低于安全库存的物料有哪些？", trace_id=observability.new_trace_id())
    three = runtime.agent.run(REVENUE, trace_id=observability.new_trace_id())
    assert one.prompt_hash != two.prompt_hash
    assert one.prompt_hash == three.prompt_hash
    turns = _of(_events(log), EVENT_TURN)
    assert len({item["prompt_hash"] for item in turns}) == 2
    assert summarize(turns)["prompt_hashes"] == 2


def test_result_dict_is_joinable_to_the_log(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log))
    trace_id = observability.new_trace_id()
    payload = runtime.agent.run(REVENUE, trace_id=trace_id).to_dict()
    assert payload["trace_id"] == trace_id
    assert payload["prompt_version"] == AGENT_PROMPT_VERSION
    assert len(payload["prompt_hash"]) == 16
    assert payload["guard_codes"] == []
    assert payload["repair_calls"] == 0
    turn = _of(_events(log), EVENT_TURN)[0]
    assert turn["prompt_hash"] == payload["prompt_hash"]


def test_guard_denial_codes_reach_both_the_tool_row_and_the_turn_row(tmp_path: Path) -> None:
    # 一段被护栏挡下的 SQL 必须留下码，而不是只留在三段截断的 tool summary 里。
    log = tmp_path / "guard.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log))
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="execute_sql", arguments={"sql": "DROP TABLE demo_orders"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=json.dumps(
                    {"status": "needs_human", "answer": "写操作被护栏拒绝", "confidence": 0.1},
                    ensure_ascii=False,
                )
            ),
        ]
    )
    agent = NL2SQLAgent(
        runtime.catalog,
        llm,
        runtime.agent.tools,
        replace(runtime.settings, provider="openai"),
        store=runtime.store,
        schema_hash=runtime.agent.schema_hash,
    )
    result = agent.run("删掉订单表", trace_id="g" * 16)
    assert {"not_select", "write_keyword"} <= set(result.guard_codes)

    events = _events(log)
    tool = next(item for item in _of(events, EVENT_TOOL) if item["tool"] == "execute_sql")
    assert {"not_select", "write_keyword"} <= set(tool["guard_codes"])
    assert tool["stage"] == "guard" and tool["ok"] is True
    turn = _of(events, EVENT_TURN)[0]
    assert turn["guard_codes"] == sorted(set(result.guard_codes))
    assert turn["status"] == result.status == "needs_human"
    metrics = summarize(events)
    assert metrics["guard_codes"]["not_select"] == 2  # 工具行 + 轮次行各计一次
    assert metrics["escalation_rate"] == 1.0


# --------------------------------------------------------------- 默认没有 sink


def test_no_log_path_means_no_handler_no_file_and_no_output(tmp_path: Path, capsys) -> None:
    assert configure_logging(None) is None
    assert _our_handlers() == []
    observability.log_event(EVENT_TURN, {"question": REVENUE})
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert list(tmp_path.iterdir()) == []


def test_configuring_the_same_channel_twice_does_not_stack_handlers(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    assert configure_logging(log) == log
    assert configure_logging(log) == log
    assert configure_logging(log, level="debug") == log
    assert len(_our_handlers()) == 1
    configure_logging(tmp_path / "other.jsonl")
    assert len(_our_handlers()) == 2
    reset_logging()
    assert _our_handlers() == []


def test_reset_logging_closes_the_file_so_it_can_be_removed(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "events.jsonl"
    configure_logging(log)
    observability.log_event(EVENT_TURN, {"status": "ok"})
    reset_logging()
    assert log.is_file()
    log.unlink()  # 句柄还开着的话，Windows 上这一步直接 PermissionError
    (tmp_path / "nested").rmdir()


def test_a_plain_log_line_still_reads_as_one_json_event(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    configure_logging(log)
    with observability.bind_trace_id("trace-mixed"):
        logging.getLogger(observability.LOGGER_NAME).warning("login failed password=hunter2")
    reset_logging()
    line = json.loads(log.read_text(encoding="utf-8").strip())
    assert line["event"] == "log"
    assert line["trace_id"] == "trace-mixed"
    assert "hunter2" not in line["message"]


def test_bind_trace_id_inherits_then_restores() -> None:
    assert observability.current_trace_id() == ""
    with observability.bind_trace_id("t1") as first:
        assert first == "t1" and observability.current_trace_id() == "t1"
        with observability.bind_trace_id() as inherited:
            assert inherited == "t1"  # 空值不猜测：内层只继承
        with observability.bind_trace_id("t2") as inner:
            assert inner == "t2"
        assert observability.current_trace_id() == "t1"
    assert observability.current_trace_id() == ""


# ------------------------------------------------------------------ 读回与折线


def test_read_events_counts_malformed_lines_instead_of_raising(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text(
        '{"event": "turn", "status": "ok"}\nnot json at all\n[1,2,3]\n\n{"event":"tool"}\n',
        encoding="utf-8",
    )
    bundle = read_events(log)
    assert bundle["missing"] is False
    assert bundle["lines"] == 4
    assert bundle["malformed"] == 2
    assert len(bundle["events"]) == 2
    assert read_events(log, limit=1)["events"] == [{"event": "tool"}]
    missing = read_events(tmp_path / "absent.jsonl")
    assert missing["missing"] is True and missing["events"] == []


def test_percentile_is_nearest_rank_without_interpolation() -> None:
    values = list(range(1, 11))
    assert percentile(values, 50) == 5.0
    assert percentile(values, 95) == 10.0
    assert percentile(values, 1) == 1.0
    assert percentile([], 95) == 0.0
    assert percentile([7], 95) == 7.0


def test_ratios_with_an_empty_denominator_are_none_not_zero() -> None:
    metrics = summarize([])
    assert metrics["turns"] == 0
    for key in (
        "qps",
        "success_rate",
        "escalation_rate",
        "clarification_rate",
        "experience_hit_rate",
        "repair_rate",
        "repair_success_rate",
        "repair_call_error_rate",
        "tool_error_rate",
        "db_error_rate",
        "guard_denial_rate",
        "dual_channel_agreement_rate",
    ):
        assert metrics[key] is None, key
    assert metrics["latency_ms"] == {"avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}


def _turn(**overrides) -> dict:
    base = {
        "event": EVENT_TURN,
        "trace_id": "a" * 16,
        "ts": "2026-10-22T10:00:00.000Z",
        "status": "ok",
        "elapsed_ms": 100.0,
        "tokens": 10,
        "prompt_version": "v1",
        "prompt_hash": "h1",
        "model": "m",
        "schema_hash": "s",
        "few_shot": {"considered": True, "injected": 0},
        "guard_codes": [],
        "repair_calls": 0,
        "repair_errors": 0,
        "dual_channel": {},
    }
    base.update(overrides)
    return base


def test_summarize_folds_the_metrics_the_plan_named() -> None:
    events = [
        _turn(
            status="needs_human",
            elapsed_ms=900.0,
            few_shot={"considered": True, "injected": 2},
            repair_calls=1,
            repair_errors=1,
        ),
        _turn(trace_id="b" * 16, elapsed_ms=200.0, repair_calls=2, repair_errors=0),
        _turn(trace_id="c" * 16, status="needs_clarification", elapsed_ms=50.0),
        _turn(trace_id="d" * 16, elapsed_ms=400.0, guard_codes=["permission_column"]),
        {"event": EVENT_TOOL, "ok": False, "guard_codes": ["permission_column", "sensitive_column"]},
        {"event": EVENT_DB, "ok": False, "error_code": "timeout", "elapsed_ms": 5000.0,
         "connection": "aaaa1111", "pooled": True},
        {"event": EVENT_DB, "ok": True, "error_code": "", "elapsed_ms": 12.0,
         "connection": "bbbb2222", "pooled": False},
    ]
    metrics = summarize(events, malformed=1)
    assert (metrics["turns"], metrics["tool_calls"], metrics["db_calls"]) == (4, 1, 2)
    assert metrics["malformed_lines"] == 1
    assert metrics["trace_ids"] == 4
    assert metrics["escalation_rate"] == 0.25
    assert metrics["clarification_rate"] == 0.25
    assert metrics["experience_hit_rate"] == 0.25
    assert metrics["repair_rate"] == 0.5
    assert metrics["repair_turns"] == 2 and metrics["repair_turns_answered"] == 1
    assert metrics["repair_success_rate"] == 0.5
    assert metrics["repair_call_error_rate"] == 0.3333
    assert metrics["tool_error_rate"] == 1.0
    assert metrics["db_error_rate"] == 0.5
    assert metrics["db_error_codes"] == {"timeout": 1}
    # Pooling has to be readable out of the log, otherwise "the pool reduces connections"
    # is a config claim: two physical handles here, one call served from a pool.
    assert metrics["db_connections"] == 2
    assert metrics["db_pooled_calls"] == 1
    assert metrics["db_latency_ms"] == {"avg": 2506.0, "p50": 12.0, "p95": 5000.0}
    assert metrics["guard_denial_rate"] == 0.25
    assert metrics["guard_codes"] == {"permission_column": 2, "sensitive_column": 1}
    assert metrics["latency_ms"] == {"avg": 387.5, "p50": 200.0, "p95": 900.0, "max": 900.0}
    assert metrics["status_counts"] == {"ok": 2, "needs_clarification": 1, "needs_human": 1}
    assert metrics["total_tokens"] == 40 and metrics["avg_tokens"] == 10.0
    assert metrics["prompt_versions"] == ["v1"] and metrics["prompt_hashes"] == 1
    assert metrics["window_sec"] == 0.0 and metrics["qps"] is None
    assert "下限" in metrics["qps_note"]


def test_qps_uses_the_event_window() -> None:
    events = [
        _turn(ts="2026-10-22T10:00:00.000Z"),
        _turn(trace_id="b" * 16, ts="2026-10-22T10:00:02.000Z"),
    ]
    metrics = summarize(events)
    assert metrics["window_sec"] == 2.0
    assert metrics["qps"] == 1.0


def test_dual_channel_disagreement_is_counted_apart_from_the_agreement_rate() -> None:
    events = [
        _turn(dual_channel={"ran": True, "status": "ok", "agreement": True}),
        _turn(trace_id="e" * 16, dual_channel={"ran": True, "status": "ok", "agreement": False}),
        _turn(trace_id="f" * 16, dual_channel={"ran": False}),
    ]
    metrics = summarize(events)
    assert metrics["dual_channel_votes"] == 2
    assert metrics["dual_channel_agreement_rate"] == 0.5
    assert metrics["dual_channel_disagreements"] == 1


def test_turn_observation_dedupes_codes_and_counts_repairs() -> None:
    observed = TurnObservation(prompt_version="v1")
    observed.add_guard_codes(["permission_column", "", "sensitive_column", "permission_column"])
    observed.note_repair(True)
    observed.note_repair(False)
    assert observed.guard_codes == ["permission_column", "sensitive_column"]
    assert (observed.repair_calls, observed.repair_errors) == (2, 1)


# --------------------------------------------------------------- CLI 与 Web 边界


def test_metrics_command_reports_a_precondition_instead_of_fake_zeros(tmp_path: Path, capsys) -> None:
    assert main(["metrics", "--home", str(tmp_path)]) == 2
    assert "NL2SQL_LOG_PATH" in capsys.readouterr().err
    assert main(["metrics", "--home", str(tmp_path), "--log-path", str(tmp_path / "x.jsonl")]) == 2
    assert "不存在" in capsys.readouterr().err


def test_metrics_command_reads_back_a_real_run(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("NL2SQL_LOG_PATH", str(tmp_path / "cli.jsonl"))
    assert main(["ask", "--home", str(tmp_path), "--provider", "mock", REVENUE]) == 0
    capsys.readouterr()
    assert main(["metrics", "--home", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["lines"] > 1
    assert report["metrics"]["turns"] == 1
    assert report["metrics"]["status_counts"] == {"ok": 1}
    assert report["metrics"]["latency_ms"]["p95"] > 0
    assert 0.0 <= report["metrics"]["experience_hit_rate"] <= 1.0

    capsys.readouterr()
    assert main(["metrics", "--home", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "观测汇总" in text and "人工升级" in text and "repair 成功率" in text


def test_verbose_ask_prints_the_trace_id(capsys) -> None:
    result = FakeResult()
    _print_result(result, verbose=True)
    assert "trace: trace-abc" in capsys.readouterr().out


class FakeResult:
    status = "ok"
    confidence = 0.9
    steps = 1
    elapsed_ms = 1.0
    answer = "答"
    sql = "SELECT 1"
    columns: list[str] = []
    rows: list[list] = []
    row_count = 1
    error = ""
    channels: list[dict] = []
    tool_trace: list[dict] = [{"step": 1, "tool": "execute_sql", "ok": True, "elapsed_ms": 1.0, "summary": ""}]
    trace_id = "trace-abc"


def test_web_health_and_metrics_report_the_switch_state(tmp_path: Path) -> None:
    pytest.importorskip("fastapi", reason="Web 控制台需要 pip install -e '.[web]'")
    pytest.importorskip("httpx", reason="TestClient 需要 httpx")
    from fastapi.testclient import TestClient

    from nl2sql_agent.web.app import create_app

    with TestClient(create_app(create_runtime(_settings(tmp_path)))) as client:
        health = client.get("/api/health").json()
        assert health["observability"] == {
            "log_enabled": False,
            "log_level": "info",
            "include_text": False,
        }
        payload = client.get("/api/metrics").json()
        assert payload["configured"] is False and payload["metrics"] is None

    log = tmp_path / "web.jsonl"
    with TestClient(create_app(create_runtime(_settings(tmp_path, log_path=log)))) as client:
        assert client.get("/api/health").json()["observability"]["log_enabled"] is True
        session = client.post("/api/sessions").json()["session_id"]
        answer = client.post(
            f"/api/sessions/{session}/ask",
            json={"question": REVENUE},
            headers={"x-trace-id": "trace-from-caller"},
        ).json()
        assert answer["result"]["trace_id"] == "trace-from-caller"
        metrics = client.get("/api/metrics").json()
    assert metrics["configured"] is True and metrics["metrics"]["turns"] == 1
    events = _events(log)
    api_rows = _of(events, EVENT_API)
    turn_rows = _of(events, EVENT_TURN)
    assert api_rows[0]["trace_id"] == turn_rows[0]["trace_id"] == "trace-from-caller"
    assert api_rows[0]["session_id"] == session
    assert api_rows[0]["turns"] == 1


def test_web_mints_a_trace_id_when_the_header_is_unusable(tmp_path: Path) -> None:
    pytest.importorskip("fastapi", reason="Web 控制台需要 pip install -e '.[web]'")
    pytest.importorskip("httpx", reason="TestClient 需要 httpx")
    from fastapi.testclient import TestClient

    from nl2sql_agent.web.app import create_app

    log = tmp_path / "web.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log))
    with TestClient(create_app(runtime)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        first = client.post(
            f"/api/sessions/{session}/ask", json={"question": REVENUE}, headers={"x-trace-id": "x" * 200}
        ).json()["result"]["trace_id"]
        second = client.post(
            f"/api/sessions/{session}/ask", json={"question": REVENUE}
        ).json()["result"]["trace_id"]
    assert len(first) == observability.TRACE_ID_CHARS and first != "x" * 200
    assert len(second) == observability.TRACE_ID_CHARS and second != first


def test_evaluation_writes_one_trace_id_per_case_into_the_log(tmp_path: Path) -> None:
    log = tmp_path / "eval.jsonl"
    runtime = create_runtime(_settings(tmp_path, log_path=log))
    cases = [
        {"id": "obs_ok", "question": REVENUE, "category": "aggregate", "gold_sql": "SELECT 1"},
        {
            "id": "obs_refusal",
            "question": "今天杭州天气怎么样？",
            "category": "scope",
            "expect_refusal": True,
        },
    ]
    report = evaluate(
        runtime.agent,
        cases,
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider="mock",
        report_dir=None,
    )
    trace_ids = {case.trace_id for case in report.cases}
    assert len(trace_ids) == 2 and "" not in trace_ids
    events = _events(log)
    assert {item["trace_id"] for item in _of(events, EVENT_TURN)} == trace_ids
    assert report.metrics["p95_latency_ms"] >= report.metrics["p50_latency_ms"]
    assert "Latency P50/P95" in render_markdown(report, 12.0)
