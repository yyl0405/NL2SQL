"""Answer cache: normalized question + schema + permission scope + history, short TTL.

The cache stores the agent's final :class:`AgentResult` only for answers that
already passed the guard and executed. A hit therefore never skips the guard on
a fresh SQL string; it skips the model loop. The TTL bounds how stale database
rows can become, and the key carries the schema hash, the principal and the
conversation prefix so an answer cannot cross a permission or catalog boundary.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Sequence

from .text import normalize_question

VERSION = 1


def cache_key(
    *,
    question: str,
    schema_hash: str,
    principal: str,
    allow_sensitive: bool,
    policy: dict[str, Any] | None = None,
    context: Sequence[dict[str, Any]] = (),
) -> str:
    """Stable key; volatile whitespace/punctuation is normalized away."""
    payload = {
        "v": VERSION,
        "question": normalize_question(question),
        "schema": str(schema_hash or ""),
        "principal": str(principal or ""),
        "allow_sensitive": bool(allow_sensitive),
        "policy": policy or {},
        "context": list(context),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ResultCache:
    """Bounded LRU with a per-entry TTL; disabled when ``ttl_sec <= 0``."""

    def __init__(
        self,
        ttl_sec: float,
        max_entries: int = 128,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_sec = max(0.0, float(ttl_sec))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock or time.monotonic
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.expired = 0
        self.evictions = 0

    @property
    def enabled(self) -> bool:
        return self.ttl_sec > 0

    def get(self, key: str) -> tuple[Any, float] | None:
        if not self.enabled:
            return None
        now = self._clock()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self.misses += 1
                return None
            stored_at, value = item
            age = now - stored_at
            if age >= self.ttl_sec:
                self._items.pop(key, None)
                self.expired += 1
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return value, age

    def put(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._items[key] = (self._clock(), value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
                self.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            entries = len(self._items)
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "ttl_sec": self.ttl_sec,
            "max_entries": self.max_entries,
            "entries": entries,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else None,
            "expired": self.expired,
            "evictions": self.evictions,
        }
