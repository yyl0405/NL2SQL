"""Read-only SQL guard: permissions, SELECT *, required filters, columns and safety."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from .ast_guard import AstAnalyzer
from .catalog import Catalog
from .dialects import normalize
from .fanout import analyze_joins
from .principal import Principal

_BLOCKED_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|attach|detach|pragma|vacuum|"
    r"grant|revoke|exec|execute|merge|load_extension)\b",
    re.IGNORECASE,
)
_LEADING_KEYWORD = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
# SQL Server identifiers may be Unicode - a legacy ERP copy can literally be named
# 客户表 - and ``mask_sql`` removes the ``[ ]`` that quote it. An ASCII-only
# identifier start made every such name invisible to extraction, so the guard
# answered "no tables" instead of denying a disabled or unknown table: naming a
# table in Chinese was enough to walk past the whitelist and the column checks.
# ``[^\W\d]`` is the Unicode letter/underscore start, so ``2024.1383`` stays a
# number and not a qualified column.
_IDENT = r"[^\W\d]\w*"
_FROM_OR_JOIN = re.compile(
    rf"\b(?:from|join)\s+(?!\()\s*(?:{_IDENT}\s*\.\s*)?({_IDENT})",
    re.IGNORECASE,
)
_FROM_CLAUSE = re.compile(
    r"\bfrom\b(.*?)(?=\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|"
    r"\bunion\b|\bwindow\b|\bqualify\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_CTE = re.compile(
    rf"(?:\bwith|,)\s+(?:recursive\s+)?({_IDENT})\s*(?:\([^)]*\))?\s+as\s*\(",
    re.IGNORECASE,
)
_AGGREGATE_STAR = re.compile(
    r"(?is)\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(?:DISTINCT\s+)?\*\s*\)"
)
_SELECT_BARE_STAR = re.compile(
    r"(?is)\bselect\b\s+(?:top\s*(?:\(\s*\d+\s*\)|\d+)\s*(?:percent\s+)?)?"
    r"(?:distinct\s+)?\*"
)
_QUALIFIED_STAR = re.compile(
    r"(?is)(?:\bselect\b\s+(?:top\s*(?:\(\s*\d+\s*\)|\d+)\s*(?:percent\s+)?)?"
    rf"(?:distinct\s+)?|,)\s*({_IDENT})\s*\.\s*\*"
)
_TABLE_HINT = re.compile(r"\bwith\s*\(([^()]*)\)", re.IGNORECASE)
_ALLOWED_TABLE_HINTS = {"NOLOCK"}
_OFFSET_FETCH = re.compile(r"\boffset\s+\d+\s+rows?\b", re.IGNORECASE)
# Standard functions that hide a keyword inside their argument list
# (`EXTRACT(YEAR FROM d)`, `TRIM(BOTH 'x' FROM c)`). That FROM is not a table
# reference; reading it as one turned a column name into an unknown table and
# falsely blocked correct PostgreSQL / MySQL gold SQL. The `[^()]` guard keeps
# the blanking inside the same parenthesis group as the function name, so
# `TRIM(c) FROM t` keeps its real FROM.
_FUNCTION_FROM = re.compile(
    r"\b(extract|overlay|position|substring|trim)\b(\s*\([^()]*?)\b(from|in)\b",
    re.IGNORECASE,
)

_KEYWORDS = {
    "select", "from", "where", "group", "by", "order", "having", "limit", "offset",
    "join", "left", "right", "inner", "outer", "full", "cross", "on", "as", "and",
    "or", "not", "in", "is", "null", "like", "between", "case", "when", "then",
    "else", "end", "union", "all", "distinct", "asc", "desc", "with", "recursive",
    "using", "window", "partition", "over", "qualify", "lateral", "exists", "any",
    "some", "into", "values", "set", "top", "percent",
}

# Dialect-aware shape rules (P2-2). The regex layer reads blanked tokens, so
# `SELECT TOP 3 …` and `… LIMIT 3` used to be the same string to it: measured on
# the dialect corpus, 22/22 foreign-dialect shapes passed. Every rule below is a
# shape that belongs to *another* dialect, so it only fires when the catalog
# declares the dialect the SQL is being sent to. Unknown dialects get no rules —
# a guess here would block correct SQL, which is the failure mode this whole
# layer is supposed to avoid.
#
# Spelling normalisation is shared with the executor and the prompts (``dialects.normalize``):
# the guard must not recognise a dialect name that the prompt then describes as something else.
_canonical_dialect = normalize


@dataclass(frozen=True)
class _ShapeRule:
    code: str
    pattern: re.Pattern
    message: str
    # Which projection of the SQL the pattern is matched against:
    #   "masked"   — literals blanked, identifier quotes stripped (default view)
    #   "quoted"   — literals blanked, identifier quotes kept: quoting-style rules
    #   "literal"  — comments stripped, literals kept: concat-intent rules
    text: str = "masked"


def _shape(code: str, pattern: str, message: str, *, text: str = "masked") -> _ShapeRule:
    return _ShapeRule(code, re.compile(pattern, re.IGNORECASE), message, text)


_TOP = r"\bselect\s+(?:distinct\s+)?top\b"
_FETCH_ROWS = r"\bfetch\s+(?:first|next)\s+\d+\s+rows?\b"
_POSTGRES_CAST = r"::"
_BRACKET_QUOTE = r"\[[A-Za-z_]\w*\]"
_BACKTICK_QUOTE = r"`[A-Za-z_]\w*`"
_DOUBLE_QUOTE_REF = r'\bfrom\s+"|\bas\s+"|"[A-Za-z_]\w*"\s*\.\s*"'
_ILIKE = r"\bilike\b"
# `+` / `||` next to a string literal is concatenation intent in a dialect where
# that operator is arithmetic or boolean logic: the engine returns a number and no
# error. Arithmetic on two unquoted identifiers is left alone.
_PLUS_STRING = r"'\s*[^']*'\s*\+|\+\s*'[^']*'"
_OR_STRING = r"'\s*[^']*'\s*\|\||\|\|\s*'[^']*'"

_SHAPE_RULES: dict[str, tuple[_ShapeRule, ...]] = {
    "sqlite": (
        _shape("dialect_top", _TOP, "SQLite 不支持 TOP n 取前 N 行，请改用 LIMIT n"),
        _shape("dialect_fetch_rows", _FETCH_ROWS, "SQLite 不支持 FETCH FIRST/NEXT … ROWS ONLY，请改用 LIMIT n OFFSET m"),
        _shape("dialect_postgres_cast", _POSTGRES_CAST, "SQLite 不支持 :: 后置类型转换，请改用 CAST(表达式 AS 类型)"),
        _shape("dialect_ilike", _ILIKE, "SQLite 没有 ILIKE，大小写不敏感匹配请改用 LIKE（SQLite 的 LIKE 对 ASCII 本来就不区分大小写）或 LOWER()"),
        _shape("dialect_bracket_quote", _BRACKET_QUOTE, "SQLite 不支持 [ ] 标识符引用，请改用双引号或裸标识符", text="quoted"),
        _shape(
            "dialect_foreign_function",
            r"\b(isnull|nvl|getdate|datepart|date_format|to_char|convert|extract|trunc|sysdate)\s*\(",
            "SQLite 没有该日期/类型函数（其他方言写法），请改用 strftime() / CAST() / COALESCE()",
        ),
    ),
    "tsql": (
        _shape("dialect_limit", r"\blimit\b", "SQL Server 不支持 LIMIT，请改用 TOP n 或 OFFSET … FETCH NEXT"),
        _shape("dialect_postgres_cast", _POSTGRES_CAST, "SQL Server 不支持 :: 后置类型转换，请改用 CAST(表达式 AS 类型)"),
        _shape("dialect_backtick_quote", _BACKTICK_QUOTE, "SQL Server 不支持反引号标识符引用，请改用 [ ] 或裸标识符", text="quoted"),
        _shape("dialect_ilike", _ILIKE, "SQL Server 没有 ILIKE，大小写不敏感匹配请改用 LOWER()/UPPER() 或改库的 collation"),
        _shape(
            "dialect_foreign_function",
            r"\b(strftime|to_char|date_format|ifnull|nvl|listagg|substring_index)\s*\(",
            "SQL Server 没有该函数（其他方言写法），请改用 CONVERT()/FORMAT()、ISNULL() 或 FOR JSON PATH",
        ),
        _shape("dialect_concat_operator", r"\|\|", "SQL Server 不支持 || 字符串拼接，请改用 + 或 CONCAT()"),
    ),
    "postgres": (
        _shape("dialect_top", _TOP, "PostgreSQL 不支持 TOP n 取前 N 行，请改用 LIMIT n 或 FETCH FIRST n ROWS ONLY"),
        _shape("dialect_bracket_quote", _BRACKET_QUOTE, "PostgreSQL 不支持 [ ] 标识符引用，请改用双引号或裸标识符", text="quoted"),
        _shape("dialect_backtick_quote", _BACKTICK_QUOTE, "PostgreSQL 不支持反引号标识符引用，请改用双引号或裸标识符", text="quoted"),
        _shape(
            "dialect_foreign_function",
            r"\b(isnull|ifnull|getdate|datepart|date_format|strftime|convert|nvl|isnumeric|listagg|year|month|day|charindex)\s*\(",
            "PostgreSQL 没有该函数（其他方言写法），请改用 COALESCE()、EXTRACT()、TO_CHAR()、SUBSTRING() 或 CAST()/::",
        ),
        _shape("dialect_concat_operator", _PLUS_STRING, "PostgreSQL 的 + 不做字符串拼接（会报类型错误），请改用 || 或 CONCAT()", text="literal"),
    ),
    "mysql": (
        _shape("dialect_top", _TOP, "MySQL 不支持 TOP n 取前 N 行，请改用 LIMIT n"),
        _shape("dialect_fetch_rows", _FETCH_ROWS, "MySQL 不支持 FETCH FIRST/NEXT … ROWS ONLY，请改用 LIMIT n OFFSET m"),
        _shape("dialect_postgres_cast", _POSTGRES_CAST, "MySQL 不支持 :: 后置类型转换，请改用 CAST(表达式 AS 类型)"),
        _shape("dialect_bracket_quote", _BRACKET_QUOTE, "MySQL 不支持 [ ] 标识符引用，请改用反引号或裸标识符", text="quoted"),
        _shape("dialect_double_quote_ref", _DOUBLE_QUOTE_REF, "MySQL 默认把双引号当字符串（除非 ANSI_QUOTES），标识符请改用反引号", text="quoted"),
        _shape("dialect_ilike", _ILIKE, "MySQL 没有 ILIKE，大小写不敏感匹配请直接用 =（默认 collation 不区分大小写）或 LOWER()"),
        _shape(
            "dialect_foreign_function",
            r"\b(strftime|to_char|getdate|datepart|isnumeric|listagg|nvl)\s*\(",
            "MySQL 没有该函数（其他方言写法），请改用 DATE_FORMAT()、CONVERT() 或 GROUP_CONCAT()",
        ),
        _shape("dialect_concat_operator", _OR_STRING, "MySQL 默认把 || 当逻辑或（除非 PIPES_AS_CONCAT），字符串拼接请改用 CONCAT()", text="literal"),
        _shape("dialect_concat_operator_plus", _PLUS_STRING, "MySQL 的 + 只做算术，字符串拼接请改用 CONCAT()", text="literal"),
    ),
}



@dataclass
class GuardIssue:
    level: str
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass
class GuardResult:
    valid: bool
    sql: str
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    issues: list[GuardIssue] = field(default_factory=list)
    ast: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[GuardIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[GuardIssue]:
        return [i for i in self.issues if i.level == "warning"]

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "valid": self.valid,
            "sql": self.sql,
            "tables": self.tables,
            "columns": self.columns,
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings],
        }
        if self.ast:
            payload["ast"] = self.ast
        return payload


class SQLGuard:
    """Validates generated SQL before it reaches a read-only connection.

    The guard is intentionally conservative. It is not a full SQL parser; it
    blocks the failure modes that caused production incidents: writing data,
    querying disabled tables, SELECT *, disabled/sensitive fields, missing
    dimension filters and mistyped qualified columns.

    Two optional layers sit on top of the regex rules:

    * ``principal`` — row-level and column-level access control. Unauthorized
      references are reported as ``permission_table`` / ``permission_column``
      rather than as tables and columns that "do not exist", and every
      ``RowFilter`` must appear in the SQL itself (``row_filter_missing``).
    * ``analyzer`` — an AST second opinion (:mod:`nl2sql_agent.ast_guard`).
      It widens what counts as a table reference and blocks structures the
      tokenizer cannot see: function-based escalation, catalog introspection,
      stacked statements. It fails closed: unavailable or unparseable is an
      error, never a silent downgrade to regex-only approval.
    """

    def __init__(
        self,
        catalog: Catalog,
        allow_sensitive: bool = False,
        principal: Principal | None = None,
        analyzer: Any = None,
    ) -> None:
        self.catalog = catalog
        self.allow_sensitive = allow_sensitive
        self.principal = principal
        self.analyzer = analyzer

    def check(self, sql: str) -> GuardResult:
        cleaned = self._clean_input(sql)
        issues: list[GuardIssue] = []
        used_tables: list[str] = []
        used_columns: list[str] = []

        if not cleaned:
            return GuardResult(False, cleaned, issues=[GuardIssue("error", "empty_sql", "SQL 不能为空")])
        if len(cleaned) > 20000:
            return GuardResult(False, cleaned, issues=[GuardIssue("error", "sql_too_long", "SQL 超过 20000 字符")])

        masked = mask_sql(cleaned)
        masked_no_semicolon = masked.rstrip().rstrip(";").rstrip()

        if not _LEADING_KEYWORD.match(masked):
            issues.append(GuardIssue("error", "not_select", "只允许 SELECT 或 WITH 查询"))
        if ";" in masked_no_semicolon:
            issues.append(GuardIssue("error", "multi_statement", "只允许单条语句，禁止分号拼接"))
        blocked = sorted({m.group(1).lower() for m in _BLOCKED_KEYWORDS.finditer(masked)})
        if blocked:
            issues.append(
                GuardIssue("error", "write_keyword", f"检测到只读查询不允许的关键字: {', '.join(blocked)}")
            )

        dialect = _canonical_dialect(self.catalog.dialect)
        dialect_label = (self.catalog.dialect or "sqlite").strip().lower()
        is_mssql = dialect == "tsql"
        hints = _table_hints(masked)
        if hints:
            if is_mssql:
                rejected = sorted(h for h in hints if h not in _ALLOWED_TABLE_HINTS)
                if rejected:
                    issues.append(
                        GuardIssue(
                            "error",
                            "table_hint",
                            f"只允许 WITH (NOLOCK) 表提示，检测到: {', '.join(rejected)}",
                        )
                    )
            else:
                issues.append(
                    GuardIssue(
                        "error",
                        "table_hint",
                        f"{dialect_label} 不支持表提示: {', '.join(sorted(hints))}",
                    )
                )
        if is_mssql and _OFFSET_FETCH.search(masked):
            issues.append(
                GuardIssue(
                    "error",
                    "offset_fetch",
                    "OFFSET/FETCH 需要 SQL Server 2012+，本项目的 2008 R2 库不支持；请改用 TOP n 或 ROW_NUMBER() 分页",
                )
            )

        issues.extend(self._dialect_shape_issues(cleaned, masked))

        ctes = _extract_ctes(masked)
        table_tokens = _extract_tables(masked, ctes)
        ast_report = None
        if self.analyzer is not None:
            ast_report = self.analyzer.analyze(cleaned)
            if ast_report.usable:
                # A table the tokenizer never saw still has to be catalogued:
                # disabled and permission checks must apply to every reference,
                # not only to the ones written as `FROM plain_name`.
                regex_tokens = set(table_tokens)
                for token in ast_report.table_names:
                    key = token.lower()
                    if key not in ctes and key not in regex_tokens:
                        table_tokens.add(key)
        table_objects = []
        for token in sorted(table_tokens):
            table = self.catalog.table(token)
            if table is None:
                issues.append(
                    GuardIssue("error", "unknown_table", f"表 '{token}' 不在 schema 目录中")
                )
                continue
            if table.disabled:
                issues.append(
                    GuardIssue("error", "disabled_table", f"表 '{table.name}' 已被禁用，不能查询")
                )
                continue
            if self.principal is not None and not self.principal.visible(table.name):
                issues.append(
                    GuardIssue(
                        "error",
                        "permission_table",
                        f"身份 {self.principal.id} 无权查询表 '{table.name}'",
                    )
                )
                continue
            table_objects.append(table)
            used_tables.append(table.name)
            for required in table.required_filters:
                column = str(required.get("column", ""))
                value = str(required.get("value", ""))
                pattern = rf"\b{re.escape(column)}\s*=\s*['\"]?{re.escape(value)}['\"]?"
                if not re.search(pattern, cleaned, re.IGNORECASE):
                    reason = str(required.get("reason", ""))
                    issues.append(
                        GuardIssue(
                            "error",
                            "required_filter",
                            f"查询 {table.name} 必须带上 {column} = '{value}'"
                            + (f"（{reason}）" if reason else ""),
                        )
                    )
            if self.principal is not None:
                for row_filter in self.principal.filters_for(table.name):
                    if not row_filter.satisfied_by(cleaned):
                        issues.append(
                            GuardIssue(
                                "error",
                                "row_filter_missing",
                                f"身份 {self.principal.id} 查询 {table.name} 必须自带范围条件 "
                                f"{row_filter.required_predicate}",
                            )
                        )
            # A default filter is a house convention, not an authority boundary: it
            # reaches the model with the table schema and comes back as a warning, so
            # "包含已删除的合同" stays answerable. `required_filters` above stays the
            # hard layer for dictionaries where a missing filter mixes data.
            for default in table.default_filters:
                column = str(default.get("column", ""))
                value = str(default.get("value", ""))
                if not column:
                    continue
                pattern = (
                    rf"\b{re.escape(column)}\s*(?:=\s*['\"]?{re.escape(value)}['\"]?"
                    r"|\s*(?:>=|<=|<>|!=|>|<)\s*|IN\s*\(|IS\s+)"
                )
                if re.search(pattern, cleaned, re.IGNORECASE):
                    continue
                reason = str(default.get("reason", ""))
                issues.append(
                    GuardIssue(
                        "warning",
                        "default_filter_missing",
                        f"查询 {table.name} 通常应带上 {column} = '{value}'"
                        + (f"（{reason}）" if reason else "")
                        + "；问题明确要包含全部行时可忽略",
                    )
                )

        if sql_has_select_star(masked):
            issues.append(
                GuardIssue(
                    "error",
                    "select_star",
                    "禁止 SELECT *，必须显式列出最小必要字段",
                )
            )

        alias_map = _extract_aliases(masked)
        old_columns = set(used_columns)
        for prefix, column_name in _qualified_columns(masked):
            key = prefix.lower()
            if key in {"main", "temp", "sqlite_master"}:
                continue
            table_name = alias_map.get(key)
            if not table_name or table_name.lower() in ctes:
                continue
            table = self.catalog.table(table_name)
            if table is None:
                continue
            column = table.column(column_name)
            if column is None:
                candidates = [
                    c.name
                    for c in table.columns
                    if not c.disabled
                    and (
                        self.principal is None
                        or self.principal.column_visible(table.name, c.name)
                    )
                ]
                close = difflib.get_close_matches(column_name, candidates, n=3, cutoff=0.55)
                hint = f"；相近字段: {', '.join(close)}" if close else ""
                issues.append(
                    GuardIssue(
                        "error",
                        "unknown_column",
                        f"表 {table.name} 中不存在字段 {column_name}{hint}",
                    )
                )
                continue
            if column.disabled:
                issues.append(
                    GuardIssue("error", "disabled_column", f"字段 {table.name}.{column.name} 已被禁用")
                )
                continue
            if column.sensitive and not self.allow_sensitive:
                issues.append(
                    GuardIssue(
                        "error",
                        "sensitive_column",
                        f"字段 {table.name}.{column.name} 属于敏感字段，默认禁止查询",
                    )
                )
                continue
            if self.principal is not None and not self.principal.column_visible(
                table.name, column.name
            ):
                issues.append(
                    GuardIssue(
                        "error",
                        "permission_column",
                        f"身份 {self.principal.id} 无权查询字段 {table.name}.{column.name}",
                    )
                )
                continue
            old_columns.add(f"{table.name}.{column.name}")

        for table in table_objects:
            for column in table.columns:
                if column.disabled:
                    if re.search(
                        rf"(?<![\w]){re.escape(column.name)}(?![\w])", masked, re.IGNORECASE
                    ):
                        issues.append(
                            GuardIssue(
                                "error", "disabled_column", f"字段 {table.name}.{column.name} 已被禁用"
                            )
                        )
                elif column.sensitive and not self.allow_sensitive:
                    if re.search(
                        rf"(?<![\w]){re.escape(column.name)}(?![\w])", masked, re.IGNORECASE
                    ):
                        issues.append(
                            GuardIssue(
                                "error",
                                "sensitive_column",
                                f"字段 {table.name}.{column.name} 属于敏感字段，默认禁止查询",
                            )
                        )
                elif self.principal is not None and not self.principal.column_visible(
                    table.name, column.name
                ):
                    # Same unqualified-reference sweep the disabled/sensitive rules
                    # already make: `SELECT sales_rep FROM ...` against one table in
                    # scope never becomes a qualified column.
                    if re.search(
                        rf"(?<![\w]){re.escape(column.name)}(?![\w])", masked, re.IGNORECASE
                    ):
                        issues.append(
                            GuardIssue(
                                "error",
                                "permission_column",
                                f"身份 {self.principal.id} 无权查询字段 {table.name}.{column.name}",
                            )
                        )

        if len(table_objects) > 5:
            issues.append(
                GuardIssue(
                    "warning",
                    "wide_join",
                    f"本次查询涉及 {len(table_objects)} 张表，注意 JOIN 扇出导致指标重复计算",
                )
            )

        for fanout_issue in analyze_joins(
            self.catalog, masked, alias_map, [table.name for table in table_objects]
        ):
            if not any(
                issue.code == fanout_issue.code and issue.message == fanout_issue.message
                for issue in issues
            ):
                issues.append(fanout_issue)

        ast_payload: dict[str, Any] = {}
        if self.analyzer is not None and ast_report is not None:
            seen = {(issue.level, issue.code) for issue in issues}
            for level, code, message in self.analyzer.issues(
                ast_report,
                known_tables=sorted(_extract_tables(masked, ctes)),
                regex_star=sql_has_select_star(masked),
            ):
                if (level, code) in seen:
                    continue
                issues.append(GuardIssue(level, code, message))
                seen.add((level, code))
            ast_payload = ast_report.to_dict()

        used_columns = sorted(old_columns)
        valid = not any(issue.level == "error" for issue in issues)
        return GuardResult(
            valid=valid,
            sql=cleaned.rstrip().rstrip(";").rstrip(),
            tables=sorted(set(used_tables)),
            columns=used_columns,
            issues=issues,
            ast=ast_payload,
        )

    def _dialect_shape_issues(self, cleaned: str, masked: str) -> list[GuardIssue]:
        rules = _SHAPE_RULES.get(_canonical_dialect(self.catalog.dialect), ())
        if not rules:
            return []
        variants: dict[str, str] = {"masked": masked}
        issues: list[GuardIssue] = []
        for rule in rules:
            text = variants.get(rule.text)
            if text is None:
                text = variants[rule.text] = mask_sql(
                    cleaned,
                    unquote=rule.text != "quoted",
                    blank_literals=rule.text != "literal",
                )
            hit = rule.pattern.search(text)
            if hit:
                snippet = " ".join(hit.group(0).split())
                issues.append(
                    GuardIssue("error", rule.code, f"{rule.message}（检测到的写法: {snippet}）")
                )
        return issues

    @staticmethod
    def _clean_input(sql: str) -> str:
        text = (sql or "").strip()
        fence = re.match(r"^```(?:sql)?\s*(.*?)```$", text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()
        return text


def mask_sql(sql: str, *, unquote: bool = True, blank_literals: bool = True) -> str:
    """Blank string literals and comments while preserving SQL structure.

    ``unquote=False`` keeps the identifier quoting characters in place. Table and
    column extraction wants them gone (``[t]``, ``"t"`` and ``` `t` ``` all name the
    same table), but a dialect shape rule is precisely a statement about the quoting
    style, so it needs the variant that still shows which quotes were written.

    ``blank_literals=False`` keeps string literals (comments are still stripped). The
    concat-intent rules have to see the quotes: ``x + ' / ' + y`` is only distinguishable
    from arithmetic ``a + b`` by the literal next to the operator, and blanking literals
    is what made the regex layer blind to exactly that difference.
    """
    out: list[str] = []
    i = 0
    length = len(sql)
    while i < length:
        ch = sql[i]
        if ch == "'":
            start = i
            i += 1
            while i < length:
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            if blank_literals:
                # Two spaces, as before: the scanner never tried to keep offsets
                # aligned, and table/column extraction only reads word tokens.
                out.append("  ")
            else:
                out.append(sql[start:i])
            continue
        if ch == "-" and i + 1 < length and sql[i + 1] == "-":
            while i < length and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < length and sql[i + 1] == "*":
            i += 2
            while i + 1 < length and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i = min(length, i + 2)
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    masked = "".join(out)
    if unquote:
        masked = re.sub(r"\[([^\]\[]+)\]", r"\1", masked)
        masked = re.sub(r'"([^"]+)"', r"\1", masked)
        masked = re.sub(r"`([^`]*)`", r"\1", masked)
    masked = _FUNCTION_FROM.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{' ' * len(m.group(3))}", masked
    )
    return masked


def _extract_ctes(masked: str) -> set[str]:
    return {m.group(1).lower() for m in _CTE.finditer(masked)}


def _table_hints(masked: str) -> set[str]:
    hints: set[str] = set()
    for match in _TABLE_HINT.finditer(masked):
        for part in match.group(1).split(","):
            hint = part.strip().upper()
            if hint:
                hints.add(hint)
    return hints


def _split_top_level(expr: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(ch)
    if buffer:
        parts.append("".join(buffer))
    return parts


def _extract_tables(masked: str, ctes: set[str]) -> set[str]:
    names: set[str] = set()
    for match in _FROM_OR_JOIN.finditer(masked):
        name = match.group(1).lower()
        if name not in ctes and name not in _KEYWORDS:
            names.add(name)

    from_match = _FROM_CLAUSE.search(masked)
    if from_match:
        for part in _split_top_level(from_match.group(1)):
            cleaned = part.strip()
            if not cleaned or cleaned.startswith("("):
                continue
            match = re.match(rf"(?:{_IDENT}\s*\.\s*)?({_IDENT})", cleaned)
            if not match:
                continue
            name = match.group(1).lower()
            if name not in ctes and name not in _KEYWORDS:
                names.add(name)
    return names


def _extract_aliases(masked: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    pattern = re.compile(
        rf"\b(?:from|join)\s+(?!\()\s*(?:{_IDENT}\s*\.\s*)?({_IDENT})"
        rf"(?:\s+(?:as\s+)?({_IDENT}))?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(masked):
        table = match.group(1).lower()
        alias = (match.group(2) or "").lower()
        aliases.setdefault(table, table)
        if alias and alias not in _KEYWORDS:
            aliases[alias] = table
    return aliases


def _qualified_columns(masked: str) -> list[tuple[str, str]]:
    return [
        (m.group(1), m.group(2))
        for m in re.finditer(rf"\b({_IDENT})\s*\.\s*({_IDENT})\b", masked)
    ]


def sql_has_select_star(masked_sql: str) -> bool:
    text = _AGGREGATE_STAR.sub(" ", masked_sql or "")
    return bool(_SELECT_BARE_STAR.search(text) or _QUALIFIED_STAR.search(text))


def validate_column_reference(catalog: Catalog, table_name: str, column_name: str) -> str | None:
    table = catalog.table(table_name)
    if table is None:
        return None
    column = table.column(column_name)
    return column.name if column else None
