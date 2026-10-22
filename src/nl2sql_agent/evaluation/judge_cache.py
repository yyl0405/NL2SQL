"""SQLite cache for AI judge verdicts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .judge import JudgeInput, JudgeModel, JudgeVerdict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS judge_cache (
    key TEXT PRIMARY KEY,
    verdict_json TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class JudgeCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> JudgeVerdict | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT verdict_json FROM judge_cache WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["verdict_json"]))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        verdict = JudgeVerdict.from_dict(payload)
        verdict.cached = True
        verdict.tokens = 0
        return verdict

    def put(self, key: str, verdict: JudgeVerdict) -> None:
        payload = json.dumps(verdict.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO judge_cache (key, verdict_json, model, prompt_version, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    verdict_json = excluded.verdict_json,
                    model = excluded.model,
                    prompt_version = excluded.prompt_version,
                    created_at = excluded.created_at
                """,
                (
                    key,
                    payload,
                    verdict.model,
                    verdict.prompt_version,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )

    def stats(self) -> dict[str, Any]:
        with self._session() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM judge_cache").fetchone()
        return {"path": str(self.path), "entries": int(row["count"]) if row else 0}


class CachedJudge:
    """Wraps a judge model and reuses successful, non-unknown verdicts."""

    def __init__(
        self,
        model: JudgeModel,
        cache: JudgeCache,
        *,
        model_name: str,
        prompt_version: str,
    ) -> None:
        self.inner = model
        self.cache = cache
        self.model = model_name
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.hits = 0
        self.misses = 0

    def judge(self, item: JudgeInput) -> JudgeVerdict:
        key = make_cache_key(self.model_name, self.prompt_version, item)
        cached = self.cache.get(key)
        if cached is not None:
            self.hits += 1
            cached.cached = True
            cached.tokens = 0
            return cached
        self.misses += 1
        verdict = self.inner.judge(item)
        if not verdict.is_unknown and not verdict.parse_error:
            self.cache.put(key, verdict)
        return verdict


def make_cache_key(model: str, prompt_version: str, item: JudgeInput) -> str:
    payload = json.dumps(
        {
            "model": model,
            "prompt_version": prompt_version,
            **item.cache_payload(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
