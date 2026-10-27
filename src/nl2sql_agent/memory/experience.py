"""Verified experience memory with an admission gate, a quality tier and a failure queue.

Only human-verified examples are ever injected into the agent context.
Agent-generated examples are stored as `candidate`, which prevents a bad
answer from being reinforced through its own output.

Two orthogonal columns describe a row: `status` is the lifecycle gate
(`candidate` / `verified` / `deprecated`) and `quality` is the tier among usable
rows (`candidate` / `verified` / `gold`). Only `status = 'verified'` is ever
retrieved; `gold` is a rank prior, not a new visibility class. A row can be
promoted to gold by hand only — nothing the agent does earns it.

Terminal agent failures land in `failure_queue` so that a question the system
could not answer becomes a reviewable work item instead of disappearing. One
question is queued once per schema version; closing an item requires a SQL that
passes the guard and executes successfully, which then promotes the pair to a
verified experience. The failing SQL itself is kept separately as a
*counterexample* (question + SQL + error code). It is deliberately **not** a
prompt input: an example of a wrong answer is still an example, and the
acceptance rule for this layer is that wrong experience never reaches the
model. Counterexamples serve triage, repeat-failure detection and the rule that
refuses to save an already-failed SQL as a reusable experience.
"""

from __future__ import annotations

import difflib
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from ..taxonomy import classify_category
from ..text import jaccard, mmr_select, normalize_question, token_union, tokenize

Validator = Callable[[str], tuple[bool, str]]

STATUS_CANDIDATE = "candidate"
STATUS_VERIFIED = "verified"
STATUS_DEPRECATED = "deprecated"
STATUSES = (STATUS_CANDIDATE, STATUS_VERIFIED, STATUS_DEPRECATED)

#: `hard` was the first name for the verified tier; stored databases are migrated on open.
QUALITY_LEGACY = "hard"
QUALITY_CANDIDATE = "candidate"
QUALITY_VERIFIED = "verified"
QUALITY_GOLD = "gold"
QUALITIES = (QUALITY_CANDIDATE, QUALITY_VERIFIED, QUALITY_GOLD)

#: Legacy rows that predate the staleness columns are migrated on open.
EXPERIENCE_COLUMNS = {
    "deprecated_at": "TEXT",
    "deprecate_reason": "TEXT NOT NULL DEFAULT ''",
}

#: User-facing complaint codes. The labels are for the console; the codes are the API.
FEEDBACK_REASONS = {
    "result_wrong": "结果不对",
    "metric_wrong": "口径不对",
    "field_wrong": "字段不对",
}
FEEDBACK_OPEN = "open"
FEEDBACK_APPROVED = "approved"
FEEDBACK_REJECTED = "rejected"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _token_union(row: dict[str, Any]) -> set[str]:
    """Question and SQL tokens together: a repeat is either same wording or same shape."""
    return token_union(str(row.get("question") or ""), str(row.get("sql") or ""))



class ExperienceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        # Concurrent writers are the point: busy_timeout waits for the write lock
        # instead of surfacing "database is locked" on the first collision. WAL is
        # set in initialize() because the journal mode is a persistent file property.
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def journal_mode(self) -> str:
        with self._session() as conn:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]) if row else ""

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
            # WAL is persistent in the database file, so every later connection sees it.
            # A read-only or network-hosted file can refuse the change; the store then
            # keeps SQLite's default journal instead of failing to open at all.
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.Error:
                pass
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
                    deprecated_at TEXT,
                    deprecate_reason TEXT NOT NULL DEFAULT '',
                    UNIQUE(normalized_question, schema_hash)
                )
                """
            )
            self._migrate_experience_columns(conn)
            conn.execute(
                "UPDATE experiences SET quality = ? WHERE quality = ?",
                (QUALITY_VERIFIED, QUALITY_LEGACY),
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS counterexamples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    normalized_question TEXT NOT NULL,
                    schema_hash TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    sql TEXT NOT NULL,
                    sql_fingerprint TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    occur_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_at TEXT,
                    UNIQUE(normalized_question, schema_hash, sql_fingerprint)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_counter_status "
                "ON counterexamples(status, schema_hash, occur_count DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL DEFAULT '',
                    turn_index INTEGER NOT NULL DEFAULT -1,
                    trace_id TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL,
                    normalized_question TEXT NOT NULL,
                    effective_question TEXT NOT NULL DEFAULT '',
                    sql TEXT NOT NULL DEFAULT '',
                    suggested_sql TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    schema_hash TEXT NOT NULL DEFAULT '',
                    principal TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    occur_count INTEGER NOT NULL DEFAULT 1,
                    failure_id INTEGER,
                    failure_owned INTEGER NOT NULL DEFAULT 0,
                    counterexample_id INTEGER,
                    experience_id INTEGER,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    reviewer TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_status "
                "ON feedback(status, schema_hash, occur_count DESC)"
            )
            # Repeated clicks on the same turn are one complaint, not two review items.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_open_turn "
                "ON feedback(session_id, turn_index, reason) "
                "WHERE status = 'open' AND session_id != '' AND turn_index >= 0"
            )

    @staticmethod
    def _migrate_experience_columns(conn: sqlite3.Connection) -> None:
        """Add the staleness columns to a database created by an earlier version."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(experiences)")}
        for name, ddl in EXPERIENCE_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE experiences ADD COLUMN {name} {ddl}")

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

        status = STATUS_VERIFIED if verified else STATUS_CANDIDATE
        quality = QUALITY_VERIFIED if verified else QUALITY_CANDIDATE
        normalized = normalize_question(question)
        category = (category or "").strip() or classify_category(question)
        now = _now()

        with self._session() as conn:
            existing = conn.execute(
                "SELECT * FROM experiences WHERE normalized_question = ? AND schema_hash = ?",
                (normalized, schema_hash),
            ).fetchone()
            if existing:
                if verified and existing["status"] != STATUS_VERIFIED:
                    conn.execute(
                        """
                        UPDATE experiences
                        SET sql = ?, category = ?, source = ?, status = ?,
                            quality = ?, last_used_at = ?, deprecated_at = NULL,
                            deprecate_reason = ''
                        WHERE id = ?
                        """,
                        (
                            sql,
                            category,
                            source,
                            STATUS_VERIFIED,
                            QUALITY_GOLD if existing["quality"] == QUALITY_GOLD else QUALITY_VERIFIED,
                            now,
                            existing["id"],
                        ),
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
                """
                UPDATE experiences
                SET status = ?,
                    quality = CASE WHEN quality = ? THEN ? ELSE quality END,
                    deprecated_at = NULL,
                    deprecate_reason = ''
                WHERE id = ?
                """,
                (STATUS_VERIFIED, QUALITY_CANDIDATE, QUALITY_VERIFIED, experience_id),
            )
            row = conn.execute("SELECT * FROM experiences WHERE id = ?", (experience_id,)).fetchone()
            return self._row(row)

    def set_grade(self, experience_id: int, quality: str) -> dict[str, Any]:
        """Move a row along the quality tier. Gold is a human decision, never an automatic one.

        Demoting a gold row to `verified` keeps it retrievable; taking it out of the prompt
        entirely is `deprecate`, which is a different action with a different consequence.
        """
        grade = (quality or "").strip().lower()
        if grade not in {QUALITY_VERIFIED, QUALITY_GOLD}:
            raise ValueError(f"quality 只能是 {QUALITY_VERIFIED} 或 {QUALITY_GOLD}")
        with self._session() as conn:
            row = conn.execute("SELECT * FROM experiences WHERE id = ?", (experience_id,)).fetchone()
            if row is None:
                raise KeyError(f"经验 {experience_id} 不存在")
            if row["status"] != STATUS_VERIFIED:
                raise ValueError(
                    f"经验 {experience_id} 当前是 {row['status']}，只有 verified 经验能定级；"
                    "请先 experience verify"
                )
            conn.execute("UPDATE experiences SET quality = ? WHERE id = ?", (grade, experience_id))
            row = conn.execute("SELECT * FROM experiences WHERE id = ?", (experience_id,)).fetchone()
            return self._row(row)

    def deprecate(self, experience_id: int, *, reason: str = "") -> dict[str, Any]:
        """Retire a row from retrieval while keeping it for audit.

        Deprecated rows stay in the table because the only honest record of "we tried this
        and it went stale" is the row itself; deleting it would make the count unanswerable.
        """
        with self._session() as conn:
            row = conn.execute("SELECT * FROM experiences WHERE id = ?", (experience_id,)).fetchone()
            if row is None:
                raise KeyError(f"经验 {experience_id} 不存在")
            conn.execute(
                "UPDATE experiences SET status = ?, deprecated_at = ?, deprecate_reason = ? WHERE id = ?",
                (STATUS_DEPRECATED, _now(), (reason or "").strip(), experience_id),
            )
            row = conn.execute("SELECT * FROM experiences WHERE id = ?", (experience_id,)).fetchone()
            return self._row(row)

    def mark_stale(
        self,
        active_schema_hash: str,
        *,
        reason: str = "schema_changed",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Retire every row written against a different catalog version.

        Retrieval already filters by exact hash, so this changes no answer — it makes the
        backlog visible and stops `stats()` from implying the library is still usable.
        It is never called automatically: a wrong catalog path would silently retire
        a good library, so a human names the hash that is current.
        """
        active = (active_schema_hash or "").strip()
        with self._session() as conn:
            rows = conn.execute(
                "SELECT id, schema_hash FROM experiences "
                "WHERE status != ? AND schema_hash != ?",
                (STATUS_DEPRECATED, active),
            ).fetchall()
            affected = [int(row["id"]) for row in rows]
            by_hash: dict[str, int] = {}
            for row in rows:
                by_hash[str(row["schema_hash"] or "")] = by_hash.get(str(row["schema_hash"] or ""), 0) + 1
            if not dry_run and affected:
                conn.executemany(
                    "UPDATE experiences SET status = ?, deprecated_at = ?, deprecate_reason = ? WHERE id = ?",
                    [(STATUS_DEPRECATED, _now(), (reason or "").strip(), experience_id) for experience_id in affected],
                )
        return {
            "active_schema_hash": active,
            "dry_run": dry_run,
            "affected": len(affected),
            "ids": affected,
            "affected_by_hash": by_hash,
        }

    def staleness(self, active_schema_hash: str) -> dict[str, Any]:
        """Report what a hash change has already made unretrievable — without writing."""
        active = (active_schema_hash or "").strip()
        with self._session() as conn:
            rows = conn.execute(
                "SELECT status, schema_hash, COUNT(*) AS n FROM experiences GROUP BY status, schema_hash"
            ).fetchall()
        by_hash: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = by_hash.setdefault(str(row["schema_hash"] or ""), {})
            bucket[str(row["status"])] = int(row["n"])
        stale = {
            name: sum(count for status, count in bucket.items() if status != STATUS_DEPRECATED)
            for name, bucket in by_hash.items()
            if name != active
        }
        return {
            "active_schema_hash": active,
            "hashes": by_hash,
            "active_rows": sum(
                count for status, count in by_hash.get(active, {}).items() if status != STATUS_DEPRECATED
            ),
            "stale_rows": sum(stale.values()),
            "stale_hashes": sorted(stale),
            "deprecated_rows": sum(
                bucket.get(STATUS_DEPRECATED, 0) for bucket in by_hash.values()
            ),
        }

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
        gold_boost: float = 0.0,
        diversity: float = 0.0,
        record: bool = True,
    ) -> list[dict[str, Any]]:
        """Lexical retrieval of verified rows, optionally de-duplicated by shape.

        `diversity` is the MMR penalty weight; 0 keeps the historical ordering
        byte-for-byte. `gold_boost` is a fraction of the best match added to a gold
        row, so the prior works on the same scale whatever the lexical scores look
        like instead of being an absolute number that only fits one corpus.
        """
        rows = self.score(
            question,
            schema_hash=schema_hash,
            min_score=min_score,
            category=category,
            category_boost=category_boost,
            strict_category=strict_category,
            gold_boost=gold_boost,
        )
        if not rows:
            return []
        picks = mmr_select(
            [float(row["score"]) for row in rows],
            [_token_union(row) for row in rows],
            top_k=max(0, top_k),
            diversity=diversity,
        )
        selected = [rows[index] for index in picks]
        if record and selected:
            self.record_hits([int(row["id"]) for row in selected])
        return [
            {
                "id": row["id"],
                "question": row["question"],
                "sql": row["sql"],
                "category": row["category"],
                "quality": row["quality"],
                "score": round(float(row["score"]), 4),
            }
            for row in selected
        ]

    def score(
        self,
        question: str,
        *,
        schema_hash: str = "",
        min_score: float = 0.0,
        category: str = "",
        category_boost: float = 0.12,
        strict_category: bool = False,
        gold_boost: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Rank verified rows lexically, without selecting or recording a hit.

        Kept separate from :meth:`search` because the vector channel needs the whole
        ranking and must decide the final selection itself. Each row is a dict plus a
        `score`, so no sqlite3.Row escapes the store.
        """
        normalized = normalize_question(question)
        query_tokens = tokenize(question)
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM experiences WHERE status = ? AND schema_hash = ?",
                (STATUS_VERIFIED, schema_hash),
            ).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if strict_category and category and row["category"] and row["category"] != category:
                continue
            if row["normalized_question"] == normalized:
                score = 1.0
            else:
                lexical = jaccard(query_tokens, tokenize(row["question"]))
                fuzzy = difflib.SequenceMatcher(None, normalized, row["normalized_question"]).ratio()
                score = 0.55 * lexical + 0.45 * fuzzy
                if normalized and (
                    normalized in row["normalized_question"] or row["normalized_question"] in normalized
                ):
                    score += 0.08
            if category and row["category"] == category:
                score += max(0.0, category_boost)
            scored.append((score, self._row(row)))
        # The gold prior is a fraction of the best match, so it can only be applied once
        # every base score exists — otherwise the first row scanned sets its own ceiling.
        ceiling = max((score for score, _ in scored), default=0.0)
        out: list[tuple[float, dict[str, Any]]] = []
        for score, row in scored:
            if gold_boost and row["quality"] == QUALITY_GOLD:
                score += abs(gold_boost) * ceiling
            score = min(1.0, score)
            if score >= min_score:
                out.append((score, row))
        out.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [dict(row, score=round(score, 6)) for score, row in out]

    def verified_rows(self, schema_hash: str = "") -> list[dict[str, Any]]:
        """Verified rows for one catalog version, as plain dicts."""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM experiences WHERE status = ? AND schema_hash = ?",
                (STATUS_VERIFIED, schema_hash),
            ).fetchall()
        return [self._row(row) for row in rows]

    def record_hits(self, experience_ids: Sequence[int]) -> None:
        if not experience_ids:
            return
        now = _now()
        with self._session() as conn:
            conn.executemany(
                "UPDATE experiences SET hit_count = hit_count + 1, last_used_at = ? WHERE id = ?",
                [(now, int(experience_id)) for experience_id in experience_ids],
            )

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
            by_status = {
                str(row["status"]): int(row["n"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM experiences GROUP BY status"
                ).fetchall()
            }
            by_quality = {
                str(row["quality"]): int(row["n"])
                for row in conn.execute(
                    "SELECT quality, COUNT(*) AS n FROM experiences "
                    "WHERE status = ? GROUP BY quality",
                    (STATUS_VERIFIED,),
                ).fetchall()
            }
            hits = conn.execute("SELECT COALESCE(SUM(hit_count), 0) AS n FROM experiences").fetchone()["n"]
        return {
            "total": sum(by_status.values()),
            "verified": by_status.get(STATUS_VERIFIED, 0),
            "candidates": by_status.get(STATUS_CANDIDATE, 0),
            "deprecated": by_status.get(STATUS_DEPRECATED, 0),
            "gold": by_quality.get(QUALITY_GOLD, 0),
            "reuse_hits": hits,
            **self.failure_stats(),
            **self.counterexample_stats(),
            **self.feedback_stats(),
        }

    def record_failure(
        self,
        question: str,
        *,
        reason: str,
        category: str = "",
        sql: str = "",
        detail: str = "",
        error_code: str = "",
        schema_hash: str = "",
    ) -> dict[str, Any]:
        """Queue a terminal failure once per question and schema version.

        A question already marked resolved flips back to `open`: a failure that
        comes back after being answered is a regression worth looking at, and it
        must not be silently dropped.

        When the failure carries a SQL, the same call also records a counterexample —
        the wrong answer itself, kept out of retrieval on purpose. See
        :meth:`record_counterexample`.
        """
        question = (question or "").strip()
        if not question:
            return {"created": False, "skipped": "empty_question"}
        normalized = normalize_question(question)
        now = _now()
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
            else:
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
        if (sql or "").strip():
            result["counterexample"] = self.record_counterexample(
                question,
                sql,
                reason=reason,
                error_code=error_code,
                category=category,
                detail=detail,
                schema_hash=schema_hash,
            )
        return result

    def record_counterexample(
        self,
        question: str,
        sql: str,
        *,
        reason: str = "",
        error_code: str = "",
        category: str = "",
        detail: str = "",
        schema_hash: str = "",
    ) -> dict[str, Any]:
        """Store a question/SQL pair that is known to be wrong, once per shape.

        This table is written by failures and read by humans and by the admission gate
        in :mod:`nl2sql_agent.tools.experience_tools`. It is never read by the prompt
        builder: retrieval only looks at `experiences`, so a wrong SQL cannot become an
        example by way of having failed.
        """
        question = (question or "").strip()
        sql = (sql or "").strip()
        if not question or not sql:
            return {"created": False, "skipped": "empty"}
        normalized = normalize_question(question)
        fingerprint = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]
        now = _now()
        category = (category or "").strip() or classify_category(question)
        with self._session() as conn:
            existing = conn.execute(
                "SELECT * FROM counterexamples WHERE normalized_question = ? "
                "AND schema_hash = ? AND sql_fingerprint = ?",
                (normalized, schema_hash, fingerprint),
            ).fetchone()
            if existing:
                # A complaint is a weaker "why" than a concrete execution/guard error:
                # keep the original code while still counting the new occurrence.
                feedback_signal = str(error_code or "").startswith("feedback_")
                next_code = existing["error_code"] if feedback_signal and existing["error_code"] else error_code
                next_reason = (
                    existing["reason"] if feedback_signal and existing["reason"] else reason
                )
                next_detail = (
                    existing["detail"] if feedback_signal and existing["detail"] else detail
                )
                conn.execute(
                    "UPDATE counterexamples SET occur_count = occur_count + 1, last_seen_at = ?, "
                    "error_code = ?, reason = ?, detail = ?, "
                    "status = CASE WHEN ? = 'resolved' THEN 'open' ELSE status END WHERE id = ?",
                    (now, next_code, next_reason, next_detail, existing["status"], existing["id"]),
                )
                row = conn.execute("SELECT * FROM counterexamples WHERE id = ?", (existing["id"],)).fetchone()
                result = self._row(row)
                result.update({"created": False, "duplicate": True})
                return result
            cursor = conn.execute(
                """
                INSERT INTO counterexamples
                    (question, normalized_question, schema_hash, category, sql, sql_fingerprint,
                     error_code, reason, detail, status, occur_count, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)
                """,
                (
                    question,
                    normalized,
                    schema_hash,
                    category,
                    sql,
                    fingerprint,
                    error_code,
                    reason,
                    detail,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM counterexamples WHERE id = ?", (cursor.lastrowid,)).fetchone()
        result = self._row(row)
        result.update({"created": True, "duplicate": False})
        return result

    def list_counterexamples(
        self, status: str | None = "open", schema_hash: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM counterexamples"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if schema_hash is not None:
            clauses.append("schema_hash = ?")
            params.append(schema_hash)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY occur_count DESC, last_seen_at DESC, id DESC LIMIT ?"
        params.append(max(1, limit))
        with self._session() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def has_counterexample(self, question: str, sql: str, *, schema_hash: str = "") -> dict[str, Any] | None:
        """The exact wrong answer for this question, if it has been recorded."""
        normalized = normalize_question(question)
        fingerprint = hashlib.sha256((sql or "").strip().encode("utf-8")).hexdigest()[:16]
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM counterexamples WHERE normalized_question = ? "
                "AND schema_hash = ? AND sql_fingerprint = ?",
                (normalized, schema_hash, fingerprint),
            ).fetchone()
        return self._row(row) if row else None

    def resolve_counterexamples(self, question: str, *, schema_hash: str = "", note: str = "") -> int:
        """Mark every recorded wrong answer for one question as superseded.

        Kept open rather than deleted: the shape still failed, and that fact is what
        makes the admission gate useful the next time a similar SQL is proposed.
        """
        normalized = normalize_question(question)
        now = _now()
        with self._session() as conn:
            cursor = conn.execute(
                "UPDATE counterexamples SET status = 'resolved', resolved_at = ?, detail = ? "
                "WHERE normalized_question = ? AND schema_hash = ? AND status = 'open'",
                (now, (note or "").strip()[:200], normalized, schema_hash),
            )
            return int(cursor.rowcount or 0)

    def counterexample_stats(self) -> dict[str, Any]:
        with self._session() as conn:
            by_status = {
                str(row["status"]): int(row["n"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM counterexamples GROUP BY status"
                ).fetchall()
            }
            by_code = {
                str(row["error_code"] or "none"): int(row["n"])
                for row in conn.execute(
                    "SELECT error_code, COUNT(*) AS n FROM counterexamples "
                    "WHERE status = 'open' GROUP BY error_code"
                ).fetchall()
            }
            repeated = conn.execute(
                "SELECT COALESCE(SUM(occur_count), 0) AS n FROM counterexamples"
            ).fetchone()["n"]
        return {
            "counterexamples_total": sum(by_status.values()),
            "counterexamples_open": by_status.get("open", 0),
            "counterexamples_resolved": by_status.get("resolved", 0),
            "counterexamples_by_code": by_code,
            "counterexample_occurrences": int(repeated),
        }

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

        now = _now()
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
        resolved_counterexamples = self.resolve_counterexamples(
            failure["question"],
            schema_hash=str(failure["schema_hash"] or ""),
            note=note or f"resolved by failure {failure_id}",
        )
        return {
            "failure": self._row(row),
            "experience": experience,
            "resolved_counterexamples": resolved_counterexamples,
        }

    def record_feedback(
        self,
        question: str,
        sql: str = "",
        *,
        reason: str,
        detail: str = "",
        suggested_sql: str = "",
        category: str = "",
        schema_hash: str = "",
        session_id: str = "",
        turn_index: int = -1,
        trace_id: str = "",
        principal: str = "",
    ) -> dict[str, Any]:
        """Turn a user complaint into a review item, a failure and a counterexample.

        The criticised SQL is recorded as a counterexample, not as a candidate
        experience: it is the answer the user rejected, so it must not become
        few-shot guidance. A correction typed in the same report is stored as a
        candidate (never retrievable) for the reviewer to accept or replace.
        """
        question = (question or "").strip()
        sql = (sql or "").strip()
        suggested_sql = (suggested_sql or "").strip()
        reason = (reason or "").strip()
        if not question:
            return {"created": False, "skipped": "empty_question"}
        if reason not in FEEDBACK_REASONS:
            raise ValueError(f"reason 只能是 {', '.join(FEEDBACK_REASONS)}")
        normalized = normalize_question(question)
        category = (category or "").strip() or classify_category(question)
        detail = (detail or "").strip()[:500]
        now = _now()

        if session_id and turn_index >= 0:
            with self._session() as conn:
                existing = conn.execute(
                    "SELECT * FROM feedback WHERE session_id = ? AND turn_index = ? "
                    "AND reason = ? AND status = ?",
                    (session_id, int(turn_index), reason, FEEDBACK_OPEN),
                ).fetchone()
                if existing is not None:
                    conn.execute(
                        """
                        UPDATE feedback
                        SET occur_count = occur_count + 1, last_seen_at = ?,
                            detail = CASE WHEN ? != '' THEN ? ELSE detail END,
                            suggested_sql = CASE WHEN ? != '' THEN ? ELSE suggested_sql END
                        WHERE id = ?
                        """,
                        (now, detail, detail, suggested_sql, suggested_sql, existing["id"]),
                    )
                    row = conn.execute("SELECT * FROM feedback WHERE id = ?", (existing["id"],)).fetchone()
                    return {
                        "created": False,
                        "duplicate": True,
                        "feedback": self._row(row),
                        "failure": None,
                        "candidate": None,
                    }

        failure = self.record_failure(
            question,
            reason=f"user_feedback:{reason}",
            category=category,
            sql=sql,
            detail=detail,
            error_code=f"feedback_{reason}",
            schema_hash=schema_hash,
        )
        counterexample_id = (failure.get("counterexample") or {}).get("id")
        candidate = None
        if suggested_sql and suggested_sql != sql:
            candidate = self.add(
                question,
                suggested_sql,
                category=category,
                source="feedback",
                verified=False,
                schema_hash=schema_hash,
            )
        row: sqlite3.Row | None = None
        try:
            with self._session() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO feedback
                        (session_id, turn_index, trace_id, question, normalized_question,
                         effective_question, sql, suggested_sql, category, reason, detail,
                         schema_hash, principal, status, occur_count, failure_id,
                         failure_owned, counterexample_id, experience_id, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        int(turn_index),
                        trace_id,
                        question,
                        normalized,
                        question,
                        sql,
                        suggested_sql,
                        category,
                        reason,
                        detail,
                        schema_hash,
                        principal,
                        FEEDBACK_OPEN,
                        failure["id"],
                        1 if failure.get("created") or failure.get("reopened") else 0,
                        counterexample_id,
                        candidate["id"] if candidate else None,
                        now,
                        now,
                    ),
                )
                row = conn.execute("SELECT * FROM feedback WHERE id = ?", (cursor.lastrowid,)).fetchone()
            created = True
        except sqlite3.IntegrityError:
            # A concurrent click won the partial unique index; fold into its row.
            with self._session() as conn:
                row = conn.execute(
                    "SELECT * FROM feedback WHERE session_id = ? AND turn_index = ? "
                    "AND reason = ? AND status = ?",
                    (session_id, int(turn_index), reason, FEEDBACK_OPEN),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE feedback SET occur_count = occur_count + 1, last_seen_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                    row = conn.execute("SELECT * FROM feedback WHERE id = ?", (row["id"],)).fetchone()
            created = False
        return {
            "created": created,
            "duplicate": not created,
            "feedback": self._row(row) if row is not None else {},
            "failure": failure,
            "candidate": candidate,
        }

    def get_feedback(self, feedback_id: int) -> dict[str, Any]:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
        if row is None:
            raise KeyError(f"反馈 {feedback_id} 不存在")
        return self._row(row)

    def list_feedback(
        self,
        status: str | None = FEEDBACK_OPEN,
        limit: int = 50,
        *,
        since: str = "",
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM feedback"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if since:
            clauses.append("last_seen_at >= ?")
            params.append(since)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY occur_count DESC, last_seen_at DESC, id DESC LIMIT ?"
        params.append(max(1, limit))
        with self._session() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def review_feedback(
        self,
        feedback_id: int,
        action: str,
        *,
        sql: str = "",
        note: str = "",
        validator: Validator | None = None,
        reviewer: str = "",
    ) -> dict[str, Any]:
        """Approve with a corrected SQL, or reject with a stated reason.

        Approval reuses :meth:`resolve_failure`, so the SQL must pass the guard and
        execute before it becomes verified. Rejection dismisses only the queue row
        this complaint created and resolves its own counterexample; an unrelated
        earlier failure on the same question is left alone.
        """
        action = (action or "").strip().lower()
        if action not in {"approve", "reject"}:
            raise ValueError("action 只能是 approve 或 reject")
        feedback = self.get_feedback(feedback_id)
        if feedback["status"] != FEEDBACK_OPEN:
            raise ValueError(f"反馈 {feedback_id} 已审核（{feedback['status']}），不能重复处理")
        note = (note or "").strip()
        reviewer = (reviewer or "").strip()
        now = _now()

        if action == "approve":
            sql = (sql or "").strip()
            if not sql:
                raise ValueError("通过反馈需要给出修正后的 SQL")
            if validator is None:
                raise ValueError("通过反馈必须提供 validator（护栏 + 真实执行），不能凭肉眼结案")
            failure_id = feedback.get("failure_id")
            if not failure_id:
                raise ValueError("反馈缺少关联的失败记录，无法通过；请先排查失败队列")
            resolved = self.resolve_failure(failure_id, sql, validator=validator, note=note)
            with self._session() as conn:
                conn.execute(
                    """
                    UPDATE feedback
                    SET status = ?, reviewed_at = ?, reviewer = ?, review_note = ?,
                        experience_id = ?
                    WHERE id = ?
                    """,
                    (
                        FEEDBACK_APPROVED,
                        now,
                        reviewer,
                        note,
                        resolved["experience"]["id"],
                        feedback_id,
                    ),
                )
                row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
            return {
                "feedback": self._row(row),
                "failure": resolved["failure"],
                "experience": resolved["experience"],
                "resolved_counterexamples": resolved["resolved_counterexamples"],
            }

        if not note:
            raise ValueError("拒绝反馈必须说明原因")
        dismissed = False
        resolved_counterexamples = 0
        with self._session() as conn:
            conn.execute(
                "UPDATE feedback SET status = ?, reviewed_at = ?, reviewer = ?, review_note = ? "
                "WHERE id = ?",
                (FEEDBACK_REJECTED, now, reviewer, note, feedback_id),
            )
            if feedback.get("failure_id") and feedback.get("failure_owned"):
                cursor = conn.execute(
                    "UPDATE failure_queue SET status = 'dismissed', resolved_at = ?, resolution = ? "
                    "WHERE id = ? AND status = 'open' AND reason LIKE 'user_feedback:%'",
                    (now, note, feedback["failure_id"]),
                )
                dismissed = bool(cursor.rowcount)
            if feedback.get("counterexample_id"):
                counterexample = conn.execute(
                    "SELECT * FROM counterexamples WHERE id = ?", (feedback["counterexample_id"],)
                ).fetchone()
                # Only undo the complaint's own record. An earlier execution/guard
                # counterexample for the same shape survives a rejected complaint.
                if counterexample is not None and str(counterexample["error_code"]).startswith("feedback_"):
                    cursor = conn.execute(
                        "UPDATE counterexamples SET status = 'resolved', resolved_at = ?, detail = ? "
                        "WHERE id = ? AND status = 'open'",
                        (now, note, feedback["counterexample_id"]),
                    )
                    resolved_counterexamples = int(cursor.rowcount or 0)
            row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
        return {
            "feedback": self._row(row),
            "dismissed_failure": dismissed,
            "resolved_counterexamples": resolved_counterexamples,
        }

    def feedback_stats(self, *, since: str = "") -> dict[str, Any]:
        query = "SELECT status, reason, COUNT(*) AS n, COALESCE(SUM(occur_count), 0) AS occurrences FROM feedback"
        params: list[Any] = []
        if since:
            query += " WHERE last_seen_at >= ?"
            params.append(since)
        query += " GROUP BY status, reason"
        with self._session() as conn:
            rows = conn.execute(query, params).fetchall()
        by_status: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        occurrences = 0
        for row in rows:
            by_status[str(row["status"])] = by_status.get(str(row["status"]), 0) + int(row["n"])
            if str(row["status"]) == FEEDBACK_OPEN:
                by_reason[str(row["reason"])] = by_reason.get(str(row["reason"]), 0) + int(row["n"])
            occurrences += int(row["occurrences"])
        return {
            "feedback_total": sum(by_status.values()),
            "feedback_open": by_status.get(FEEDBACK_OPEN, 0),
            "feedback_approved": by_status.get(FEEDBACK_APPROVED, 0),
            "feedback_rejected": by_status.get(FEEDBACK_REJECTED, 0),
            "feedback_by_reason": by_reason,
            "feedback_occurrences": occurrences,
        }

    def top_failures(
        self,
        *,
        since: str = "",
        limit: int = 10,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM failure_queue"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if since:
            clauses.append("last_seen_at >= ?")
            params.append(since)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY occur_count DESC, last_seen_at DESC, id DESC LIMIT ?"
        params.append(max(1, limit))
        with self._session() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def schema_impact(self, active_schema_hash: str) -> dict[str, Any]:
        """What a catalog version change touches across experiences, failures and feedback."""
        active = (active_schema_hash or "").strip()
        with self._session() as conn:
            feedback_rows = conn.execute(
                "SELECT schema_hash, status, COUNT(*) AS n FROM feedback GROUP BY schema_hash, status"
            ).fetchall()
            failure_rows = conn.execute(
                "SELECT schema_hash, status, COUNT(*) AS n FROM failure_queue GROUP BY schema_hash, status"
            ).fetchall()

        def _group(rows: Sequence[sqlite3.Row]) -> dict[str, dict[str, int]]:
            out: dict[str, dict[str, int]] = {}
            for row in rows:
                out.setdefault(str(row["schema_hash"] or ""), {})[str(row["status"])] = int(row["n"])
            return out

        feedback_by_schema = _group(feedback_rows)
        failures_by_schema = _group(failure_rows)
        return {
            "active_schema_hash": active,
            "experience": self.staleness(active),
            "feedback": {
                "by_schema": feedback_by_schema,
                "on_stale": sum(
                    bucket.get(FEEDBACK_OPEN, 0)
                    for name, bucket in feedback_by_schema.items()
                    if name != active
                ),
            },
            "failures": {
                "by_schema": failures_by_schema,
                "on_stale": sum(
                    bucket.get("open", 0)
                    for name, bucket in failures_by_schema.items()
                    if name != active
                ),
            },
        }

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
