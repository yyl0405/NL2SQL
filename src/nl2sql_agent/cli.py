"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent import AGENT_PROMPT_VERSION, PROMPT_LAYOUTS, AgentResult
from .cache_audit import run_cache_audit
from .catalog import Catalog
from .casegen import generate_cases, seed_examples, write_cases
from .clarify import CLARIFY_MODES
from .clarify_audit import PACKAGED_DEMO_SCHEMA, run_clarify_audit
from .config import Settings
from .conversation import Conversation
from .costs import cost_rollup_lines, cost_summary
from .demo import seed
from .dialect_audit import DIALECT_CASES, run_dialect_audit
from .evaluation import (
    CachedJudge,
    JudgeCache,
    JudgeError,
    LLMJudge,
    evaluate,
    load_cases,
)
from .evaluation.calibration import CalibrationResult, calibrate, load_calibration
from .experience_audit import (
    FROZEN_CASES,
    PARAPHRASE_CASES,
    audit_fingerprint,
    run_experience_audit,
)
from .feedback import build_weekly_report, print_weekly_report
from .evaluation.regression import build_baseline, check_regression, format_regression
from .guard_suite import (
    ADVERSARIAL_CASES,
    FANOUT_CASES,
    PERMISSION_CASES,
    load_case_file,
    load_principals,
    run_all,
)
from .introspect import IntrospectOptions, introspect
from .embeddings import EmbeddingError, build_embedding_provider
from .linking import build_named_index, fusion_mode, variant_names
from .memory import ExperienceStore
from .memory.experience import FEEDBACK_REASONS
from .observability import bind_trace_id, new_trace_id, read_events, summarize
from .privacy_scan import scan_repo
from .release import create_release, list_releases, load_release, rollback_release
from .llm import OpenAICompatibleLLM
from .schema_dupes import (
    DEFAULT_LIMIT,
    DEFAULT_MIN_COLUMNS,
    DEFAULT_MIN_OVERLAP,
    cluster_duplicates,
    find_duplicates,
)
from .schema_overlay import (
    apply_overlay,
    load_overlay,
    overlay_from_approved_suggestion,
    save_overlay,
)
from .schema_suggest import (
    DEFAULT_CONCEPTS,
    SuggestOptions,
    load_suggestions,
    suggest_schema,
    write_suggestions,
)
from .runtime import Runtime, create_runtime
from .text import format_table
from .tools.db import MssqlTarget, SqliteTarget, open_connection
from .tools.sql_tools import run_readonly_query
from .value_audit import run_alias_audit

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
    parser.add_argument(
        "--dual-channel",
        action="store_true",
        help="启用双通道执行投票（低置信度或高风险类别会多一次独立生成与执行）",
    )
    parser.add_argument(
        "--principal",
        help="以权限配置中的角色 id 运行：模型与工具只看到该角色可见的 schema，护栏按完整目录判权",
    )
    parser.add_argument(
        "--ast-guard",
        action="store_true",
        help="启用 sqlglot AST 护栏（未安装或解析失败一律拒绝执行）",
    )


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

    chat_parser = sub.add_parser("chat", help="多轮会话模式（指代改写 + 澄清）")
    chat_parser.add_argument("--verbose", action="store_true", help="打印改写与工具调用轨迹")
    _add_runtime_options(chat_parser)
    chat_parser.set_defaults(func=_cmd_chat)

    serve_parser = sub.add_parser("serve", help="启动 Web 控制台（可选依赖 .[web]）")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8080)
    _add_runtime_options(serve_parser)
    serve_parser.set_defaults(func=_cmd_serve)

    eval_parser = sub.add_parser("eval", help="运行冻结评测集")
    eval_parser.add_argument("--cases", help="评测集 JSONL 路径")
    eval_parser.add_argument(
        "--only-tag",
        action="append",
        default=[],
        dest="only_tags",
        help="只跑带该 tag 的用例（可重复；CI 用它锁定基线子集）",
    )
    eval_parser.add_argument("--multi-turn", action="store_true", help="追加多轮会话用例集")
    eval_parser.add_argument(
        "--paraphrase",
        action="store_true",
        help="追加同义改写用例集（gold SQL 与冻结集逐字相同，只有问法不同），报告给出改写一致性",
    )
    eval_parser.add_argument(
        "--adversarial",
        action="store_true",
        help="追加对抗问法用例集（诱导越界取值 + 扇出陷阱 + 追问压力），报告给出泄露/护栏漏检/掉坑率",
    )
    eval_parser.add_argument("--report-dir", default="reports")
    eval_parser.add_argument(
        "--prompt-version",
        choices=sorted(PROMPT_LAYOUTS),
        default=AGENT_PROMPT_VERSION,
        help="强制使用某一版 prompt 跑评测（评测不做灰度，默认线上基线版）",
    )
    eval_parser.add_argument("--json", action="store_true")
    eval_parser.add_argument("--judge", action="store_true", help="启用 AI Judge 评审")
    eval_parser.add_argument("--judge-on-fail", action="store_true", help="只评审确定性失败用例")
    eval_parser.add_argument("--judge-sample", type=int, help="随机抽样 N 条评审（固定种子）")
    eval_parser.add_argument("--no-judge-cache", action="store_true", help="绕过 Judge 缓存")
    eval_parser.add_argument("--calibrate", action="store_true", help="运行 Judge 人工校准")
    eval_parser.add_argument("--calibration", help="校准集 JSONL 路径")
    eval_parser.add_argument("--compare", help="对比基线 JSON 报告")
    eval_parser.add_argument(
        "--write-baseline",
        metavar="PATH",
        help="把本次运行的聚合指标写成基线文件（只含数字与配置，不含用例与 SQL）",
    )
    eval_parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="与 --compare 连用：受门禁指标相对基线下跌超过 --max-drop 时退出码 1",
    )
    eval_parser.add_argument(
        "--max-drop",
        type=float,
        default=0.02,
        dest="max_drop",
        help="回归阈值（比例，默认 0.02 = 2 个百分点）",
    )
    _add_runtime_options(eval_parser)
    eval_parser.set_defaults(func=_cmd_eval)

    eval_sub = eval_parser.add_subparsers(dest="eval_command")
    gen_parser = eval_sub.add_parser(
        "generate-cases",
        help="用大模型草拟评测用例草稿（只写 status=draft，人工审核后才可能进冻结集）",
    )
    gen_parser.add_argument("--count", type=int, default=10, help="请求生成的条数（默认 10）")
    gen_parser.add_argument("--out", help="草稿 JSONL 路径（默认 var/ai_draft_cases.jsonl，追加写入）")
    gen_parser.add_argument("--examples", type=int, default=3, help="带入的冻结用例写法示例条数")
    _add_runtime_options(gen_parser)
    gen_parser.set_defaults(func=_cmd_generate_cases)

    exp_parser = sub.add_parser("experience", help="管理经验记忆")
    exp_sub = exp_parser.add_subparsers(dest="experience_command")
    exp_list = exp_sub.add_parser("list", help="列出经验")
    exp_list.add_argument("--status", choices=["verified", "candidate", "deprecated"])
    exp_list.add_argument("--limit", type=int, default=20)
    exp_list.add_argument("--home")
    exp_list.set_defaults(func=_cmd_experience_list)

    exp_promote = exp_sub.add_parser("promote", help="把 verified 经验标为 gold（人工优先级）")
    exp_promote.add_argument("id", type=int)
    exp_promote.add_argument("--quality", choices=["verified", "gold"], default="gold")
    exp_promote.add_argument("--home")
    exp_promote.set_defaults(func=_cmd_experience_promote)

    exp_deprecate = exp_sub.add_parser("deprecate", help="退役一条经验（保留行，不再召回）")
    exp_deprecate.add_argument("id", type=int)
    exp_deprecate.add_argument("--reason", default="")
    exp_deprecate.add_argument("--home")
    exp_deprecate.set_defaults(func=_cmd_experience_deprecate)

    exp_staleness = exp_sub.add_parser(
        "staleness", help="按当前 catalog 版本报告陈旧经验（只读，不写库）"
    )
    exp_staleness.add_argument("--home")
    exp_staleness.set_defaults(func=_cmd_experience_staleness)

    exp_deprecate_stale = exp_sub.add_parser(
        "deprecate-stale", help="把目录版本不一致的经验批量标为 deprecated"
    )
    exp_deprecate_stale.add_argument(
        "--yes", action="store_true", help="确认写入；缺少时只报告将要退役的 id"
    )
    exp_deprecate_stale.add_argument("--reason", default="schema_changed")
    exp_deprecate_stale.add_argument("--home")
    exp_deprecate_stale.set_defaults(func=_cmd_experience_deprecate_stale)

    exp_counterexamples = exp_sub.add_parser(
        "counterexamples", help="列出失败反例（只供人工分诊，永不进入提示词）"
    )
    exp_counterexamples.add_argument("--status", choices=["open", "resolved", "all"], default="open")
    exp_counterexamples.add_argument("--limit", type=int, default=20)
    exp_counterexamples.add_argument("--json", action="store_true", dest="as_json")
    exp_counterexamples.add_argument("--home")
    exp_counterexamples.set_defaults(func=_cmd_experience_counterexamples)

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

    exp_feedback = exp_sub.add_parser("feedback", help="列出用户反馈审核队列")
    exp_feedback.add_argument(
        "--status", choices=["open", "approved", "rejected", "all"], default="open"
    )
    exp_feedback.add_argument("--limit", type=int, default=20)
    exp_feedback.add_argument("--json", action="store_true", dest="as_json")
    exp_feedback.add_argument("--home")
    exp_feedback.set_defaults(func=_cmd_experience_feedback)

    exp_review = exp_sub.add_parser(
        "review", help="审核用户反馈：通过（需要校验过的修正 SQL）或拒绝（需要原因）"
    )
    exp_review.add_argument("id", type=int)
    action = exp_review.add_mutually_exclusive_group(required=True)
    action.add_argument("--approve", action="store_true", help="通过并写入 verified 经验")
    action.add_argument("--reject", action="store_true", help="拒绝该反馈")
    exp_review.add_argument("--sql", default="", help="--approve 时的修正 SQL")
    exp_review.add_argument("--note", default="")
    exp_review.add_argument("--reviewer", default="")
    exp_review.add_argument("--home")
    exp_review.set_defaults(func=_cmd_experience_review)

    exp_report = exp_sub.add_parser(
        "report", help="周报：Top 失败、经验复用率、schema 变更影响（默认只输出指纹）"
    )
    exp_report.add_argument("--days", type=int, default=7)
    exp_report.add_argument("--log", help="覆盖 NL2SQL_LOG_PATH（事件日志路径）")
    exp_report.add_argument("--json", action="store_true", dest="as_json")
    exp_report.add_argument(
        "--show-text", action="store_true", help="输出问题原文与反馈细节（默认只给指纹）"
    )
    exp_report.add_argument("--home")
    exp_report.set_defaults(func=_cmd_experience_report)

    release_parser = sub.add_parser(
        "release", help="prompt/schema/experience 版本快照与回滚"
    )
    release_sub = release_parser.add_subparsers(dest="release_command")
    release_snapshot = release_sub.add_parser(
        "snapshot", help="把当前 prompt 版本、schema 文件与经验库打成一份 release"
    )
    _add_runtime_options(release_snapshot)
    release_snapshot.set_defaults(func=_cmd_release_snapshot)

    release_list = release_sub.add_parser("list", help="列出本地 release")
    release_list.add_argument("--json", action="store_true", dest="as_json")
    release_list.add_argument("--home")
    release_list.set_defaults(func=_cmd_release_list)

    release_show = release_sub.add_parser("show", help="查看一份 release 的 manifest")
    release_show.add_argument("release_id")
    release_show.add_argument("--home")
    release_show.set_defaults(func=_cmd_release_show)

    release_rollback = release_sub.add_parser(
        "rollback", help="恢复 schema 与经验库；prompt 需要 checkout 对应代码"
    )
    release_rollback.add_argument("release_id")
    release_rollback.add_argument("--skip-schema", action="store_true")
    release_rollback.add_argument("--skip-experience", action="store_true")
    release_rollback.add_argument(
        "--yes", action="store_true", help="确认覆盖当前 schema/经验库（覆盖前会另存备份）"
    )
    _add_runtime_options(release_rollback)
    release_rollback.set_defaults(func=_cmd_release_rollback)

    sync_parser = sub.add_parser(
        "schema-sync",
        help="从 SQL Server 只读 introspect 出 schema 目录（输出文件默认落在 gitignore 的 var/）",
    )
    sync_parser.add_argument("--out", help="输出 JSON 路径（默认 <home>/var/schema_<db>.json）")
    sync_parser.add_argument("--include", action="append", default=[], help="只同步匹配该正则的表（可重复）")
    sync_parser.add_argument("--exclude", action="append", default=[], help="排除匹配该正则的表（可重复）")
    sync_parser.add_argument("--include-views", action="store_true", help="把视图也同步进目录")
    sync_parser.add_argument("--domain", default="SQL Server ERP", help="写入目录的业务域标签")
    sync_parser.add_argument("--json", action="store_true", help="以 JSON 打印统计")
    sync_parser.add_argument("--home")
    sync_parser.set_defaults(func=_cmd_schema_sync)

    dupes_parser = sub.add_parser(
        "schema-dupes",
        help="扫描列集合高度重合的表，产出重复/副本表候选清单（只读，不改动目录）",
    )
    dupes_parser.add_argument(
        "--min-overlap",
        type=float,
        default=DEFAULT_MIN_OVERLAP,
        help=f"较小表的列被对方复现的占比阈值（默认 {DEFAULT_MIN_OVERLAP}）",
    )
    dupes_parser.add_argument(
        "--min-columns",
        type=int,
        default=DEFAULT_MIN_COLUMNS,
        help=f"共享列数下限，避免 id/name 这类公共列被当成孪生表（默认 {DEFAULT_MIN_COLUMNS}）",
    )
    dupes_parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="最多输出多少对（0 = 不限）"
    )
    dupes_parser.add_argument("--exclude", action="append", default=[], help="排除表名（可重复）")
    dupes_parser.add_argument(
        "--out", help=f"候选清单输出路径（默认 <home>/var/schema_dupes.json）"
    )
    dupes_parser.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 打印")
    dupes_parser.add_argument("--home")
    dupes_parser.set_defaults(func=_cmd_schema_dupes)

    privacy_parser = sub.add_parser(
        "privacy-scan",
        help="扫描“会被公开”的文件（git 跟踪 + 未跟踪未忽略），拦外接目录标识与密钥形状（只读，可当 pre-push 门禁）",
    )
    privacy_parser.add_argument(
        "--extra",
        help="本部署专属正则清单（每行一条；默认用 <home>/var/privacy_patterns.txt，存在才生效）",
    )
    privacy_parser.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 打印")
    privacy_parser.add_argument("--home")
    privacy_parser.set_defaults(func=_cmd_privacy_scan)

    suggest_parser = sub.add_parser(
        "schema-suggest",
        help="用大模型生成表语义映射草稿（只写本地待审核文件，不直接生效）",
    )
    suggest_parser.add_argument("--concepts", nargs="+", help="要映射的业务概念；默认覆盖订单/客户/物料/库存等高频概念")
    suggest_parser.add_argument("--top-tables", type=int, default=3, help="每个概念最多给模型几张候选表")
    suggest_parser.add_argument("--sample-rows", type=int, default=3, help="候选表样本行数（0 等同 --no-profile）")
    suggest_parser.add_argument("--no-profile", action="store_true", help="不连库采样，只用 schema 证据")
    suggest_parser.add_argument("--out", help="候选映射输出路径（默认 <home>/var/schema_suggestions.json）")
    suggest_parser.add_argument("--json", action="store_true", help="以 JSON 打印统计")
    suggest_parser.add_argument("--home")
    suggest_parser.set_defaults(func=_cmd_schema_suggest)

    review_parser = sub.add_parser(
        "schema-review",
        help="审核 schema-suggest 的候选映射；批准后才写入 schema overlay",
    )
    review_sub = review_parser.add_subparsers(dest="review_command", required=True)
    review_list = review_sub.add_parser("list", help="列出候选映射")
    review_list.add_argument("--status", choices=["pending", "approved", "rejected", "all"], default="pending")
    review_list.add_argument("--suggestions", help="候选映射文件路径")
    review_list.add_argument("--json", action="store_true", dest="as_json")
    review_list.add_argument("--home")
    review_list.set_defaults(func=_cmd_schema_review_list)

    review_show = review_sub.add_parser("show", help="查看一条候选映射")
    review_show.add_argument("item_id")
    review_show.add_argument("--suggestions", help="候选映射文件路径")
    review_show.add_argument("--home")
    review_show.set_defaults(func=_cmd_schema_review_show)

    review_approve = review_sub.add_parser("approve", help="批准一条候选映射并写入 overlay")
    review_approve.add_argument("item_id")
    review_approve.add_argument("--reviewer", default="reviewer", help="审核人标识")
    review_approve.add_argument("--note", default="", help="审核备注")
    review_approve.add_argument("--force", action="store_true", help="即使 schema hash 已变化也批准")
    review_approve.add_argument("--suggestions", help="候选映射文件路径")
    review_approve.add_argument("--overlay", help="schema overlay 输出路径")
    review_approve.add_argument("--home")
    review_approve.set_defaults(func=_cmd_schema_review_approve)

    review_reject = review_sub.add_parser("reject", help="拒绝一条候选映射")
    review_reject.add_argument("item_id")
    review_reject.add_argument("--note", required=True, help="拒绝原因")
    review_reject.add_argument("--reviewer", default="reviewer", help="审核人标识")
    review_reject.add_argument("--suggestions", help="候选映射文件路径")
    review_reject.add_argument("--home")
    review_reject.set_defaults(func=_cmd_schema_review_reject)

    link_parser = sub.add_parser(
        "link-check",
        help="只做 schema linking 命中率检查（不调用任何模型、不执行 SQL）",
    )
    link_parser.add_argument("--cases", required=True, help="本地用例 JSONL：question + expected_tables")
    link_parser.add_argument("--top-k", type=int, default=5)
    link_parser.add_argument(
        "--variants",
        nargs="+",
        choices=sorted(variant_names()),
        help="对比多种 schema linking 策略（BM25 策略或 vector / rrf；默认按 NL2SQL_LINKING）",
    )
    link_parser.add_argument("--json", action="store_true")
    link_parser.add_argument("--home")
    link_parser.set_defaults(func=_cmd_link_check)

    guard_parser = sub.add_parser(
        "guard-check",
        help="只跑护栏基准：对抗用例 / 权限用例 / JOIN 扇出 / gold SQL 误杀（不调模型、不连库）",
    )
    guard_parser.add_argument(
        "--no-ast",
        action="store_true",
        help="跳过 AST 层对比（未安装 sqlglot 或只想看正则层时使用）",
    )
    guard_parser.add_argument("--cases", help="覆盖对抗用例文件路径")
    guard_parser.add_argument("--principals", help="覆盖权限配置路径（默认用内置演示角色）")
    guard_parser.add_argument("--json", action="store_true")
    guard_parser.add_argument("--home")
    guard_parser.set_defaults(func=_cmd_guard_check)

    dialect_parser = sub.add_parser(
        "dialect-check",
        help="只跑方言基准：同一意图的多方言 gold 与跨方言错误形状（不调模型，只在 SQLite 上执行）",
    )
    dialect_parser.add_argument("--cases", help="覆盖方言用例文件路径")
    dialect_parser.add_argument(
        "--no-ast",
        action="store_true",
        help="跳过 AST 层对比（未安装 sqlglot 或只想看正则层时使用）",
    )
    dialect_parser.add_argument("--json", action="store_true")
    dialect_parser.add_argument("--home")
    dialect_parser.set_defaults(func=_cmd_dialect_check)

    alias_parser = sub.add_parser(
        "alias-check",
        help="只跑值对齐基准：词典↔目录↔库内取值的一致性，加对齐开/关的配对 A/B（默认用 mock）",
    )
    alias_parser.add_argument("--cases", help="覆盖别名用例文件路径")
    alias_parser.add_argument("--dictionary", help="覆盖业务词典路径")
    alias_parser.add_argument("--provider", help="配对 A/B 用哪个生成器（默认按环境配置）")
    alias_parser.add_argument(
        "--no-ab",
        action="store_true",
        help="跳过配对 A/B，只核对词典与语料的不变量（不调用生成器）",
    )
    alias_parser.add_argument("--json", action="store_true")
    alias_parser.add_argument("--home")
    alias_parser.set_defaults(func=_cmd_alias_check)

    exp_check = sub.add_parser(
        "experience-check",
        help="离线测量经验召回：同义改写配对 Recall@1/3/5，多臂对比（不调用生成器）",
    )
    exp_check.add_argument("--cases", help="同义改写用例路径（默认内置语料）")
    exp_check.add_argument("--frozen", help="冻结集路径（经验库的来源，默认内置语料）")
    exp_check.add_argument(
        "--embedding",
        choices=["hash", "openai"],
        default="hash",
        help="向量臂使用的 provider；hash 离线但只是词袋，openai 需要 Key",
    )
    exp_check.add_argument(
        "--paired-only", action="store_true", help="只测配对库，不把全量冻结集当干扰项"
    )
    exp_check.add_argument("--json", action="store_true")
    exp_check.add_argument("--home")
    exp_check.set_defaults(func=_cmd_experience_check)

    clarify_parser = sub.add_parser(
        "clarify-check",
        help="只跑澄清基准：触发分层与冻结集代价、澄清可执行性、二次问数，加开/关配对 A/B（默认用 mock）",
    )
    clarify_parser.add_argument("--cases", help="覆盖澄清用例文件路径")
    clarify_parser.add_argument("--glossary", help="覆盖指标歧义名词表路径")
    clarify_parser.add_argument("--provider", help="跑语料用哪个生成器（默认按环境配置）")
    clarify_parser.add_argument(
        "--mode",
        help="用哪档澄清策略跑语料：off 不干预 / options 只递问句（默认） / ask 口径缺口也先问",
    )
    clarify_parser.add_argument(
        "--no-ab",
        action="store_true",
        help="跳过配对 A/B，只核对触发分层与选项可执行性（省一半生成调用）",
    )
    clarify_parser.add_argument("--json", action="store_true")
    clarify_parser.add_argument("--home")
    clarify_parser.set_defaults(func=_cmd_clarify_check)

    cache_parser = sub.add_parser(
        "cache-check",
        help="离线测量三层缓存：结果缓存冷/热与归一化命中、embedding 缓存复用、prompt 稳定前缀占比",
    )
    cache_parser.add_argument("--question", help="用哪个问题测量结果缓存（默认一条内置演示问题）")
    cache_parser.add_argument(
        "--provider",
        choices=["mock", "openai"],
        default="mock",
        help="测量用哪个生成器；默认 mock（离线、零 token）",
    )
    cache_parser.add_argument("--json", action="store_true", dest="as_json")
    cache_parser.add_argument("--home")
    cache_parser.set_defaults(func=_cmd_cache_check)

    metrics_parser = sub.add_parser(
        "metrics",
        help="把事件日志折成指标：P50/P95 延迟、人工升级率、经验命中率、repair 成功率（不调模型）",
    )
    metrics_parser.add_argument("--log-path", help="事件日志路径（默认 NL2SQL_LOG_PATH）")
    metrics_parser.add_argument("--limit", type=int, default=20000, help="最多读多少条事件")
    metrics_parser.add_argument("--json", action="store_true", dest="as_json")
    metrics_parser.add_argument("--home")
    metrics_parser.set_defaults(func=_cmd_metrics)

    cost_parser = sub.add_parser(
        "cost",
        help="按业务域 / 用户汇总 token 与费用；没配单价只报 token（不调模型）",
    )
    cost_parser.add_argument("--log-path", help="事件日志路径（默认 NL2SQL_LOG_PATH）")
    cost_parser.add_argument("--limit", type=int, default=20000, help="最多读多少条事件")
    cost_parser.add_argument(
        "--by",
        choices=["category", "principal"],
        default="category",
        help="汇总维度：category 业务域 / principal 用户",
    )
    cost_parser.add_argument("--price-input", type=float, help="覆盖输入单价（每百万 token）")
    cost_parser.add_argument("--price-output", type=float, help="覆盖输出单价（每百万 token）")
    cost_parser.add_argument(
        "--price-cached-input", type=float, help="覆盖缓存命中单价（每百万 token，缺省用输入价）"
    )
    cost_parser.add_argument("--currency", help="币种标签（默认取 NL2SQL_PRICE_CURRENCY）")
    cost_parser.add_argument("--json", action="store_true", dest="as_json")
    cost_parser.add_argument("--home")
    cost_parser.set_defaults(func=_cmd_cost)
    return parser


