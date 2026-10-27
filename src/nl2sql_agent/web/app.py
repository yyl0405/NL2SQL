"""FastAPI console: ask the agent, browse the schema, run the frozen evaluation.

The HTTP layer is intentionally thin — every endpoint delegates to the same
runtime objects the CLI uses, so the web UI cannot be a second, weaker copy of
the guardrails.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import observability
from ..agent import AGENT_PROMPT_VERSION
from ..conversation import Conversation
from ..costs import cost_summary
from ..evaluation import (
    CachedJudge,
    JudgeCache,
    JudgeError,
    LLMJudge,
    evaluate,
    load_cases,
)
from ..runtime import Runtime
from ..memory.experience import FEEDBACK_REASONS
from ..text import to_jsonable
from ..tools.db import pool_stats
from ..tools.sql_tools import run_readonly_query

MAX_SESSIONS = 50
# A turn can sit in one model call for tens of seconds; an empty frame keeps the
# connection and any proxy in front of it from deciding the stream is dead.
SSE_HEARTBEAT_SEC = 2.0
_REPORT_NAME = re.compile(r"^[\w.\-]+\.json$")
# 客户端可以自带 trace_id（把前端日志和后端事件对上），但不能拿它当日志注入通道用。
_TRACE_ID = re.compile(r"^[\w:.\-]{1,64}$")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _llm_snapshot(llm: Any) -> dict[str, Any]:
    """Retry/breaker state for the console; the mock provider has neither."""
    snapshot = getattr(llm, "snapshot", None)
    if callable(snapshot):
        return snapshot()
    return {"enabled": False, "reason": "mock provider 不经过网络，不包装重试/熔断"}


class AskRequest(BaseModel):
    question: str


def _sse(event: str, data: dict[str, Any]) -> str:
    """One ``text/event-stream`` frame.

    The payload uses the same ``to_dict`` output as the REST answer, so streaming
    cannot hand the browser anything the plain ``/ask`` would not already return.
    """
    return f"event: {event}\ndata: {json.dumps(to_jsonable(data), ensure_ascii=False)}\n\n"


class EvalRequest(BaseModel):
    multi_turn: bool = True
    judge: bool = False
    judge_sample: int | None = None


class FeedbackRequest(BaseModel):
    reason: str
    detail: str = ""
    turn_index: int | None = None
    suggested_sql: str = ""


class FeedbackReviewRequest(BaseModel):
    action: str
    sql: str = ""
    note: str = ""
    reviewer: str = ""


class SessionStore:
    """In-process sessions with a per-session execution lock.

    The store lock only protects the dict; it must not be held while an agent
    turn runs for seconds. Each session gets its own lock so two turns in the
    same conversation cannot interleave their history reads and appends, while
    distinct conversations still run in parallel (FastAPI's sync endpoints
    already execute in a thread pool).
    """

    def __init__(self, limit: int = MAX_SESSIONS) -> None:
        self._sessions: OrderedDict[str, Conversation] = OrderedDict()
        self._session_locks: dict[str, threading.Lock] = {}
        self._limit = limit
        self._lock = threading.Lock()

    def create(self, session_id: str) -> None:
        with self._lock:
            self._sessions[session_id] = Conversation()
            self._session_locks.setdefault(session_id, threading.Lock())
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > self._limit:
                evicted, _ = self._sessions.popitem(last=False)
                self._session_locks.pop(evicted, None)

    def get(self, session_id: str) -> Conversation:
        with self._lock:
            conversation = self._sessions.get(session_id)
            if conversation is None:
                raise HTTPException(status_code=404, detail="会话不存在或已过期")
            self._sessions.move_to_end(session_id)
            return conversation

    def lock(self, session_id: str) -> threading.Lock:
        """The per-session turn lock; unknown sessions fail before any work starts."""
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                raise HTTPException(status_code=404, detail="会话不存在或已过期")
            return lock

    def reset(self, session_id: str) -> None:
        """Replace the history but keep the lock, so waiting turns see the new state."""
        with self._lock:
            if session_id not in self._sessions:
                raise HTTPException(status_code=404, detail="会话不存在或已过期")
            self._sessions[session_id] = Conversation()
            self._sessions.move_to_end(session_id)

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._session_locks.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._sessions)


def create_app(runtime: Runtime) -> FastAPI:
    settings = runtime.settings
    sessions = SessionStore()
    eval_lock = threading.Lock()

    app = FastAPI(
        title="NL2SQL Agent Console",
        version=AGENT_PROMPT_VERSION,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "provider": settings.provider,
            # Which route turns take: a stale process answering with the other mode
            # is otherwise invisible in the console.
            "mode": settings.mode,
            "model": settings.model if settings.provider == "openai" else "RuleBasedLLM",
            "prompt_version": AGENT_PROMPT_VERSION,
            # The hash the agent operates under, not the full catalog's:
            # experience records are keyed by it, so this is what makes the
            # "schema 已变更" chip in the console true rather than always-on.
            "schema_hash": runtime.agent.schema_hash,
            "domain": runtime.catalog.domain,
            "dialect": runtime.catalog.dialect,
            "as_of_date": runtime.catalog.as_of_date,
            "single_turn_cases": len(load_cases(settings.eval_cases_path)),
            "multi_turn_cases": len(load_cases(settings.multi_turn_cases_path)),
            "multi_turn_enabled": settings.multi_turn_enabled,
            "rewriter": settings.rewriter,
            # Never build the index here: /api/health must stay cheap even when
            # the first vector build would call an embedding endpoint.
            "linking": {
                "mode": settings.linking,
                "embedding": getattr(runtime.catalog.embedder, "name", "") or "none",
                "rerank": bool(runtime.catalog.reranker),
            },
            "few_shot_enabled": bool(settings.few_shot_enabled),
            "few_shot_max": settings.few_shot_max,
            # Which role this console is serving: the schema hash above is the
            # filtered view, so the principal id is what makes it interpretable.
            "guard": {
                "principal": runtime.principal.id if runtime.principal else "",
                "ast_guard": settings.ast_guard,
            },
            "judge_enabled": bool(settings.judge_enabled),
            # 熔断状态每次请求都读，因为它会随时间变化；日志是否开启则不会。
            "llm_resilience": _llm_snapshot(runtime.llm or runtime.agent.llm),
            "sessions": len(sessions),
            "database": settings.database_label,
            # An empty list means pooling is off (the default): every query opens its own
            # connection. With NL2SQL_DB_POOL_SIZE>1 this is where connects vs. reuses stop
            # being a claim in a config file.
            "pool": pool_stats(),
            # 空对象/ttl 0 表示缓存关闭；打开后命中率是运行事实而不是配置声明。
            "result_cache": (
                runtime.cache.stats()
                if runtime.cache is not None
                else {"enabled": False, "ttl_sec": 0.0}
            ),
            "gray": {
                "percent": settings.gray_percent,
                "base": AGENT_PROMPT_VERSION,
                "candidate": settings.gray_prompt_version,
                "salt": settings.gray_salt,
            },
            # 价格未配置时只报 token；费用永远是用户给的价格算出来的估算值。
            "pricing": {
                "configured": settings.price_table is not None,
                "currency": settings.price_currency,
            },
            # 观测是否真的在记：没配 NL2SQL_LOG_PATH 时 /api/metrics 只能报"没有事件"，
            # 这一位让人一眼看出是"没开"还是"开了但指标很差"。
            "observability": {
                "log_enabled": settings.log_path is not None,
                "log_level": settings.log_level,
                "include_text": bool(settings.log_include_text),
            },
        }

    @app.get("/api/cost")
    def cost(limit: int = 20000) -> dict[str, Any]:
        """Token (and optionally price-derived) rollup over the event log."""
        if settings.log_path is None:
            return {
                "enabled": False,
                "reason": "未配置 NL2SQL_LOG_PATH，没有事件日志可汇总",
                "report": None,
            }
        bundle = observability.read_events(settings.log_path, limit=max(1, min(limit, 100000)))
        if bundle["missing"]:
            return {
                "enabled": True,
                "reason": f"事件日志不存在: {settings.log_path}（先跑一次 ask / eval）",
                "report": None,
            }
        return {
            "enabled": True,
            "path": str(settings.log_path),
            "lines": bundle["lines"],
            "malformed_lines": bundle["malformed"],
            "report": cost_summary(bundle["events"], prices=settings.price_table),
        }

    @app.post("/api/sessions")
    def create_session() -> dict[str, Any]:
        session_id = uuid.uuid4().hex[:16]
        sessions.create(session_id)
        return {"session_id": session_id, "turns": []}

    def _session_payload(conversation: Conversation) -> dict[str, Any]:
        return {
            "turns": [turn.to_dict() for turn in conversation.turns],
            "slots": conversation.pending_slots().to_dict(),
            "transcript": conversation.render(max_turns=20),
        }

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        return _session_payload(sessions.get(session_id))

    @app.delete("/api/sessions/{session_id}")
    def reset_session(session_id: str) -> dict[str, bool]:
        # Wait for an in-flight turn instead of yanking the conversation out from
        # under it; the lock survives the reset for whoever queued behind it.
        with sessions.lock(session_id):
            sessions.reset(session_id)
        return {"reset": True}

    def _question_or_422(payload: AskRequest) -> str:
        question = (payload.question or "").strip()
        if not question:
            raise HTTPException(status_code=422, detail="问题不能为空")
        return question

    def _request_trace_id(request: Request) -> str:
        # 请求边界是铸 trace_id 的唯一地方：Agent 只继承，不猜。
        incoming = (request.headers.get("x-trace-id") or "").strip()
        return incoming if _TRACE_ID.match(incoming) else observability.new_trace_id()

    def _run_turn(
        session_id: str,
        question: str,
        trace_id: str,
        *,
        endpoint: str,
        progress: Any = None,
    ) -> dict[str, Any]:
        """One turn at a time per conversation: the agent reads the last answered
        turn and the append must happen before the next turn reads it. Both the
        plain and the streaming endpoint go through here, so the guardrails, the
        conversation write and the ``api`` event cannot drift apart."""
        with sessions.lock(session_id):
            conversation = sessions.get(session_id)
            result = runtime.agent.run(
                question,
                history=conversation,
                trace_id=trace_id,
                gray_key=f"session:{session_id}",
                progress=progress,
            )
            conversation.add(result.to_turn())
            # 绑上同一个 id 再写：agent.run 返回时它自己的绑定已经退出，否则这条 API 行
            # 会落在 trace_id="" 上，正是"从接口追到数据库"要断的那一处。
            with observability.bind_trace_id(trace_id):
                observability.log_event(
                    observability.EVENT_API,
                    {
                        "endpoint": endpoint,
                        "session_id": session_id,
                        "status": result.status,
                        "elapsed_ms": round(result.elapsed_ms, 2),
                        "turns": len(conversation),
                    },
                )
            return {
                "result": result.to_dict(),
                "conversation": _session_payload(conversation),
            }

    @app.post("/api/sessions/{session_id}/ask")
    def ask(session_id: str, payload: AskRequest, request: Request) -> dict[str, Any]:
        return _run_turn(
            session_id,
            _question_or_422(payload),
            _request_trace_id(request),
            endpoint="/api/sessions/{session_id}/ask",
        )

    @app.post("/api/sessions/{session_id}/ask/stream")
    def ask_stream(
        session_id: str, payload: AskRequest, request: Request
    ) -> StreamingResponse:
        """同一个 runtime、同一把会话锁，只是把每一步工具结果提前推给浏览器。

        问答本身仍走 :func:`_run_turn`，所以护栏、会话写入与 ``api`` 事件和非流式
        ``/ask`` 完全一致；区别只有 Agent 的 ``progress`` 回调把 step 落进队列。
        """
        question = _question_or_422(payload)
        trace_id = _request_trace_id(request)
        events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()

        def worker() -> None:
            try:
                outcome = _run_turn(
                    session_id,
                    question,
                    trace_id,
                    endpoint="/api/sessions/{session_id}/ask/stream",
                    progress=lambda entry: events.put(("step", entry)),
                )
                events.put(("result", outcome))
            except Exception as exc:  # noqa: BLE001
                events.put(("error", {"message": str(exc)[:200]}))
            finally:
                # 浏览器断开时 worker 仍会把这一轮写完：会话历史不能因为没人收就半途作废。
                events.put(("done", {"trace_id": trace_id}))

        def frames():
            threading.Thread(
                target=worker, daemon=True, name=f"nl2sql-stream-{trace_id or 'anon'}"
            ).start()
            yield _sse("accepted", {"trace_id": trace_id})
            while True:
                try:
                    kind, data = events.get(timeout=SSE_HEARTBEAT_SEC)
                except queue.Empty:
                    yield _sse("heartbeat", {})
                    continue
                yield _sse(kind, data)
                if kind == "done":
                    return

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/metrics")
    def metrics(limit: int = Query(default=20000, ge=1, le=200000)) -> dict[str, Any]:
        """把事件文件折成 P95 / 人工升级率 / 经验命中率 / repair 成功率。

        读的是本地日志，不接任何监控系统；没开日志时直说没开，不给一个看着像 0 的空表。
        """
        if settings.log_path is None:
            return {
                "configured": False,
                "note": "未配置 NL2SQL_LOG_PATH，事件没有落盘，指标不可计算",
                "metrics": None,
            }
        bundle = observability.read_events(settings.log_path, limit=limit)
        return {
            "configured": True,
            "path": str(settings.log_path),
            "missing": bundle["missing"],
            "lines": bundle["lines"],
            "malformed_lines": bundle["malformed"],
            "metrics": observability.summarize(bundle["events"], malformed=bundle["malformed"]),
        }

    @app.get("/api/schema")
    def schema() -> dict[str, Any]:
        catalog = runtime.catalog
        tables = [
            {
                "name": table.name,
                "description": table.description,
                "synonyms": list(table.synonyms),
                "primary_key": list(table.primary_key),
                "disabled": table.disabled,
                "required_filters": [dict(item) for item in table.required_filters],
                "columns": [
                    {
                        "name": column.name,
                        "type": column.type,
                        "description": column.description,
                        "synonyms": list(column.synonyms),
                        "sensitive": column.sensitive,
                        "disabled": column.disabled,
                    }
                    for column in table.columns
                ],
            }
            for table in catalog.tables.values()
        ]
        return {
            "domain": catalog.domain,
            "dialect": catalog.dialect,
            "as_of_date": catalog.as_of_date,
            "schema_hash": catalog.schema_hash(),
            "tables": tables,
            "relationships": [dict(item) for item in catalog.relationships],
            "business_rules": list(catalog.business_rules),
        }

    @app.get("/api/schema/search")
    def search_schema(q: str = Query(min_length=1, max_length=80), top_k: int = 5) -> dict[str, Any]:
        result = runtime.catalog.search(q, top_k=max(1, min(top_k, 10)))
        return {
            "query": q,
            "mode": result.mode,
            "embedding": result.embedding or "none",
            "degraded": result.degraded,
            "reranked": result.reranked,
            "tables": [
                {
                    "name": hit.table.name,
                    "score": round(hit.score, 4),
                    "description": hit.table.description,
                    "sources": list(hit.sources),
                    "ranks": dict(hit.ranks),
                }
                for hit in result.hits
            ],
        }

    @app.get("/api/experience")
    def experience(
        status: str | None = Query(default=None, pattern="^(verified|candidate)$"),
        limit: int = 20,
    ) -> dict[str, Any]:
        store = runtime.store
        return {
            "items": store.list(status=status, limit=max(1, min(limit, 200))),
            "stats": store.stats(),
        }

    @app.get("/api/failures")
    def failures(
        status: str = Query(default="open", pattern="^(open|resolved|dismissed|all)$"),
        limit: int = 20,
    ) -> dict[str, Any]:
        store = runtime.store
        rows = store.list_failures(
            status=None if status == "all" else status, limit=max(1, min(limit, 200))
        )
        return {"items": rows, "stats": store.failure_stats()}

    @app.post("/api/sessions/{session_id}/feedback")
    def submit_feedback(
        session_id: str, payload: FeedbackRequest, request: Request
    ) -> dict[str, Any]:
        """File a complaint against one answered turn.

        The question and SQL come from the stored turn, never from the request:
        a client cannot attach feedback to an answer that was not shown to it.
        """
        reason = payload.reason.strip()
        if reason not in FEEDBACK_REASONS:
            raise HTTPException(
                status_code=422,
                detail=f"reason 只能是 {', '.join(FEEDBACK_REASONS)}",
            )
        with sessions.lock(session_id):
            conversation = sessions.get(session_id)
            turns = conversation.turns
            if not turns:
                raise HTTPException(status_code=422, detail="会话还没有可反馈的回答")
            index = len(turns) - 1 if payload.turn_index is None else payload.turn_index
            if index < 0 or index >= len(turns):
                raise HTTPException(status_code=422, detail="turn_index 超出会话范围")
            turn = turns[index]
            if turn.status != "ok" or not turn.sql:
                raise HTTPException(status_code=422, detail="该轮没有可反馈的 SQL 结果")
            suggested_sql = payload.suggested_sql.strip()
            if suggested_sql:
                check = runtime.guard.check(suggested_sql)
                if not check.valid:
                    messages = "; ".join(issue.message for issue in check.errors)
                    raise HTTPException(status_code=422, detail=f"建议 SQL 未通过护栏: {messages}")
                suggested_sql = check.sql
            result = runtime.store.record_feedback(
                turn.resolved,
                turn.sql,
                reason=reason,
                detail=payload.detail,
                suggested_sql=suggested_sql,
                schema_hash=runtime.agent.schema_hash,
                session_id=session_id,
                turn_index=index,
                trace_id=turn.trace_id,
                principal=runtime.principal.id if runtime.principal else "",
            )
            with observability.bind_trace_id(turn.trace_id):
                observability.log_event(
                    observability.EVENT_API,
                    {
                        "endpoint": "/api/sessions/{session_id}/feedback",
                        "session_id": session_id,
                        "turn_index": index,
                        "reason": reason,
                        "created": result.get("created", False),
                    },
                )
        return result

    @app.get("/api/feedback")
    def feedback(
        status: str = Query(default="open", pattern="^(open|approved|rejected|all)$"),
        limit: int = 20,
    ) -> dict[str, Any]:
        store = runtime.store
        rows = store.list_feedback(
            status=None if status == "all" else status, limit=max(1, min(limit, 200))
        )
        return {
            "items": rows,
            "stats": store.feedback_stats(),
            "reasons": dict(FEEDBACK_REASONS),
        }

    @app.post("/api/feedback/{feedback_id}/review")
    def review_feedback(feedback_id: int, payload: FeedbackReviewRequest) -> dict[str, Any]:
        action = payload.action.strip().lower()
        if action not in {"approve", "reject"}:
            raise HTTPException(status_code=422, detail="action 只能是 approve 或 reject")
        if action == "reject" and not payload.note.strip():
            raise HTTPException(status_code=422, detail="拒绝反馈必须说明原因")

        def validator(sql: str) -> tuple[bool, str]:
            check = runtime.guard.check(sql)
            if not check.valid:
                return False, "; ".join(issue.message for issue in check.errors)
            execution = run_readonly_query(runtime.settings.db_path, check.sql, max_rows=10)
            if not execution.get("success"):
                return False, str(execution.get("error", "execution failed"))
            return True, "ok"

        try:
            result = runtime.store.review_feedback(
                feedback_id,
                action,
                sql=payload.sql,
                note=payload.note,
                validator=validator if action == "approve" else None,
                reviewer=payload.reviewer,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @app.get("/api/eval/cases")
    def eval_cases() -> dict[str, Any]:
        def _summary(path: Path) -> list[dict[str, Any]]:
            return [
                {
                    "id": case.id,
                    "kind": case.kind,
                    "category": case.category,
                    "difficulty": case.difficulty,
                    "question": case.question,
                    "turns": list(case.turns),
                    "expect_refusal": case.expect_refusal,
                    "expect_clarification": case.expect_clarification,
                }
                for case in load_cases(path)
            ]

        return {
            "single": _summary(settings.eval_cases_path),
            "multi": _summary(settings.multi_turn_cases_path),
        }

    @app.post("/api/eval/run")
    def run_eval(payload: EvalRequest = Body(default_factory=EvalRequest)) -> dict[str, Any]:
        cases = list(load_cases(settings.eval_cases_path))
        if payload.multi_turn:
            cases += list(load_cases(settings.multi_turn_cases_path))
        judge_model = None
        if payload.judge:
            try:
                judge_model = CachedJudge(
                    LLMJudge(settings),
                    JudgeCache(settings.judge_cache_path),
                    model_name=settings.judge_model,
                    prompt_version=settings.judge_prompt_version,
                )
            except JudgeError as exc:
                raise HTTPException(status_code=400, detail=f"Judge 配置错误: {exc}") from exc
        if not eval_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有一次评测正在运行")
        report_dir = Path(settings.home) / "reports"
        try:
            report = evaluate(
                runtime.agent,
                cases,
                db_path=settings.target,
                guard=runtime.guard,
                provider=settings.provider,
                report_dir=report_dir,
                model="" if settings.provider == "mock" else settings.model,
                judge_model=judge_model,
                judge_sample=payload.judge_sample,
                judge_max_calls=settings.judge_max_calls,
                judge_max_rows=settings.judge_max_rows,
            )
        finally:
            eval_lock.release()
        saved = sorted(report_dir.glob("eval_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        return {
            "report": report.to_dict(),
            "report_dir": str(report_dir),
            "saved_report": saved[0].name if saved else "",
        }

    @app.get("/api/eval/reports")
    def eval_reports() -> dict[str, Any]:
        report_dir = Path(settings.home) / "reports"
        files = sorted(report_dir.glob("eval_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        return {
            "dir": str(report_dir),
            "items": [
                {
                    "name": item.name,
                    "markdown": item.with_suffix(".md").name,
                    "size": item.stat().st_size,
                    "modified_at": int(item.stat().st_mtime),
                }
                for item in files[:20]
            ],
        }

    @app.get("/api/eval/reports/{name}")
    def eval_report(name: str) -> FileResponse:
        if not _REPORT_NAME.match(name):
            raise HTTPException(status_code=400, detail="非法报告文件名")
        report_dir = (Path(settings.home) / "reports").resolve()
        target = (report_dir / name).resolve()
        if target.parent != report_dir or not target.is_file():
            raise HTTPException(status_code=404, detail="报告不存在")
        return FileResponse(target, media_type="application/json")

    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="console")
    return app
