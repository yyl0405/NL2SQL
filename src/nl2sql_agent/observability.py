"""trace_id 贯穿、结构化事件与脱敏：让"这一轮到底发生了什么"可以被回答。

问数链路的失败是分层的（改写 → 值对齐 → 澄清 → 生成 → 护栏 → 执行 → 复核），而
``AgentResult`` 只保留最后一次结论：一次 ``execute_sql`` 超时、一轮护栏拒绝、一次
经验注入，都在返回后被丢弃，于是"P95 是多少""多少次是被护栏挡住的"这类问题没有
答案可查。这一层把这些过程量写成一行的 JSON 事件，并用同一个 trace_id 把
API → Agent → 工具 → 数据库串起来。

三条约束决定了实现形状：

* **默认不落盘**：没有 ``NL2SQL_LOG_PATH`` 时不建 handler、不写文件、不产生任何输出。
  一个库不该在被 import 的时候就开始写磁盘。
* **先脱敏再出门**：凭证与连接串在写入前替换，问题原文与 SQL 全文默认不记。
* **事件不是判据**：``metrics`` 汇总的是观测值，不进任何退出码。
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import math
import re
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER_NAME = "nl2sql"
EVENT_TURN = "turn"
EVENT_TOOL = "tool"
EVENT_DB = "db"
#: HTTP 层自己的一条：它知道 session_id 和端点，Agent 不知道。
EVENT_API = "api"

#: 与 ``session_id`` 同长度，日志里可读、足够定位一轮对话。
TRACE_ID_CHARS = 16
#: 事件行里自由文本的上限：够看出问题在哪，不够把一份报告抄进日志。
TEXT_CAP = 300
#: 只有这些字段的取值会被整段扣下（``NL2SQL_LOG_INCLUDE_TEXT=true`` 才写）：它们是业务内容。
TEXT_FIELDS = ("question", "answer", "sql")
#: 永远不写的字段：结果集与凭证。观测要的是"第几行错了"，不是"错的那行内容"。
DROP_FIELDS = frozenset(
    {"rows", "api_key", "apikey", "password", "passwd", "pwd", "dsn", "secret", "token", "arguments"}
)

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("nl2sql_trace_id", default="")
_logger = logging.getLogger(LOGGER_NAME)
#: 由 ``configure_logging`` 从配置里带出来：越靠近数据库的那一层拿不到 Settings，
#: 但必须知道"这一份部署允不允许把 SQL 全文写进日志"。
_INCLUDE_TEXT = False

_SECRET_ASSIGN = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|apikey|access[_-]?key|secret|token)\b(\s*[=:]\s*)(\S+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-/+=]{8,}")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9]{8,}")
_ODBC_DSN = re.compile(r"(?i)\bDRIVER=\{[^}]*\};[^\s'\"]+")
_DSN_PARTS = re.compile(r"(?i)\b(UID|PWD|SERVER|DATABASE)\s*=\s*[^;\"'\s]+")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:TRACE_ID_CHARS]


def new_connection_id() -> str:
    """物理连接号：不是链路 id，只用于把"这一条 db 行用了第几个连接"分开。

    没有它，"连接池减少了多少连接"只能靠读配置；有了它，日志本身就能数出
    ``distinct(connection)`` 与 db 行数的比。
    """
    return uuid.uuid4().hex[:8]


@contextlib.contextmanager
def bind_trace_id(value: str = "") -> Iterator[str]:
    """把这一轮的 trace_id 绑进当前调用栈，退出时还原。

    空值不猜测：调用方（HTTP / CLI / 评测）才是请求边界，由它铸 id；库内只继承。
    """
    token = _trace_id.set(value or current_trace_id())
    try:
        yield _trace_id.get()
    finally:
        _trace_id.reset(token)


def current_trace_id() -> str:
    return _trace_id.get()


def redact(text: str) -> str:
    """把凭证与连接串换成 ``***``；幂等，且不改长度以外的东西。"""
    if not text:
        return text
    out = _ODBC_DSN.sub("DRIVER={***}", text)
    out = _DSN_PARTS.sub(lambda m: f"{m.group(1)}=***", out)
    out = _SECRET_ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    out = _BEARER.sub(lambda m: f"{m.group(1)} ***", out)
    out = _SK_KEY.sub("sk-***", out)
    return out


def _segments(key: str) -> tuple[str, str]:
    """把 ``rows[0].sql`` 这类路径拆成（根段, 叶段），两段都要受审查。

    只看叶子的话，``{"result": {"rows": [...]}}`` 里的结果集会写成 ``result.rows``
    而躲过丢弃名单；只看根段则相反，``{"channels": [{"sql": ...}]}`` 的业务原文会漏。
    """
    root = key.split(".", 1)[0].split("[", 1)[0]
    leaf = key.rsplit(".", 1)[-1].split("[", 1)[0]
    return root.lower(), leaf.lower()


def _clean_field(key: str, value: Any, *, include_text: bool) -> Any:
    root, leaf = _segments(key)
    if root in DROP_FIELDS or leaf in DROP_FIELDS:
        return None
    if isinstance(value, str):
        # 问题原文 / 答案 / SQL 全文是业务内容，默认整段扣下，只留 fingerprint；
        # 失败原因（error、summary）默认写，因为"为什么错了"正是这层要回答的问题。
        if leaf in TEXT_FIELDS and not include_text:
            return None
        text = redact(value)
        return text[:TEXT_CAP] + ("…" if len(text) > TEXT_CAP else "")
    if isinstance(value, dict):
        nested = {
            k: _clean_field(f"{key}.{k}", v, include_text=include_text) for k, v in value.items()
        }
        kept = {k: v for k, v in nested.items() if v is not None}
        # 整段都被扣下的容器不留一个空壳：`{"result": {}}` 只会让日志行看起来像有内容。
        return kept or None
    if isinstance(value, (list, tuple)):
        items = [
            _clean_field(key, item, include_text=include_text)
            if isinstance(item, str)
            else _clean_field(f"{key}[{index}]", item, include_text=include_text)
            for index, item in enumerate(value[:20])
        ]
        kept_items = [item for item in items if item is not None]
        return kept_items or None
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))[:TEXT_CAP]


def clean_payload(payload: dict[str, Any], *, include_text: bool = False) -> dict[str, Any]:
    """脱敏 + 截断 + 丢弃禁写字段；返回可直接 JSON 化的新字典。"""
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        item = _clean_field(key, value, include_text=include_text)
        if item is not None:
            cleaned[key] = item
    return cleaned


def fingerprint(text: str) -> str:
    """同一问法/同一段提示的可分组标识；不可逆，所以不算敏感信息。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class TurnObservation:
    """一轮问数在循环里攒下来的过程量，最后由 ``_result`` 盖到 ``AgentResult`` 上。

    单独成对象是因为这些量在 7 个返回点上都可能要用：早退的澄清没有 prompt hash，
    跑到步数上限的轮次有 repair 计数。放在一个可写对象里，比给每个返回点加五个参数少
    一处会写错的地方。
    """

    prompt_version: str = ""
    gray_arm: str = "base"
    gray_bucket: int | None = None
    prompt_hash: str = ""
    few_shot: dict[str, Any] = field(default_factory=dict)
    guard_codes: list[str] = field(default_factory=list)
    repair_calls: int = 0
    repair_errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def add_usage(self, usage: dict[str, int] | None) -> None:
        usage = usage or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.cached_tokens += int(usage.get("cached_tokens") or 0)

    def add_guard_codes(self, codes: Sequence[str]) -> None:
        for code in codes:
            if code and code not in self.guard_codes:
                self.guard_codes.append(str(code))

    def note_repair(self, ok: bool) -> None:
        self.repair_calls += 1
        if not ok:
            self.repair_errors += 1


