"""Verified experience memory with an admission gate.

Only human-verified examples are ever injected into the agent context.
Agent-generated examples are stored as `candidate`, which prevents a bad
answer from being reinforced through its own output.
"""

from __future__ import annotations

import difflib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

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
        }

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}
