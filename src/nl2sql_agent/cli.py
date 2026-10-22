"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent import AgentResult
from .config import Settings
from .demo import seed
from .evaluation import (
    CachedJudge,
    JudgeCache,
    JudgeError,
    LLMJudge,
    evaluate,
    load_cases,
)
from .evaluation.calibration import CalibrationResult, calibrate, load_calibration
from .memory import ExperienceStore
from .runtime import Runtime, create_runtime
from .text import format_table
from .tools.sql_tools import run_readonly_query

DEMO_QUESTIONS = [
    "2026年6月华东地区销售订单总金额是多少？",
    "库存低于安全库存的物料有哪些？",
    "整机设备的BOM所有子件及单台总用量是多少？",
    "订单ORD-1012的物料齐套率是多少？",
    "客户满意度评分是多少？",
]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    return int(args.func(args) or 0)


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=["mock", "openai"], help="覆盖 NL2SQL_PROVIDER")
    parser.add_argument("--home", help="运行目录（默认当前目录，var/ 放在其下）")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nl2sql-agent",
        description="Tool-calling NL2SQL agent for manufacturing ERP analytics.",
    )
    sub = parser.add_subparsers(dest="command")

    init_parser = sub.add_parser("init", help="初始化演示数据库与经验库")
    init_parser.add_argument("--home")
    init_parser.add_argument("--force", action="store_true", help="重建演示库")
    init_parser.set_defaults(func=_cmd_init)

    ask_parser = sub.add_parser("ask", help="提问一次")
    ask_parser.add_argument("question")
    ask_parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    ask_parser.add_argument("--verbose", action="store_true", help="打印工具调用轨迹")
    _add_runtime_options(ask_parser)
    ask_parser.set_defaults(func=_cmd_ask)

    demo_parser = sub.add_parser("demo", help="运行内置演示问题")
    demo_parser.add_argument("--verbose", action="store_true")
    _add_runtime_options(demo_parser)
    demo_parser.set_defaults(func=_cmd_demo)

    eval_parser = sub.add_parser("eval", help="运行冻结评测集")
    eval_parser.add_argument("--cases", help="评测集 JSONL 路径")
    eval_parser.add_argument("--report-dir", default="reports")
    eval_parser.add_argument("--json", action="store_true")
    eval_parser.add_argument("--judge", action="store_true", help="启用 AI Judge 评审")
    eval_parser.add_argument("--judge-on-fail", action="store_true", help="只评审确定性失败用例")
    eval_parser.add_argument("--judge-sample", type=int, help="随机抽样 N 条评审（固定种子）")
    eval_parser.add_argument("--no-judge-cache", action="store_true", help="绕过 Judge 缓存")
    eval_parser.add_argument("--calibrate", action="store_true", help="运行 Judge 人工校准")
    eval_parser.add_argument("--calibration", help="校准集 JSONL 路径")
    eval_parser.add_argument("--compare", help="对比基线 JSON 报告")
    _add_runtime_options(eval_parser)
    eval_parser.set_defaults(func=_cmd_eval)

    exp_parser = sub.add_parser("experience", help="管理经验记忆")
    exp_sub = exp_parser.add_subparsers(dest="experience_command")
    exp_list = exp_sub.add_parser("list", help="列出经验")
    exp_list.add_argument("--status", choices=["verified", "candidate"])
    exp_list.add_argument("--limit", type=int, default=20)
    exp_list.add_argument("--home")
    exp_list.set_defaults(func=_cmd_experience_list)

    exp_add = exp_sub.add_parser("add", help="新增经验（默认候选）")
    exp_add.add_argument("--question", required=True)
    exp_add.add_argument("--sql", required=True)
    exp_add.add_argument("--category", default="")
    exp_add.add_argument("--verified", action="store_true", help="人工验证并直接启用")
    exp_add.add_argument("--home")
    exp_add.set_defaults(func=_cmd_experience_add)

    exp_verify = exp_sub.add_parser("verify", help="人工验证候选经验")
    exp_verify.add_argument("id", type=int)
    exp_verify.add_argument("--home")
    exp_verify.set_defaults(func=_cmd_experience_verify)

    exp_stats = exp_sub.add_parser("stats", help="经验库统计")
    exp_stats.add_argument("--home")
    exp_stats.set_defaults(func=_cmd_experience_stats)

    exp_failures = exp_sub.add_parser("failures", help="列出待人工处理的失败队列")
    exp_failures.add_argument("--status", choices=["open", "resolved", "all"], default="open")
    exp_failures.add_argument("--limit", type=int, default=20)
    exp_failures.add_argument("--json", action="store_true", dest="as_json")
    exp_failures.add_argument("--home")
    exp_failures.set_defaults(func=_cmd_experience_failures)

    exp_resolve = exp_sub.add_parser(
        "resolve", help="用通过护栏且真实执行成功的 SQL 结案一条失败记录"
    )
    exp_resolve.add_argument("id", type=int)
    exp_resolve.add_argument("--sql", required=True)
    exp_resolve.add_argument("--note", default="")
    exp_resolve.add_argument("--home")
    exp_resolve.set_defaults(func=_cmd_experience_resolve)
    return parser


