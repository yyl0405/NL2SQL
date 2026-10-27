"""Weekly feedback report: what users rejected, what failed, and what is stale.

The report is an instrument, not a chat transcript. By default every question is
replaced by its fingerprint and the SQL/detail text is omitted, so the output can
be pasted into a ticket without dragging business wording along. `--show-text`
opts into the local text for a human sitting in front of the database.

Experience reuse is read from the event log because the experience table only
stores lifetime hit counts. A lifetime number cannot answer "how much did the
library help this week", and mixing the two would be a silent unit change.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import observability
from .memory.experience import (
    FEEDBACK_APPROVED,
    FEEDBACK_OPEN,
    FEEDBACK_REASONS,
    FEEDBACK_REJECTED,
    ExperienceStore,
)

TOP_LIMIT = 10


def fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _window_metrics(log_path: str | Path | None, since: datetime) -> dict[str, Any]:
    if log_path is None:
        return {
            "configured": False,
            "note": "未配置 NL2SQL_LOG_PATH，窗口内的经验复用率不可计算",
            "turns": 0,
            "experience_reuse_rate": None,
        }
    bundle = observability.read_events(log_path, limit=0)
    turns = []
    for event in bundle["events"]:
        if event.get("event") != observability.EVENT_TURN:
            continue
        stamp = _parse_ts(event.get("ts"))
        if stamp is not None and stamp >= since:
            turns.append(event)
    hits = sum(
        1 for turn in turns if int((turn.get("few_shot") or {}).get("injected") or 0) > 0
    )
    tokens = [int(turn.get("tokens") or 0) for turn in turns]
    return {
        "configured": True,
        "path": str(bundle["path"]),
        "missing": bool(bundle.get("missing")),
        "malformed_lines": int(bundle.get("malformed") or 0),
        "turns": len(turns),
        "experience_reuse_hits": hits,
        "experience_reuse_rate": round(hits / len(turns), 4) if turns else None,
        "total_tokens": sum(tokens),
        "avg_tokens": round(sum(tokens) / len(tokens), 2) if tokens else 0.0,
    }


def _feedback_item(row: dict[str, Any], *, show_text: bool) -> dict[str, Any]:
    item = {
        "fingerprint": fingerprint(str(row.get("question") or "")),
        "category": row.get("category") or "",
        "reason": row.get("reason") or "",
        "reason_label": FEEDBACK_REASONS.get(str(row.get("reason") or ""), ""),
        "status": row.get("status") or "",
        "occur_count": int(row.get("occur_count") or 0),
        "schema_hash": row.get("schema_hash") or "",
        "last_seen_at": row.get("last_seen_at") or "",
    }
    if show_text:
        item["question"] = row.get("question") or ""
        item["detail"] = row.get("detail") or ""
    return item


def _failure_item(row: dict[str, Any], *, show_text: bool) -> dict[str, Any]:
    item = {
        "fingerprint": fingerprint(str(row.get("question") or "")),
        "category": row.get("category") or "",
        "reason": row.get("reason") or "",
        "status": row.get("status") or "",
        "occur_count": int(row.get("occur_count") or 0),
        "schema_hash": row.get("schema_hash") or "",
        "last_seen_at": row.get("last_seen_at") or "",
    }
    if show_text:
        item["question"] = row.get("question") or ""
        item["detail"] = row.get("detail") or ""
    return item


def build_weekly_report(
    store: ExperienceStore,
    *,
    active_schema_hash: str = "",
    days: int = 7,
    log_path: str | Path | None = None,
    show_text: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    days = max(1, int(days))
    since_dt = now - timedelta(days=days)
    since = since_dt.isoformat(timespec="seconds")
    feedback = store.list_feedback(status=None, limit=1000, since=since)
    failures = store.top_failures(since=since, limit=TOP_LIMIT)
    by_reason: dict[str, int] = {}
    by_status: dict[str, int] = {}
    occurrences = 0
    for row in feedback:
        status = str(row.get("status") or "")
        reason = str(row.get("reason") or "")
        by_status[status] = by_status.get(status, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        occurrences += int(row.get("occur_count") or 0)
    feedback_sorted = sorted(
        feedback,
        key=lambda row: (-int(row.get("occur_count") or 0), str(row.get("last_seen_at") or "")),
    )
    lifetime = store.stats()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_days": days,
        "since": since,
        "privacy": {
            "question_text": "included" if show_text else "fingerprint_only",
            "sql": "omitted",
        },
        "feedback": {
            "total": len(feedback),
            "occurrences": occurrences,
            "by_status": by_status,
            "open": by_status.get(FEEDBACK_OPEN, 0),
            "approved": by_status.get(FEEDBACK_APPROVED, 0),
            "rejected": by_status.get(FEEDBACK_REJECTED, 0),
            "by_reason": by_reason,
            "top": [_feedback_item(row, show_text=show_text) for row in feedback_sorted[:TOP_LIMIT]],
        },
        "top_failures": [_failure_item(row, show_text=show_text) for row in failures],
        "experience_reuse": {
            **_window_metrics(log_path, since_dt),
            "lifetime_hits": int(lifetime.get("reuse_hits") or 0),
            "verified_rows": int(lifetime.get("verified") or 0),
        },
        "schema_impact": store.schema_impact(active_schema_hash),
    }


def print_weekly_report(report: dict[str, Any]) -> None:
    feedback = report["feedback"]
    print(
        f"反馈周报 · 最近 {report['window_days']} 天 · since {report['since']} "
        f"· 问题文本 {report['privacy']['question_text']}"
    )
    print(
        f"反馈 {feedback['total']} 条（open {feedback['open']} / approved {feedback['approved']} "
        f"/ rejected {feedback['rejected']}，累计 {feedback['occurrences']} 次）"
    )
    if feedback["by_reason"]:
        print("  按原因：" + " · ".join(f"{key}={value}" for key, value in feedback["by_reason"].items()))
    for item in feedback["top"]:
        label = item.get("question") or item["fingerprint"]
        print(
            f"  - [{item['status']}] {label} {item['reason_label'] or item['reason']} "
            f"×{item['occur_count']} · {item['category'] or 'uncategorized'}"
        )
    print("Top 失败问题：")
    if not report["top_failures"]:
        print("  窗口内没有失败记录。")
    for item in report["top_failures"]:
        label = item.get("question") or item["fingerprint"]
        print(
            f"  - {label} ×{item['occur_count']} · {item['reason'] or 'unknown'} "
            f"· {item['category'] or 'uncategorized'} · {item['status']}"
        )
    reuse = report["experience_reuse"]
    if reuse.get("configured"):
        rate = reuse.get("experience_reuse_rate")
        shown = "n/a（窗口内没有轮次）" if rate is None else f"{rate * 100:.1f}%"
        print(
            f"经验复用率：{shown}（{reuse['experience_reuse_hits']}/{reuse['turns']} 轮注入）"
            f" · 窗口 tokens {reuse['total_tokens']} · 库内累计命中 {reuse['lifetime_hits']}"
        )
    else:
        print(f"经验复用率：{reuse.get('note')}；库内累计命中 {reuse['lifetime_hits']}")
    schema = report["schema_impact"]
    experience = schema["experience"]
    active = schema["active_schema_hash"] or "(empty)"
    print(
        f"Schema 影响：当前 {active[:12]} · 经验 active {experience['active_rows']} / "
        f"陈旧 {experience['stale_rows']}（{len(experience['stale_hashes'])} 个旧版本） · "
        f"反馈陈旧 open {schema['feedback']['on_stale']} · 失败陈旧 open {schema['failures']['on_stale']}"
    )