class JsonLineFormatter(logging.Formatter):
    """一行一个 JSON 事件；非事件日志退化为普通一行，方便混在同一份文件里读。"""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "nl2sql_event", None)
        if isinstance(event, dict):
            line = {"ts": _now_iso(), "level": record.levelname.lower(), **event}
            return json.dumps(line, ensure_ascii=False, default=str)
        return json.dumps(
            {
                "ts": _now_iso(),
                "level": record.levelname.lower(),
                "event": "log",
                "trace_id": current_trace_id(),
                "message": redact(record.getMessage())[:TEXT_CAP],
            },
            ensure_ascii=False,
        )


def configure_logging(
    log_path: str | Path | None,
    *,
    level: str = "info",
    include_text: bool = False,
    stream: Any = None,
) -> Path | None:
    """按需接上 sink；未配置路径时什么都不做，返回 ``None``。

    重复调用（同一个进程里多条命令共用 ``create_runtime``）不会叠加 handler。
    """
    global _INCLUDE_TEXT
    _INCLUDE_TEXT = bool(include_text)
    if stream is None and not log_path:
        return None
    resolved = Path(log_path).expanduser() if log_path else None
    numeric = getattr(logging, str(level).strip().lower(), None)
    if not isinstance(numeric, int):
        numeric = logging.INFO
    _logger.setLevel(numeric)
    _logger.propagate = False

    wanted = f"nl2sql:{resolved or 'stream'}"
    for handler in list(_logger.handlers):
        if getattr(handler, "nl2sql_channel", "") != wanted:
            continue
        handler.setLevel(numeric)
        return resolved

    handler: logging.Handler
    if resolved is not None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(resolved, encoding="utf-8")
    else:
        handler = logging.StreamHandler(stream)
    handler.nl2sql_channel = wanted  # type: ignore[attr-defined]
    handler.setFormatter(JsonLineFormatter())
    handler.setLevel(numeric)
    _logger.addHandler(handler)
    return resolved


