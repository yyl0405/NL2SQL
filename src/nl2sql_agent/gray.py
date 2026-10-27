"""Deterministic traffic split for prompt gray releases.

The bucket is a pure function of ``(key, salt)``: the same conversation lands on
the same arm every turn, in every process. Per-request randomness would flip the
prompt mid-conversation and turn the comparison into noise.
"""

from __future__ import annotations

import hashlib

BUCKETS = 10_000


def bucket(key: str, *, salt: str = "gray-v1") -> int:
    raw = f"{salt}:{key}".encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16) % BUCKETS


def in_gray(key: str, percent: float, *, salt: str = "gray-v1") -> bool:
    """``percent`` is 0–100; 0 and 100 are exact, not approximations."""
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    return bucket(key, salt=salt) < int(round(percent * BUCKETS / 100))


def share(key: str, percent: float, *, salt: str = "gray-v1") -> dict[str, object]:
    """Explain one assignment; the bucket is kept so logs can be audited."""
    value = bucket(key, salt=salt)
    return {
        "key": key,
        "bucket": value,
        "threshold": int(round(max(0.0, min(100.0, percent)) * BUCKETS / 100)),
        "candidate": in_gray(key, percent, salt=salt),
    }
