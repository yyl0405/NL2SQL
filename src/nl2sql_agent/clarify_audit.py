"""Clarification audit: does asking help, and does it ever cost an answer?

The policy layer has two jobs, and each can fail in a way the frozen eval cannot see.
If it asks too often it blocks questions that were perfectly answerable — a 澄清 is
not a result set, so it simply disappears from execution accuracy while the user
stops getting numbers. If it asks too little, the user gets 「请补充必要的条件后重新提
问。」, which is a clarification that cannot be answered by picking anything.

So the instrument measures three separable things:

* **the policy alone, over the frozen corpus** (no model, no database): which trigger
  codes fire, and — the gate that matters — how many answerable questions the policy
  would block in each mode. This is what justifies splitting the five triggers into
  two tiers instead of treating every gap as a reason to ask;
* **the corpus of vague, narrowed, out-of-scope and two-turn questions**: did the
  expected code fire, is every emitted clarification actionable, and can every
  option actually be sent back and answered;
* **a paired A/B of ``clarify=off`` against the shipped mode**, with separate
  experience stores, so an option that only *looks* useful is not credited to the
  policy by a success the other arm had too.

Exit codes are driven by invariants only. ``ask`` 档代价 and the A/B deltas are
reported, never gated: the clarify corpus is assistant-authored measurement material.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .clarify import (
    ASK_CODES,
    CLARIFY_CODES,
    CODE_ENTITY,
    CODE_METRIC,
    CODE_TIME,
    MODE_ASK,
    MODE_OFF,
    MODE_OPTIONS,
    ClarificationPolicy,
    MetricGlossary,
)
from .config import Settings
from .guard_suite import load_case_file
from .runtime import create_runtime
from .value_align import ValueAligner, ValueDictionary

CLARIFY_CASES = Path(__file__).resolve().parent / "data" / "clarify_cases.jsonl"
# The packaged corpus is pinned to the packaged demo seed: its expected values, its
# periods and every "can this option be sent back" verdict only hold there. Point the
# command at a attached catalog with your own --cases, never with these.
PACKAGED_DEMO_SCHEMA = Path(__file__).resolve().parent / "data" / "demo_schema.json"
KINDS = ("trigger", "quiet", "enrich", "followup")
UNMEASURED = "unmeasured"
# The two codes this layer owns. An entity / permission / schema gap is established by
# another component and only *labelled* here, so the policy's own blocking count is the
# number of answerable questions this stage would have stolen.
POLICY_CODES = (CODE_METRIC, CODE_TIME)
POLICY_STATUSES = ("ok", "needs_clarification", "refused", "error")


@dataclass
class ClarifyRow:
    """One corpus case, as the policy and the agent actually handled it."""

    id: str
    kind: str
    question: str
    reply: str = ""
    expect_codes: list[str] = field(default_factory=list)
    reject_codes: list[str] = field(default_factory=list)
    min_options: int = 0
    reask: bool = False
    plan_codes: list[str] = field(default_factory=list)
    plan_blocking: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    status: str = UNMEASURED
    code: str = ""
    options: list[str] = field(default_factory=list)
    missing_slots: list[str] = field(default_factory=list)
    actionable: str = UNMEASURED
    reask_total: int = 0
    reask_ok: int = 0
    reask_empty: int = 0
    reask_distinct: int = 0
    reask_failed: list[str] = field(default_factory=list)
    resolved_question: str = ""
    rewrite_source: str = ""
    final_status: str = UNMEASURED
    final_code: str = ""
    expect_effective: str = ""
    expect_final_status: str = ""
    error: str = ""

    @property
    def observed_codes(self) -> list[str]:
        out = list(self.plan_codes)
        if self.code and self.code not in out:
            out.append(self.code)
        return out

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "question": self.question,
            "reply": self.reply,
            "expect_codes": list(self.expect_codes),
            "reject_codes": list(self.reject_codes),
            "expect_effective": self.expect_effective,
            "expect_final_status": self.expect_final_status,
            "min_options": self.min_options,
            "plan_codes": list(self.plan_codes),
            "plan_blocking": list(self.plan_blocking),
            "alternatives": list(self.alternatives),
            "status": self.status,
            "code": self.code,
            "options": list(self.options),
            "missing_slots": list(self.missing_slots),
            "actionable": self.actionable,
            "reask": {"total": self.reask_total, "ok": self.reask_ok, "empty": self.reask_empty,
                      "distinct": self.reask_distinct},
            "reask_failed": list(self.reask_failed),
            "resolved_question": self.resolved_question,
            "rewrite_source": self.rewrite_source,
            "final_status": self.final_status,
            "final_code": self.final_code,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def validate_cases(cases: list[dict[str, Any]]) -> None:
    """A malformed clarification case cannot be judged, so refuse the whole run."""

    seen: set[str] = set()
    for index, case in enumerate(cases):
        where = f"第 {index + 1} 条 {case.get('id') or '(无 id)'}"
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            raise ValueError(f"{where} 缺少 id")
        if case_id in seen:
            raise ValueError(f"澄清用例 id 重复: {case_id}")
        seen.add(case_id)
        question = str(case.get("question") or "").strip()
        if not question:
            raise ValueError(f"{where} 缺少 question")
        kind = str(case.get("kind") or "").strip()
        if kind not in KINDS:
            raise ValueError(f"{where} 的 kind 未知: {kind!r}，可选 {list(KINDS)}")
        codes = case.get("expect_codes")
        if not isinstance(codes, list):
            raise ValueError(f"{where} 的 expect_codes 必须是数组")
        for code in list(codes) + list(case.get("reject_codes") or []):
            if str(code) not in CLARIFY_CODES:
                raise ValueError(f"{where} 引用了未知触发码 {code!r}，可选 {list(CLARIFY_CODES)}")
        if kind == "followup" and not str(case.get("reply") or "").strip():
            raise ValueError(f"{where} 是 followup 但缺少 reply（第二轮要发送的选项编号）")


def _policy(settings: Settings, mode: str, glossary: MetricGlossary | None) -> ClarificationPolicy:
    return ClarificationPolicy(glossary, as_of=settings.demo_today, mode=mode)


def _aligner(settings: Settings) -> ValueAligner | None:
    path = settings.value_dictionary_path
    if path is None or not Path(path).exists():
        return None
    return ValueAligner(ValueDictionary.load(path))


def plan_of(policy: ClarificationPolicy, question: str) -> dict[str, Any]:
    plan = policy.decide(question)
    return {
        "codes": plan.codes,
        "blocking": [item.code for item in plan.blocking],
        "alternatives": plan.alternatives,
    }


def frozen_footprint(
    frozen_cases: list[dict[str, Any]],
    settings: Settings,
    glossary: MetricGlossary | None,
    aligner: ValueAligner | None,
) -> dict[str, Any]:
    """What the policy would do to the frozen corpus, measured without a model.

    The tier decision has to be justified on the questions that already have golds:
    a blocked case loses its EX contribution silently, so this counts exactly that.
    """

    options_policy = _policy(settings, MODE_OPTIONS, glossary)
    ask_policy = _policy(settings, MODE_ASK, glossary)
    metric_ids: list[str] = []
    time_ids: list[str] = []
    both_ids: list[str] = []
    quiet_ids: list[str] = []
    blocked_options: list[str] = []
    blocked_ask: list[str] = []
    entity_ids: list[str] = []
    offered = 0
    for case in frozen_cases:
        question = str(case.get("question") or "")
        case_id = str(case.get("id") or "")
        plan = plan_of(options_policy, question)
        codes = plan["codes"]
        if CODE_METRIC in codes:
            metric_ids.append(case_id)
        if CODE_TIME in codes:
            time_ids.append(case_id)
        if CODE_METRIC in codes and CODE_TIME in codes:
            both_ids.append(case_id)
        if not codes:
            quiet_ids.append(case_id)
        offered += len(plan["alternatives"])
        if plan["blocking"]:
            blocked_options.append(case_id)
        if plan_of(ask_policy, question)["blocking"]:
            blocked_ask.append(case_id)
        if aligner is not None and aligner.align(question).ambiguous:
            entity_ids.append(case_id)
    return {
        "cases": len(frozen_cases),
        "metric": len(metric_ids),
        "time": len(time_ids),
        "both": both_ids,
        "quiet": len(quiet_ids),
        "trigger_rate": round((len(frozen_cases) - len(quiet_ids)) / len(frozen_cases), 4)
        if frozen_cases
        else 0.0,
        "options_offered": offered,
        "blocking_options": sorted(blocked_options),
        "blocking_ask": len(blocked_ask),
        "blocking_ask_ids": sorted(blocked_ask),
        "entity_blocking": sorted(entity_ids),
        "metric_ids": metric_ids[:10],
        "time_ids": time_ids[:10],
        "policy_codes": list(POLICY_CODES),
    }


def _clarification_view(result: Any) -> dict[str, Any]:
    clarification = getattr(result, "clarification", None)
    if clarification is None:
        return {"code": "", "options": [], "missing_slots": [], "present": False}
    return {
        "code": str(clarification.code or ""),
        "options": [str(item) for item in clarification.options],
        "missing_slots": [str(item) for item in clarification.missing_slots],
        "present": True,
    }


def _is_empty_result(rows: list[Any]) -> bool:
    """No rows, or one all-NULL row: a SUM over an empty period reads as an answer."""

    if not rows:
        return True
    return all(all(cell is None for cell in row) for row in rows)


def measure_rows(
    agent: Any,
    policy: ClarificationPolicy,
    cases: list[dict[str, Any]],
    *,
    with_reask: bool = True,
) -> list[ClarifyRow]:
    """Run every case through one arm and record what the user would have seen."""

    rows: list[ClarifyRow] = []
    for case in cases:
        row = ClarifyRow(
            id=str(case.get("id") or ""),
            kind=str(case.get("kind") or ""),
            question=str(case.get("question") or ""),
            reply=str(case.get("reply") or ""),
            expect_codes=[str(item) for item in (case.get("expect_codes") or [])],
            reject_codes=[str(item) for item in (case.get("reject_codes") or [])],
            min_options=int(case.get("min_options") or 0),
            reask=bool(case.get("reask")),
            expect_effective=str(case.get("expect_effective_contains") or ""),
            expect_final_status=str(case.get("expect_final_status") or ""),
        )
        plan = plan_of(policy, row.question)
        row.plan_codes = plan["codes"]
        row.plan_blocking = plan["blocking"]
        row.alternatives = plan["alternatives"]
        try:
            first = agent.run(row.question)
        except Exception as exc:  # noqa: BLE001 - one broken case must not kill the run
            row.error = f"{type(exc).__name__}: {exc}"[:160]
            rows.append(row)
            continue
        view = _clarification_view(first)
        row.status = str(first.status)
        row.code = view["code"]
        row.options = view["options"]
        row.missing_slots = view["missing_slots"]
        row.actionable = (
            "yes" if (view["options"] or view["missing_slots"]) else ("no" if view["present"] else "n/a")
        )
        emitted = list(row.options) or list(row.alternatives)
        if with_reask and row.reask and emitted:
            row.reask_total = len(emitted)
            signatures: set[str] = set()
            for option in emitted:
                try:
                    again = agent.run(option)
                except Exception as exc:  # noqa: BLE001
                    row.reask_failed.append(f"{option}（{type(exc).__name__}）")
                    continue
                if str(again.status) == "ok":
                    row.reask_ok += 1
                    if _is_empty_result(list(again.rows or [])):
                        row.reask_empty += 1
                    signatures.add(json.dumps(again.rows, ensure_ascii=False, default=str)[:200])
                else:
                    row.reask_failed.append(f"{option}（{again.status}）")
            row.reask_distinct = len(signatures)
        if row.kind == "followup" and row.reply:
            try:
                second = agent.run(row.reply, history=[first.to_turn()])
            except Exception as exc:  # noqa: BLE001
                row.error = f"第二轮 {type(exc).__name__}: {exc}"[:160]
                rows.append(row)
                continue
            row.resolved_question = str(second.effective_question or second.question or "")
            row.rewrite_source = str(second.rewrite_source)
            row.final_status = str(second.status)
            row.final_code = _clarification_view(second)["code"]
        rows.append(row)
    return rows


def bare_clarification_count(rows: list[ClarifyRow]) -> int:
    """Clarifications the user could not answer by picking anything."""

    return sum(1 for row in rows if row.status == "needs_clarification" and row.actionable == "no")


def corpus_findings(rows: list[ClarifyRow]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {
        "not_triggered": set(),
        "fired_when_quiet": set(),
        "unactionable_clarification": set(),
        "assume_tier_blocking": set(),
        "option_not_sendable": set(),
        "too_few_options": set(),
        "reply_unresolved": set(),
        "clarification_loop": set(),
        "wrong_effective": set(),
        "followup_not_answered": set(),
    }
    for row in rows:
        observed = row.observed_codes
        if row.kind == "quiet":
            if row.plan_codes:
                out["fired_when_quiet"].add(row.id)
        elif not set(row.expect_codes) <= set(observed):
            out["not_triggered"].add(row.id)
        if set(row.reject_codes) & set(row.plan_codes):
            out["fired_when_quiet"].add(row.id)
        if row.actionable == "no":
            out["unactionable_clarification"].add(row.id)
        if set(row.plan_blocking) & set(POLICY_CODES):
            out["assume_tier_blocking"].add(row.id)
        if row.reask and row.reask_failed:
            out["option_not_sendable"].add(row.id)
        offered = max(len(row.options), len(row.alternatives))
        if row.min_options and offered < row.min_options:
            out["too_few_options"].add(row.id)
        if row.kind == "followup":
            if row.rewrite_source != "clarify_reply":
                out["reply_unresolved"].add(row.id)
            if row.final_code == CODE_ENTITY and row.code == CODE_ENTITY:
                out["clarification_loop"].add(row.id)
            if row.expect_effective and row.expect_effective not in row.resolved_question:
                out["wrong_effective"].add(row.id)
            if row.expect_final_status and row.final_status != row.expect_final_status:
                out["followup_not_answered"].add(row.id)
    return {name: sorted(ids) for name, ids in out.items() if ids}


def observations(rows: list[ClarifyRow]) -> dict[str, list[str]]:
    """Reported, never gated: these are generator or data facts, not policy breaks."""

    out: dict[str, list[str]] = {
        "quiet_not_answered": [],
        "option_period_inert": [],
        "option_period_empty": [],
    }
    for row in rows:
        if row.kind == "quiet" and row.status != "ok":
            out["quiet_not_answered"].append(f"{row.id}（{row.status}）")
        if not row.reask or not row.reask_total or row.reask_ok != row.reask_total:
            continue
        if CODE_TIME in row.plan_codes:
            if row.reask_distinct < 2:
                out["option_period_inert"].append(f"{row.id}（{row.reask_distinct} 种结果）")
            if row.reask_empty:
                out["option_period_empty"].append(f"{row.id}（{row.reask_empty}/{row.reask_total} 空）")
    return {name: sorted(values) for name, values in out.items() if values}


def paired_ab(
    settings: Settings,
    cases: list[dict[str, Any]],
    glossary: MetricGlossary | None,
    *,
    modes: tuple[str, ...] = (MODE_OFF, MODE_OPTIONS),
) -> dict[str, Any]:
    """The same arm twice, differing only in ``clarify``, each with its own experience store."""

    root = Path(settings.home or Path(".")) / "var" / "clarify_ab"
    root.mkdir(parents=True, exist_ok=True)
    arms: dict[str, Any] = {}
    for mode in modes:
        runtime = create_runtime(
            replace(settings, clarify=mode, experience_path=root / f"{mode}.db")
        )
        arms[mode] = measure_rows(
            runtime.agent, _policy(settings, mode, glossary), cases, with_reask=False
        )
    primary = arms.get(modes[-1]) or []
    baseline = arms.get(modes[0]) or []

    def asked(rows: list[ClarifyRow]) -> int:
        return sum(1 for row in rows if row.status == "needs_clarification")

    def with_options(rows: list[ClarifyRow]) -> int:
        return sum(1 for row in rows if row.options or row.alternatives)

    return {
        "measured": True,
        "provider": settings.provider,
        "modes": list(modes),
        "cases": len(cases),
        "asked": {mode: asked(arms[mode]) for mode in modes},
        "actionable": {mode: sum(1 for row in arms[mode] if row.actionable == "yes") for mode in modes},
        "bare": {mode: bare_clarification_count(arms[mode]) for mode in modes},
        "with_options": {mode: with_options(arms[mode]) for mode in modes},
        "options_offered": {
            mode: sum(len(row.options) + len(row.alternatives) for row in arms[mode]) for mode in modes
        },
        "status_by_mode": {
            mode: {
                status: sum(1 for row in arms[mode] if row.status == status)
                for status in POLICY_STATUSES
            }
            for mode in modes
        },
        "regressed": sorted(
            row.id
            for off_row, on_row in zip(baseline, primary)
            if off_row.status == "ok" and on_row.status != "ok"
        ),
        "improved": sorted(
            row.id
            for off_row, on_row in zip(baseline, primary)
            if off_row.status == "needs_clarification" and on_row.status == "ok"
        ),
    }


def run_clarify_audit(
    settings: Settings,
    catalog: Catalog,
    cases: list[dict[str, Any]],
    frozen_cases: list[dict[str, Any]],
    *,
    glossary_path: str | Path | None = None,
    with_ab: bool = True,
) -> dict[str, Any]:
    if glossary_path is not None:
        settings = replace(settings, metric_glossary_override=Path(glossary_path))
    glossary = MetricGlossary.load(settings.metric_glossary_path) if settings.metric_glossary_path else None
    policy = _policy(settings, settings.clarify or MODE_OPTIONS, glossary)
    aligner = _aligner(settings)
    validate_cases(cases)

    rows = measure_rows(create_runtime(settings).agent, policy, cases)
    footprint = frozen_footprint(frozen_cases, settings, glossary, aligner)
    findings = corpus_findings(rows)
    if footprint["blocking_options"]:
        findings["frozen_blocked_in_options"] = footprint["blocking_options"]
    findings = {name: ids for name, ids in findings.items() if ids}

    ab = paired_ab(settings, cases, glossary) if with_ab else {"measured": False}
    return {
        "cases": len(rows),
        "policy": {
            "mode": policy.mode,
            "as_of": policy.as_of.isoformat(),
            "periods": [[phrase, literal] for phrase, literal in policy.periods()],
            "ask_codes": list(ASK_CODES),
            "policy_codes": list(POLICY_CODES),
            "glossary": glossary.stats() if glossary else MetricGlossary.default().stats(),
            "glossary_source": str(settings.metric_glossary_path or "(内置名词表)"),
            "value_alignment": settings.value_alignment,
            "dictionary": bool(aligner),
        },
        "catalog": {
            "dialect": catalog.dialect,
            "tables": len(catalog.tables),
            "backend": settings.db_backend,
            "schema": str(settings.schema_path),
            "packaged_demo": settings.schema_path.resolve() == PACKAGED_DEMO_SCHEMA,
        },
        "kinds": {kind: sum(1 for row in rows if row.kind == kind) for kind in KINDS},
        "codes": {
            code: sum(1 for row in rows if code in row.observed_codes) for code in CLARIFY_CODES
        },
        "ask_quality": {
            "clarified": sum(1 for row in rows if row.status == "needs_clarification"),
            "actionable": sum(1 for row in rows if row.status == "needs_clarification"
                              and row.actionable == "yes"),
            "bare": bare_clarification_count(rows),
            "options_offered": sum(len(row.options) for row in rows),
            "alternatives_offered": sum(len(row.alternatives) for row in rows),
            "reask_cases": sum(1 for row in rows if row.reask_total),
            "reask_total": sum(row.reask_total for row in rows),
            "reask_ok": sum(row.reask_ok for row in rows),
            "reask_empty": sum(row.reask_empty for row in rows),
        },
        "followup": {
            "cases": sum(1 for row in rows if row.kind == "followup"),
            "resolved": sum(1 for row in rows if row.kind == "followup" and row.rewrite_source == "clarify_reply"),
            "answered": sum(
                1 for row in rows if row.kind == "followup" and row.final_status == "ok"
            ),
            "looped": sum(
                1 for row in rows if row.kind == "followup" and row.final_code == CODE_ENTITY
            ),
        },
        "frozen": footprint,
        "ab": ab,
        "observations": observations(rows),
        "findings": findings,
        "rows": [row.to_dict() for row in rows],
    }
