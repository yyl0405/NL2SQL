"""The NL2SQL agent loop: plan, call tools, reflect on errors, answer."""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .cache import ResultCache, cache_key
from .catalog import Catalog
from .clarify import (
    CODE_ENTITY,
    CODE_MODEL,
    Clarification,
    ClarificationPolicy,
    ClarifyPlan,
    MetricGlossary,
    clarification_text,
    resolve_clarification_reply,
    with_options,
)
from .config import Settings
from .conversation import Slots, Turn, as_turns, extract_slots
from .dialects import get as get_dialect
from .gray import bucket, in_gray
from .guard import SQLGuard
from .llm import ChatModel, LLMError, LLMResponse, ToolCall, usage_breakdown, usage_tokens
from .memory import ExperienceStore
from .observability import (
    TurnObservation,
    bind_trace_id,
    fingerprint,
    log_tool_call,
    log_turn,
)
from .pipeline import (
    MAX_PIPELINE_TABLES,
    MAX_VALUE_PROBES,
    answer_from,
    assumptions_from,
    execution_repair_prompt,
    generation_prompt,
    guard_repair_prompt,
    phrasing_messages,
    probe_candidates,
    value_mentions,
    value_probe_block,
)
from .recall import ExperienceRecall, recall_from_settings
from .rewriter import QuestionRewriter, Rewrite, RuleBasedRewriter
from .result_compare import describe_diff, rows_equal
from .taxonomy import classify
from .text import parse_json_object, to_jsonable
from .tools.base import ToolRegistry
from .value_align import KIND_QUALIFIERS, Alignment, ValueAligner
from .voting import SecondOpinion, SecondOpinionGenerator, extract_sql, should_verify

_ALLOWED_STATUS = {"ok", "needs_clarification", "needs_human", "refused", "failed"}
# Outcomes a human has to look at: they are queued instead of silently dropped.
_FAILURE_STATUS = {"needs_human", "failed", "max_steps"}
AGENT_PROMPT_VERSION = "agent-v5"
#: Candidate used by the 10% gray release. Same instructions and JSON contract as
#: the base prompt; only the few-shot block moves back before the contract, which
#: is exactly the prefix-cache trade-off the layout change was about.
GRAY_PROMPT_VERSION = "agent-v5-fewshot-early"
PROMPT_LAYOUTS = {
    AGENT_PROMPT_VERSION: "few_shot_tail",
    GRAY_PROMPT_VERSION: "few_shot_early",
}
_CLARIFY_NUDGE = (
    "在执行任何澄清之前，必须先调用 search_tables 和 get_table_schema 查看真实表与字段。"
    "请先完成这两步；查完后仍缺少无法推断的具体过滤值，再返回 needs_clarification。"
    "没有 execute_sql 的返回结果时，不要给出任何数字结论。"
)
_EXECUTE_NUDGE = (
    "你已经查看过 schema。若存在可用表，请基于已确认字段生成 SQL，并调用 validate_sql "
    "与 execute_sql 获取真实结果；只有在执行失败或确实没有合适表时，才返回 "
    "needs_clarification / needs_human，并且不要给出任何数字结论。"
)
_UNVERIFIED_CLAIM = re.compile(r"\d[\d,.]*\s*(?:台|元|条|个|件|套|份|%)")
_PROBE_NUDGE = (
    "你已经连续多轮只做探索（检索表、查看字段、试取值），但还没有写出并执行任何 SQL。"
    "停止探索：基于 get_table_schema 已确认的字段直接生成 SQL，并调用 validate_sql 与 "
    "execute_sql。如果某个条件在已确认字段里没有对应列，就按现有口径查询并在 assumptions "
    "里说明该条件未落地；不要再换更多的表或列去试。"
)
# The mirror of _PROBE_NUDGE: _PROBE_NUDGE catches a model that explores without
# ever executing, this one catches a model that executed successfully and then
# wanders back into exploration until the step budget runs out. The loop already
# returns the last successful execution when the budget expires, so a turn that
# ends in max_steps is a turn that never executed - and a turn that executed and
# kept exploring is one nudge away from a usable answer.
_FINALIZE_NUDGE = (
    "你已经成功执行过一条查询并拿到了结果。如果那个结果就是答案，请停止调用工具，"
    "直接按 JSON 契约输出最终回答（status / answer / sql / confidence），answer 里的"
    "数字必须来自这条已执行的结果。只有确认口径确实不对时才改 SQL 并重新执行；"
    "不要回到 search_tables / get_table_schema / search_values 重新探索。"
)
_EXPLORATORY_TOOLS = {"search_tables", "get_table_schema", "search_values", "search_experience"}


@dataclass
class AgentResult:
    question: str
    status: str
    answer: str = ""
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    confidence: float = 0.0
    steps: int = 0
    elapsed_ms: float = 0.0
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cached: bool = False
    cache: dict[str, Any] = field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    error: str = ""
    effective_question: str = ""
    rewrite_source: str = "none"
    inherited_slots: list[str] = field(default_factory=list)
    slots: Slots = field(default_factory=Slots)
    clarification: Clarification | None = None
    alternatives: list[str] = field(default_factory=list)
    clarify: dict[str, Any] | None = None
    channels: list[dict[str, Any]] = field(default_factory=list)
    value_alignment: dict[str, Any] | None = None
    trace_id: str = ""
    prompt_version: str = ""
    gray_arm: str = "base"
    gray_bucket: int | None = None
    prompt_hash: str = ""
    few_shot: dict[str, Any] = field(default_factory=dict)
    guard_codes: list[str] = field(default_factory=list)
    repair_calls: int = 0
    repair_errors: int = 0
    #: Which route produced this turn: ``pipeline`` (deterministic retrieve -> generate
    #: -> guard -> execute -> phrase) or ``agent`` (the tool-calling loop).
    mode: str = "agent"

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "answer": self.answer,
            "sql": self.sql,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "confidence": round(self.confidence, 3),
            "steps": self.steps,
            "elapsed_ms": self.elapsed_ms,
            "tokens": self.tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "cached": self.cached,
            "cache": dict(self.cache),
            "tool_trace": self.tool_trace,
            "assumptions": self.assumptions,
            "error": self.error,
            "effective_question": self.effective_question or self.question,
            "rewrite_source": self.rewrite_source,
            "inherited_slots": list(self.inherited_slots),
            "slots": self.slots.to_dict(),
            "clarification": self.clarification.to_dict() if self.clarification else None,
            "alternatives": list(self.alternatives),
            "clarify": self.clarify,
            "channels": [to_jsonable(item) for item in self.channels],
            "value_alignment": self.value_alignment,
            "trace_id": self.trace_id,
            "prompt_version": self.prompt_version,
            "gray_arm": self.gray_arm,
            "gray_bucket": self.gray_bucket,
            "prompt_hash": self.prompt_hash,
            "few_shot": dict(self.few_shot),
            "guard_codes": list(self.guard_codes),
            "repair_calls": self.repair_calls,
            "repair_errors": self.repair_errors,
            "mode": self.mode,
        }

    def to_turn(self) -> Turn:
        return Turn(
            question=self.question,
            effective_question=self.effective_question or self.question,
            status=self.status,
            answer=self.answer,
            sql=self.sql or "",
            row_count=self.row_count,
            slots=self.slots,
            rewrite_source=self.rewrite_source,
            inherited=list(self.inherited_slots),
            clarification=self.clarification.to_dict() if self.clarification else None,
            trace_id=self.trace_id,
        )


