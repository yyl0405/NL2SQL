"""Paired recall audit for the experience library.

The question this answers is narrow and measurable offline: when a user asks a
paraphrase of a question the library already knows, does retrieval put the paired
original in the top 3? The paraphrase corpus carries `paraphrase_of`, so the target is a
property of the corpus rather than a judgement call, and every arm is scored against the
same pairs.

What it cannot answer is whether the *model* answers better with those examples. That
needs a paid run and a judge, and the few-shot A/B is reported separately. It also cannot
prove semantic recall: the default offline provider is `hash`, which is a bag of tokens
in vector clothing, so the vector arms demonstrate the cache/fusion/degradation plumbing
and nothing about meaning. With `--embedding openai` the same instrument measures the
real thing, if a key is configured.

Nothing here writes to the user's own experience database: the library is rebuilt in a
temporary directory from the frozen corpus, and answers are never generated.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from .config import Settings
from .embeddings import EmbeddingProvider, build_embedding_provider
from .guard_suite import load_case_file
from .memory import ExperienceStore
from .recall import ExperienceRecall

PARAPHRASE_CASES = Path(__file__).resolve().parent / "data" / "paraphrase_cases.jsonl"
FROZEN_CASES = Path(__file__).resolve().parent / "data" / "demo_cases.jsonl"
TOP_K = 5
DIVERSITY = 0.5
GOLD_BOOST = 0.2
ARMS = ("lexical", "diversity", "gold-half", "vector-hash", "rrf-hash")


@dataclass
class ArmScore:
    arm: str
    mode: str
    provider: str = ""
    queries: int = 0
    hit: dict[int, int] = field(default_factory=dict)
    reciprocal_rank: float = 0.0
    displaced: int = 0
    gold_target_hit3: int = 0
    gold_target_queries: int = 0
    plain_target_hit3: int = 0
    plain_target_queries: int = 0
    degraded: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "mode": self.mode,
            "provider": self.provider,
            "queries": self.queries,
            "recall": {f"@{k}": round(v / self.queries, 4) if self.queries else None for k, v in self.hit.items()},
            "mrr": round(self.reciprocal_rank / self.queries, 4) if self.queries else None,
            "displaced_by_diversity": self.displaced,
            "degraded": self.degraded,
        }


@dataclass
class LibraryProfile:
    name: str
    rows: int
    arms: dict[str, ArmScore] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.name,
            "library_rows": self.rows,
            "arms": {name: score.to_dict() for name, score in self.arms.items()},
            "gold_split": {
                score.arm: {
                    "gold_targets": f"{score.gold_target_hit3}/{score.gold_target_queries}",
                    "plain_targets": f"{score.plain_target_hit3}/{score.plain_target_queries}",
                }
                for score in self.arms.values()
                if score.arm == "gold-half"
            },
        }


def load_pairs(
    frozen_path: Path = FROZEN_CASES, paraphrase_path: Path = PARAPHRASE_CASES
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Frozen rows usable as experience, the resolved pairs, and how many rows were skipped.

    A frozen case without a `gold_sql` is a refusal or clarification case: it has no SQL to
    store as experience, so it cannot be a retrieval target either.
    """
    raw = load_case_file(frozen_path)
    frozen = [case for case in raw if case.get("question") and case.get("gold_sql")]
    skipped = len(raw) - len(frozen)
    by_id = {str(case.get("id")): case for case in frozen}
    pairs: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for case in load_case_file(paraphrase_path):
        target = str(case.get("paraphrase_of") or "")
        if target not in by_id:
            unresolved.append(str(case.get("id")))
            continue
        pairs.append(
            {
                "id": str(case.get("id")),
                "query": str(case.get("question") or ""),
                "target_id": target,
                "target_question": str(by_id[target].get("question") or ""),
            }
        )
    if unresolved:
        pairs.append({"unresolved": unresolved})
    return frozen, pairs, skipped


def _seed(rows: Sequence[dict[str, Any]], schema_hash: str, *, gold: bool) -> ExperienceStore:
    tmp = tempfile.mkdtemp(prefix="nl2sql-experience-audit-")
    store = ExperienceStore(Path(tmp) / "experience.db")
    for index, row in enumerate(rows):
        record = store.add(
            str(row["question"]),
            str(row["gold_sql"]),
            category=str(row.get("category") or ""),
            verified=True,
            schema_hash=schema_hash,
        )
        if gold and index % 2 == 0:
            store.set_grade(int(record["id"]), "gold")
    return store


def _target_id(store: ExperienceStore, question: str, schema_hash: str) -> int | None:
    for row in store.verified_rows(schema_hash):
        if row["question"] == question:
            return int(row["id"])
    return None


