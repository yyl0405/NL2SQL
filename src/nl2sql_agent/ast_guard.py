"""Optional AST second opinion for the SQL guard, built on ``sqlglot``.

The core guard is a regex machine on purpose: zero dependencies, and it reads a
masked statement, so it never mistakes a string literal for SQL. That same
property is its blind spot — anything the tokenizer mis-reads escapes validation
entirely. These are measured, not hypothetical (``guard-check`` reproduces them):

* ``SELECT created_at FROM[internal_audit]`` — the ``FROM`` pattern requires
  whitespace, so a *disabled* table referenced without a space passes a
  regex-only guard;
* ``SELECT randomblob(100000000)`` / ``SELECT xp_cmdshell('dir')`` — resource
  and escalation functions are invisible to the keyword blocklist, which only
  knows ``EXEC``;
* ``SELECT order_id INTO #tmp FROM sales_order`` — a write that never contains
  the word INSERT;
* ``sqlite_master`` / ``information_schema.tables`` — caught, but as
  ``unknown_table``: the right answer is "you may not introspect the catalog".

This module parses those statements into an AST and reports structure, not
opinions: statement count, top-level kinds, write nodes, real table
identifiers, projection stars and dangerous function calls. ``SQLGuard`` turns
that into issues.

Failure is closed, deliberately. When the AST layer is enabled, a missing
``sqlglot`` install (``ast_unavailable``) or a statement that does not parse
(``ast_parse_failed``) is an *error*, not a downgrade to regex-only approval.
"Enable the AST guard" has to mean "unparseable SQL does not run", otherwise the
switch is security theatre: the cheapest way to bypass a guard is to write
something the guard cannot read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .dialects import normalize

_WRITE_KEYS = {
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "merge", "copy", "replace", "grant", "revoke", "attach", "detach",
    "pragma", "vacuum", "set", "call", "execute", "exec", "command", "into",
}
_QUERY_KEYS = {"select", "union", "intersect", "except", "subquery", "values"}
_DANGEROUS_FUNCTIONS = {
    "sqlite": {
        "load_extension", "randomblob", "zeroblob", "sleep", "benchmark",
        "readfile", "writefile", "fts3_tokenizer",
    },
    "tsql": {
        "xp_cmdshell", "sp_configure", "sp_oacreate", "sp_oamethod",
        "sp_addextendedproc", "sp_makewebtask", "openrowset", "opendatasource",
        "openquery", "shutdown", "bcp", "xp_regread", "xp_regwrite",
        "sp_password", "sp_addsrvrolemember", "sp_enumoledbprops",
    },
}
_METADATA_SCHEMAS = {"information_schema", "sys", "msysobjects", "sqlite_master", "pg_catalog"}
_PREFIXED_DANGEROUS = re.compile(r"^(xp_|sp_(oacreate|oamethod|addextendedproc|makewebtask|addsrvrolemember))", re.I)
# One flat set on purpose: load_extension() reaching a SQL Server connection and
# xp_cmdshell() reaching a SQLite build are both escalation, so a caller never
# gets a narrower list than the dialect it asked for.
_ALL_DANGEROUS = {name.lower() for group in _DANGEROUS_FUNCTIONS.values() for name in group}


@dataclass(frozen=True)
class AstTable:
    """One table identifier as the parser saw it, quoting and prefix included."""

    name: str
    db: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.db}.{self.name}" if self.db else self.name


@dataclass
class AstReport:
    """Structure the parser found. Empty on purpose when parsing failed."""

    status: str = "ok"  # ok / unavailable / parse_failed
    dialect: str = ""
    error: str = ""
    statements: int = 0
    kinds: list[str] = field(default_factory=list)
    write_nodes: list[str] = field(default_factory=list)
    tables: list[AstTable] = field(default_factory=list)
    ctes: list[str] = field(default_factory=list)
    star_projections: list[str] = field(default_factory=list)
    dangerous: list[str] = field(default_factory=list)
    metadata_access: list[str] = field(default_factory=list)
    parse_ms: float = 0.0

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables if t.name]

    @property
    def real_tables(self) -> list[AstTable]:
        """Tables that are not CTE aliases and have a real name."""
        ctes = {name.lower() for name in self.ctes}
        return [t for t in self.tables if t.name and t.name.lower() not in ctes]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dialect": self.dialect,
            "error": self.error,
            "statements": self.statements,
            "kinds": list(self.kinds),
            "write_nodes": list(self.write_nodes),
            "tables": [t.qualified for t in self.tables],
            "ctes": list(self.ctes),
            "star_projections": list(self.star_projections),
            "dangerous": list(self.dangerous),
            "metadata_access": list(self.metadata_access),
        }


def ast_available() -> bool:
    try:
        import sqlglot  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means the layer cannot run
        return False
    return True


def map_dialect(dialect: str) -> str:
    # Aliases come from the shared table so the parser never reads a different dialect
    # than the guard and the prompt do. Anything unregistered is passed through: sqlglot
    # knows engines this project has no backend for, and guessing "sqlite" there would
    # silently parse the wrong grammar.
    name = str(dialect or "").strip().lower()
    return normalize(name) or name or "sqlite"


class AstAnalyzer:
    """Parse one statement and describe it. Never raises; failures are in the report."""

    def __init__(self, dialect: str = "sqlite", max_sql_chars: int = 20000) -> None:
        self.dialect = map_dialect(dialect)
        self.max_sql_chars = int(max_sql_chars)

    def analyze(self, sql: str) -> AstReport:
        report = AstReport(dialect=self.dialect)
        text = (sql or "").strip().rstrip(";").strip()
        if not text or len(text) > self.max_sql_chars:
            report.status = "parse_failed"
            report.error = "SQL 为空或超出 AST 解析长度上限"
            return report
        try:
            import sqlglot
            from sqlglot import exp
        except Exception as exc:  # noqa: BLE001 - missing optional dependency
            report.status = "unavailable"
            report.error = str(exc).strip() or "sqlglot 未安装"
            return report

        try:
            trees = [tree for tree in sqlglot.parse(text, read=self.dialect) if tree is not None]
        except Exception as exc:  # noqa: BLE001 - ParseError/TokenError/RecursionError
            report.status = "parse_failed"
            report.error = f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:200]}"
            return report

        report.statements = len(trees)
        if not trees:
            report.status = "parse_failed"
            report.error = "解析结果为空"
            return report

        write_keys: set[str] = set()
        for tree in trees:
            report.kinds.append(tree.key)
            for node in tree.walk():
                key = getattr(node, "key", "")
                if key in _WRITE_KEYS:
                    write_keys.add(key)
                elif isinstance(node, exp.CTE):
                    alias = str(getattr(node, "alias", "") or "")
                    if alias:
                        report.ctes.append(alias)
                elif isinstance(node, exp.Table):
                    name = str(getattr(node, "name", "") or "").strip()
                    db = str(getattr(node, "db", "") or "").strip()
                    if name or db:
                        report.tables.append(AstTable(name=name, db=db))
                        if db.lower() in _METADATA_SCHEMAS or name.lower() in _METADATA_SCHEMAS:
                            report.metadata_access.append(f"{db}.{name}" if db else name)
                elif isinstance(node, (exp.Anonymous, exp.Func)):
                    self._collect_function(node, exp, report)
            for select in tree.find_all(exp.Select):
                report.star_projections.extend(_star_projections(select, exp))

        report.write_nodes = sorted(write_keys)
        report.dangerous = sorted(set(report.dangerous))
        report.metadata_access = sorted(set(report.metadata_access))
        report.star_projections = sorted(set(report.star_projections))
        return report

    def _collect_function(self, node: Any, exp: Any, report: AstReport) -> None:
        if isinstance(node, exp.Anonymous):
            name = str(node.this or "")
        else:
            match = re.match(r"^([A-Za-z_][\w\.]*)\s*\(", node.sql() if hasattr(node, "sql") else "")
            name = match.group(1) if match else ""
        if not name:
            return
        bare = name.split(".")[-1].lower()
        if bare in _ALL_DANGEROUS or _PREFIXED_DANGEROUS.match(bare):
            if all(bare != existing.split(".")[-1].rstrip("(").lower() for existing in report.dangerous):
                report.dangerous.append(f"{name}(")

    def issues(
        self,
        report: AstReport,
        *,
        known_tables: Sequence[str] = (),
        regex_star: bool = False,
    ) -> list[tuple[str, str, str]]:
        """Translate a report into ``(level, code, message)`` triples.

        Kept free of ``GuardIssue`` so the module stays importable without
        sqlglot and the guard owns issue construction.
        """
        if report.status == "unavailable":
            return [
                (
                    "error",
                    "ast_unavailable",
                    "AST 护栏已启用但 sqlglot 不可用，按 fail-closed 拒绝执行："
                    f"{report.error}（pip install -e \".[ast]\" 或设 NL2SQL_AST_GUARD=off）",
                )
            ]
        if report.status == "parse_failed":
            return [
                (
                    "error",
                    "ast_parse_failed",
                    f"AST 护栏无法解析该语句，按 fail-closed 拒绝执行：{report.error}",
                )
            ]

        issues: list[tuple[str, str, str]] = []
        if report.statements > 1:
            issues.append(
                ("error", "multi_statement", f"AST 解析出 {report.statements} 条语句，只允许单条 SELECT")
            )
        bad_kinds = sorted({kind for kind in report.kinds if kind not in _QUERY_KEYS})
        if bad_kinds:
            issues.append(
                (
                    "error",
                    "not_select",
                    f"AST 判定顶层语句类型不是查询：{', '.join(bad_kinds)}；只允许 SELECT/WITH 查询",
                )
            )
        if report.write_nodes:
            issues.append(
                (
                    "error",
                    "write_keyword",
                    "AST 在语句树里发现写操作/DDL 节点："
                    f"{', '.join(report.write_nodes)}（可能藏在 EXEC 或字符串里）",
                )
            )
        if report.dangerous:
            issues.append(
                (
                    "error",
                    "dangerous_function",
                    f"检测到危险函数调用：{', '.join(report.dangerous)}；只读分析查询不允许调用扩展、"
                    "阻塞或外部数据源函数",
                )
            )
        if report.metadata_access:
            issues.append(
                (
                    "error",
                    "metadata_access",
                    f"检测到数据库元数据访问：{', '.join(report.metadata_access)}；"
                    "schema 信息只能来自目录，不能靠查询系统表绕过目录与权限",
                )
            )
        if report.star_projections and not regex_star:
            issues.append(
                (
                    "error",
                    "select_star",
                    f"AST 发现投影了全部列：{', '.join(report.star_projections)}（正则层未识别的写法，"
                    "例如括号或别名限定的 .*）",
                )
            )
        known = {name.lower() for name in known_tables}
        hidden = [
            t.qualified
            for t in report.real_tables
            if t.qualified.lower() not in known and t.name.lower() not in known
        ]
        if hidden:
            issues.append(
                (
                    "warning",
                    "ast_table_bypass",
                    f"AST 发现正则层没有登记的表引用：{', '.join(hidden)}；"
                    "这些表已按目录校验，但说明 SQL 写法绕过了常规的 FROM/JOIN 识别",
                )
            )
        return issues


def _star_projections(select: Any, exp: Any) -> list[str]:
    found: list[str] = []
    for projection in select.selects:
        if isinstance(projection, exp.Star):
            found.append("*")
        elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            found.append(f"{projection.table}.*")
    return found