class NL2SQLAgent:
    def __init__(
        self,
        catalog: Catalog,
        llm: ChatModel,
        tools: ToolRegistry,
        settings: Settings,
        store: ExperienceStore | None = None,
        schema_hash: str = "",
        rewriter: QuestionRewriter | None = None,
        second_opinion: SecondOpinionGenerator | None = None,
        guard: SQLGuard | None = None,
        aligner: ValueAligner | None = None,
        clarifier: ClarificationPolicy | None = None,
        recall: ExperienceRecall | None = None,
        cache: ResultCache | None = None,
    ) -> None:
        self.catalog = catalog
        self.llm = llm
        self.tools = tools
        self.settings = settings
        self.store = store
        self.schema_hash = schema_hash
        self.rewriter = rewriter if rewriter is not None else RuleBasedRewriter()
        self.second_opinion = second_opinion
        self.guard = guard
        self.aligner = aligner
        self.clarifier = clarifier or _build_policy(settings)
        self._recall = recall
        self._recall_fallback: ExperienceRecall | None = None
        self.cache = cache

    def _recall_for(self) -> ExperienceRecall | None:
        """The runtime's recall object, or a lexical one built from settings for library calls."""
        if self.store is None:
            return None
        if self._recall is not None:
            return self._recall
        if self._recall_fallback is None:
            self._recall_fallback = recall_from_settings(self.store, self.settings)
        return self._recall_fallback

    def run(
        self,
        question: str,
        history: Any = None,
        trace_id: str = "",
        *,
        use_cache: bool = True,
        force_prompt: str = "",
        gray_key: str = "",
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentResult:
        # The id is minted at the request boundary (HTTP / CLI / eval) and only
        # inherited here, so a bare library call stays byte-identical to itself.
        with bind_trace_id(trace_id) as bound:
            prompt_version, gray_arm, gray_bucket = self._prompt_route(
                force_prompt, gray_key or question
            )
            cache_ready = (
                use_cache and self.cache is not None and self.cache.enabled
            )
            key = (
                self._cache_key(question, history, prompt_version)
                if cache_ready
                else ""
            )
            if cache_ready:
                started = time.perf_counter()
                found = self.cache.get(key)
                if found is not None:
                    value, age = found
                    result = copy.deepcopy(value)
                    result.cached = True
                    result.cache = {
                        "hit": True,
                        "age_sec": round(age, 3),
                        "saved_tokens": int(value.tokens),
                        "saved_steps": int(value.steps),
                        "saved_elapsed_ms": float(value.elapsed_ms),
                    }
                    result.tokens = 0
                    result.prompt_tokens = 0
                    result.completion_tokens = 0
                    result.cached_tokens = 0
                    result.steps = 0
                    result.tool_trace = []
                    result.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                    result.trace_id = bound
                    self._emit_turn(result)
                    return result
            result = self._verify(
                self._run_loop(
                    question,
                    history=history,
                    prompt_version=prompt_version,
                    gray_arm=gray_arm,
                    gray_bucket=gray_bucket,
                    progress=progress,
                )
            )
            result.trace_id = bound
            if cache_ready and self._cacheable(result):
                self.cache.put(key, copy.deepcopy(result))
            self._emit_turn(result)
            return result

    def _prompt_route(
        self, force_prompt: str, key: str
    ) -> tuple[str, str, int | None]:
        """One arm per key; eval passes ``force_prompt`` so it never grays."""
        if force_prompt:
            if force_prompt not in PROMPT_LAYOUTS:
                raise ValueError(
                    f"未知 prompt 版本 {force_prompt!r}，可选: "
                    + " / ".join(PROMPT_LAYOUTS)
                )
            arm = "candidate" if force_prompt != AGENT_PROMPT_VERSION else "base"
            return force_prompt, arm, None
        candidate = str(self.settings.gray_prompt_version or GRAY_PROMPT_VERSION)
        if candidate not in PROMPT_LAYOUTS:
            raise ValueError(
                f"未知 NL2SQL_GRAY_PROMPT_VERSION={candidate!r}，可选: "
                + " / ".join(PROMPT_LAYOUTS)
            )
        percent = float(self.settings.gray_percent or 0.0)
        if percent <= 0:
            return AGENT_PROMPT_VERSION, "base", None
        value = bucket(key or "anonymous", salt=self.settings.gray_salt or "gray-v1")
        on = in_gray(
            key or "anonymous", percent, salt=self.settings.gray_salt or "gray-v1"
        )
        return (
            candidate if on else AGENT_PROMPT_VERSION,
            "candidate" if on else "base",
            value,
        )

    def _cache_key(
        self,
        question: str,
        history: Any,
        prompt_version: str = AGENT_PROMPT_VERSION,
    ) -> str:
        turns = as_turns(history)[-max(1, int(self.settings.history_turns)):]
        context = [
            {"q": turn.resolved, "sql": turn.sql, "status": turn.status}
            for turn in turns
        ]
        policy = {
            "prompt": prompt_version,
            "mode": self.settings.mode,
            "few_shot": [
                self.settings.few_shot_enabled,
                self.settings.few_shot_max,
                self.settings.few_shot_max_chars,
                self.settings.few_shot_diversity,
                self.settings.experience_recall,
                self.settings.experience_vector,
            ],
            "value_alignment": self.settings.value_alignment,
            "clarify": self.settings.clarify,
            "rewriter": self.settings.rewriter,
            "history_turns": self.settings.history_turns,
            "carry_prior_sql": self.settings.carry_prior_sql,
            "max_value_probes": self.settings.max_value_probes,
            "dual_channel": [
                self.settings.dual_channel,
                self.settings.dual_channel_confidence_threshold,
                list(self.settings.dual_channel_categories),
            ],
            "max_rows": self.settings.max_rows,
            "max_repairs": self.settings.max_repairs,
        }
        return cache_key(
            question=question,
            schema_hash=self.schema_hash,
            principal=self.settings.principal_id,
            allow_sensitive=self.settings.allow_sensitive,
            policy=policy,
            context=context,
        )

    def _cacheable(self, result: AgentResult) -> bool:
        # Only a guarded, executed answer is worth replaying. Clarifications,
        # refusals and failures either cost nothing or must be re-decided.
        return (
            result.status == "ok"
            and bool(result.sql)
            and not result.error
            and not result.cached
        )

    def _uses_pipeline(self) -> bool:
        """Which route this turn takes.

        ``auto`` picks the pipeline only where the tool loop is the expensive path: a
        an online provider on a attached (non-demo) catalog. The packaged demo catalog and
        the offline mock model stay on the loop, so every published synthetic number
        is still measured against the code it was published from.
        """
        mode = str(self.settings.mode or "auto")
        if mode == "agent":
            return False
        if mode == "pipeline":
            return True
        return self.settings.provider == "openai" and self.settings.schema_override is not None

    def _run_pipeline(
        self,
        question: str,
        *,
        effective: str,
        rewrite: Rewrite,
        plan: ClarifyPlan | None,
        examples: str,
        few_shot_note: dict[str, Any],
        carry: str,
        observed: TurnObservation,
        started: float,
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> AgentResult | None:
        """Deterministic primary path; ``None`` means escalate to the tool loop.

        Every stage that can fail is bounded at one repair, and a repair that does not
        work hands the turn to the loop rather than producing a worse answer: the
        pipeline can lose a turn the loop would still win, but it never loses a turn
        the loop could not. Model calls are capped at four (generate, guard repair,
        execution repair, phrasing) against the loop's ``max_steps``.
        """
        trace: list[dict[str, Any]] = []
        tally = {"calls": 0, "tokens": 0}
        failure = {"model": ""}

        def record(
            tool: str,
            arguments: dict[str, Any],
            *,
            ok: bool,
            summary: str,
            stage: str,
            elapsed_ms: float,
            codes: Iterable[str] = (),
        ) -> None:
            entry = {
                "step": len(trace) + 1,
                "tool": tool,
                "arguments": to_jsonable(arguments),
                "ok": ok,
                "stage": stage,
                "summary": summary,
                "elapsed_ms": round(elapsed_ms, 2),
            }
            trace.append(entry)
            if progress is not None:
                # Same contract as the loop: a dead SSE consumer cannot change the turn.
                try:
                    progress(dict(entry))
                except Exception:  # noqa: BLE001
                    pass
            log_tool_call(
                tool=tool,
                step=entry["step"],
                ok=ok,
                stage=stage,
                elapsed_ms=entry["elapsed_ms"],
                guard_codes=list(codes),
                summary=summary,
                include_text=self.settings.log_include_text,
            )

        def call_tool(
            tool: str, arguments: dict[str, Any], *, logged: bool = True
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            began = time.perf_counter()
            payload = self.tools.call(tool, arguments)
            elapsed = (time.perf_counter() - began) * 1000
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            codes = _guard_issue_codes(data) if data else []
            if codes:
                observed.add_guard_codes(codes)
            if logged:
                record(
                    tool,
                    arguments,
                    ok=bool(payload.get("ok")),
                    summary=_summarize_tool_result(tool, payload),
                    stage=_trace_stage(tool, payload),
                    elapsed_ms=elapsed,
                    codes=codes,
                )
            return payload, data

        def escalate(reason: str) -> None:
            """The only way out of the pipeline that is not an answer."""
            log_tool_call(
                tool="pipeline",
                step=len(trace) + 1,
                ok=False,
                stage="pipeline_escalate",
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                summary=reason,
                include_text=self.settings.log_include_text,
            )

        def generate(extra: str = "") -> str:
            """One tool-free model call; the assistant turn is kept for the next one."""
            if extra:
                messages.append({"role": "user", "content": extra})
            began = time.perf_counter()
            try:
                response = self.llm.chat(messages)
            except LLMError as exc:
                failure["model"] = str(exc)
                record(
                    "generate_sql",
                    {"round": tally["calls"] + 1},
                    ok=False,
                    summary=f"模型调用失败: {exc}",
                    stage="model",
                    elapsed_ms=(time.perf_counter() - began) * 1000,
                )
                return ""
            content = str(response.content or "")
            tally["calls"] += 1
            tally["tokens"] += usage_tokens(response.usage)
            observed.add_usage(usage_breakdown(response.usage))
            messages.append({"role": "assistant", "content": content})
            candidate = extract_sql(content)
            record(
                "generate_sql",
                {"round": tally["calls"]},
                ok=bool(candidate),
                summary=f"sql={candidate[:160]}" if candidate else "模型没有给出可执行的 SQL",
                stage="" if candidate else "generation",
                elapsed_ms=(time.perf_counter() - began) * 1000,
            )
            return content

        payload, data = call_tool(
            "search_tables", {"query": effective, "top_k": MAX_PIPELINE_TABLES}
        )
        names = [
            str(item.get("name"))
            for item in (data or {}).get("tables", [])
            if str(item.get("name") or "").strip()
        ][:MAX_PIPELINE_TABLES]
        if not names:
            escalate(f"schema 检索没有候选表: {payload.get('error') or '空结果'}")
            return None

        # Value linking belongs in the retriever, not in the model's good intentions:
        # a model code in the question has to arrive as the real stored value, or the
        # pipeline matches it against the code column and answers an empty result set.
        # Probes are capped because a LIKE scan is the one stage here whose cost
        # depends on the customer's table.
        known = {item.name for item in self.catalog.tables.values()}
        for name in names:
            table = self.catalog.table(name)
            if table is not None:
                known.add(table.name)
                known.update(column.name for column in table.columns)
        mentions = value_mentions(effective, known)
        probe_hits: list[dict[str, Any]] = []
        probes = 0
        probe_started = time.perf_counter()
        for mention in mentions:
            for table_name, column_name in probe_candidates(self.catalog, names):
                if probes >= MAX_VALUE_PROBES:
                    break
                probes += 1
                _, value_data = call_tool(
                    "search_values",
                    {"table": table_name, "column": column_name, "query": mention, "limit": 5},
                    logged=False,
                )
                found = [str(item) for item in (value_data or {}).get("values", []) if str(item).strip()]
                if found:
                    probe_hits.append(
                        {"table": table_name, "column": column_name, "mention": mention, "values": found}
                    )
                    break
        if probes:
            record(
                "search_values",
                {"mentions": mentions, "probes": probes},
                ok=bool(probe_hits),
                summary=(
                    "; ".join(
                        f"{hit['table']}.{hit['column']}={hit['values'][:3]}" for hit in probe_hits
                    )
                    if probe_hits
                    else "取值探测无命中"
                ),
                stage="" if probe_hits else "value_probe",
                elapsed_ms=(time.perf_counter() - probe_started) * 1000,
            )

        prompt = generation_prompt(
            self.catalog,
            self.settings,
            names=names,
            examples=examples,
            carry=carry,
            values=value_probe_block(probe_hits),
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": effective},
        ]
        observed.few_shot = few_shot_note
        observed.prompt_hash = fingerprint(
            json.dumps(messages, ensure_ascii=False, sort_keys=True)
        )

        content = generate()
        sql = extract_sql(content)
        if failure["model"]:
            escalate(f"生成通道模型调用失败: {failure['model']}")
            return None
        if not sql:
            escalate("生成通道判定检索到的表不足以回答（返回空 SQL）")
            return None
        assumptions = assumptions_from(content)

        def guarded_execute(sql_text: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
            """Guard then execute; the guard is never bypassed, not even here."""
            _, guard_data = call_tool("validate_sql", {"sql": sql_text})
            if not guard_data.get("valid"):
                return "", {}, guard_data
            guarded = str(guard_data.get("sql") or sql_text)
            _, execution = call_tool("execute_sql", {"sql": guarded})
            return guarded, execution, guard_data

        current = sql
        guarded, execution, guard_data = guarded_execute(current)
        if not guarded:
            errors = [str(item.get("message") or "") for item in guard_data.get("errors", [])]
            content = generate(guard_repair_prompt(errors))
            current = extract_sql(content)
            if not current:
                escalate(
                    f"护栏拒绝后没有可修正的 SQL: {failure['model'] or '空 SQL'}"
                )
                return None
            assumptions = assumptions_from(content) or assumptions
            guarded, execution, guard_data = guarded_execute(current)
            if not guarded:
                escalate("同一条 SQL 连续两次未通过护栏")
                return None

        if not execution.get("success"):
            # The deterministic repairer goes first: it fixes the projection and
            # GROUP BY shapes the guard cannot see, and it costs no model call.
            error_text = str(execution.get("error") or "")
            _, repair_data = call_tool("repair_sql", {"sql": guarded, "error": error_text})
            candidate = str(repair_data.get("sql") or "") if repair_data.get("changed") else ""
            if candidate and candidate != guarded:
                retry_sql, retry_execution, _ = guarded_execute(candidate)
                if retry_sql and retry_execution.get("success"):
                    guarded, execution = retry_sql, retry_execution
            if not execution.get("success"):
                content = generate(execution_repair_prompt(error_text))
                candidate = extract_sql(content)
                if not candidate:
                    escalate(
                        f"执行失败后没有可修正的 SQL: {failure['model'] or error_text[:160]}"
                    )
                    return None
                assumptions = assumptions_from(content) or assumptions
                guarded, execution, _ = guarded_execute(candidate)
                if not execution.get("success"):
                    escalate(f"执行连续失败: {str(execution.get('error') or '')[:200]}")
                    return None

        guarded = str(execution.get("sql") or guarded)
        answer = ""
        began = time.perf_counter()
        try:
            response = self.llm.chat(phrasing_messages(effective, guarded, execution))
            tally["calls"] += 1
            tally["tokens"] += usage_tokens(response.usage)
            observed.add_usage(usage_breakdown(response.usage))
            answer = answer_from(str(response.content or ""))
            record(
                "phrase_answer",
                {"rows": int(execution.get("row_count") or 0)},
                ok=bool(answer),
                summary=f"answer={answer[:120]}" if answer else "措辞返回为空",
                stage="" if answer else "phrasing",
                elapsed_ms=(time.perf_counter() - began) * 1000,
            )
        except LLMError as exc:
            record(
                "phrase_answer",
                {"rows": int(execution.get("row_count") or 0)},
                ok=False,
                summary=f"模型调用失败: {exc}",
                stage="model",
                elapsed_ms=(time.perf_counter() - began) * 1000,
            )

        row_count = int(execution.get("row_count") or 0)
        notes = [
            "确定性管道：检索候选表 → 一次无工具生成 → 护栏 → 只读执行 → 结果措辞。"
        ]
        notes.extend(assumptions)
        if not answer:
            # The number is already verified by the execution; losing the phrasing
            # call costs the wording, never the result.
            answer = _fallback_answer(execution)
            notes.append("结果措辞调用未成功，答案由已执行结果直接给出。")

        result = self._result(
            question,
            status="ok",
            answer=answer,
            sql=guarded,
            execution=execution,
            confidence=0.8 if row_count else 0.6,
            assumptions=notes,
            steps=max(1, tally["calls"]),
            elapsed_ms=(time.perf_counter() - started) * 1000,
            tokens=tally["tokens"],
            trace=trace,
            rewrite=rewrite,
            clarify=plan,
            observed=observed,
        )
        result.mode = "pipeline"
        return result

    def _run_loop(
        self,
        question: str,
        history: Any = None,
        *,
        prompt_version: str = AGENT_PROMPT_VERSION,
        gray_arm: str = "base",
        gray_bucket: int | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentResult:
        # perf_counter, not monotonic: on Windows time.monotonic() only ticks every
        # 15.6ms, so a mock turn reported 0.0 ms and every latency metric was fiction.
        started = time.perf_counter()
        observed = TurnObservation(
            prompt_version=prompt_version,
            gray_arm=gray_arm,
            gray_bucket=gray_bucket,
        )
        question = (question or "").strip()
        rewrite = self._rewrite(question, history)
        if rewrite.ambiguous:
            clarification = Clarification(
                reason=rewrite.reason,
                question=_clarification_answer(rewrite),
                options=list(rewrite.options),
                missing_slots=list(rewrite.missing),
                code=CODE_ENTITY if "指代" in rewrite.reason else CODE_MODEL,
            )
            return self._result(
                question,
                status="needs_clarification",
                answer=clarification.question,
                rewrite=rewrite,
                confidence=0.2,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                clarification=clarification,
                observed=observed,
            )
        # The rewritten question is the only user message: the tool loop, the
        # guard and the evaluator all stay single-turn.
        alignment = self._align(rewrite.question)
        rewrite.value_alignment = (
            alignment.to_dict() if alignment.source == "dictionary" else None
        )
        if alignment.ambiguous:
            answer = _value_clarification_answer(alignment)
            return self._result(
                question,
                status="needs_clarification",
                answer=answer,
                rewrite=rewrite,
                confidence=0.2,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                observed=observed,
                clarification=Clarification(
                    reason="简称在本库里对应多个取值，需要确认问的是哪一个",
                    question=answer,
                    options=alignment.options,
                    missing_slots=[item.mention for item in alignment.ambiguous],
                    code=CODE_ENTITY,
                ),
            )
        # A resolved alias appends the stored value instead of editing the user's
        # wording, so an unmatched question reaches generation byte-for-byte unchanged.
        rewrite.question = alignment.effective
        # Triggered on the user's own wording: the appended 值对齐 block carries
        # stored values, and those must not read as a口径 the user chose.
        plan = self.clarifier.decide(alignment.question, slots=rewrite.slots)
        if plan.blocking:
            trigger = plan.blocking[0]
            clarification = trigger.as_clarification()
            return self._result(
                question,
                status="needs_clarification",
                answer=clarification_text(clarification),
                rewrite=rewrite,
                confidence=0.2,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                clarification=clarification,
                clarify=plan,
                observed=observed,
            )
        effective = rewrite.question
        classification = classify(effective)
        examples, few_shot_note = self._few_shot_observed(effective, classification.category)
        carry = self._carry_prior(history)
        if self._uses_pipeline():
            # The pipeline is the primary path: it decides the table set itself and
            # spends 2-4 model calls instead of up to max_steps tool rounds. Returning
            # None means it could not reach an executed answer, and the loop below
            # gets the turn with its full step budget - nothing is lost, only spent.
            outcome = self._run_pipeline(
                question,
                effective=effective,
                rewrite=rewrite,
                plan=plan,
                examples=examples,
                few_shot_note=few_shot_note,
                carry=carry,
                observed=observed,
                started=started,
                progress=progress,
            )
            if outcome is not None:
                return outcome
        system_prompt = self._system_prompt(
            examples, layout=PROMPT_LAYOUTS[prompt_version]
        )
        if carry:
            # The carried SQL goes in the system block: the user message stays
            # exactly the effective question, so slot-based readers (the offline
            # model, the evaluator) cannot pick the previous question's filters.
            system_prompt = f"{system_prompt}\n\n{carry}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": effective},
        ]
        observed.few_shot = few_shot_note
        # The hash covers the prompt actually sent for this turn — system block plus
        # the effective question — so it moves when the catalog, the few-shot set or
        # the wording moves, and stays put while the tool conversation grows.
        observed.prompt_hash = fingerprint(json.dumps(messages, ensure_ascii=False, sort_keys=True))
        trace: list[dict[str, Any]] = []
        last_sql: str | None = None
        last_execution: dict[str, Any] | None = None
        response: LLMResponse | None = None
        tokens = 0
        repairs = 0
        nudged = False
        nudged_for_execution = False
        exploration_streak = 0
        nudged_probing = False
        explored_after_execution = 0
        nudged_finalize = False
        guard_codes: set[str] = set()
        repair_limit = max(1, self.settings.max_repairs)

        for step in range(1, max(2, self.settings.max_steps) + 1):
            try:
                response = self.llm.chat(messages, tools=self.tools.specs())
            except LLMError as exc:
                return self._result(
                    question,
                    status="failed",
                    answer=f"模型调用失败: {exc}",
                    error=str(exc),
                    steps=step - 1,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    tokens=tokens,
                    trace=trace,
                    rewrite=rewrite,
                    observed=observed,
                )
            tokens += usage_tokens(response.usage)
            observed.add_usage(usage_breakdown(response.usage))

            if not response.tool_calls:
                wants_clarification = _wants_clarification(response.content)
                wants_stop = wants_clarification or _wants_human(response.content)
                if (
                    not nudged
                    and not trace
                    and self.settings.provider != "mock"
                    and (wants_stop or _claims_ok(response.content))
                ):
                    # Real models sometimes ask for clarification — or, once an approved
                    # schema rule names the table for them, answer outright — without ever
                    # opening the catalog; force one schema lookup so the question is
                    # judged against actual tables instead of assumptions.
                    nudged = True
                    messages.append({"role": "assistant", "content": response.content or ""})
                    messages.append({"role": "user", "content": _CLARIFY_NUDGE})
                    continue
                if (
                    not nudged_for_execution
                    and last_execution is None
                    and self.settings.provider != "mock"
                    and (wants_stop or _claims_ok(response.content))
                    and any(item.get("tool") == "get_table_schema" for item in trace)
                ):
                    # The model already inspected the schema — and then either asked the
                    # user or asserted a number without running anything. One nudge turns
                    # that draft into a real, verifiable result instead of a guessed one.
                    nudged_for_execution = True
                    messages.append({"role": "assistant", "content": response.content or ""})
                    messages.append({"role": "user", "content": _EXECUTE_NUDGE})
                    continue
                return self._finalize(
                    question,
                    response,
                    last_sql,
                    last_execution,
                    trace,
                    step,
                    started,
                    tokens=tokens,
                    rewrite=rewrite,
                    plan=plan,
                    guard_codes=guard_codes,
                    observed=observed,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )

            for call in response.tool_calls:
                call_started = time.perf_counter()
                if call.name == "repair_sql" and repairs >= repair_limit:
                    payload = {
                        "ok": False,
                        "error": (
                            f"确定性修复已达上限 {repair_limit} 轮，不再接受新的修复调用。"
                            "请停止修复，输出 status=needs_human 并说明未解决的报错。"
                        ),
                        "data": None,
                    }
                else:
                    payload = self.tools.call(call.name, call.arguments)
                if call.name == "repair_sql":
                    repairs += 1
                    observed.note_repair(bool(payload.get("ok")))
                payload["tool"] = call.name
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(to_jsonable(payload), ensure_ascii=False, default=str),
                    }
                )
                call_elapsed = round((time.perf_counter() - call_started) * 1000, 2)
                call_stage = _trace_stage(call.name, payload)
                call_summary = _summarize_tool_result(call.name, payload)
                entry = {
                    "step": step,
                    "tool": call.name,
                    "arguments": to_jsonable(call.arguments),
                    "ok": bool(payload.get("ok")),
                    "stage": call_stage,
                    "summary": call_summary,
                    "elapsed_ms": call_elapsed,
                }
                trace.append(entry)
                if progress is not None:
                    # Observational only: a stalled or disconnected SSE consumer must
                    # never change the turn, so a raising callback is swallowed here.
                    try:
                        progress(dict(entry))
                    except Exception:  # noqa: BLE001
                        pass
                data = payload.get("data") if payload.get("ok") else None
                call_codes: list[str] = []
                if isinstance(data, dict):
                    call_codes = _guard_issue_codes(data)
                    guard_codes.update(call_codes)
                    observed.add_guard_codes(call_codes)
                    if call.name == "validate_sql" and data.get("sql"):
                        last_sql = str(data["sql"])
                    elif call.name == "execute_sql":
                        if data.get("sql"):
                            last_sql = str(data["sql"])
                        if data.get("success"):
                            last_execution = data
                    elif call.name == "repair_sql" and data.get("sql"):
                        last_sql = str(data["sql"])
                if call.name in _EXPLORATORY_TOOLS:
                    exploration_streak += 1
                elif call.name in {"validate_sql", "execute_sql", "repair_sql"}:
                    exploration_streak = 0
                if call.name in _EXPLORATORY_TOOLS and last_execution is not None:
                    explored_after_execution += 1
                log_tool_call(
                    tool=call.name,
                    step=step,
                    ok=bool(payload.get("ok")),
                    stage=call_stage,
                    elapsed_ms=call_elapsed,
                    guard_codes=call_codes,
                    summary=call_summary,
                    include_text=self.settings.log_include_text,
                )

            if (
                not nudged_probing
                and self.settings.max_value_probes > 0
                and self.settings.provider != "mock"
                and last_execution is None
                and exploration_streak >= self.settings.max_value_probes
            ):
                # On a 500-table catalog a model can spend the whole budget
                # exploring - re-listing tables, re-reading schema, trying values -
                # and never write a query. One nudge converts probing into an
                # attempt that can be repaired or refused. The streak counts every
                # exploratory tool: the loop actually observed was
                # search_tables/get_table_schema, which the old value-only counter
                # never saw, so the budget expired with no SQL at all.
                nudged_probing = True
                exploration_streak = 0
                messages.append({"role": "user", "content": _PROBE_NUDGE})

            if (
                not nudged_finalize
                and self.settings.provider != "mock"
                and last_execution is not None
                and explored_after_execution >= 2
            ):
                # Two exploratory calls after a successful execution: one can be a
                # legitimate column re-check, the pattern after that is wandering.
                nudged_finalize = True
                explored_after_execution = 0
                messages.append({"role": "user", "content": _FINALIZE_NUDGE})

        if last_execution:
            # The query ran and returned data before the step limit; discarding
            # that result would turn a usable answer into a false failure.
            return self._result(
                question,
                status="ok",
                answer=_fallback_answer(last_execution),
                sql=last_sql,
                execution=last_execution,
                confidence=0.4,
                assumptions=["达到最大工具调用轮次，返回最后一次成功执行的结果。"],
                steps=max(2, self.settings.max_steps),
                elapsed_ms=(time.perf_counter() - started) * 1000,
                tokens=tokens,
                trace=trace,
                rewrite=rewrite,
                observed=observed,
            )
        if (last_sql or "").strip():
            # A query is in hand and the budget is out. Run it here instead of
            # throwing the answer away: the tool re-checks the guard and executes
            # read-only under the same row cap and timeout, so the number still
            # comes from a real execution - that is the point of the "no unexecuted
            # claims" rule, not something it works around. Without this, a model
            # that never calls execute_sql turns a correct query into no answer.
            attempt = self.tools.call("execute_sql", {"sql": last_sql})
            forced = attempt.get("data") if attempt.get("ok") else None
            trace.append(
                {
                    "step": max(2, self.settings.max_steps),
                    "tool": "execute_sql",
                    "arguments": {"sql": last_sql},
                    "ok": bool(forced),
                    "stage": "runtime_forced",
                    "summary": (
                        _summarize_tool_result("execute_sql", attempt)
                        if forced
                        else f"运行时兜底执行失败: {attempt.get('error') or '未通过护栏'}"
                    ),
                    "elapsed_ms": float(attempt.get("elapsed_ms") or 0.0),
                }
            )
            if forced:
                return self._result(
                    question,
                    status="ok",
                    answer=_fallback_answer(forced),
                    sql=last_sql,
                    execution=forced,
                    confidence=0.45,
                    assumptions=[
                        "达到最大工具调用轮次，由运行时执行最后一条通过护栏的 SQL 取数。"
                    ],
                    steps=max(2, self.settings.max_steps),
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    tokens=tokens,
                    trace=trace,
                    rewrite=rewrite,
                    observed=observed,
                )
        return self._result(
            question,
            status="max_steps",
            answer="Agent 达到最大工具调用轮次，未能形成结论。",
            sql=last_sql,
            execution=last_execution,
            steps=max(2, self.settings.max_steps),
            elapsed_ms=(time.perf_counter() - started) * 1000,
            tokens=tokens,
            trace=trace,
            error="max_steps_exceeded",
            rewrite=rewrite,
            observed=observed,
        )

    def _carry_prior(self, history: Any) -> str:
        """Hand a follow-up turn the SQL that already worked one turn ago.

        Without it every narrowing follow-up ("只看华东地区的") re-explores the
        catalog from zero: 12 steps, 50k tokens, and on a 500-table database it
        more often than not runs out of steps. The carried SQL is only a starting
        point - it still goes through the guard and executes for real.
        """
        if not self.settings.carry_prior_sql:
            return ""
        sql = ""
        prior_question = ""
        for turn in reversed(list(as_turns(history))):
            if turn.status == "ok" and str(turn.sql or "").strip():
                sql = str(turn.sql).strip()
                prior_question = turn.resolved
                break
        if not sql or len(sql) > 2000:
            return ""
        return (
            f"上一轮已在这套表上成功执行过一条查询：\n问：{prior_question}\nSQL：{sql}\n"
            "本轮如果只是换时间/地区/维度/排序/取值，请在上面这条 SQL 上做最小改动，"
            "然后直接 execute_sql；口径不同或字段对不上就重新检索，不要照抄。"
        )

    def _rewrite(self, question: str, history: Any) -> Rewrite:
        turns = as_turns(history)
        if not turns:
            return Rewrite(question=question, slots=extract_slots(question), source="none")
        # Picking an option is a menu selection, not anaphora: it resolves with the
        # rewriter switched off too, so this runs before the disabled check.
        reply = self._resolve_reply(question, turns[-1])
        if reply is not None:
            return reply
        if not self.settings.multi_turn_enabled or self.settings.rewriter == "off":
            return Rewrite(question=question, slots=extract_slots(question), source="off")
        return self.rewriter.rewrite(question, turns)

    def _resolve_reply(self, question: str, last: Turn) -> Rewrite | None:
        payload = last.clarification or {}
        options = [str(item) for item in (payload.get("options") or []) if str(item).strip()]
        if not options:
            return None
        picked = resolve_clarification_reply(question, options)
        if not picked:
            return None
        if str(payload.get("code") or "") == CODE_ENTITY:
            # Entity options name a stored value, not a question. The pick replaces
            # the ambiguous mention: leaving it in place would make the aligner
            # report the same collision again, so the second turn could never end.
            value, sep, rest = picked.partition("（")
            column = rest.rstrip("）").strip() if sep else ""
            mention = str((payload.get("missing_slots") or [""])[0])
            if mention and mention in last.question:
                resolved = last.question.replace(mention, value.strip())
                if resolved == last.question and column:
                    # The stored value *is* the short form the user typed ("轴承" is a
                    # material called 轴承). Replacing it changes nothing, so the pick
                    # has to travel as the category word the aligner reads instead.
                    word = _kind_word(self.aligner, column)
                    if word:
                        resolved = last.question.replace(mention, f"{word}{mention}", 1)
            elif column:
                resolved = f"{last.question}\n\n[值对齐] {column}={value.strip()}"
            else:
                resolved = f"{last.question}\n\n[值对齐] {value.strip()}"
        else:
            resolved = picked
        return Rewrite(
            question=resolved,
            slots=extract_slots(resolved),
            source="clarify_reply",
            inherited=[last.question],
        )

    def _align(self, question: str) -> Alignment:
        """Value alignment is a separate layer: it fixes *values*, the rewriter fixes *referents*."""
        if self.aligner is None or self.settings.value_alignment in {"", "off", "none"}:
            return Alignment.passthrough(question)
        return self.aligner.align(question)

    def _verify(self, result: AgentResult) -> AgentResult:
        """Second channel: generate independently, execute for real, compare result sets.

        Agreement is evidence; disagreement is not a coin flip to resolve silently,
        so it goes to a human instead of picking a winner.
        """
        if self.second_opinion is None:
            return result
        question = result.effective_question or result.question
        if not should_verify(
            self.settings,
            status=result.status,
            confidence=result.confidence,
            category=classify(question).category,
            executed=bool(result.columns),
        ):
            return result

        started = time.perf_counter()
        opinion = self._second_opinion(question, result.sql or "")
        channels: list[dict[str, Any]] = [
            {
                "role": "primary",
                "sql": result.sql,
                "row_count": result.row_count,
                "status": "executed",
            }
        ]
        entry: dict[str, Any] = {"role": "second", "source": opinion.source, "sql": opinion.sql or None}

        if not opinion.sql:
            entry.update({"status": "not_generated", "agreement": None, "error": opinion.error})
            result.channels = channels + [entry]
            result.tokens += opinion.tokens
            result.elapsed_ms = round(result.elapsed_ms + (time.perf_counter() - started) * 1000, 2)
            result.assumptions.append(
                f"复核通道未能给出 SQL（{opinion.error}），本次结果未经交叉验证。"
            )
            return result

        payload = self.tools.call("execute_sql", {"sql": opinion.sql})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if data is None or not data.get("success"):
            reason = (data or {}).get("error") or payload.get("error") or "复核通道没有返回结果"
            entry.update(
                {
                    "status": "guard_failed" if (data or {}).get("stage") == "guard" else "execution_failed",
                    "agreement": None,
                    "error": str(reason)[:300],
                }
            )
            result.channels = channels + [entry]
            result.tokens += opinion.tokens
            result.elapsed_ms = round(result.elapsed_ms + (time.perf_counter() - started) * 1000, 2)
            result.assumptions.append(
                "复核通道的 SQL 未通过护栏或执行失败，本次结果未经交叉验证。"
            )
            return result

        second_rows = list(data.get("rows") or [])
        agreed = rows_equal(list(result.rows), second_rows)
        entry.update(
            {
                "status": "executed",
                "row_count": int(data.get("row_count") or 0),
                "agreement": agreed,
            }
        )
        result.channels = channels + [entry]
        result.tokens += opinion.tokens
        result.elapsed_ms = round(result.elapsed_ms + (time.perf_counter() - started) * 1000, 2)

        if agreed:
            result.confidence = min(
                0.95, result.confidence + max(0.0, self.settings.dual_channel_agree_boost)
            )
            result.assumptions.append("双通道独立生成并执行，结果集一致。")
            return result

        result.status = "needs_human"
        result.error = "dual_channel_disagreement"
        result.confidence = min(result.confidence, 0.25)
        result.answer = (
            f"两个独立通道的结果不一致（主通道 {result.row_count} 行，复核通道 {entry['row_count']} 行："
            f"{describe_diff(list(result.rows), second_rows)}），需要人工确认口径，系统不默认取舍。"
        )
        # The primary result was "ok", so nothing queued it yet; a disagreement is
        # exactly the case a human has to close out.
        self._record_failure(result)
        return result

    def _second_opinion(self, question: str, sql: str) -> SecondOpinion:
        generator = self.second_opinion
        assert generator is not None
        tables: list[str] = []
        if self.guard is not None and sql:
            tables = [str(name) for name in self.guard.check(sql).tables]
        try:
            return generator.generate(question, tables)
        except Exception as exc:  # noqa: BLE001 - a broken vote must not kill a working answer
            return SecondOpinion(source=getattr(generator, "source", "second"), error=str(exc)[:300])

    def _finalize(
        self,
        question: str,
        response: LLMResponse,
        last_sql: str | None,
        last_execution: dict[str, Any] | None,
        trace: list[dict[str, Any]],
        steps: int,
        started: float,
        *,
        tokens: int = 0,
        rewrite: Rewrite | None = None,
        plan: ClarifyPlan | None = None,
        guard_codes: Iterable[str] = (),
        observed: TurnObservation | None = None,
    ) -> AgentResult:
        payload = parse_json_object(response.content) or {}
        answer = str(payload.get("answer") or response.content or "").strip()
        status = str(payload.get("status") or "").strip().lower()
        sql = payload.get("sql") or last_sql
        confidence = _as_float(payload.get("confidence"), default=0.5)
        assumptions = payload.get("assumptions")
        assumptions = [str(item) for item in assumptions] if isinstance(assumptions, list) else []

        if status not in _ALLOWED_STATUS:
            if last_execution:
                status = "ok"
            elif any(word in answer for word in ("请补充", "无法", "不包含", "不支持")):
                status = "needs_clarification"
            else:
                status = "needs_human"

        if status == "ok" and not last_execution:
            status = "needs_clarification"
            confidence = min(confidence, 0.4)
            # The model asserted a conclusion without executing anything, so every digit
            # in its answer is unverified regardless of which counter word follows it;
            # the unit whitelist further down is only a backstop for other statuses.
            if any(char.isdigit() for char in answer):
                answer = "尚未执行查询，没有可验证的数字结果。请补充问题范围后重试。"
            answer = answer or "模型没有执行 SQL，无法给出可验证的结论。"
        if status == "ok" and last_execution:
            answer = answer or _fallback_answer(last_execution)
        if status != "ok" and last_execution is None and _UNVERIFIED_CLAIM.search(answer):
            answer = "尚未执行查询，没有可验证的数字结果。请补充问题范围后重试。"
            confidence = min(confidence, 0.2)
        if status in {"needs_clarification", "refused"} and last_execution is None:
            # A drafted-but-unexecuted SQL is not a result; exposing it next to a
            # clarification makes it look verified.
            sql = None

        error = ""
        if status not in {"ok", "needs_clarification", "refused"}:
            error = str(payload.get("error") or "answer_not_verified")

        clarification: Clarification | None = None
        alternatives: list[str] = list(plan.alternatives) if plan else []
        if status == "needs_clarification":
            # Every clarification leaves this layer with a code and something the
            # user can send back, so 「请补充」 alone is never a final answer.
            clarification, _ = self.clarifier.enrich(
                Clarification(reason=answer, question=answer, code=CODE_MODEL),
                question=rewrite.question if rewrite else question,
                plan=plan,
                guard_codes=guard_codes,
                keep_open=bool(answer.strip()),
            )
            answer = with_options(answer, clarification.options)
            clarification.question = answer

        return self._result(
            question,
            status=status,
            answer=answer,
            sql=sql,
            execution=last_execution,
            confidence=confidence,
            assumptions=assumptions,
            steps=steps,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            tokens=tokens,
            trace=trace,
            error=error,
            rewrite=rewrite,
            clarification=clarification,
            alternatives=alternatives,
            clarify=plan,
            observed=observed,
        )

    def _result(
        self,
        question: str,
        *,
        status: str,
        answer: str,
        sql: str | None = None,
        execution: dict[str, Any] | None = None,
        confidence: float = 0.0,
        assumptions: list[str] | None = None,
        steps: int = 0,
        elapsed_ms: float = 0.0,
        tokens: int = 0,
        trace: list[dict[str, Any]] | None = None,
        error: str = "",
        rewrite: Rewrite | None = None,
        clarification: Clarification | None = None,
        alternatives: list[str] | None = None,
        clarify: ClarifyPlan | None = None,
        observed: TurnObservation | None = None,
    ) -> AgentResult:
        rewrite = rewrite or Rewrite(question=question)
        result = AgentResult(
            question=question,
            status=status,
            answer=answer,
            sql=sql,
            columns=list((execution or {}).get("columns") or []),
            rows=list((execution or {}).get("rows") or []),
            row_count=int((execution or {}).get("row_count") or 0),
            confidence=confidence,
            steps=steps,
            elapsed_ms=round(elapsed_ms, 2),
            tokens=tokens,
            tool_trace=trace or [],
            assumptions=assumptions or [],
            error=error,
            effective_question=rewrite.question,
            rewrite_source=rewrite.source,
            alternatives=list(alternatives or []),
            clarify=plan_dict(clarify),
            inherited_slots=list(rewrite.inherited),
            slots=rewrite.slots,
            clarification=clarification,
            value_alignment=rewrite.value_alignment,
        )
        if observed is not None:
            result.prompt_version = observed.prompt_version
            result.gray_arm = observed.gray_arm
            result.gray_bucket = observed.gray_bucket
            result.prompt_hash = observed.prompt_hash
            result.few_shot = dict(observed.few_shot)
            result.guard_codes = list(observed.guard_codes)
            result.repair_calls = observed.repair_calls
            result.repair_errors = observed.repair_errors
            result.prompt_tokens = observed.prompt_tokens
            result.completion_tokens = observed.completion_tokens
            result.cached_tokens = observed.cached_tokens
        if status in _FAILURE_STATUS:
            self._record_failure(result)
        return result

    def _record_failure(self, result: AgentResult) -> None:
        if not self.settings.capture_failures or self.store is None:
            return
        question = result.effective_question or result.question
        try:
            self.store.record_failure(
                question,
                reason=result.status,
                category=classify(question).category,
                sql=result.sql or "",
                detail=(result.error or result.answer)[:200],
                error_code=(result.guard_codes[0] if result.guard_codes else ""),
                schema_hash=self.schema_hash,
            )
        except Exception:  # noqa: BLE001 - queueing must never change the answer
            return

    def _few_shot_block(self, question: str, category: str = "") -> str:
        return self._few_shot_observed(question, category)[0]

    def _few_shot_observed(self, question: str, category: str = "") -> tuple[str, dict[str, Any]]:
        """The block plus what happened: 「命中率」 must be answerable from the log."""
        settings = self.settings
        if not settings.few_shot_enabled or self.store is None:
            return "", {"considered": False, "candidates": 0, "injected": 0, "chars": 0}
        limit = max(0, settings.few_shot_max)
        budget = max(0, settings.few_shot_max_chars)
        if limit == 0 or budget == 0:
            return "", {"considered": False, "candidates": 0, "injected": 0, "chars": 0}
        try:
            recall = self._recall_for()
            if recall is None:
                return "", {"considered": False, "candidates": 0, "injected": 0, "chars": 0}
            examples = recall.search(
                question,
                schema_hash=self.schema_hash,
                top_k=limit,
                category=category,
                category_boost=settings.category_boost,
            )
            note = {"recall": recall.last.to_dict()}
        except Exception:  # noqa: BLE001 - retrieval is an enhancement, not a dependency
            return "", {"considered": True, "candidates": 0, "injected": 0, "chars": 0, "error": True}

        blocks: list[str] = []
        used = 0
        for item in examples[:limit]:
            block = f"问：{str(item.get('question') or '').strip()}\nSQL：{str(item.get('sql') or '').strip()}"
            if len(block) > budget - used and blocks:
                break
            if len(block) > budget:
                continue
            blocks.append(block)
            used += len(block)
        return "\n".join(blocks), {
            "considered": True,
            "candidates": len(examples),
            "injected": len(blocks),
            "chars": used,
            **note,
        }

    def _model_label(self) -> str:
        return self.settings.model if self.settings.provider == "openai" else "RuleBasedLLM"

    def _emit_turn(self, result: AgentResult) -> None:
        """One line per turn carrying what the result object cannot: process, not verdict."""
        log_turn(
            provider=self.settings.provider,
            model=self._model_label(),
            prompt_version=result.prompt_version,
            prompt_hash=result.prompt_hash,
            gray_arm=result.gray_arm,
            gray_bucket=result.gray_bucket,
            schema_hash=self.schema_hash,
            dialect=self.catalog.dialect,
            principal=self.settings.principal_id,
            status=result.status,
            steps=result.steps,
            elapsed_ms=result.elapsed_ms,
            tokens=result.tokens,
            row_count=result.row_count,
            category=classify(result.effective_question or result.question).category,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cached_tokens=result.cached_tokens,
            cached=result.cached,
            cache_age_sec=float((result.cache or {}).get("age_sec") or 0.0),
            cache_saved_tokens=int((result.cache or {}).get("saved_tokens") or 0),
            question=result.question,
            answer=result.answer,
            sql=result.sql or "",
            error=result.error,
            guard_codes=result.guard_codes,
            clarify_codes=_clarify_codes(result),
            few_shot=result.few_shot,
            repair_calls=result.repair_calls,
            repair_errors=result.repair_errors,
            dual_channel=_dual_channel_note(result.channels),
            value_alignment=result.value_alignment,
            include_text=self.settings.log_include_text,
        )

    def _system_prompt(self, examples: str = "", *, layout: str = "few_shot_tail") -> str:
        enabled = self.catalog.enabled_tables()
        # A real ERP catalog has hundreds of tables; listing them all would blow up
        # the prompt and defeat the point of search_tables, so only small catalogs
        # get the full list.
        if len(enabled) <= 40:
            table_lines = "\n".join(f"- {table.name}: {table.description}" for table in enabled)
            table_note = "可用表（完整字段必须通过 get_table_schema 获取）:"
        else:
            table_lines = (
                f"- 本库共 {len(enabled)} 张启用表，表清单不在此列出；"
                "必须先调用 search_tables，再用 get_table_schema 确认字段。"
            )
            table_note = "表目录:"
        rules = "\n".join(f"- {rule}" for rule in self.catalog.business_rules)
        if rules:
            # An approved mapping names a table; it is not a result. Without this line a
            # live run answered an aggregate straight off the 业务口径 block and never
            # called a tool, which the digit scrub below then had to catch.
            rules += (
                "\n（以上只决定用哪张表/哪个字段，不含任何数据；"
                "仍然必须先 search_tables + get_table_schema，数字只能来自 execute_sql 的结果）"
            )
        dialect = get_dialect(self.catalog.dialect)
        dialect_name = dialect.label
        row_limit = dialect.row_limit_hint
        forbidden = dialect.forbidden
        few_shot = (
            f"""
参考经验（人工验证过的相似问法，只作 SQL 写法参考；表名和字段仍必须用 get_table_schema 确认，
照抄前先确认口径与当前问题一致）:
{examples}
"""
            if examples
            else ""
        )
        head = f"""你是一个面向离散制造 ERP 的 NL2SQL Agent。你通过工具调用完成问数，而不是凭记忆猜表名或字段名。

数据库: {dialect_name}（只读）
今天/基准日期: {self.catalog.as_of_date or self.settings.demo_today}

{table_note}
{table_lines}

业务口径:
{rules}

工作流（必须遵守）:
1. 先调用 search_tables，找到与问题相关的候选表。
2. 调用 get_table_schema 查看真实字段、禁用/敏感标记、必要 JOIN 过滤条件和关系。禁止猜测字段。
3. 需要更多同类问法示例时调用 search_experience，只返回人工验证过的经验；候选经验不可信。
4. 过滤中文枚举值（客户、物料、状态、区域）不确定时，调用 search_values 对齐真实值。
5. 只生成单条 {dialect_name} SELECT/WITH 查询。列必须显式列出；金额保留 2 位，比率保留 4 位；{row_limit}。
6. 执行前必须调用 validate_sql。校验失败就根据错误修正，不能绕过护栏。
7. 校验通过后调用 execute_sql。执行报错时调用 repair_sql，然后重新 execute_sql；确定性修复最多 {self.settings.max_repairs} 轮，超出后系统会直接拒绝继续修复。
8. 如果仍无法完成，转入人工，不要编造数据。

硬性约束:
- 只允许查询 schema 中启用的表和字段；禁用表、禁用字段、敏感字段一律不可查询。
- {forbidden}。
- 经验记忆只作为 few-shot 参考；没有执行结果时不得声称“查询成功”。
- 涉及聚合时注意 JOIN 扇出，避免重复计数。
- 相对时间按基准日期换算（今年=所在自然年，本月=所在月，上月=前一个月）；业务名词按 schema 注释中最接近的字段理解。
- 禁止在未调用 search_tables 和 get_table_schema 前返回 needs_clarification / needs_human。口径差异可按合理假设执行，并把假设写入 assumptions；只有缺少无法推断的具体过滤值（如具体客户编码、具体物料编码）时才澄清或转人工。
- 过滤值必须来自 search_values 的真实返回值。若目标值不在返回值中，不要臆造枚举值（例如 State 只返回 1/2/3/4 时不要写 State=5）；改用已确认字段，或在 assumptions 中说明口径。

"""
        contract = f"""最终输出必须是一个 JSON 对象，不要输出 Markdown:
{{
  "status": "ok | needs_clarification | needs_human | refused",
  "answer": "面向业务人员的简洁回答；其中的数字只能来自 execute_sql 的返回结果",
  "sql": "实际执行并得到结果的 SQL，未执行则为 null",
  "confidence": 0.0,
  "assumptions": ["必要的口径假设"]
}}
"""
        # ``few_shot_tail`` is the shipped layout: the JSON contract sits at a stable
        # offset so a provider prefix cache can hit. The candidate arm moves the
        # examples before the contract; that placement is the only difference.
        if layout == "few_shot_early":
            block = few_shot.lstrip("\n")
            sep = "\n" if block else ""
            return head + block + sep + contract
        return head + contract + few_shot


def _clarification_answer(rewrite: Rewrite) -> str:
    parts = [f"这个问题需要再确认一下：{rewrite.reason or '上下文不足'}。"]
    if rewrite.options:
        parts.append("你可以直接这样问：")
        parts.extend(f"{index}. {option}" for index, option in enumerate(rewrite.options, 1))
    else:
        parts.append("请补充必要的条件后重新提问。")
    return "\n".join(parts)


def _value_clarification_answer(alignment: Alignment) -> str:
    mentions = "、".join(f"“{item.mention}”" for item in alignment.ambiguous)
    parts = [f"这个问题需要再确认一下：{mentions} 在本库里对应多个取值，直接猜会给出错的数字。"]
    if alignment.options:
        parts.append("你可以直接这样问：")
        parts.extend(f"{index}. {option}" for index, option in enumerate(alignment.options, 1))
    return "\n".join(parts)


def _build_policy(settings: Settings) -> ClarificationPolicy:
    """The packaged noun table is a constant; a deployment points NL2SQL_METRIC_GLOSSARY at its own."""
    path = settings.metric_glossary_path
    glossary = MetricGlossary.load(path) if path is not None else None
    return ClarificationPolicy(glossary, as_of=settings.demo_today, mode=settings.clarify)


def _kind_word(aligner: ValueAligner | None, column: str) -> str:
    """The category word ("物料") that tells the aligner which collision to keep."""
    if aligner is None or not column:
        return ""
    for entry in aligner.dictionary.entries:
        if entry.column != column:
            continue
        words = KIND_QUALIFIERS.get(entry.kind) or ()
        return words[0] if words else ""
    return ""


def plan_dict(plan: ClarifyPlan | None) -> dict[str, Any] | None:
    if plan is None or not plan.triggers:
        return None
    return plan.to_dict()


def _clarify_codes(result: AgentResult) -> list[str]:
    """Every code that shaped this turn's clarification, whichever layer raised it."""
    codes: list[str] = []
    for code in (
        list((result.clarify or {}).get("codes") or [])
        + ([result.clarification.code] if result.clarification else [])
    ):
        if code and code not in codes:
            codes.append(str(code))
    return codes


def _dual_channel_note(channels: list[dict[str, Any]]) -> dict[str, Any]:
    second = next((item for item in channels if item.get("role") == "second"), None)
    if second is None:
        return {"ran": False}
    return {
        "ran": True,
        "status": str(second.get("status") or ""),
        "agreement": second.get("agreement"),
        "source": str(second.get("source") or ""),
    }


def _guard_issue_codes(data: dict[str, Any]) -> list[str]:
    """Codes the guard raised in this turn, so a blocked run is classified, not guessed."""
    out: list[str] = []
    for key in ("errors", "warnings"):
        for issue in data.get(key) or []:
            if isinstance(issue, dict) and issue.get("code"):
                out.append(str(issue["code"]))
    inner = data.get("guard")
    if isinstance(inner, dict):
        out.extend(_guard_issue_codes(inner))
    return out


def _trace_stage(name: str, payload: dict[str, Any]) -> str:
    """Which layer of the stack this tool call failed in, for offline attribution.

    ``guard`` and ``execution`` are the two the eval report needs to tell apart: a
    SQL the guard rejected is a guard/granularity problem, a SQL that reached the
    database and errored is a dialect problem, and neither is the same failure as
    "executed fine, wrong number". Returns "" when the call succeeded.
    """
    if not payload.get("ok"):
        return "tool"
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    if data.get("stage"):
        return str(data["stage"])
    if name == "validate_sql" and data.get("valid") is False:
        return "guard"
    if name == "execute_sql" and data.get("success") is False:
        return "execution"
    return ""


def _summarize_tool_result(name: str, payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return f"error: {payload.get('error', 'tool failed')}"
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return "ok"
    if name == "search_tables":
        return "tables=" + ",".join(item.get("name", "") for item in data.get("tables", [])[:6])
    if name == "get_table_schema":
        return "schemas=" + ",".join(item.get("name", "") for item in data.get("tables", []))
    if name == "search_values":
        return f"values={data.get('values', [])[:5]}"
    if name == "validate_sql":
        if data.get("valid"):
            return f"valid, tables={data.get('tables')}"
        return "invalid: " + "; ".join(
            str(item.get("message", "")) for item in data.get("errors", [])[:3]
        )
    if name == "execute_sql":
        if data.get("success"):
            return f"rows={data.get('row_count')}, truncated={data.get('truncated')}"
        return f"error: {data.get('error', '')}"
    if name == "repair_sql":
        return f"changed={data.get('changed')}, applied={data.get('applied')}"
    if name in {"search_experience", "save_experience"}:
        return f"count={data.get('count', data.get('record', {}).get('status', ''))}"
    return "ok"


def _fallback_answer(execution: dict[str, Any]) -> str:
    rows = execution.get("rows") or []
    columns = execution.get("columns") or []
    if not rows:
        return "查询完成，但没有匹配的数据。"
    if len(rows) == 1 and len(columns) == 1:
        return f"查询结果：{rows[0][0]}"
    return f"查询完成，共 {len(rows)} 行。"


def _wants_clarification(content: str) -> bool:
    payload = parse_json_object(content) or {}
    return str(payload.get("status") or "").strip().lower() == "needs_clarification"


def _wants_human(content: str) -> bool:
    payload = parse_json_object(content) or {}
    return str(payload.get("status") or "").strip().lower() == "needs_human"


def _claims_ok(content: str) -> bool:
    payload = parse_json_object(content) or {}
    return str(payload.get("status") or "").strip().lower() == "ok"


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