def _score_arm(
    store: ExperienceStore,
    pairs: list[dict[str, Any]],
    schema_hash: str,
    *,
    arm: str,
    mode: str,
    provider: EmbeddingProvider | None,
    diversity: float = 0.0,
    gold_boost: float = 0.0,
    cache_dir: Path | None = None,
) -> ArmScore:
    recall = ExperienceRecall(
        store,
        provider=provider,
        cache_dir=cache_dir,
        mode=mode,
        diversity=diversity,
        gold_boost=gold_boost,
    )
    score = ArmScore(
        arm=arm,
        mode=recall.effective_mode,
        provider=provider.name if provider is not None else "",
        hit={k: 0 for k in (1, 3, TOP_K)},
    )
    gold_ids = {
        int(row["id"]) for row in store.verified_rows(schema_hash) if row["quality"] == "gold"
    }
    for pair in pairs:
        if "unresolved" in pair:
            continue
        target = _target_id(store, pair["target_question"], schema_hash)
        if target is None:
            continue
        result = recall.rank(
            pair["query"], schema_hash=schema_hash, top_k=TOP_K, min_score=0.0
        )
        ids = [int(item["id"]) for item in result.items]
        score.queries += 1
        score.displaced += result.displaced_by_diversity
        score.degraded += int(result.degraded)
        for k in score.hit:
            if target in ids[:k]:
                score.hit[k] += 1
        if target in ids:
            score.reciprocal_rank += 1.0 / (ids.index(target) + 1)
        if arm == "gold-half":
            if target in gold_ids:
                score.gold_target_queries += 1
                score.gold_target_hit3 += int(target in ids[:3])
            else:
                score.plain_target_queries += 1
                score.plain_target_hit3 += int(target in ids[:3])
    return score


def run_experience_audit(
    *,
    frozen_path: Path = FROZEN_CASES,
    paraphrase_path: Path = PARAPHRASE_CASES,
    settings: Settings | None = None,
    embedding: str = "hash",
    include_full_library: bool = True,
) -> dict[str, Any]:
    frozen, raw_pairs, skipped = load_pairs(frozen_path, paraphrase_path)
    unresolved = next((pair["unresolved"] for pair in raw_pairs if "unresolved" in pair), [])
    pairs = [pair for pair in raw_pairs if "unresolved" not in pair]
    settings = settings or Settings(provider="mock")
    settings.embedding_provider = embedding
    provider = build_embedding_provider(replace(settings, embedding_provider=embedding))

    profiles: list[LibraryProfile] = []
    invariant_violations: list[str] = []
    referenced = {pair["target_id"] for pair in pairs}
    for name, rows in (
        ("paired", [row for row in frozen if str(row.get("id")) in referenced]),
        ("full", frozen),
    ):
        if name == "full" and not include_full_library:
            continue
        store = _seed(rows, "audit", gold=False)
        profile = LibraryProfile(name=name, rows=len(store.verified_rows("audit")))
        arms = {
            "lexical": dict(mode="lexical"),
            "diversity": dict(mode="lexical", diversity=DIVERSITY),
            "gold-half": dict(mode="lexical", gold_boost=GOLD_BOOST, gold_rows=True),
            "vector-hash": dict(mode="vector"),
            "rrf-hash": dict(mode="rrf"),
        }
        for arm, kwargs in arms.items():
            if kwargs.pop("gold_rows", False):
                store = _seed(rows, "audit", gold=True)
                profile.rows = len(store.verified_rows("audit"))
            mode = kwargs.pop("mode")
            profile.arms[arm] = _score_arm(
                store,
                pairs,
                "audit",
                arm=arm,
                mode=mode,
                provider=provider if mode != "lexical" else None,
                **kwargs,
            )
        profiles.append(profile)
        # The default configuration must reproduce the legacy store path exactly.
        recall = ExperienceRecall(store, mode="lexical")
        for pair in pairs:
            legacy = [int(row["id"]) for row in store.search(
                pair["query"], schema_hash="audit", top_k=TOP_K, min_score=0.0, record=False
            )]
            default = [int(item["id"]) for item in recall.rank(
                pair["query"], schema_hash="audit", top_k=TOP_K, min_score=0.0
            ).items]
            if legacy != default:
                invariant_violations.append(f"{name}:{pair['id']}:lexical_differs_from_store_search")
                break

    report: dict[str, Any] = {
        "measured": bool(pairs),
        "provider": provider.name if provider is not None else "",
        "provider_is_lexical": (provider.name.startswith("hash") if provider else True),
        "queries": len(pairs),
        "frozen_cases": len(frozen),
        "frozen_without_sql": skipped,
        "unresolved": unresolved,
        "profiles": [profile.to_dict() for profile in profiles],
        "invariant_violations": invariant_violations,
        "caveats": [
            "仅测量召回，不涉及模型生成质量；few-shot 对准确率的增益需付费实跑。",
            "hash 向量是词袋，vector/rrf 两臂只验证缓存与融合管道，不能作为语义召回证据。",
            "gold-half 臂用 id 奇偶确定性确定 gold，报告按目标是否被提升分组，是偏差审计而非增益。",
            "库在临时目录重建，不读写 var/experience.db。",
        ],
    }
    return report


def audit_fingerprint(report: dict[str, Any]) -> str:
    payload = "|".join(
        f"{profile['profile']}:" + ",".join(
            f"{arm}:{data['recall']['@3']}" for arm, data in profile["arms"].items()
        )
        for profile in report["profiles"]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