def _runtime(args: argparse.Namespace) -> Runtime:
    settings = Settings.load(home=args.home, provider=getattr(args, "provider", None))
    return create_runtime(settings, provider=getattr(args, "provider", None))


def _cmd_init(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    settings.ensure_dirs()
    info = seed(settings.db_path, force=args.force)
    store = ExperienceStore(settings.experience_path)
    print(json.dumps({"database": info, "experience": store.stats()}, ensure_ascii=False, indent=2))
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    result = runtime.agent.run(args.question)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_result(result, verbose=args.verbose)
    return 0 if result.status in {"ok", "needs_clarification", "refused"} else 1


def _cmd_demo(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    print(f"Provider: {runtime.settings.provider} | DB: {runtime.settings.db_path}\n")
    for idx, question in enumerate(DEMO_QUESTIONS, 1):
        print(f"[{idx}] {question}")
        result = runtime.agent.run(question)
        _print_result(result, verbose=args.verbose, compact=True)
        print()
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    judge_model = None
    judge_requested = (
        args.judge
        or args.judge_on_fail
        or args.judge_sample is not None
        or args.calibrate
        or runtime.settings.judge_enabled
    )
    if judge_requested:
        try:
            judge_model = _build_judge(args, runtime.settings)
        except JudgeError as exc:
            print(f"Judge 配置错误: {exc}", file=sys.stderr)
            return 2

    if args.calibrate:
        cases_path = Path(args.calibration) if args.calibration else runtime.settings.calibration_path
        result = calibrate(
            judge_model,
            load_calibration(cases_path),
            db_path=runtime.settings.db_path,
            catalog=runtime.catalog,
            max_rows=runtime.settings.judge_max_rows,
        )
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_calibration(result)
        return 0 if _calibration_passed(result) else 1

    cases_path = Path(args.cases) if args.cases else runtime.settings.eval_cases_path
    cases = load_cases(cases_path)
    report = evaluate(
        runtime.agent,
        cases,
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider=runtime.settings.provider,
        report_dir=args.report_dir,
        model="" if runtime.settings.provider == "mock" else runtime.settings.model,
        judge_model=judge_model,
        judge_on_fail=args.judge_on_fail,
        judge_sample=args.judge_sample,
        judge_max_calls=runtime.settings.judge_max_calls,
        judge_max_rows=runtime.settings.judge_max_rows,
    )
    payload = report.to_dict()
    if args.compare:
        try:
            payload["comparison"] = _compare_reports(Path(args.compare), report)
        except (OSError, ValueError) as exc:
            print(f"无法读取基线报告: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Provider: {report.provider}")
        print(f"Cases: {report.total} | Passed: {report.passed}")
        for key, value in report.metrics.items():
            print(f"{key}: {value}")
        if report.judge.get("enabled"):
            print(
                f"AI Judge: {report.judge.get('status')} | "
                f"judged={report.judge.get('judged_cases')} | "
                f"unknown={report.judge.get('unknown_cases')} | "
                f"cache_hits={report.judge.get('cache_hits')}"
            )
        if args.compare:
            _print_comparison(payload["comparison"])
        print(f"Reports written to: {Path(args.report_dir).resolve()}")
    failed = [case for case in report.cases if not case.passed]
    return 0 if not failed else 1


def _build_judge(args: argparse.Namespace, settings: Any) -> Any:
    judge = LLMJudge(settings)
    if args.no_judge_cache:
        return judge
    cache = JudgeCache(settings.judge_cache_path)
    return CachedJudge(
        judge,
        cache,
        model_name=settings.judge_model,
        prompt_version=settings.judge_prompt_version,
    )


def _print_calibration(result: CalibrationResult) -> None:
    print(f"Cases: {result.total} | Evaluated: {result.evaluated} | Unknown: {result.unknown}")
    print(f"Agreement: {result.agreement:.2%}")
    for field, metrics in result.field_metrics.items():
        print(
            f"{field}: agreement={metrics['agreement']:.2%} "
            f"kappa={metrics['kappa']:.4f} (n={metrics['considered']})"
        )
    print("Target: agreement >= 85%, kappa >= 0.6")


def _calibration_passed(result: CalibrationResult) -> bool:
    if result.agreement < 0.85:
        return False
    for field in ("sql_equivalent", "answer_faithful"):
        metrics = result.field_metrics.get(field)
        if metrics and metrics["kappa"] < 0.6:
            return False
    return True


def _compare_reports(path: Path, report: Any) -> dict[str, Any]:
    baseline = json.loads(path.read_text(encoding="utf-8"))
    baseline_metrics = baseline.get("metrics") or {}
    comparisons: dict[str, dict[str, float]] = {}
    for key in (
        "execution_accuracy",
        "valid_sql_rate",
        "execution_success_rate",
        "refusal_accuracy",
        "table_recall_at_5",
        "avg_steps",
        "avg_latency_ms",
        "avg_tokens",
    ):
        old = baseline_metrics.get(key)
        new = report.metrics.get(key)
        if old is None or new is None:
            continue
        comparisons[key] = {
            "baseline": float(old),
            "current": float(new),
            "delta": round(float(new) - float(old), 4),
        }
    return {"baseline": str(path), "metrics": comparisons}


def _print_comparison(comparison: dict[str, Any]) -> None:
    print(f"Baseline: {comparison.get('baseline')}")
    for key, item in (comparison.get("metrics") or {}).items():
        print(f"  {key}: {item['baseline']} -> {item['current']} ({item['delta']:+.4f})")


def _cmd_experience_list(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    store = ExperienceStore(settings.experience_path)
    rows = store.list(status=args.status, limit=args.limit)
    if not rows:
        print("(empty)")
        return 0
    for row in rows:
        print(
            f"#{row['id']} [{row['status']}] {row['question']}\n"
            f"  category={row['category']} source={row['source']} hits={row['hit_count']}\n"
            f"  {row['sql']}"
        )
    return 0


def _sql_validator(runtime: Runtime):
    """Guard + real read-only execution against the demo DB."""

    def validator(sql: str) -> tuple[bool, str]:
        check = runtime.guard.check(sql)
        if not check.valid:
            return False, "; ".join(issue.message for issue in check.errors)
        execution = run_readonly_query(
            runtime.settings.db_path,
            check.sql,
            max_rows=runtime.settings.max_rows,
            timeout_ms=runtime.settings.query_timeout_ms,
        )
        if not execution.get("success"):
            return False, str(execution.get("error", "execution failed"))
        return True, "ok"

    return validator


def _cmd_experience_add(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    validator = _sql_validator(runtime) if args.verified else None

    store = runtime.store
    record = store.add(
        args.question,
        args.sql,
        category=args.category,
        source="human",
        verified=args.verified,
        schema_hash=runtime.catalog.schema_hash(),
        validator=validator,
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _cmd_experience_verify(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    record = runtime.store.verify(args.id, validator=_sql_validator(runtime))
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _cmd_experience_stats(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    store = ExperienceStore(settings.experience_path)
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    return 0


def _cmd_experience_failures(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    store = ExperienceStore(settings.experience_path)
    status = None if args.status == "all" else args.status
    rows = store.list_failures(status=status, limit=args.limit)
    if args.as_json:
        print(json.dumps({"items": rows, **store.failure_stats()}, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("失败队列为空（status=%s）。" % (args.status,))
        return 0
    print(f"失败队列 {args.status} 共 {len(rows)} 条 | {json.dumps(store.failure_stats(), ensure_ascii=False)}\n")
    for row in rows:
        print(
            f"#{row['id']} [{row['status']}] x{row['occur_count']} "
            f"({row['category'] or '-'}): {row['question']}"
        )
        print(f"    reason={row['reason']} detail={str(row['detail'])[:120]}")
        if row["last_sql"]:
            print(f"    最后一次尝试: {str(row['last_sql'])[:160]}")
    print("\n结案：nl2sql-agent experience resolve <id> --sql \"...\" --note \"口径说明\"")
    return 0


def _cmd_experience_resolve(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    try:
        result = runtime.store.resolve_failure(
            args.id,
            args.sql,
            validator=_sql_validator(runtime),
            note=args.note,
        )
    except (KeyError, ValueError) as exc:
        print(f"结案失败: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("已结案，并把该问答对写为 verified 经验（后续同类问题会自动注入 few-shot）。")
    return 0


def _print_result(result: AgentResult, *, verbose: bool = False, compact: bool = False) -> None:
    print(f"status: {result.status} | confidence: {result.confidence:.2f} | steps: {result.steps} | {result.elapsed_ms:.1f} ms")
    print(f"answer: {result.answer}")
    if result.sql:
        print(f"sql: {result.sql}")
    if result.columns:
        print(format_table(result.columns, result.rows[:10]))
        if result.row_count > 10:
            print(f"... {result.row_count} rows total")
    if verbose:
        print("tools:")
        for item in result.tool_trace:
            print(
                f"  [{item['step']}] {item['tool']} ok={item['ok']} "
                f"{item['elapsed_ms']}ms :: {item['summary']}"
            )
    if result.error and result.status not in {"ok", "needs_clarification"}:
        print(f"error: {result.error}")


if __name__ == "__main__":
    raise SystemExit(main())
