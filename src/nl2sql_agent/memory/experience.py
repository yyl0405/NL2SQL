"""Verified experience memory with an admission gate and a failure queue.

Only human-verified examples are ever injected into the agent context.
Agent-generated examples are stored as `candidate`, which prevents a bad
answer from being reinforced through its own output.

Terminal agent failures land in `failure_queue` so that a question the system
could not answer becomes a reviewable work item instead of disappearing. One
question is queued once per schema version; closing an item requires a SQL that
passes the guard and executes successfully, which then promotes the pair to a
verified experience.
"""

from __future__ import annotations

import difflib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ..taxonomy import classify_category
from ..text import jaccard, normalize_question, tokenize

Validator = Callable[[str], tuple[bool, str]]


class ExperienceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._session() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS experiences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    normalized_question TEXT NOT NULL,
                    sql TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'agent',
                    status TEXT NOT NULL DEFAULT 'candidate',
                    quality TEXT NOT NULL DEFAULT 'candidate',
                    schema_hash TEXT NOT NULL DEFAULT '',
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    UNIQUE(normalized_question, schema_hash)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_experience_status ON experiences(status, schema_hash)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS failure_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    normalized_question TEXT NOT NULL,
                    schema_hash TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    last_sql TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    occur_count INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution TEXT NOT NULL DEFAULT '',
                    UNIQUE(normalized_question, schema_hash)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_failure_status ON failure_queue(status, occur_count DESC)"
            )

    def add(
        self,
        question: str,
        sql: str,
        *,
        category: str = "",
        source: str = "agent",
        verified: bool = False,
        schema_hash: str = "",
        validator: Validator | None = None,
    ) -> dict[str, Any]:
        question = (question or "").strip()
        sql = (sql or "").strip()
        if not question or not sql:
            raise ValueError("question 和 sql 不能为空")
        if verified and validator is not None:
            ok, message = validator(sql)
            if not ok:
                raise ValueError(f"经验未通过 SQL 校验，不能标记为已验证: {message}")

        status = "verified" if verified else "candidate"
        quality = "hard" if verified else "candidate"
        normalized = normalize_question(question)
        category = (category or "").strip() or classify_category(question)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with self._session() as conn:
            existing = conn.execute(
                "SELECT * FROM experiences WHERE normalized_question = ? AND schema_hash = ?",
                (normalized, schema_hash),
            ).fetchone()
            if existing:
                if verified and existing["status"] != "verified":
                    conn.execute(
                        """
                        UPDATE experiences
                        SET sql = ?, category = ?, source = ?, status = 'verified',
                            quality = 'hard', last_used_at = ?
                        WHERE id = ?
                        """,
                        (sql, category, source, now, existing["id"]),
                    )
                    row = conn.execute("SELECT * FROM experiences WHERE id = ?", (existing["id"],)).fetchone()
                    result = self._row(row)
                    result["created"] = False
                    return result
                result = self._row(existing)
                result["created"] = False
                return result

            cursor = conn.execute(
                """
                INSERT INTO experiences
                    (question, normalized_question, sql, category, source, status,
                     quality, schema_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (question, normalized, sql, category, source, status, quality, schema_hash, now),
            )
            row = conn.execute("SELECT * FROM experiences WHERE id = ?", (cursor.lastrowid,)).fetchone()
        result = self._row(row)
        result["created"] = True
        return result

    def verify(self, experience_id: int, validator: Validator | None = None) -> dict[str, Any]:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM experiences WHERE id = ?", (experience_id,)).fetchone()
            if row is None:
                raise KeyError(f"经验 {experience_id} 不存在")
            if validator is not None:
                ok, message = validator(row["sql"])
                if not ok:
                    raise ValueError(f"经验未通过 SQL 校验: {message}")
            conn.execute(
                "UPDATE experiences SET status = 'verified', quality = 'hard' WHERE id = ?",
                (experience_id,),
            )
            row = conn.execute("SELECT * FROM experiences WHERE id = ?", (experience_id,)).fetchone()
            return self._row(row)

    def search(
        self,
        question: str,
        *,
        schema_hash: str = "",
        top_k: int = 3,
        min_score: float = 0.3,
        category: str = "",
        category_boost: float = 0.12,
        strict_category: bool = False,
    ) -> list[dict[str, Any]]:
        normalized = normalize_question(question)
        query_tokens = tokenize(question)
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM experiences
                WHERE status = 'verified' AND schema_hash = ?
                """,
                (schema_hash,),
            ).fetchall()
            scored: list[tuple[float, sqlite3.Row]] = []
            for row in rows:
                if strict_category and category and row["category"] and row["category"] != category:
                    continue
                if row["normalized_question"] == normalized:
                    score = 1.0
                else:
                    lexical = jaccard(query_tokens, tokenize(row["question"]))
                    fuzzy = difflib.SequenceMatcher(
                        None, normalized, row["normalized_question"]
                    ).ratio()
                    score = 0.55 * lexical + 0.45 * fuzzy
                    if normalized and (
                        normalized in row["normalized_question"] or row["normalized_question"] in normalized
                    ):
                        score += 0.08
                if category and row["category"] == category:
                    score += max(0.0, category_boost)
                score = min(1.0, score)
                if score >= min_score:
                    scored.append((score, row))
            scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
            selected = scored[: max(0, top_k)]
            if selected:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                conn.executemany(
                    "UPDATE experiences SET hit_count = hit_count + 1, last_used_at = ? WHERE id = ?",
                    [(now, row["id"]) for _, row in selected],
                )
        return [
            {
                "id": row["id"],
                "question": row["question"],
                "sql": row["sql"],
                "category": row["category"],
                "quality": row["quality"],
                "score": round(score, 4),
            }
            for score, row in selected
        ]

    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM experiences"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, limit))
        with self._session() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._session() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"]
            verified = conn.execute(
                "SELECT COUNT(*) AS n FROM experiences WHERE status = 'verified'"
            ).fetchone()["n"]
            candidates = conn.execute(
                "SELECT COUNT(*) AS n FROM experiences WHERE status = 'candidate'"
            ).fetchone()["n"]
            hits = conn.execute("SELECT COALESCE(SUM(hit_count), 0) AS n FROM experiences").fetchone()["n"]
        return {
            "total": total,
            "verified": verified,
            "candidates": candidates,
            "reuse_hits": hits,
            **self.failure_stats(),
        }

    def record_failure(
        self,
        question: str,
        *,
        reason: str,
        category: str = "",
        sql: str = "",
        detail: str = "",
        schema_hash: str = "",
    ) -> dict[str, Any]:
        """Queue a terminal failure once per question and schema version.

        A question already marked resolved flips back to `open`: a failure that
        comes back after being answered is a regression worth looking at, and it
        must not be silently dropped.
        """
        question = (question or "").strip()
        if not question:
            return {"created": False, "skipped": "empty_question"}
        normalized = normalize_question(question)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        category = (category or "").strip() or classify_category(question)
        with self._session() as conn:
            existing = conn.execute(
                "SELECT * FROM failure_queue WHERE normalized_question = ? AND schema_hash = ?",
                (normalized, schema_hash),
            ).fetchone()
            if existing:
                reopened = existing["status"] != "open"
                conn.execute(
                    """
                    UPDATE failure_queue
                    SET occur_count = occur_count + 1, last_seen_at = ?, reason = ?,
                        category = ?, last_sql = ?, detail = ?,
                        status = CASE WHEN ? = 'resolved' OR ? = 'dismissed' THEN 'open' ELSE status END
                    WHERE id = ?
                    """,
                    (now, reason, category, sql, detail, existing["status"], existing["status"], existing["id"]),
                )
                row = conn.execute("SELECT * FROM failure_queue WHERE id = ?", (existing["id"],)).fetchone()
                result = self._row(row)
                result.update({"created": False, "duplicate": True, "reopened": reopened})
                return result

            cursor = conn.execute(
                """
                INSERT INTO failure_queue
                    (question, normalized_question, schema_hash, category, reason, last_sql,
                     detail, status, occur_count, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)
                """,
                (question, normalized, schema_hash, category, reason, sql, detail, now, now),
            )
            row = conn.execute("SELECT * FROM failure_queue WHERE id = ?", (cursor.lastrowid,)).fetchone()
        result = self._row(row)
        result.update({"created": True, "duplicate": False, "reopened": False})
        return result

    def list_failures(self, status: str | None = "open", limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM failure_queue"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY occur_count DESC, last_seen_at DESC, id DESC LIMIT ?"
        params.append(max(1, limit))
        with self._session() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def get_failure(self, failure_id: int) -> dict[str, Any]:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM failure_queue WHERE id = ?", (failure_id,)).fetchone()
        if row is None:
            raise KeyError(f"失败记录 {failure_id} 不存在")
        return self._row(row)

    def resolve_failure(
        self,
        failure_id: int,
        sql: str,
        *,
        validator: Validator,
        note: str = "",
    ) -> dict[str, Any]:
        """Close a queued failure with SQL that passes the guard and executes.

        The question/SQL pair is promoted straight to `verified` because a human
        supplied and machine-checked the SQL; the queue item and the reusable
        experience are written in the same call so the two cannot drift apart.
        """
        sql = (sql or "").strip()
        if not sql:
            raise ValueError("resolve 需要给出 SQL")
        if validator is None:
            raise ValueError("resolve 必须提供 validator（护栏 + 真实执行），不能凭肉眼结案")
        failure = self.get_failure(failure_id)
        ok, message = validator(sql)
        if not ok:
            raise ValueError(f"SQL 未通过护栏或执行校验，不能结案: {message}")

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._session() as conn:
            conn.execute(
                """
                UPDATE failure_queue
                SET status = 'resolved', resolved_at = ?, resolution = ?, last_sql = ?
                WHERE id = ?
                """,
                (now, (note or "").strip(), sql, failure_id),
            )
            row = conn.execute("SELECT * FROM failure_queue WHERE id = ?", (failure_id,)).fetchone()
        experience = self.add(
            failure["question"],
            sql,
            category=failure["category"] or classify_category(failure["question"]),
            source="human",
            verified=True,
            schema_hash=str(failure["schema_hash"] or ""),
            validator=validator,
        )
        return {"failure": self._row(row), "experience": experience}

    def failure_stats(self) -> dict[str, Any]:
        """`failures_by_category` only counts open rows: it is the triage backlog."""
        with self._session() as conn:
            by_status = {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM failure_queue GROUP BY status"
                ).fetchall()
            }
            by_category = {
                row["category"] or "uncategorized": row["n"]
                for row in conn.execute(
                    "SELECT category, COUNT(*) AS n FROM failure_queue "
                    "WHERE status = 'open' GROUP BY category"
                ).fetchall()
            }
            repeated = conn.execute(
                "SELECT COALESCE(SUM(occur_count), 0) AS n FROM failure_queue"
            ).fetchone()["n"]
        return {
            "failures_total": sum(by_status.values()),
            "failures_open": by_status.get("open", 0),
            "failures_resolved": by_status.get("resolved", 0),
            "failures_by_category": by_category,
            "failure_occurrences": repeated,
        }

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}