def reset_logging() -> None:
    """拆掉本模块装上的 handler 并关闭文件——Windows 上不关就删不掉测试目录。"""
    for handler in list(_logger.handlers):
        if getattr(handler, "nl2sql_channel", ""):
            _logger.removeHandler(handler)
            handler.close()


def log_event(
    event: str, payload: dict[str, Any] | None = None, *, include_text: bool | None = None
) -> None:
    """写一条事件；没有 sink 时 ``logging`` 自己吞掉，所以调用方不需要判断。"""
    if not _logger.handlers:
        return
    withheld = _INCLUDE_TEXT if include_text is None else include_text
    record = {
        "event": event,
        "trace_id": current_trace_id(),
        **clean_payload(payload or {}, include_text=withheld),
    }
    _logger.info("nl2sql.%s", event, extra={"nl2sql_event": record})


def log_tool_call(
    *,
    tool: str,
    step: int,
    ok: bool,
    stage: str,
    elapsed_ms: float,
    guard_codes: Sequence[str] = (),
    summary: str = "",
    include_text: bool | None = None,
) -> None:
    log_event(
        EVENT_TOOL,
        {
            "tool": tool,
            "step": step,
            "ok": ok,
            "stage": stage,
            "elapsed_ms": elapsed_ms,
            "guard_codes": sorted(set(guard_codes)),
            "summary": summary,
        },
        include_text=include_text,
    )


def log_db_call(
    *,
    dialect: str,
    target: str,
    ok: bool,
    elapsed_ms: float,
    row_count: int = 0,
    truncated: bool = False,
    error_code: str = "",
    sql: str = "",
    connection: str = "",
    pooled: bool = False,
    include_text: bool | None = None,
) -> None:
    log_event(
        EVENT_DB,
        {
            "dialect": dialect,
            "target": target,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
            "row_count": row_count,
            "truncated": truncated,
            "error_code": error_code,
            "sql": sql,
            "sql_fingerprint": fingerprint(sql) if sql else "",
            "connection": connection,
            "pooled": pooled,
        },
        include_text=include_text,
    )


def log_turn(
    *,
    provider: str,
    model: str,
    prompt_version: str,
    prompt_hash: str,
    gray_arm: str = "base",
    gray_bucket: int | None = None,
    schema_hash: str,
    dialect: str,
    principal: str,
    status: str,
    steps: int,
    elapsed_ms: float,
    tokens: int,
    row_count: int,
    category: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    cached: bool = False,
    cache_age_sec: float = 0.0,
    cache_saved_tokens: int = 0,
    question: str = "",
    answer: str = "",
    sql: str = "",
    error: str = "",
    guard_codes: Sequence[str] = (),
    clarify_codes: Sequence[str] = (),
    few_shot: dict[str, Any] | None = None,
    repair_calls: int = 0,
    repair_errors: int = 0,
    dual_channel: dict[str, Any] | None = None,
    value_alignment: dict[str, Any] | None = None,
    include_text: bool | None = None,
) -> None:
    """一轮问数的完整观测行：模型版本、prompt hash、耗时、token、护栏拒绝原因都在上面。"""
    log_event(
        EVENT_TURN,
        {
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
            "gray_arm": gray_arm,
            "gray_bucket": gray_bucket,
            "schema_hash": schema_hash,
            "dialect": dialect,
            "principal": principal,
            "status": status,
            "steps": steps,
            "elapsed_ms": round(elapsed_ms, 2),
            "tokens": tokens,
            "row_count": row_count,
            "category": category,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
            "cached": bool(cached),
            "cache_age_sec": round(cache_age_sec, 3) if cached else 0.0,
            "cache_saved_tokens": int(cache_saved_tokens or 0),
            "question": question,
            "question_fingerprint": fingerprint(question),
            "answer": answer,
            "sql": sql,
            "sql_fingerprint": fingerprint(sql) if sql else "",
            "error": error,
            "guard_codes": sorted(set(guard_codes)),
            "clarify_codes": sorted(set(clarify_codes)),
            "few_shot": few_shot or {},
            "repair_calls": repair_calls,
            "repair_errors": repair_errors,
            "dual_channel": dual_channel or {},
            "value_alignment": {
                "source": (value_alignment or {}).get("source", ""),
                "resolved": len((value_alignment or {}).get("resolved") or []),
                "verified": len((value_alignment or {}).get("verified") or []),
                "ambiguous": len((value_alignment or {}).get("ambiguous") or []),
                "dropped": len((value_alignment or {}).get("dropped") or []),
            },
        },
        include_text=include_text,
    )