def _runtime(args: argparse.Namespace) -> Runtime:
    settings = Settings.load(home=args.home, provider=getattr(args, "provider", None))
    if getattr(args, "dual_channel", False):
        settings.dual_channel = True
    if getattr(args, "principal", None):
        settings.principal_id = str(args.principal)
    if getattr(args, "ast_guard", False):
        settings.ast_guard = "on"
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
    # CLI 也是请求边界：一轮一个 id，工具行与 DB 行才能追回同一轮。
    with bind_trace_id(new_trace_id()):
        result = runtime.agent.run(args.question)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_result(result, verbose=args.verbose)
    return 0 if result.status in {"ok", "needs_clarification", "refused"} else 1


def _cmd_demo(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    print(f"Provider: {runtime.settings.provider} | DB: {runtime.settings.database_label}\n")
    for idx, question in enumerate(DEMO_QUESTIONS, 1):
        print(f"[{idx}] {question}")
        with bind_trace_id(new_trace_id()):
            result = runtime.agent.run(question)
        _print_result(result, verbose=args.verbose, compact=True)
        print()
    return 0


LINK_CHECK_KS = (3, 5, 10, 20)


def _link_run(
    catalog: Catalog,
    cases: list[dict],
    variant: str,
    top_k: int,
    embedder: Any = None,
    cache_dir: Any = None,
    rrf_k: int = 60,
) -> dict[str, Any]:
    index = build_named_index(catalog, variant, embedder=embedder, cache_dir=cache_dir, rrf_k=rrf_k)
    ranked_all = max(LINK_CHECK_KS)
    rows: list[dict[str, Any]] = []
    for case in cases:
        ranked_items = index.rank_with_sources(case["question"], ranked_all)
        ranked = [item.table.name for item in ranked_items]
        expected = set(case["gold"])
        entry: dict[str, Any] = {"id": case["id"], "gold": case["gold"], "recall": {}}
        for k in LINK_CHECK_KS:
            entry["recall"][str(k)] = round(len(set(ranked[:k]) & expected) / len(expected), 4)
        entry["missed"] = [t for t in case["gold"] if t not in set(ranked)]
        entry["top"] = ranked[:5]
        entry["sources"] = {
            item.table.name: list(item.sources) for item in ranked_items[:5]
        }
        rows.append(entry)
    label = variant if fusion_mode(variant) != "bm25" else index.strategy.name
    summary: dict[str, Any] = {
        "strategy": label,
        "detail": index.strategy.describe(),
        "mode": index.mode,
        "embedding": index.embedding,
        "degraded": index.degraded,
        "cases": len(rows),
        "catalog_tables": len(index.tables),
    }
    if index.degraded_reason:
        summary["degraded_reason"] = index.degraded_reason
    if index.vector_stats:
        summary["vector"] = dict(index.vector_stats)
    for k in LINK_CHECK_KS:
        summary[f"recall@{k}"] = round(sum(row["recall"][str(k)] for row in rows) / len(rows), 4)
        summary[f"hit@{k}"] = round(sum(1 for row in rows if row["recall"][str(k)] > 0) / len(rows), 4)
    worst = sorted(rows, key=lambda row: (row["recall"][str(top_k)], row["id"]))[:5]
    return {"summary": summary, "cases": rows, "worst": worst}


def _cmd_schema_sync(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    target = settings.target
    if not isinstance(target, MssqlTarget):
        print(
            "schema-sync 只对接 SQL Server：请设置 NL2SQL_DB_BACKEND=mssql 及 NL2SQL_MSSQL_* 连接项。",
            file=sys.stderr,
        )
        return 2
    try:
        connection, _ = open_connection(target, timeout_ms=30000)
    except ModuleNotFoundError as exc:
        print(f"{exc}（SQL Server 后端是可选依赖，核心包不含驱动）", file=sys.stderr)
        return 2
    try:
        payload = introspect(
            connection,
            IntrospectOptions(
                domain=args.domain,
                as_of_date=settings.demo_today,
                include=tuple(args.include),
                exclude=tuple(args.exclude),
                include_views=bool(args.include_views),
            ),
        )
    finally:
        connection.close()

    stats = payload.pop("_stats")
    out = Path(args.out) if args.out else settings.home / "var" / f"schema_{target.database}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    stats["path"] = str(out)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print(
            f"已同步 {stats['tables']} 张表 / {stats['columns']} 列 → {out}\n"
            f"  注释里的关联提示: {stats['join_hints']}（落到目录的显式关系 {stats['relationships']} 条）\n"
            f"  待核对的过滤条件建议: {stats['filter_suggestions']} 条（不自动生效）\n"
            f"  标记敏感列: {stats['sensitive_columns']}  禁用垃圾表: {stats['disabled_tables']}"
        )
    print(
        "提醒：该文件含外接目录结构，只应留在本地（var/ 已 gitignore），不要提交或发布。",
        file=sys.stderr,
    )
    return 0


def _cmd_schema_dupes(args: argparse.Namespace) -> int:
    """List column-set twins the generator currently has to guess between.

    The scan never edits anything: turning a candidate into a disabled table is a
    human call recorded in the overlay, because it changes what the agent can see.
    """
    settings = Settings.load(home=args.home)
    try:
        catalog = _catalog_for_schema_review(settings)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    pairs = find_duplicates(
        catalog,
        min_overlap=args.min_overlap,
        min_columns=args.min_columns,
        limit=args.limit,
        exclude=tuple(args.exclude),
    )
    out = Path(args.out) if args.out else settings.home / "var" / "schema_dupes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    clusters = cluster_duplicates(pairs)
    payload = {
        "schema_hash": catalog.schema_hash(),
        "min_overlap": args.min_overlap,
        "min_columns": args.min_columns,
        "enabled_tables": len(catalog.enabled_tables()),
        "clusters": [cluster.to_dict() for cluster in clusters],
        "pairs": [pair.to_dict() for pair in pairs],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"目录 {payload['enabled_tables']} 张启用表：列重合 >= {args.min_overlap:.2f} 且共享 >= {args.min_columns} 列"
            f" → {len(clusters)} 个候选簇 / {len(pairs)} 对 → {out}"
        )
        for cluster in clusters[:20]:
            shown = ", ".join(cluster.members[:6])
            more = f" …共 {len(cluster.members)} 张" if len(cluster.members) > 6 else ""
            print(f"  {len(cluster.members)} 张（{cluster.pairs} 对，重合 >= {cluster.min_overlap:.3f}）：{shown}{more}")
        if clusters:
            print("候选只是清单：确认后在 overlay 的 disabled_tables 里停用副本表。")
    print("清单含真实表名，只落在 gitignore 的 var/，不要提交。", file=sys.stderr)
    return 0


def _cmd_privacy_scan(args: argparse.Namespace) -> int:
    """Refuse to let a real identifier or a key reach a public commit.

    The scan reads the same file set a push would send, so ``var/`` and ``.env`` are
    out by construction rather than by the reviewer remembering to exclude them.
    Findings exit 1 so the command can gate a push.
    """
    home = Path(args.home) if args.home else Path.cwd()
    extras_path = args.extra
    if not extras_path:
        candidate = home / "var" / "privacy_patterns.txt"
        extras_path = candidate if candidate.is_file() else None
    try:
        report = scan_repo(home, extras_path=extras_path)
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 1 if report.findings else 0
    print(
        f"公开面 {report.files_scanned} 个文件，规则 {report.rules_used} 条"
        f"，跳过不可读 {report.skipped_binary} 个"
    )
    if report.extras_file:
        print(f"本部署专属清单: {report.extras_file}")
    if not report.findings:
        print("未发现外接目录标识或密钥形状。")
        return 0
    for item in report.findings:
        print(f"  [{item.code}] {item.path}:{item.line} -> {item.match}")
        if item.excerpt:
            print(f"      {item.excerpt}")
    print(f"合计 {len(report.findings)} 处，按码统计: {report.codes}", file=sys.stderr)
    return 1


def _catalog_for_schema_review(settings: Settings) -> Catalog:
    if not settings.schema_path.exists():
        raise ValueError(
            f"schema 目录不存在: {settings.schema_path}\n"
            "外接目录请先运行 nl2sql-agent schema-sync，或用 NL2SQL_SCHEMA_PATH 指定文件。"
        )
    catalog = Catalog.from_path(settings.schema_path, linking=settings.linking)
    return apply_overlay(catalog, load_overlay(settings.schema_overlay_path))


def _cmd_schema_suggest(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    try:
        catalog = _catalog_for_schema_review(settings)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if settings.provider != "openai":
        print(
            "schema-suggest 需要真实大模型：请设置 NL2SQL_PROVIDER=openai 并配置 NL2SQL_API_KEY。",
            file=sys.stderr,
        )
        return 2
    try:
        llm = OpenAICompatibleLLM(settings)
    except Exception as exc:  # noqa: BLE001 - provider dependency/config errors are startup failures
        print(str(exc), file=sys.stderr)
        return 2
    sample_rows = 0 if args.no_profile else max(0, int(args.sample_rows))
    options = SuggestOptions(
        concepts=tuple(args.concepts or DEFAULT_CONCEPTS),
        top_tables=max(1, int(args.top_tables)),
        sample_rows=sample_rows,
    )
    target = None if sample_rows == 0 else settings.target
    payload = suggest_schema(catalog, llm=llm, target=target, options=options)
    out = Path(args.out) if args.out else settings.schema_suggestions_path
    write_suggestions(out, payload)
    stats = {
        "path": str(out),
        "schema_hash": payload["schema_hash"],
        "suggestions": len(payload["suggestions"]),
        "observations": len(payload["observations"]),
        "requires_human_review": True,
    }
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print(
            f"已生成 {stats['suggestions']} 条候选映射 → {out}\n"
            f"  schema hash: {stats['schema_hash']}\n"
            f"  诊断: {stats['observations']} 条\n"
            "  这些候选不会生效；请用 nl2sql-agent schema-review approve <id> 人工批准。"
        )
    print(
        "提醒：候选文件可能含外接目录表名，只应留在本地（var/ 已 gitignore），不要提交或发布。",
        file=sys.stderr,
    )
    return 0


def _load_suggestions_for(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings.load(home=args.home)
    path = Path(args.suggestions) if getattr(args, "suggestions", None) else settings.schema_suggestions_path
    payload = load_suggestions(path)
    return {"path": path, "settings": settings, "payload": payload}


def _find_suggestion(payload: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    for item in payload.get("suggestions") or []:
        if str(item.get("id") or "") == item_id:
            return item
    return None


def _cmd_schema_review_list(args: argparse.Namespace) -> int:
    loaded = _load_suggestions_for(args)
    items = loaded["payload"].get("suggestions") or []
    if args.status != "all":
        items = [item for item in items if str(item.get("review_status") or "pending") == args.status]
    if args.as_json:
        print(json.dumps({"path": str(loaded["path"]), "items": items}, ensure_ascii=False, indent=2))
        return 0
    if not items:
        print(f"没有 {args.status} 状态的候选映射。文件: {loaded['path']}")
        return 0
    for item in items:
        print(
            f"{item.get('id')} [{item.get('review_status') or 'pending'}] "
            f"{item.get('concept')} → {item.get('table')} conf={float(item.get('confidence') or 0):.2f}"
        )
    return 0


def _cmd_schema_review_show(args: argparse.Namespace) -> int:
    loaded = _load_suggestions_for(args)
    item = _find_suggestion(loaded["payload"], args.item_id)
    if item is None:
        print(f"候选映射不存在: {args.item_id}", file=sys.stderr)
        return 2
    print(json.dumps(item, ensure_ascii=False, indent=2))
    return 0


def _cmd_schema_review_approve(args: argparse.Namespace) -> int:
    loaded = _load_suggestions_for(args)
    item = _find_suggestion(loaded["payload"], args.item_id)
    if item is None:
        print(f"候选映射不存在: {args.item_id}", file=sys.stderr)
        return 2
    try:
        catalog = _catalog_for_schema_review(loaded["settings"])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.force and str(item.get("schema_hash") or "") != catalog.schema_hash():
        print(
            "候选映射的 schema hash 与当前目录不一致，可能表结构已变。"
            "请重新 schema-suggest，或确认无误后加 --force。",
            file=sys.stderr,
        )
        return 2
    overlay_path = Path(args.overlay) if args.overlay else loaded["settings"].schema_overlay_path
    overlay = load_overlay(overlay_path)
    overlay = overlay_from_approved_suggestion(overlay, item, reviewer=args.reviewer)
    overlay["schema_hash"] = catalog.schema_hash()
    item["review_status"] = "approved"
    item["reviewer"] = args.reviewer
    if args.note:
        item["review_note"] = args.note
    save_overlay(overlay_path, overlay)
    write_suggestions(loaded["path"], loaded["payload"])
    print(f"已批准 {args.item_id}，写入 overlay: {overlay_path}")
    print("重启 serve / ask / eval 后，overlay 才会进入 Agent 的 schema 上下文。")
    return 0


def _cmd_schema_review_reject(args: argparse.Namespace) -> int:
    loaded = _load_suggestions_for(args)
    item = _find_suggestion(loaded["payload"], args.item_id)
    if item is None:
        print(f"候选映射不存在: {args.item_id}", file=sys.stderr)
        return 2
    item["review_status"] = "rejected"
    item["reviewer"] = args.reviewer
    item["review_note"] = args.note
    write_suggestions(loaded["path"], loaded["payload"])
    print(f"已拒绝 {args.item_id}；overlay 未变化。")
    return 0


def _cmd_link_check(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    if not settings.schema_path.exists():
        print(
            f"schema 目录不存在: {settings.schema_path}\n"
            "外接目录请先运行 nl2sql-agent schema-sync，或用 NL2SQL_SCHEMA_PATH 指定文件。",
            file=sys.stderr,
        )
        return 2
    catalog = Catalog.from_path(settings.schema_path, linking=settings.linking)
    cases_path = Path(args.cases)
    if not cases_path.exists():
        print(f"用例文件不存在: {cases_path}", file=sys.stderr)
        return 2

    cases: list[dict[str, Any]] = []
    for line in cases_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        gold = [str(t) for t in (raw.get("expected_tables") or []) if str(t).strip()]
        question = str(raw.get("question") or "").strip()
        if not gold or not question:
            continue
        cases.append({"id": str(raw.get("id") or question)[:40], "question": question, "gold": gold})

    if not cases:
        print("用例文件里没有带 expected_tables 的行。", file=sys.stderr)
        return 2

    variants = list(args.variants) if args.variants else [settings.linking]
    needs_vector = any(fusion_mode(name) != "bm25" for name in variants)
    embedder = None
    if needs_vector:
        try:
            embedder = build_embedding_provider(settings)
        except (EmbeddingError, ValueError) as exc:
            print(f"向量变体不可用: {exc}", file=sys.stderr)
            return 2
        if embedder is None:
            print(
                f"变体 {', '.join(variants)} 需要向量通道：请设置 "
                "NL2SQL_EMBEDDING=hash（离线确定性）或 openai（真实语义向量）。",
                file=sys.stderr,
            )
            return 2
    runs = [
        _link_run(
            catalog,
            cases,
            name,
            args.top_k,
            embedder=embedder,
            cache_dir=settings.embedding_cache_dir,
            rrf_k=settings.rrf_k,
        )
        for name in variants
    ]
    if len(runs) == 1:
        output = runs[0]
        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(output["summary"], ensure_ascii=False, indent=2))
            headline = str(args.top_k)
            for row in output["cases"]:
                if row["missed"] or row["recall"][headline] < 1.0:
                    print(
                        f"  recall@{headline}={row['recall'][headline]:.2f} {row['id']} | "
                        f"漏: {', '.join(row['missed']) or '-'} | top5: {', '.join(row['top'])}"
                    )
        return 0

    if args.json:
        print(json.dumps({"runs": runs}, ensure_ascii=False, indent=2))
        return 0
    header = "strategy".ljust(20) + "".join(f"recall@{k}".rjust(12) for k in LINK_CHECK_KS)
    first = runs[0]["summary"]
    print(
        f"catalog={first['catalog_tables']} cases={first['cases']} "
        f"embedding={first.get('embedding') or 'none'}"
    )
    print(header)
    for run in runs:
        summary = run["summary"]
        label = f"{summary['strategy']}"[:19]
        print(label.ljust(20) + "".join(f"{summary[f'recall@{k}']:.3f}".rjust(12) for k in LINK_CHECK_KS))
        if summary.get("degraded"):
            print(f"  ! {label} 已降级为 BM25: {summary.get('degraded_reason', '')}")
        vector = summary.get("vector") or {}
        if vector:
            print(
                f"  {label} 向量缓存: texts={vector.get('texts')} calls={vector.get('calls')} "
                f"cache_hit={vector.get('cache_hit')} {vector.get('elapsed_ms')}ms"
            )
    return 0


def _cmd_guard_check(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    if not settings.schema_path.exists():
        print(
            f"schema 目录不存在: {settings.schema_path}\n"
            "外接目录请先运行 nl2sql-agent schema-sync，或用 NL2SQL_SCHEMA_PATH 指定文件。",
            file=sys.stderr,
        )
        return 2
    catalog = Catalog.from_path(settings.schema_path, linking=settings.linking)
    adversarial = load_case_file(Path(args.cases) if args.cases else ADVERSARIAL_CASES)
    permission = load_case_file(PERMISSION_CASES)
    fanout = load_case_file(FANOUT_CASES)
    principals = load_principals(
        Path(args.principals) if args.principals else settings.principals_path
    )
    gold = [
        {"id": case.id, "gold_sql": case.gold_sql} for case in load_cases(settings.eval_cases_path)
    ]

    report = run_all(
        catalog,
        adversarial=adversarial,
        permission=permission,
        fanout=fanout,
        gold=gold,
        principals=principals,
        with_ast=False if args.no_ast else None,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_guard_report(report)
    return 1 if any(report["failures"].values()) else 0


def _print_guard_report(report: dict[str, Any]) -> None:
    meta = report["catalog"]
    adv = report["adversarial"]
    perm = report["permission"]
    fan = report["fanout"]
    gold = report["gold_sql"]
    ast_state = "可用" if meta["ast_available"] else "未安装(sqlglot)"
    measured = "，本次已测量" if meta["ast_measured"] else "，本次跳过 AST 对比"
    print(f"护栏基准  dialect={meta['dialect']} tables={meta['tables']} AST={ast_state}{measured}")
    print(
        f"对抗用例  {adv['total']} 条（应拦截 {adv['blocking']} / 应放行 {adv['pass_through']}，"
        f"其中 {adv['tagged_ast']} 条的判定依赖 AST 层）"
    )
    if meta["ast_measured"]:
        print(
            f"  正则层拦截 {adv['regex_blocked']}/{adv['regex_blocking_scope']}"
            f"（正则作用域 {adv['regex_scope']} 条），"
            f"正则+AST 拦截 {adv['ast_blocked']}/{adv['blocking']}"
        )
        if adv["ast_only_blocked"]:
            print(f"  AST 独占拦截: {', '.join(adv['ast_only_blocked'])}")
        if adv["regex_also_blocked_ast_tagged"]:
            misdiagnosed = ", ".join(adv["regex_also_blocked_ast_tagged"])
            print(
                f"  正则也拦下但原因不对的 AST 用例: {misdiagnosed}"
                "（正则报 unknown_table，AST 报 metadata_access）"
            )
    else:
        print(
            f"  正则层拦截 {adv['regex_blocked']}/{adv['regex_blocking_scope']}"
            f"（正则作用域 {adv['regex_scope']} 条），AST 层本次未测量"
        )
    if adv["regex_missed"]:
        print(f"  未拦截(应为 bug): {', '.join(adv['regex_missed'])}")
    if adv["false_positive_ast"]:
        print(f"  AST 误杀放行用例: {', '.join(adv['false_positive_ast'])}")
    print(
        f"权限用例  {perm['total']} 条，角色 {len(perm['principals'])} 个，"
        f"拦截 {perm['blocked']} 条，视图泄漏 {len(perm['view_leaks'])} 条"
    )
    print(f"扇出用例  {fan['total']} 条，命中告警 {fan['warned']} 条")
    gold_ast_note = (
        f"；AST 层 error {gold['ast']['errors']} warning {gold['ast']['warnings']}"
        if meta["ast_measured"]
        else "；AST 层本次未测量"
    )
    print(
        f"gold SQL  {gold['regex']['total']} 条"
        f"（跳过 {gold['regex']['skipped']} 条无 gold 的拒答用例）："
        f"正则层 error {gold['regex']['errors']} warning {gold['regex']['warnings']}"
        f"{gold_ast_note}"
    )
    for section, rows in (
        ("对抗(正则)", adv["failed_regex"]),
        ("对抗(AST)", adv["failed_ast"]),
        ("权限", perm["failed"]),
        ("扇出", fan["failed"]),
    ):
        for row in rows:
            print(
                f"  ✗ {section} {row['id']}: expect_valid={row['expect_valid']} "
                f"actual={row['actual_valid']} codes={','.join(row['codes']) or '-'} "
                f"missing={','.join(row['missing_codes']) or '-'}"
            )
            if row.get("detail"):
                print(f"     {row['detail'][:120]}")
    for leak in perm["view_leaks"]:
        print(f"  ✗ 视图泄漏: {leak}")
    for row in gold["regex"]["error_cases"] + gold["ast"]["error_cases"]:
        print(f"  ✗ gold SQL 被拦: {row}")


def _cmd_dialect_check(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    if not settings.schema_path.exists():
        print(
            f"schema 目录不存在: {settings.schema_path}\n"
            "外接目录请先运行 nl2sql-agent schema-sync，或用 NL2SQL_SCHEMA_PATH 指定文件。",
            file=sys.stderr,
        )
        return 2
    target = settings.target
    if isinstance(target, SqliteTarget) and not target.path.exists():
        print(
            f"演示库不存在: {target.path}\n先运行 nl2sql-agent init 建库，"
            "否则结果集比对一列都是未测量。",
            file=sys.stderr,
        )
        return 2
    catalog = Catalog.from_path(settings.schema_path, linking=settings.linking)
    cases = load_case_file(Path(args.cases) if args.cases else DIALECT_CASES)
    report = run_dialect_audit(
        catalog, cases, target=target, with_ast=False if args.no_ast else None
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_dialect_report(report)
    if report["catalog"]["missing_tables"]:
        missing = ", ".join(report["catalog"]["missing_tables"])
        print(
            f"目录与语料不匹配：目录里缺 {missing}。\n"
            "这份语料查的是合成演示库的表，护栏/解析列换目录就没有意义（每条 gold 都会因为 "
            "unknown_table 被判成拦截），所以这一步拒绝给出判定。"
            "请用演示目录跑（去掉 NL2SQL_SCHEMA_PATH 与 mssql 后端）。",
            file=sys.stderr,
        )
        return 2
    return 1 if any(report["invariant_failures"].values()) else 0


def _print_dialect_report(report: dict[str, Any]) -> None:
    parser_state = "可用" if report["parser_measured"] else "未安装(sqlglot)"
    print(
        f"方言基准  backend={report['backend']} cases={report['cases']} "
        f"intents={report['intents']} axes={len(report['axes'])} "
        f"dialects={report['dialects']} 解析器={parser_state}"
    )
    gold_parse, guard, ref = report["gold_parse"], report["gold_guard"], report["reference"]
    cat = report["catalog"]
    print(
        f"目录  dialect={cat['dialect']} tables={cat['tables']} 语料引用 "
        f"{len(cat['corpus_tables'])} 张表，目录缺 {len(cat['missing_tables'])} 张"
    )
    if cat["missing_tables"]:
        print(f"  ✗ 目录与语料不匹配: {', '.join(cat['missing_tables'])}")
    print(
        f"gold 解析  ok {gold_parse['ok']} / failed {gold_parse['failed']} / "
        f"未测量 {gold_parse['unmeasured']}"
    )
    if gold_parse["failed_ids"]:
        print(f"  ✗ 解析失败: {', '.join(gold_parse['failed_ids'])}")
    print(
        f"gold 护栏  正则层拦截 {guard['regex_blocked']} 条"
        + (f"（{', '.join(guard['blocked_ids'])}）" if guard["blocked_ids"] else "")
        + ("，AST 层本次未测量" if not guard["ast_checked"] else "")
    )
    print(
        f"参考执行  已执行 {ref['executed']} 条，失败 {ref['failed']}，"
        f"空结果 {ref['empty']}"
    )
    eq = report["equivalence"]
    rate = eq["match_rate"]
    print(
        f"结果集比对  可比 {eq['comparable']} 条（原生 {eq['native']} / 转译 {eq['transpiled']}）："
        f"一致 {eq['match']}、静默偏离 {eq['divergence']}、执行报错 {eq['exec_error']}、"
        f"转译失败 {eq['transpile_error']}、未测量 {eq['unmeasured']}"
        + (f"，一致率 {rate:.2%}" if isinstance(rate, (int, float)) else "")
        + (
            f"（可比 {eq['comparable']}/{report['cases']} 条）"
            if isinstance(rate, (int, float)) and eq["comparable"] < report["cases"]
            else ""
        )
        + (
            f"（只看转译的 {eq['comparable_transpiled']} 条：{eq['transpiled_match_rate']:.2%}）"
            if isinstance(eq["transpiled_match_rate"], (int, float))
            else ""
        )
    )
    if eq["divergence_ids"]:
        print(f"  · 静默偏离（结果不同且没有报错）: {', '.join(eq['divergence_ids'])}")
    if eq["exec_error_ids"]:
        print(f"  · 响亮失败（执行/转译直接报错）: {', '.join(eq['exec_error_ids'])}")
    wrong = report["wrong_shapes"]
    print(
        f"跨方言错误形状  {wrong['total']} 条：正则层拦 {wrong['regex_blocked']}、"
        f"AST 层拦 {wrong['ast_blocked']}（AST 独占 {len(wrong['ast_only_blocked_ids'])}）、"
        f"SQLite 引擎拒绝 {wrong['engine_rejects']}、引擎照跑 {wrong['engine_runs']}"
    )
    if wrong["unguarded_ids"]:
        print(f"  · 两层护栏都没拦: {', '.join(wrong['unguarded_ids'])}")
    if not report["parser_measured"]:
        print("  注意：未安装 sqlglot，本次解析与转译列均为未测量，不是 0 命中。")
    for name, ids in report["invariant_failures"].items():
        if ids:
            print(f"  ✗ 语料不变量破坏（{name}）: {', '.join(ids)}")


def _cmd_alias_check(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home, provider=args.provider)
    if not settings.schema_path.exists():
        print(
            f"schema 目录不存在: {settings.schema_path}\n"
            "词典里的 表.列 要对着目录核对，外接目录请先运行 nl2sql-agent schema-sync。",
            file=sys.stderr,
        )
        return 2
    cases_path = Path(args.cases) if args.cases else settings.alias_cases_path
    if not cases_path.exists():
        print(f"别名用例文件不存在: {cases_path}", file=sys.stderr)
        return 2
    dictionary_path = Path(args.dictionary) if args.dictionary else settings.value_dictionary_path
    if not dictionary_path.exists():
        print(f"业务词典不存在: {dictionary_path}", file=sys.stderr)
        return 2
    if not settings.is_server_backend and not settings.target.path.exists():
        print(
            f"演示库不存在: {settings.target.path}\n先运行 nl2sql-agent init 建库，"
            "否则词典取值核对与配对 A/B 全是未测量。",
            file=sys.stderr,
        )
        return 2
    catalog = Catalog.from_path(settings.schema_path, linking=settings.linking)
    cases = load_case_file(cases_path)
    frozen = load_case_file(settings.eval_cases_path) if settings.eval_cases_path.exists() else []
    try:
        report = run_alias_audit(
            settings,
            catalog,
            cases,
            frozen,
            dictionary_path=dictionary_path,
            with_ab=not args.no_ab,
        )
    except ValueError as exc:
        print(f"业务词典读不出来: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_alias_report(report)
    if report["catalog"]["missing"]:
        print(
            f"目录与词典不匹配：词典声明的列在目录里找不到 "
            f"{', '.join(report['catalog']['missing'])}。\n"
            "换错目录时每条注入都会指向一个不存在的列，核对结果没有意义，"
            "所以这一步拒绝给出判定（用演示目录跑，或用 --dictionary 指定配套词典）。",
            file=sys.stderr,
        )
        return 2
    findings = {name: ids for name, ids in report["findings"].items() if ids}
    if findings:
        for name, ids in findings.items():
            print(f"✗ 不变量破坏（{name}）: {', '.join(ids)}", file=sys.stderr)
        return 1
    return 0


def _print_alias_report(report: dict[str, Any]) -> None:
    dictionary, cat = report["dictionary"], report["catalog"]
    print(
        f"值对齐基准  cases={report['cases']} 词典={dictionary['path']} "
        f"条目 {dictionary['entries']} / 别名 {dictionary['aliases']} / "
        f"提法 {dictionary['mentions']}，声明碰撞 {dictionary['collisions']}"
    )
    print(
        f"目录  dialect={cat['dialect']} tables={cat['tables']} 词典声明列 "
        f"{cat['columns']} 张，目录缺 {len(cat['missing'])}"
        + (f"，列名不安全无法核对 {len(cat['uncheckable'])}" if cat["uncheckable"] else "")
    )
    data = report["data"]
    print(
        f"库内取值  查了 {data['columns']} 列的 DISTINCT：陈旧取值 "
        f"{len(data['stale'])}，跳过 {len(data['skipped'])}"
        + (f"，执行报错 {len(data['errors'])}" if data["errors"] else "")
    )
    for message in data["errors"]:
        print(f"  · {message}")
    dim = report["dim_lookup"]
    if dim["present"]:
        print(
            f"枚举登记  状态条目 {dim['status_entries']} 条，声称来自 dim_lookup 的 "
            f"{dim['declared']} 条里 {dim['labels']} 条对上了编码和中文名，"
            f"缺编码 {len(dim['missing_code'])}，缺登记名 {len(dim['missing_label'])}，"
            f"没有字典可对照 {len(dim['unchecked'])}"
        )
    else:
        print(
            f"枚举登记  目录里没有 dim_lookup：声称登记的 {dim['declared']} 条全部无法核对"
        )
    for item in dim["missing_code"] + dim["missing_label"]:
        print(f"  ✗ {item}")
    frozen = report["frozen"]
    print(
        f"冻结集触达  {frozen['changed']}/{frozen['cases']} 条问法被追加取值，"
        f"转成澄清 {len(frozen['ambiguous'])} 条"
    )
    if frozen["injected_values"]:
        top = ", ".join(f"{key}×{value}" for key, value in list(frozen["injected_values"].items())[:4])
        print(f"  · 注入值分布: {top}")
    print(
        "语料期望  "
        + " / ".join(f"{key} {value}" for key, value in report["expectations"].items())
    )
    print(
        "对齐判定  "
        + " / ".join(
            f"{key} {value}" for key, value in report["align"].items() if key != "unmeasured"
        )
    )
    ab = report["ab"]
    if not ab.get("measured"):
        print("配对 A/B  本次未测量（--no-ab）")
    else:
        clar = ab["clarification"]
        print(
            f"配对 A/B  provider={ab['provider']} 可比 {ab['scored']}/{ab['cases']} 条"
            f"（gold 可执行 {ab['gold_executed']}）："
            f"关闭对齐通过 {ab['off_pass']}，开启对齐通过 {ab['on_pass']}；"
            f"修好 {len(ab['fixed'])}、改坏 {len(ab['broken'])}、"
            f"两臂都过 {ab['both_pass']}、两臂都败 {ab['both_fail']}、未测量 {ab['unmeasured']}"
        )
        if ab["fixed"]:
            print(f"  · 只有对齐才对: {', '.join(ab['fixed'])}")
        if ab["broken"]:
            print(f"  · 对齐后反而错（语料差异，不是门禁）: {', '.join(ab['broken'])}")
        if ab["gold_failed"]:
            print(f"  · gold 执行失败: {', '.join(ab['gold_failed'])}")
        print(
            f"  · 歧义问法 {clar['cases']} 条不计入上面：开启对齐后反问 {clar['asked_on']} 条、"
            f"带候选项 {clar['with_options']} 条；关闭对齐时只有 {clar['asked_off']} 条会反问，"
            "且没有候选项可点"
        )
    findings = {name: ids for name, ids in report["findings"].items() if ids}
    if not findings:
        print("不变量  全部通过")


def _cmd_clarify_check(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home, provider=args.provider)
    if args.mode:
        mode = str(args.mode).strip().lower()
        if mode not in CLARIFY_MODES:
            print(f"未知的澄清策略模式 {args.mode}，可选 {list(CLARIFY_MODES)}", file=sys.stderr)
            return 2
        settings.clarify = mode
    if not settings.schema_path.exists():
        print(
            f"schema 目录不存在: {settings.schema_path}\n"
            "触发码要对着目录核对，外接目录请先运行 nl2sql-agent schema-sync。",
            file=sys.stderr,
        )
        return 2
    cases_path = Path(args.cases) if args.cases else settings.clarify_cases_path
    if not cases_path.exists():
        print(f"澄清用例文件不存在: {cases_path}", file=sys.stderr)
        return 2
    packaged_corpus = cases_path.resolve() == Path(settings.clarify_cases_path).resolve()
    if packaged_corpus and settings.schema_path.resolve() != PACKAGED_DEMO_SCHEMA:
        print(
            f"目录与语料不匹配: 当前 schema 是 {settings.schema_path}（backend={settings.db_backend}），"
            "而打包的澄清语料、名词表和期望取值都写死在演示库上。\n"
            "换外接目录请自带 --cases 与 --glossary，否则「选项能否回问」「二次问数」"
            "测的是目录对不上，不是策略。这一步拒绝给出判定。",
            file=sys.stderr,
        )
        return 2
    glossary_path = Path(args.glossary) if args.glossary else settings.metric_glossary_path
    if glossary_path is not None and not glossary_path.exists():
        print(f"指标歧义名词表不存在: {glossary_path}", file=sys.stderr)
        return 2
    if not settings.is_server_backend and not settings.target.path.exists():
        print(
            f"演示库不存在: {settings.target.path}\n先运行 nl2sql-agent init 建库，"
            "否则选项可执行性与配对 A/B 全是未测量。",
            file=sys.stderr,
        )
        return 2
    catalog = Catalog.from_path(settings.schema_path, linking=settings.linking)
    cases = load_case_file(cases_path)
    frozen = load_case_file(settings.eval_cases_path) if settings.eval_cases_path.exists() else []
    try:
        report = run_clarify_audit(
            settings,
            catalog,
            cases,
            frozen,
            glossary_path=glossary_path,
            with_ab=not args.no_ab,
        )
    except ValueError as exc:
        print(f"澄清语料或名词表读不出来: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_clarify_report(report)
    if not frozen:
        print(
            f"冻结集缺失: {settings.eval_cases_path}\n没有冻结集就无法核对「澄清不会吃掉已能回答的题」，"
            "这一步拒绝给出判定。",
            file=sys.stderr,
        )
        return 2
    findings = {name: ids for name, ids in report["findings"].items() if ids}
    if findings:
        for name, ids in findings.items():
            print(f"✗ 不变量破坏（{name}）: {', '.join(ids)}", file=sys.stderr)
        return 1
    return 0


def _cmd_cache_check(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home, provider=args.provider)
    try:
        report = run_cache_audit(settings, provider=args.provider, question=args.question or "")
    except (ValueError, RuntimeError) as exc:
        print(f"缓存测量跑不起来: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_cache_report(report)
    return 0


def _print_cache_report(report: dict[str, Any]) -> None:
    result = report["result_cache"]
    print(f"缓存测量  provider={report['provider']}  问题={report['question']}")
    cold, warm = result["cold"], result["warm"]
    print(
        f"结果缓存  冷启动 status={cold['status']} tokens={cold['tokens']} steps={cold['steps']} "
        f"{cold['elapsed_ms']:.1f}ms → 热命中 cached={warm['cached']} tokens={warm['tokens']} "
        f"steps={warm['steps']} {warm['elapsed_ms']:.1f}ms"
    )
    print(
        f"  · 归一化变体（不同标点/空白，同一条键）命中={result['normalized_variant']['cached']}；"
        f"不同问题命中={result['distinct_question']['cached']}"
    )
    print(f"  · {result['note']}")
    stats = result["stats"]
    if stats.get("enabled"):
        print(
            f"  · 统计 hits={stats['hits']} misses={stats['misses']} 命中率={_metric_ratio(stats['hit_rate'])} "
            f"过期={stats['expired']} 淘汰={stats['evictions']} 条目={stats['entries']}/{stats['max_entries']}"
        )
    embedding = report["embedding_cache"]
    print(
        f"embedding 缓存  首建 cache_hit={embedding['first']['cache_hit']} calls={embedding['first']['calls']} "
        f"{embedding['first']['elapsed_ms']}ms → 复用 cache_hit={embedding['second']['cache_hit']} "
        f"calls={embedding['second']['calls']} {embedding['second']['elapsed_ms']}ms；"
        f"文档={embedding['documents']} 缓存文件={embedding['cache_files']} "
        f"未再调 provider={embedding['reused_without_embedding']}"
    )
    prompt = report["prompt_prefix"]
    print(
        f"prompt 前缀  稳定前缀 {prompt['common_prefix_chars']}/{prompt['prompt_chars']} 字符 "
        f"= {prompt['stable_ratio'] * 100:.1f}%（尾部可变 {prompt['volatile_chars']} 字符，"
        f"few-shot 在末尾={prompt['few_shot_at_tail']}）"
    )
    print(f"  · {prompt['note']}")


def _cmd_metrics(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    path = Path(args.log_path).expanduser() if args.log_path else settings.log_path
    if path is None:
        print(
            "未配置事件日志: 设 NL2SQL_LOG_PATH=var/agent.jsonl（或 --log-path）再运行。\n"
            "日志默认不落盘——一个库不该在被 import 的时候就开始写磁盘，所以这一步没有数据可判。",
            file=sys.stderr,
        )
        return 2
    bundle = read_events(path, limit=max(1, int(args.limit or 0)))
    if bundle["missing"]:
        print(f"事件日志不存在: {path}（先跑一次 ask / chat / eval，日志只会往后追加）", file=sys.stderr)
        return 2
    report = {
        "path": str(path),
        "lines": bundle["lines"],
        "malformed_lines": bundle["malformed"],
        "metrics": summarize(bundle["events"], malformed=bundle["malformed"]),
    }
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_metrics_report(report)
    return 0


def _metric_ratio(value: Any) -> str:
    return "—" if value is None else f"{value:.4f}"


def _cmd_cost(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    path = Path(args.log_path).expanduser() if args.log_path else settings.log_path
    if path is None:
        print(
            "未配置事件日志: 设 NL2SQL_LOG_PATH=var/agent.jsonl（或 --log-path）再运行。\n"
            "成本是从历史 turn 事件里折出来的，没有日志就没有可汇总的 token。",
            file=sys.stderr,
        )
        return 2
    prices = settings.price_table
    if args.price_input is not None or args.price_output is not None or args.price_cached_input is not None:
        if args.price_input is None or args.price_output is None:
            print(
                "只给一半单价会算出漏掉另一半的假成本: --price-input 与 --price-output 必须同时给。",
                file=sys.stderr,
            )
            return 2
        prices = {
            "input_per_1m": args.price_input,
            "output_per_1m": args.price_output,
            "currency": args.currency or settings.price_currency,
        }
        cached = (
            args.price_cached_input
            if args.price_cached_input is not None
            else settings.price_cached_input_per_1m
        )
        if cached is not None:
            prices["cached_input_per_1m"] = cached
    elif args.currency:
        prices = dict(prices or {}, currency=args.currency) if prices else None
    try:
        bundle = read_events(path, limit=max(1, int(args.limit or 0)))
        if bundle["missing"]:
            print(
                f"事件日志不存在: {path}（先跑一次 ask / chat / eval，日志只会往后追加）",
                file=sys.stderr,
            )
            return 2
        report = cost_summary(bundle["events"], prices=prices)
    except ValueError as exc:
        print(f"价格配置不合法: {exc}", file=sys.stderr)
        return 2
    payload = {
        "path": str(path),
        "lines": bundle["lines"],
        "malformed_lines": bundle["malformed"],
        "by": args.by,
        "report": report,
    }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_cost_report(payload)
    return 0


def _print_cost_report(payload: dict[str, Any]) -> None:
    report = payload["report"]
    print(f"事件日志  {payload['path']} lines={payload['lines']} 坏行={payload['malformed_lines']}")
    for line in cost_rollup_lines(report, by=payload["by"]):
        print(line)


def _print_metrics_report(report: dict[str, Any]) -> None:
    metrics = report["metrics"]
    latency = metrics["latency_ms"]
    print(
        f"观测汇总  events={metrics['events']} turns={metrics['turns']} "
        f"tool={metrics['tool_calls']} db={metrics['db_calls']} "
        f"物理连接={metrics['db_connections']} 池化调用={metrics['db_pooled_calls']} "
        f"文件={report['path']} 坏行={report['malformed_lines']}"
    )
    print(
        f"延迟(ms)  avg={latency['avg']} p50={latency['p50']} p95={latency['p95']} "
        f"max={latency['max']}  QPS={_metric_ratio(metrics['qps'])} "
        f"（窗口 {metrics['window_sec']}s，{metrics['qps_note']}）"
    )
    db_latency = metrics["db_latency_ms"]
    print(
        f"执行层延迟(ms)  avg={db_latency['avg']} p50={db_latency['p50']} "
        f"p95={db_latency['p95']}（只算 db 事件，不含模型耗时）"
    )
    print(
        f"比率  成功={_metric_ratio(metrics['success_rate'])} "
        f"人工升级={_metric_ratio(metrics['escalation_rate'])} "
        f"澄清={_metric_ratio(metrics['clarification_rate'])} "
        f"经验命中={_metric_ratio(metrics['experience_hit_rate'])}"
    )
    print(
        f"修复与工具  repair 轮数={metrics['repair_turns']}/{metrics['turns']} "
        f"其中答对={metrics['repair_turns_answered']} "
        f"repair 成功率={_metric_ratio(metrics['repair_success_rate'])} "
        f"repair 调用失败率={_metric_ratio(metrics['repair_call_error_rate'])} "
        f"工具错误率={_metric_ratio(metrics['tool_error_rate'])} "
        f"DB 错误率={_metric_ratio(metrics['db_error_rate'])}"
    )
    print(
        f"状态  " + "  ".join(f"{name}={count}" for name, count in metrics["status_counts"].items())
    )
    if metrics["guard_codes"]:
        print(
            f"护栏拒绝  {len(metrics['guard_codes'])} 类  "
            + "  ".join(f"{code}×{count}" for code, count in metrics["guard_codes"].items())
        )
    if metrics["db_error_codes"]:
        print("DB 错误码  " + "  ".join(f"{code}×{count}" for code, count in metrics["db_error_codes"].items()))
    print(
        f"口径可回溯  prompt_version={','.join(metrics['prompt_versions']) or '—'} "
        f"prompt_hash 数={metrics['prompt_hashes']} schema_hash 数={len(metrics['schema_hashes'])} "
        f"model={','.join(metrics['models']) or '—'} trace 数={metrics['trace_ids']}"
    )
    by_version = metrics.get("by_prompt_version") or {}
    if len(by_version) > 1:
        # 灰度对比要的是同一时间窗内两臂的结论差异，所以直接用 turn 行里的
        # prompt_version 分组，不需要另建一张实验表。
        print(
            "灰度对比  "
            + "  ".join(
                f"{version}×{row['turns']} ok={_metric_ratio(row['ok_rate'])} "
                f"澄清={_metric_ratio(row['clarification_rate'])} "
                f"升级={_metric_ratio(row['escalation_rate'])}"
                for version, row in by_version.items()
            )
        )
    usage = metrics["token_usage"]
    cache = metrics["cache"]
    print(
        f"token  avg={metrics['avg_tokens']} total={metrics['total_tokens']} "
        f"prompt={usage['prompt_tokens']} completion={usage['completion_tokens']} "
        f"cached={usage['cached_tokens']} 未分类={usage['unclassified_tokens']}  "
        f"双通道  投票={metrics['dual_channel_votes']} "
        f"一致率={_metric_ratio(metrics['dual_channel_agreement_rate'])} "
        f"分歧={metrics['dual_channel_disagreements']}"
    )
    print(
        f"结果缓存  命中={cache['hits']} 命中率={_metric_ratio(cache['hit_rate'])} "
        f"省下 token={cache['saved_tokens']}（命中轮不调模型、不执行 SQL）"
    )


def _print_clarify_report(report: dict[str, Any]) -> None:
    policy = report["policy"]
    catalog = report["catalog"]
    print(
        f"澄清基准  cases={report['cases']} mode={policy['mode']} 基准日={policy['as_of']} "
        f"名词表={policy['glossary_source']}（{len(policy['glossary']['terms'])} 个歧义名词 / "
        f"{policy['glossary']['candidates']} 个候选口径）"
    )
    print(
        f"目录  backend={catalog['backend']} dialect={catalog['dialect']} "
        f"tables={catalog['tables']} schema={catalog['schema']}"
        + ("" if catalog["packaged_demo"] else "  ⚠ 非打包演示库：语料与期望取值不适用")
    )
    print(
        f"触发分层  阻塞档 {', '.join(policy['ask_codes'])}；"
        f"假设档 {', '.join(policy['policy_codes'])}；期间选项 "
        + "、".join(f"{phrase}={literal}" for phrase, literal in policy["periods"])
    )
    print(
        "语料种类  "
        + " / ".join(f"{key} {value}" for key, value in report["kinds"].items() if value)
    )
    print(
        "观测触发码  "
        + " / ".join(f"{key} {value}" for key, value in report["codes"].items() if value)
    )
    ask = report["ask_quality"]
    print(
        f"澄清质量  反问 {ask['clarified']} 条：可点选 {ask['actionable']}、"
        f"裸「请补充」{ask['bare']}；候选问句 {ask['options_offered']} 条，"
        f"另有 {ask['alternatives_offered']} 条更窄问法跟着正常答案一起给出"
    )
    print(
        f"选项回问  {ask['reask_cases']} 条用例的 {ask['reask_total']} 条问句回问，"
        f"{ask['reask_ok']} 条拿到结果，其中 {ask['reask_empty']} 条结果为空"
    )
    fu = report["followup"]
    print(
        f"二次问数  {fu['cases']} 条：序号/片段被解析 {fu['resolved']}，"
        f"解析后拿到数据 {fu['answered']}，仍卡在实体歧义 {fu['looped']}"
    )
    frozen = report["frozen"]
    print(
        f"冻结集代价  {frozen['cases']} 条里指标歧义 {frozen['metric']}、时间缺失 {frozen['time']}"
        f"（同时命中 {len(frozen['both'])}）、不触发 {frozen['quiet']}，"
        f"共递出 {frozen['options_offered']} 条更窄的问法"
    )
    print(
        f"  · options 档（默认）被本层拦下 {len(frozen['blocking_options'])} 条；"
        f"换成 ask 档会拦下 {frozen['blocking_ask']}/{frozen['cases']} 条"
        + (f"：{', '.join(frozen['blocking_ask_ids'])}" if frozen["blocking_ask_ids"] else "")
    )
    print(
        f"  · 实体多义在冻结集上拦下 {len(frozen['entity_blocking'])} 条"
        + (f"：{', '.join(frozen['entity_blocking'])}" if frozen["entity_blocking"] else "")
    )
    ab = report["ab"]
    if not ab.get("measured"):
        print("配对 A/B  本次未测量（--no-ab）")
    else:
        modes = ab["modes"]
        print(
            f"配对 A/B  provider={ab['provider']} modes={'→'.join(modes)} 共 {ab['cases']} 条："
            + "，".join(
                f"{mode} 反问 {ab['asked'][mode]} / 可点选 {ab['actionable'][mode]} / "
                f"裸澄清 {ab['bare'][mode]} / 选项 {ab['options_offered'][mode]}"
                for mode in modes
            )
        )
        if ab["regressed"]:
            print(f"  · 开启后反而不答（语料差异，不是门禁）: {', '.join(ab['regressed'])}")
        if ab["improved"]:
            print(f"  · 只有开启才答得上来: {', '.join(ab['improved'])}")
    for name, ids in report["observations"].items():
        print(f"  · 观测（不计入门禁）{name}: {', '.join(ids)}")
    findings = {name: ids for name, ids in report["findings"].items() if ids}
    if not findings:
        print("不变量  全部通过")


_CHAT_HELP = """会话命令：
  :help     显示帮助
  :history  显示当前会话上下文与槽位
  :reset    清空会话
  :verbose  切换改写/工具轨迹输出
  :exit     退出（Ctrl+C、Ctrl+D 同样退出）
上一条给了候选问法时，直接输入序号即可选中。"""


def _cmd_chat(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    conversation = Conversation()
    verbose = bool(args.verbose)
    pending_options: list[str] = []
    # 会话内固定同一个 key：灰度桶按会话落，不能在多轮中间换 prompt。
    chat_key = f"cli-chat:{new_trace_id()}"
    print(f"Provider: {runtime.settings.provider} | 改写通道: {runtime.settings.rewriter}")
    print("输入问题开始多轮问数会话，:help 查看命令。")

    while True:
        try:
            line = input("\n问> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        except UnicodeDecodeError:
            print("输入解码失败：请先设置 PYTHONIOENCODING=utf-8 再运行 chat。", file=sys.stderr)
            return 2
        if not line:
            continue
        command = line.lower()
        if command in {":exit", ":quit", "exit", "quit"}:
            break
        if command == ":help":
            print(_CHAT_HELP)
            continue
        if command == ":reset":
            conversation = Conversation()
            pending_options = []
            print("会话已清空。")
            continue
        if command == ":verbose":
            verbose = not verbose
            print(f"verbose = {verbose}")
            continue
        if command == ":history":
            transcript = conversation.render(max_turns=20)
            print(transcript if transcript else "(尚无已回答的轮次)")
            print(f"当前上下文：{conversation.pending_slots().summary()}")
            continue
        if line.isdigit() and pending_options:
            index = int(line)
            if not 1 <= index <= len(pending_options):
                print(f"序号超出范围（1-{len(pending_options)}），请直接输入问题。")
                continue
            line = pending_options[index - 1]
            print(f"选择问法：{line}")
        pending_options = []

        with bind_trace_id(new_trace_id()):
            result = runtime.agent.run(
                line, history=conversation, gray_key=chat_key
            )
        conversation.add(result.to_turn())
        if result.effective_question != result.question:
            print(f"resolved: {result.effective_question} | source: {result.rewrite_source}")
        _print_result(result, verbose=verbose, compact=True)
        clarification = result.clarification
        if clarification and clarification.options:
            pending_options = list(clarification.options)
            print(f"输入序号 1-{len(pending_options)} 可直接选择上面的问法。")
        elif result.alternatives:
            print("想收窄口径，可以直接这样再问一次：")
            for index, option in enumerate(result.alternatives, 1):
                print(f"  {index}. {option}")
            pending_options = list(result.alternatives)

    answered = sum(1 for turn in conversation.turns if turn.status == "ok")
    print(f"会话结束：{len(conversation)} 轮，其中 {answered} 轮已回答。")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .web.app import create_app
    except ImportError:
        print('Web 控制台需要可选依赖，请先安装：pip install -e ".[web]"', file=sys.stderr)
        return 2
    runtime = _runtime(args)
    print(f"NL2SQL 控制台: http://{args.host}:{args.port} | provider={runtime.settings.provider}")
    print("Ctrl+C 停止。API 文档见 /api/docs")
    uvicorn.run(create_app(runtime), host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_generate_cases(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    if runtime.settings.provider == "mock":
        print(
            "generate-cases 需要在线模型：规则基线只会重复模板问法，草拟没有意义。"
            "请配好 NL2SQL_API_KEY 后用 --provider openai。",
            file=sys.stderr,
        )
        return 2
    frozen = load_cases(runtime.settings.eval_cases_path)
    out = Path(args.out) if args.out else Path(runtime.settings.home) / "var" / "ai_draft_cases.jsonl"
    result = generate_cases(
        runtime.agent.llm,
        runtime.catalog,
        db_path=runtime.settings.target,
        count=args.count,
        seeds=seed_examples(frozen, limit=args.examples),
        avoid=[case.question for case in frozen],
        guard=runtime.guard,
        max_rows=runtime.settings.max_rows,
        timeout_ms=runtime.settings.query_timeout_ms,
    )
    written = write_cases(out, result.drafts)
    print(
        f"模型 {runtime.settings.model}：生成 {result.generated} 条，"
        f"通过校验 {written} 条，丢弃 {len(result.rejected)} 条，token {result.tokens}"
    )
    print(f"草稿追加到: {out.resolve()}（status=draft，评测器计入 skipped、不会移动冻结集指标）")
    for item in result.rejected[:10]:
        print(f"  丢弃 {item['question'][:40] or '<无问题>'}: {item['reason'][:80]}")
    print("人工审核：把选中草稿的 status 改为 active（或删除该行），再合并进冻结集。")
    return 0 if written else 1


def _cmd_eval(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    if args.fail_on_regression and not args.compare:
        print("--fail-on-regression 需要和 --compare 一起用（门禁要有基线）", file=sys.stderr)
        return 2
    if runtime.settings.schema_override is not None:
        # 冻结集的期望值绑在内置 demo 目录上：对着别的目录跑，EX 会因为表根本不存在而
        # 归零，那是配置事故不是模型质量。
        print(
            f"警告：本次评测用的目录是 {runtime.settings.schema_path}，不是内置 demo；"
            "冻结用例的期望值只对内置目录成立，与基线（benchmarks/）的比较在这一列不可信。",
            file=sys.stderr,
        )
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
            db_path=runtime.settings.target,
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
    if args.multi_turn:
        cases = cases + load_cases(runtime.settings.multi_turn_cases_path)
    if args.paraphrase:
        cases = cases + load_cases(runtime.settings.paraphrase_cases_path)
    if args.adversarial:
        cases = cases + load_cases(runtime.settings.adversarial_question_cases_path)
    if args.only_tags:
        wanted = set(args.only_tags)
        cases = [case for case in cases if wanted & set(case.tags)]
        if not cases:
            print(f"没有任何用例带有标签 {sorted(wanted)}", file=sys.stderr)
            return 2
    report = evaluate(
        runtime.agent,
        cases,
        db_path=runtime.settings.target,
        guard=runtime.guard,
        provider=runtime.settings.provider,
        report_dir=args.report_dir,
        model="" if runtime.settings.provider == "mock" else runtime.settings.model,
        prompt_version=args.prompt_version,
        judge_model=judge_model,
        judge_on_fail=args.judge_on_fail,
        judge_sample=args.judge_sample,
        judge_max_calls=runtime.settings.judge_max_calls,
        judge_max_rows=runtime.settings.judge_max_rows,
    )
    payload = report.to_dict()
    if args.write_baseline:
        try:
            _write_baseline(Path(args.write_baseline), payload)
        except ValueError as exc:
            print(f"无法生成基线: {exc}", file=sys.stderr)
            return 2
        # stderr: `eval --json --write-baseline` must still print one parseable document.
        print(f"Baseline written to: {Path(args.write_baseline).resolve()}", file=sys.stderr)
    regression_verdict = None
    if args.compare:
        baseline_path = Path(args.compare)
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            payload["comparison"] = _compare_reports(baseline, report, baseline_path)
        except (OSError, ValueError) as exc:
            print(f"无法读取基线报告: {exc}", file=sys.stderr)
            return 2
        if args.fail_on_regression:
            try:
                regression_verdict = check_regression(baseline, payload, args.max_drop)
            except ValueError as exc:
                print(f"EX 回归门禁拒绝比对: {exc}", file=sys.stderr)
                return 2
            payload["regression"] = regression_verdict
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Provider: {report.provider}")
        print(f"Cases: {report.total} | Passed: {report.passed}")
        for key, value in report.metrics.items():
            print(f"{key}: {value}")
        if report.paraphrase:
            block = report.paraphrase
            agreement = f"{block['verdict_agreement_rate']:.2%}" if block["pairs"] else "n/a"
            print(
                f"paraphrase_pairs: {block['pairs']} | verdict agreement: {agreement} | "
                f"brittle: {block['brittle']} | reverse: {block['reverse']} | "
                f"unmatched: {len(block['unmatched'])}"
            )
        if report.adversarial:
            safety = report.adversarial["safety"]
            fanout = report.adversarial["fanout"]
            pressure = report.adversarial["guess_pressure"]
            print(
                f"adversarial_cases: {report.adversarial['cases']} | leaked: {safety['leaked']} | "
                f"guard_missed: {safety['guard_missed']} | blocked_by_guard: "
                f"{safety['blocked_by_guard']} | no_attempt: {safety['clean_no_attempt']} | "
                f"refusal_hit: {safety['refusal_hit']}/{safety['refusal_expected_cases']}"
            )
            if fanout["cases"]:
                print(
                    f"adversarial_fanout: {fanout['cases']} | gold: {fanout['gold']} | "
                    f"fell_into_trap: {fanout['trap']} | neither: {fanout['neither']} | "
                    f"no_sql: {fanout['no_sql']} | join_fanout_warnings: "
                    f"{fanout['fanout_warning_cases']}"
                )
            if pressure["cases"]:
                print(
                    f"adversarial_pressure: asked {pressure['asked']} | guessed "
                    f"{pressure['guessed']} | over_clarified {pressure['over_clarified']} | "
                    f"answered {pressure['answered']}"
                )
        if report.judge.get("enabled"):
            print(
                f"AI Judge: {report.judge.get('status')} | "
                f"judged={report.judge.get('judged_cases')} | "
                f"unknown={report.judge.get('unknown_cases')} | "
                f"cache_hits={report.judge.get('cache_hits')}"
            )
        if args.compare:
            _print_comparison(payload["comparison"])
        if regression_verdict is not None:
            for line in format_regression(regression_verdict):
                print(line)
            if regression_verdict["ok"] and any(not case.passed for case in report.cases):
                print("（另有未通过用例：这一步只拦相对基线的指标回归，不拦已知失败）")
        print(f"Reports written to: {Path(args.report_dir).resolve()}")
    failed = [case for case in report.cases if not case.passed]
    if regression_verdict is not None:
        # The gate answers one question: did this change make the metrics worse? A corpus
        # that is report-only by design (mock fails 62 of 123) must not drown that answer,
        # so the verdict replaces the pass count here — and the human output says so.
        return 0 if regression_verdict["ok"] else 1
    return 0 if not failed else 1


def _write_baseline(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_baseline(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def _compare_reports(baseline: dict[str, Any], report: Any, path: Path) -> dict[str, Any]:
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
            f"#{row['id']} [{row['status']}/{row['quality']}] {row['question']}\n"
            f"  category={row['category']} source={row['source']} hits={row['hit_count']}\n"
            f"  {row['sql']}"
        )
        if row["status"] == "deprecated" and row.get("deprecate_reason"):
            print(f"  退役原因: {row['deprecate_reason']} ({row.get('deprecated_at') or '-'})")
    return 0


def _cmd_experience_promote(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    store = ExperienceStore(settings.experience_path)
    try:
        record = store.set_grade(args.id, args.quality)
    except (KeyError, ValueError) as exc:
        print(f"定级失败: {exc}")
        return 1
    print(json.dumps(record, ensure_ascii=False, indent=2))
    print("gold 只影响同类候选里的排序权重，可见性与 verified 相同。")
    return 0


def _cmd_experience_deprecate(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    store = ExperienceStore(settings.experience_path)
    try:
        record = store.deprecate(args.id, reason=args.reason)
    except KeyError as exc:
        print(f"退役失败: {exc}")
        return 1
    print(json.dumps(record, ensure_ascii=False, indent=2))
    print("已退役：行保留在库里可审计，但不再被任何召回通道返回。")
    return 0


def _cmd_experience_staleness(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    report = runtime.store.staleness(runtime.catalog.schema_hash())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["stale_rows"]:
        print(
            f"目录版本 {report['active_schema_hash'][:8]} 之外还有 "
            f"{report['stale_rows']} 条未退役经验（{len(report['stale_hashes'])} 个旧版本）。"
            "它们已经检索不到（召回按 hash 精确过滤），所以这里只是积压提示；"
            "确认后用 nl2sql-agent experience deprecate-stale --yes 退役。"
        )
    else:
        print("没有陈旧经验：库里每一行都属于当前目录版本。")
    return 0


def _cmd_experience_deprecate_stale(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    report = runtime.store.mark_stale(
        runtime.catalog.schema_hash(), reason=args.reason, dry_run=not args.yes
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.yes:
        print("这是预演：没有写库。加 --yes 才会把上面这些 id 标为 deprecated。")
        return 0
    print(
        f"已退役 {report['affected']} 条；库里剩下的每一行都与当前目录版本一致或已经退役。"
    )
    return 0


def _cmd_experience_check(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    try:
        report = run_experience_audit(
            frozen_path=Path(args.frozen) if args.frozen else FROZEN_CASES,
            paraphrase_path=Path(args.cases) if args.cases else PARAPHRASE_CASES,
            settings=settings,
            embedding=args.embedding,
            include_full_library=not args.paired_only,
        )
    except EmbeddingError as exc:
        print(f"向量 provider 不可用: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_experience_report(report)
    if not report["measured"]:
        print(
            "没有可测量的配对：同义改写语料的 paraphrase_of 与冻结集对不上，拒绝给出数字。",
            file=sys.stderr,
        )
        return 2
    if report["invariant_violations"]:
        print(
            "召回不变量被破坏: " + ", ".join(report["invariant_violations"]),
            file=sys.stderr,
        )
        return 1
    return 0


def _print_experience_report(report: dict[str, Any]) -> None:
    print(
        f"经验召回基准  配对查询={report['queries']} 冻结集={report['frozen_cases']} "
        f"(跳过 {report['frozen_without_sql']} 条无 gold SQL) "
        f"provider={report['provider']} 指纹={audit_fingerprint(report)}"
    )
    for profile in report["profiles"]:
        print(f"\n库={profile['profile']} rows={profile['library_rows']}")
        print(f"  {'arm':<12}{'mode':<9}{'R@1':>7}{'R@3':>7}{'R@5':>7}{'MRR':>8}{'diver':>7}{'降级':>6}")
        for arm, data in profile["arms"].items():
            recall = data["recall"]
            print(
                f"  {arm:<12}{data['mode']:<9}"
                f"{_metric(recall['@1']):>7}{_metric(recall['@3']):>7}{_metric(recall['@5']):>7}"
                f"{_metric(data['mrr']):>8}{data['displaced_by_diversity']:>7}{data['degraded']:>6}"
            )
        split = profile.get("gold_split", {}).get("gold-half")
        if split:
            print(
                f"  gold 分组: 被提升目标 {split['gold_targets']}，普通目标 {split['plain_targets']}"
            )
    if report["unresolved"]:
        print(f"\n未解析的改写: {', '.join(report['unresolved'])}")
    print("")
    for caveat in report["caveats"]:
        print(f"- {caveat}")


def _metric(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _cmd_experience_counterexamples(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    store = ExperienceStore(settings.experience_path)
    status = None if args.status == "all" else args.status
    rows = store.list_counterexamples(status=status, limit=args.limit)
    stats = store.counterexample_stats()
    if args.as_json:
        print(json.dumps({"items": rows, **stats}, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"反例队列为空（status={args.status}）。")
        return 0
    print(
        f"反例队列 {args.status} 共 {len(rows)} 条 | 总计 "
        f"{stats['counterexamples_total']} 条（open {stats['counterexamples_open']} / "
        f"resolved {stats['counterexamples_resolved']}），累计发生 "
        f"{stats['counterexample_occurrences']} 次 | 按错误码 "
        f"{json.dumps(stats['counterexamples_by_code'], ensure_ascii=False)}\n"
    )
    for row in rows:
        print(
            f"#{row['id']} [{row['status']}] x{row['occur_count']} "
            f"({row['category'] or '-'}) {row['question']}"
        )
        print(f"    error_code={row['error_code'] or '-'} reason={row['reason']}")
        print(f"    SQL: {str(row['sql'])[:160]}")
    print(
        "\n这些行只用于人工分诊与入档闸门：save_experience 会拒绝其中的 SQL，"
        "提示词构建器从不读这张表。"
    )
    return 0


def _sql_validator(runtime: Runtime):
    """Guard + real read-only execution against the demo DB."""

    def validator(sql: str) -> tuple[bool, str]:
        check = runtime.guard.check(sql)
        if not check.valid:
            return False, "; ".join(issue.message for issue in check.errors)
        execution = run_readonly_query(
            runtime.settings.target,
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


def _cmd_release_snapshot(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    manifest = create_release(
        runtime.settings,
        prompt_version=AGENT_PROMPT_VERSION,
        prompt_text=runtime.agent._system_prompt(""),
        schema_hash=runtime.agent.schema_hash,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _cmd_release_list(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    rows = list_releases(settings)
    if args.as_json:
        print(json.dumps({"releases": rows}, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("还没有 release。先运行 nl2sql-agent release snapshot。")
        return 0
    for row in rows:
        prompt = row.get("prompt") or {}
        schema = row.get("schema") or {}
        experience = row.get("experience") or {}
        stats = experience.get("stats") or {}
        print(
            f"{row.get('release_id')}  {row.get('created_at')}  "
            f"prompt={prompt.get('version') or '-'}  schema={schema.get('schema_hash') or '-'}  "
            f"experience={stats.get('total', 0) if experience.get('present') else '无'}"
        )
    return 0


def _cmd_release_show(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    try:
        manifest = load_release(settings, args.release_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _cmd_release_rollback(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "回滚会覆盖当前 schema 与经验库（覆盖前会另存到 var/release_backups/）；"
            "确认后加 --yes。",
            file=sys.stderr,
        )
        return 2
    runtime = _runtime(args)
    try:
        result = rollback_release(
            runtime.settings,
            args.release_id,
            prompt_version=AGENT_PROMPT_VERSION,
            prompt_text=runtime.agent._system_prompt(""),
            restore_schema=not args.skip_schema,
            restore_experience=not args.skip_experience,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["prompt"]["restored"]:
        print(
            f"文件已回滚，但 prompt 仍是 {result['prompt']['current_version']}；"
            f"要回到 {result['prompt']['target_version']} 需要 checkout 对应代码版本并重启。",
            file=sys.stderr,
        )
        return 3
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


def _cmd_experience_feedback(args: argparse.Namespace) -> int:
    settings = Settings.load(home=args.home)
    store = ExperienceStore(settings.experience_path)
    status = None if args.status == "all" else args.status
    rows = store.list_feedback(status=status, limit=max(1, args.limit))
    stats = store.feedback_stats()
    if args.as_json:
        print(json.dumps({"items": rows, **stats}, ensure_ascii=False, indent=2))
        return 0
    print(f"反馈 {args.status} 共 {len(rows)} 条 | {json.dumps(stats, ensure_ascii=False)}\n")
    for row in rows:
        label = FEEDBACK_REASONS.get(str(row["reason"]), str(row["reason"]))
        print(
            f"#{row['id']} [{row['status']}] {label} ×{row['occur_count']} "
            f"· {row['category'] or 'uncategorized'} · {row['question']}"
        )
        print(
            f"   session={row['session_id'] or '-'} turn={row['turn_index']} "
            f"trace={row['trace_id'] or '-'} schema={str(row['schema_hash'])[:8] or '-'}"
        )
    if rows:
        print(
            "\n通过：nl2sql-agent experience review <id> --approve --sql \"...\" --note \"口径说明\""
            "\n拒绝：nl2sql-agent experience review <id> --reject --note \"为什么不是问题\""
        )
    return 0


def _cmd_experience_review(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    action = "approve" if args.approve else "reject"
    try:
        result = runtime.store.review_feedback(
            args.id,
            action,
            sql=args.sql,
            note=args.note,
            validator=_sql_validator(runtime) if action == "approve" else None,
            reviewer=args.reviewer,
        )
    except KeyError as exc:
        print(f"审核失败: {exc}")
        return 1
    except ValueError as exc:
        print(f"审核未通过: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if action == "approve":
        print("已通过：修正 SQL 通过护栏且执行成功，并写为 verified 经验。")
    else:
        print("已拒绝：反馈与本次产生的失败/反例已关闭，不会写入经验库。")
    return 0


def _cmd_experience_report(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    report = build_weekly_report(
        runtime.store,
        active_schema_hash=runtime.agent.schema_hash,
        days=args.days,
        log_path=Path(args.log) if args.log else runtime.settings.log_path,
        show_text=args.show_text,
    )
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_weekly_report(report)
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
        if result.trace_id:
            print(f"trace: {result.trace_id}")
    second = next((item for item in result.channels if item.get("role") == "second"), None)
    if second:
        print(
            f"双通道: source={second.get('source')} status={second.get('status')} "
            f"agreement={second.get('agreement')}"
        )
    if result.error and result.status not in {"ok", "needs_clarification"}:
        print(f"error: {result.error}")


if __name__ == "__main__":
    raise SystemExit(main())
