"""Token and cost rollups for the event log.

No vendor price is hardcoded. A deployment supplies its negotiated prices via
``NL2SQL_PRICE_INPUT_PER_1M`` / ``NL2SQL_PRICE_OUTPUT_PER_1M`` (and optionally
``NL2SQL_PRICE_CACHED_INPUT_PER_1M``); without them this module reports tokens
and marks the cost as not configured instead of printing a confident zero.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .observability import EVENT_TURN


def normalize_prices(prices: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate a price table; ``None`` means "prices not configured"."""
    if not prices:
        return None
    input_price = prices.get("input_per_1m")
    output_price = prices.get("output_per_1m")
    if input_price is None and output_price is None:
        return None
    if input_price is None or output_price is None:
        raise ValueError("价格表必须同时给出 input_per_1m 与 output_per_1m")
    table = {
        "input_per_1m": float(input_price),
        "output_per_1m": float(output_price),
        "currency": str(prices.get("currency") or "USD").upper(),
    }
    if table["input_per_1m"] < 0 or table["output_per_1m"] < 0:
        raise ValueError("价格不能为负数")
    cached = prices.get("cached_input_per_1m")
    if cached is not None:
        if float(cached) < 0:
            raise ValueError("缓存命中价格不能为负数")
        table["cached_input_per_1m"] = float(cached)
    return table


def _tokens(item: dict[str, Any]) -> dict[str, int]:
    prompt = int(item.get("prompt_tokens") or 0)
    completion = int(item.get("completion_tokens") or 0)
    cached = int(item.get("cached_tokens") or 0)
    total = int(item.get("tokens") or 0)
    return {
        "total": total,
        "prompt": prompt,
        "completion": completion,
        "cached": cached,
        "unclassified": max(0, total - prompt - completion),
    }


def _cost(tokens: dict[str, int], prices: dict[str, Any] | None) -> float | None:
    if prices is None:
        return None
    cached = min(tokens["cached"], tokens["prompt"]) if tokens["prompt"] else tokens["cached"]
    billable_prompt = max(0, tokens["prompt"] - cached)
    cached_price = float(prices.get("cached_input_per_1m", prices["input_per_1m"]))
    # Unclassified tokens (e.g. a second-channel call that only reported a total)
    # are billed at the input rate: an explicit lower bound, never a hidden zero.
    amount = (
        (billable_prompt + tokens["unclassified"]) / 1_000_000 * float(prices["input_per_1m"])
        + cached / 1_000_000 * cached_price
        + tokens["completion"] / 1_000_000 * float(prices["output_per_1m"])
    )
    return round(amount, 6)


def _empty_bucket() -> dict[str, Any]:
    return {
        "turns": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "unclassified_tokens": 0,
        "estimated_cost": None,
    }


def _add(bucket: dict[str, Any], tokens: dict[str, int], cost: float | None) -> None:
    bucket["turns"] += 1
    bucket["total_tokens"] += tokens["total"]
    bucket["prompt_tokens"] += tokens["prompt"]
    bucket["completion_tokens"] += tokens["completion"]
    bucket["cached_tokens"] += tokens["cached"]
    bucket["unclassified_tokens"] += tokens["unclassified"]
    if cost is not None:
        bucket["estimated_cost"] = round((bucket["estimated_cost"] or 0.0) + cost, 6)


def cost_summary(
    events: Sequence[dict[str, Any]], *, prices: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Roll turns into token (and, when priced, cost) totals by domain and user."""
    table = normalize_prices(prices)
    turns = [item for item in events if item.get("event") == EVENT_TURN]
    overall = _empty_bucket()
    by_category: dict[str, dict[str, Any]] = {}
    by_principal: dict[str, dict[str, Any]] = {}
    cache_hits = 0
    saved_tokens = 0
    for item in turns:
        tokens = _tokens(item)
        cost = _cost(tokens, table)
        _add(overall, tokens, cost)
        category = str(item.get("category") or "unknown")
        principal = str(item.get("principal") or "anonymous")
        _add(by_category.setdefault(category, _empty_bucket()), tokens, cost)
        _add(by_principal.setdefault(principal, _empty_bucket()), tokens, cost)
        if item.get("cached"):
            cache_hits += 1
        saved_tokens += int(item.get("cache_saved_tokens") or 0)

    def _sorted(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return dict(sorted(raw.items(), key=lambda kv: (-kv[1]["total_tokens"], kv[0])))

    return {
        "turns": len(turns),
        "tokens": {
            "total": overall["total_tokens"],
            "prompt": overall["prompt_tokens"],
            "completion": overall["completion_tokens"],
            "cached": overall["cached_tokens"],
            "unclassified": overall["unclassified_tokens"],
        },
        "estimated_cost": overall["estimated_cost"],
        "pricing": {"configured": True, **table}
        if table is not None
        else {
            "configured": False,
            "note": "未配置价格：设置 NL2SQL_PRICE_INPUT_PER_1M / _OUTPUT_PER_1M 后才计算费用",
        },
        "cache": {
            "hits": cache_hits,
            "hit_rate": round(cache_hits / len(turns), 4) if turns else None,
            "saved_tokens": saved_tokens,
        },
        "by_category": _sorted(by_category),
        "by_principal": _sorted(by_principal),
    }


def cost_rollup_lines(report: dict[str, Any], *, by: str = "category") -> Iterable[str]:
    """Human-readable lines; shared by the CLI and the weekly-style reports."""
    pricing = report.get("pricing") or {}
    tokens = report.get("tokens") or {}
    yield (
        f"成本汇总  turns={report.get('turns', 0)} "
        f"tokens={tokens.get('total', 0)} prompt={tokens.get('prompt', 0)} "
        f"completion={tokens.get('completion', 0)} cached={tokens.get('cached', 0)} "
        f"未分类={tokens.get('unclassified', 0)}"
    )
    if pricing.get("configured"):
        cached_price = pricing.get("cached_input_per_1m", pricing.get("input_per_1m"))
        yield (
            f"价格  input={pricing['input_per_1m']}/1M output={pricing['output_per_1m']}/1M "
            f"cached={cached_price}/1M {pricing.get('currency', 'USD')}（用户配置）"
        )
        yield f"估算费用  {report.get('estimated_cost'):.6f} {pricing.get('currency', 'USD')}"
    else:
        yield "费用未配置（" + str(pricing.get("note") or "只报 token") + "）"
    cache = report.get("cache") or {}
    yield (
        f"结果缓存  命中={cache.get('hits', 0)} 命中率={cache.get('hit_rate')} "
        f"节省 token={cache.get('saved_tokens', 0)}"
    )
    buckets = report.get("by_category" if by == "category" else "by_principal") or {}
    label = "业务域" if by == "category" else "用户"
    if not buckets:
        yield f"按{label}  没有 turn 事件"
        return
    yield f"按{label}"
    currency = pricing.get("currency", "")
    for name, item in buckets.items():
        cost = item.get("estimated_cost")
        cost_text = f"{cost:.6f} {currency}".strip() if cost is not None else "—"
        yield (
            f"  {name}  turns={item['turns']} tokens={item['total_tokens']} "
            f"prompt={item['prompt_tokens']} completion={item['completion_tokens']} "
            f"cached={item['cached_tokens']} 费用={cost_text}"
        )
