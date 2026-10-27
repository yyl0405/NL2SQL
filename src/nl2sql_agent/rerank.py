"""Optional LLM reranking for the top schema-linking candidates.

Reranking is deliberately outside the measured retrieval chain: it costs a
model call, and ``link-check`` must stay model-free. It is a production-only
option (``NL2SQL_RERANK=llm``) whose contract is strict — names not present in
the candidate list are dropped, and any failure returns the original BM25 or
fused order untouched.
"""

from __future__ import annotations

from typing import Any, Sequence

from .catalog import TableHit
from .text import parse_json_object


class RerankError(RuntimeError):
    pass


class LLMReranker:
    def __init__(self, llm: Any, max_candidates: int = 12) -> None:
        self.llm = llm
        self.max_candidates = max(1, int(max_candidates))

    def rerank(self, question: str, hits: Sequence[TableHit], top_k: int) -> list[TableHit]:
        candidates = list(hits)[: self.max_candidates]
        if len(candidates) <= top_k:
            return list(hits)[:top_k]
        listing = "\n".join(
            f"- {hit.table.name}: {hit.table.description}" for hit in candidates
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 schema linking 重排器。只输出 JSON 对象，不要解释。\n"
                    '契约: {"tables": ["表名", ...]}，按完成查询所需程度排序。'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户问题: {question}\n\n候选表:\n{listing}\n\n"
                    f"最多返回 {top_k} 个表名，只能使用上面出现过的表名。"
                ),
            },
        ]
        response = self.llm.chat(messages, tools=None)
        payload = parse_json_object(getattr(response, "content", "") or "")
        if not isinstance(payload, dict):
            raise RerankError("rerank 响应不是 JSON 对象")
        names = payload.get("tables")
        if not isinstance(names, list) or not names:
            raise RerankError("rerank 响应缺少 tables 数组")

        by_name = {hit.table.name.strip().lower(): hit for hit in candidates}
        chosen: list[TableHit] = []
        seen: set[str] = set()
        for raw in names:
            key = str(raw).strip().strip('"[]`').lower()
            if key in by_name and key not in seen:
                chosen.append(by_name[key])
                seen.add(key)
        if not chosen:
            raise RerankError("rerank 返回的表名都不在候选集内")
        chosen.extend(hit for hit in candidates if hit.table.name.strip().lower() not in seen)
        return chosen[:top_k]
