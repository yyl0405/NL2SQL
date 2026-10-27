"""Compare an evaluation run against a committed baseline and decide whether it regressed.

A baseline holds aggregate numbers only (metrics, provider, corpus size, guard
configuration), so a run against a attached catalog can produce one without putting a
single table name, column name or result row into the repository.
"""

from __future__ import annotations

from typing import Any

BASELINE_KIND = "nl2sql-ex-baseline"
CORPUS_FIELDS = ("total", "answerable", "refusals", "clarifications", "skipped")

GATED_METRICS = (
    "execution_accuracy",
    "overall_accuracy",
    "valid_sql_rate",
    "execution_success_rate",
    "refusal_accuracy",
    "clarification_accuracy",
    "table_recall_at_5",
)

# `avg_latency_ms`, `avg_steps` and `avg_tokens` are deliberately not gated: they move
# with machine load and model sampling, so a gate that fires on them gets ignored — and
# an ignored gate no longer protects the accuracy numbers.


def build_baseline(report: dict[str, Any]) -> dict[str, Any]:
    metrics = report.get("metrics") or {}
    metadata = report.get("metadata") or {}
    missing = [key for key in ("provider", "total", "answerable") if report.get(key) is None]
    if missing:
        raise ValueError(f"报告缺少 {missing}，不能写成基线")
    return {
        "kind": BASELINE_KIND,
        "generated_at": report.get("generated_at"),
        "provider": report.get("provider"),
        "model": metadata.get("model"),
        "prompt_version": metadata.get("prompt_version"),
        "git_sha": metadata.get("git_sha"),
        "guard": metadata.get("guard"),
        "corpus": {key: int(report.get(key) or 0) for key in CORPUS_FIELDS},
        "metrics": {
            key: float(metrics[key])
            for key in GATED_METRICS
            if isinstance(metrics.get(key), (int, float))
        },
    }


def check_regression(baseline: dict[str, Any], report: dict[str, Any], max_drop: float) -> dict[str, Any]:
    """Return the gate verdict. Raises ``ValueError`` when the two runs are not comparable.

    Refusing to compare is the point: a baseline taken on the 40-case subset differs from
    a full-corpus run by more than any real regression, so a silent subtraction would
    fail every merge until someone deleted the gate.
    """
    if not 0.0 <= max_drop <= 1.0:
        raise ValueError(f"--max-drop 要在 0 到 1 之间，当前是 {max_drop}")
    for key in ("provider", "total", "answerable"):
        if report.get(key) is None:
            raise ValueError(f"本次报告缺少 {key}，无法与基线比对")
    baseline_metrics = baseline.get("metrics")
    if not isinstance(baseline_metrics, dict) or not baseline_metrics:
        raise ValueError("基线里没有 metrics，请先用 --write-baseline 生成")
    baseline_provider = baseline.get("provider")
    if baseline_provider is None:
        raise ValueError("基线里没有 provider：一份不知道该运行在哪个模型上的基线无法判定可比性")
    if baseline_provider != report["provider"]:
        raise ValueError(
            f"provider 不同（基线 {baseline_provider} / 本次 {report['provider']}），"
            "两者的执行准确率不是同一件事"
        )
    baseline_corpus = baseline.get("corpus")
    if not isinstance(baseline_corpus, dict):
        raise ValueError("基线里没有 corpus 计数，看不出它跑的是哪套语料")
    for key in ("total", "answerable"):
        if int(baseline_corpus.get(key, -1)) != int(report.get(key, -2)):
            raise ValueError(
                f"语料规模不同（基线 {baseline_corpus.get(key)} / 本次 {report.get(key)} 个 {key} 用例），"
                "不同语料的准确率不能直接相减"
            )

    metrics = report.get("metrics") or {}
    checked: list[dict[str, Any]] = []
    skipped: list[str] = []
    regressions: list[dict[str, Any]] = []
    for key in GATED_METRICS:
        old = baseline_metrics.get(key)
        new = metrics.get(key)
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            skipped.append(key)
            continue
        delta = float(new) - float(old)
        # Rounded before comparing: `0.44 - 0.46` is not exactly `-0.02` in binary
        # floating point, and a threshold that a metric can sit on exactly must not be
        # decided by representation noise.
        regressed = round(delta, 10) < round(-max_drop, 10)
        entry = {
            "metric": key,
            "baseline": round(float(old), 6),
            "current": round(float(new), 6),
            "delta": round(delta, 6),
            "delta_pp": round(delta * 100.0, 2),
            "regressed": regressed,
        }
        checked.append(entry)
        if regressed:
            regressions.append(entry)
    if not checked:
        raise ValueError("基线与本次报告没有任何一个受门禁指标重叠，比对没有意义")
    return {
        "kind": baseline.get("kind"),
        "max_drop": max_drop,
        "baseline": {
            "provider": baseline_provider,
            "generated_at": baseline.get("generated_at"),
            "git_sha": baseline.get("git_sha"),
            "prompt_version": baseline.get("prompt_version"),
            "guard": baseline.get("guard"),
            "corpus": baseline_corpus,
        },
        "checked": checked,
        "skipped": skipped,
        "regressions": regressions,
        "ok": not regressions,
    }


def format_regression(verdict: dict[str, Any]) -> list[str]:
    """Human-readable lines; the numbers are proportions in the file, percent on screen."""
    limit = f"{verdict['max_drop'] * 100:.2f}pp"
    if verdict["ok"]:
        lines = [f"EX 回归门禁: 通过（阈值 {limit}，比对 {len(verdict['checked'])} 项指标）"]
    else:
        lines = [f"EX 回归门禁: 失败（阈值 {limit}）"]
        for item in verdict["regressions"]:
            lines.append(
                f"  {item['metric']}: {item['baseline']:.2%} -> {item['current']:.2%} "
                f"({item['delta_pp']:+.2f}pp)"
            )
    improved = [item for item in verdict["checked"] if item["delta_pp"] > 0]
    for item in improved:
        lines.append(
            f"  {item['metric']}: {item['baseline']:.2%} -> {item['current']:.2%} "
            f"({item['delta_pp']:+.2f}pp) 上升"
        )
    if verdict["skipped"]:
        lines.append(f"  本次未比对（缺任一侧数字）: {', '.join(verdict['skipped'])}")
    return lines