def read_events(path: str | Path, *, limit: int = 0) -> dict[str, Any]:
    """读回事件文件；坏行只计数不抛异常——一份被写坏一半的日志仍然可用。"""
    file = Path(path)
    if not file.is_file():
        return {"events": [], "lines": 0, "malformed": 0, "missing": True, "path": str(file)}
    events: list[dict[str, Any]] = []
    lines = 0
    malformed = 0
    with file.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            lines += 1
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(item, dict):
                events.append(item)
            else:
                malformed += 1
    if limit > 0:
        events = events[-limit:]
    return {"events": events, "lines": lines, "malformed": malformed, "missing": False, "path": str(file)}


def percentile(values: Sequence[float], pct: float) -> float:
    """最近秩法（向上取整），不插值：观测样本少时插值出来的小数只会假装精度。"""
    if not values:
        return 0.0
    ordered = sorted(float(item) for item in values)
    rank = max(1, math.ceil((pct / 100.0) * len(ordered)))
    return ordered[min(len(ordered) - 1, rank - 1)]


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _parse_ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def summarize(events: Sequence[dict[str, Any]], *, malformed: int = 0) -> dict[str, Any]:
    """把事件流折成指标。分母为 0 的比率一律给 ``None``，不给假的 0.0。"""
    turns = [item for item in events if item.get("event") == EVENT_TURN]
    tools = [item for item in events if item.get("event") == EVENT_TOOL]
    dbs = [item for item in events if item.get("event") == EVENT_DB]
    latencies = [float(item.get("elapsed_ms") or 0.0) for item in turns]
    # The executor's own clock: turn latency includes the model, so a pooling or timeout
    # change is only visible against the db events.
    db_latencies = [float(item.get("elapsed_ms") or 0.0) for item in dbs]
    guard_codes: dict[str, int] = {}
    for item in turns + tools:
        for code in item.get("guard_codes") or []:
            guard_codes[str(code)] = guard_codes.get(str(code), 0) + 1
    tool_errors = sum(1 for item in tools if not item.get("ok"))
    db_errors = sum(1 for item in dbs if not item.get("ok"))
    error_codes: dict[str, int] = {}
    for item in dbs:
        code = str(item.get("error_code") or "")
        if code:
            error_codes[code] = error_codes.get(code, 0) + 1
    escalations = sum(1 for item in turns if item.get("status") == "needs_human")
    few_shot_hits = sum(1 for item in turns if int((item.get("few_shot") or {}).get("injected") or 0) > 0)
    repaired = [item for item in turns if int(item.get("repair_calls") or 0) > 0]
    repaired_ok = sum(1 for item in repaired if item.get("status") == "ok")
    repair_error_calls = sum(int(item.get("repair_errors") or 0) for item in turns)
    disagreed = sum(
        1 for item in turns if (item.get("dual_channel") or {}).get("agreement") is False
    )
    voted = [item for item in turns if "agreement" in (item.get("dual_channel") or {})]
    agreements = sum(1 for item in voted if item["dual_channel"].get("agreement") is True)
    stamps = [ts for ts in (_parse_ts(item.get("ts")) for item in events) if ts is not None]
    window = max(stamps) - min(stamps) if len(stamps) >= 2 else 0.0
    tokens = [int(item.get("tokens") or 0) for item in turns]
    cache_hits = sum(1 for item in turns if item.get("cached"))
    cache_saved = sum(int(item.get("cache_saved_tokens") or 0) for item in turns)
    prompt_tokens = sum(int(item.get("prompt_tokens") or 0) for item in turns)
    completion_tokens = sum(int(item.get("completion_tokens") or 0) for item in turns)
    cached_tokens = sum(int(item.get("cached_tokens") or 0) for item in turns)
    by_version: dict[str, dict[str, Any]] = {}
    for item in turns:
        version = str(item.get("prompt_version") or "unknown")
        row = by_version.setdefault(
            version,
            {"turns": 0, "ok": 0, "needs_human": 0, "needs_clarification": 0},
        )
        row["turns"] += 1
        status = str(item.get("status") or "")
        if status in row:
            row[status] += 1
    for row in by_version.values():
        row["ok_rate"] = _ratio(row["ok"], row["turns"])
        row["escalation_rate"] = _ratio(row["needs_human"], row["turns"])
        row["clarification_rate"] = _ratio(row["needs_clarification"], row["turns"])
    return {
        "events": len(events),
        "turns": len(turns),
        "tool_calls": len(tools),
        "db_calls": len(dbs),
        "malformed_lines": malformed,
        "window_sec": round(window, 3),
        "qps": _ratio(len(turns), window),
        "qps_note": "单进程顺序请求，这是吞吐下限而非上限",
        "latency_ms": {
            "avg": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            "p50": round(percentile(latencies, 50), 2),
            "p95": round(percentile(latencies, 95), 2),
            "max": round(max(latencies), 2) if latencies else 0.0,
        },
        "status_counts": _counts(item.get("status") for item in turns),
        "success_rate": _ratio(sum(1 for item in turns if item.get("status") == "ok"), len(turns)),
        "escalation_rate": _ratio(escalations, len(turns)),
        "clarification_rate": _ratio(
            sum(1 for item in turns if item.get("status") == "needs_clarification"), len(turns)
        ),
        "experience_hit_rate": _ratio(few_shot_hits, len(turns)),
        "repair_rate": _ratio(len(repaired), len(turns)),
        "repair_turns": len(repaired),
        "repair_turns_answered": repaired_ok,
        "repair_success_rate": _ratio(repaired_ok, len(repaired)),
        "repair_call_error_rate": _ratio(repair_error_calls, sum(int(item.get("repair_calls") or 0) for item in turns)),
        "tool_error_rate": _ratio(tool_errors, len(tools)),
        "db_error_rate": _ratio(db_errors, len(dbs)),
        "db_latency_ms": {
            "avg": round(sum(db_latencies) / len(db_latencies), 2) if db_latencies else 0.0,
            "p50": round(percentile(db_latencies, 50), 2),
            "p95": round(percentile(db_latencies, 95), 2),
        },
        "db_connections": len({str(item.get("connection") or "") for item in dbs} - {""}),
        "db_pooled_calls": sum(1 for item in dbs if item.get("pooled")),
        "db_error_codes": error_codes,
        "guard_denial_rate": _ratio(sum(1 for item in turns if item.get("guard_codes")), len(turns)),
        "guard_codes": dict(sorted(guard_codes.items(), key=lambda kv: (-kv[1], kv[0]))),
        "dual_channel_votes": len(voted),
        "dual_channel_agreement_rate": _ratio(agreements, len(voted)),
        "dual_channel_disagreements": disagreed,
        "avg_tokens": round(sum(tokens) / len(tokens), 2) if tokens else 0.0,
        "total_tokens": sum(tokens),
        "token_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
            "unclassified_tokens": max(0, sum(tokens) - prompt_tokens - completion_tokens),
        },
        "cache": {
            "hits": cache_hits,
            "hit_rate": _ratio(cache_hits, len(turns)),
            "saved_tokens": cache_saved,
        },
        "prompt_versions": sorted({str(item.get("prompt_version") or "") for item in turns}),
        "by_prompt_version": dict(sorted(by_version.items())),
        "prompt_hashes": len({str(item.get("prompt_hash") or "") for item in turns}),
        "models": sorted({str(item.get("model") or "") for item in turns}),
        "schema_hashes": sorted({str(item.get("schema_hash") or "") for item in turns}),
        "trace_ids": len({str(item.get("trace_id") or "") for item in turns}),
    }


def _counts(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
