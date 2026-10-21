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
from .evaluation import evaluate, load_cases
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
    cases_path = Path(args.cases) if args.cases else runtime.settings.eval_cases_path
    cases = load_cases(cases_path)
    report = evaluate(
        runtime.agent,
        cases,
        db_path=runtime.settings.db_path,
        guard=runtime.guard,
        provider=runtime.settings.provider,
        report_dir=args.report_dir,
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Provider: {report.provider}")
        print(f"Cases: {report.total} | Passed: {report.passed}")
        for key, value in report.metrics.items():
            print(f"{key}: {value}")
        print(f"Reports written to: {Path(args.report_dir).resolve()}")
    failed = [case for case in report.cases if not case.passed]
    return 0 if not failed else 1


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


def _cmd_experience_add(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    validator = None
    if args.verified:
        def validator(sql: str) -> tuple[bool, str]:  # type: ignore[no-redef]
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

    record = runtime.store.verify(args.id, validator=validator)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _cmd_experience_stats(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    store = ExperienceStore(settings.experience_path)
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
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
