"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Experience recall modes, kept here because this module is an import leaf.
#: `tests/test_recall.py` pins it equal to `recall.RECALL_MODES`.
EXPERIENCE_RECALL_MODES = ("lexical", "vector", "rrf")

#: Execution routes. `pipeline` is the deterministic retrieve -> generate -> guard ->
#: execute path, `agent` is the tool-calling loop, `auto` picks the pipeline only where
#: the loop is known to be expensive (an online provider on a attached, non-demo catalog).
PIPELINE_MODES = ("auto", "pipeline", "agent")


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_optional_float(value: str | None) -> float | None:
    """Missing/blank means "not configured"; a typo raises instead of becoming 0."""
    if value is None or not str(value).strip():
        return None
    return float(str(value).strip())


def _as_words(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """Comma-separated list; empty input keeps the default, explicit blanks drop it."""
    if value is None:
        return default
    items = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    return items


#: Accepted NL2SQL_DB_BACKEND spellings -> the backend actually wired up. The names here
#: are the drivers' own spellings (``psycopg2``, ``SQLServer``), not the dialect names in
#: ``dialects.py``: a backend that exists is not a dialect that can be executed offline.
BACKEND_ALIASES: dict[str, str] = {
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "mssql": "mssql",
    "sqlserver": "mssql",
    "tsql": "mssql",
    "postgres": "postgres",
    "postgresql": "postgres",
    "pg": "postgres",
}


@dataclass
class Settings:
    provider: str = "mock"
    model: str = "qwen-plus"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    home: Path = field(default_factory=lambda: Path.cwd())
    db_path: Path | None = None
    experience_path: Path | None = None
    max_steps: int = 12
    #: Route taken by ``NL2SQLAgent``. See ``PIPELINE_MODES``. ``auto`` is the default
    #: so the shipped offline measurements stay on the tool loop.
    mode: str = "auto"
    max_rows: int = 200
    query_timeout_ms: int = 5000
    max_tokens: int = 4096
    disable_thinking: bool = True
    allow_sensitive: bool = False
    demo_today: str = "2026-07-01"
    request_timeout_sec: float = 120.0
    #: Transient retries for the an online provider (timeouts / connection / 429 / 5xx).
    #: The mock path is never wrapped, so every offline number keeps its behaviour.
    llm_retries: int = 1
    llm_retry_backoff_sec: float = 2.0
    #: Consecutive failed **attempts** before the breaker opens; 0 disables it.
    llm_circuit_failures: int = 5
    llm_circuit_reset_sec: float = 30.0
    #: Read-only retry for pool exhaustion. Never applied to SQL errors or timeouts:
    #: a failed statement is the model's problem and a slow one gets slower.
    sql_retries: int = 1
    sql_retry_backoff_sec: float = 0.05
    #: Final-answer cache. 0 (the default) keeps every published number on the
    #: uncached path; eval always bypasses it regardless of this setting.
    result_cache_ttl_sec: float = 0.0
    result_cache_max_entries: int = 128
    #: Prompt gray release: percentage of traffic on the candidate prompt layout.
    #: 0 (default) keeps every turn on the base prompt; eval always forces one arm.
    gray_percent: float = 0.0
    gray_prompt_version: str = "agent-v5-fewshot-early"
    gray_salt: str = "gray-v1"
    #: User-supplied prices per 1M tokens. No vendor price is hardcoded: without
    #: both input and output configured the cost report prints tokens and says so.
    price_input_per_1m: float | None = None
    price_output_per_1m: float | None = None
    price_cached_input_per_1m: float | None = None
    price_currency: str = "USD"
    judge_enabled: bool = False
    judge_provider: str = "openai"
    judge_model: str = "qwen-max"
    judge_base_url: str = ""
    judge_api_key: str = ""
    judge_max_tokens: int = 800
    judge_max_rows: int = 20
    judge_cache_path: Path | None = None
    judge_prompt_version: str = "judge-v2"
    judge_max_calls: int = 100
    few_shot_enabled: bool = True
    few_shot_max: int = 3
    few_shot_max_chars: int = 1200
    #: MMR redundancy weight for few-shot selection. 0 keeps the plain top-k ordering.
    few_shot_diversity: float = 0.0
    #: Retrieval ranking mode for experiences: lexical | vector | rrf. The vector
    #: channels only exist when an embedding provider is configured.
    experience_recall: str = "rrf"
    #: Opt-in switch for sending stored question text to the embedding provider.
    experience_vector: bool = False
    #: Rank prior for gold rows, as a fraction of the best match (0 disables it).
    experience_gold_boost: float = 0.05
    capture_failures: bool = True
    max_repairs: int = 2
    category_boost: float = 0.12
    multi_turn_enabled: bool = True
    rewriter: str = "auto"
    history_turns: int = 3
    carry_prior_sql: bool = True
    max_value_probes: int = 4
    dual_channel: bool = False
    dual_channel_confidence_threshold: float = 0.75
    dual_channel_categories: tuple[str, ...] = ("bom", "kitting")
    dual_channel_agree_boost: float = 0.1
    db_backend: str = "sqlite"
    mssql_server: str = ""
    mssql_port: int = 1433
    mssql_database: str = ""
    mssql_username: str = ""
    mssql_password: str = ""
    mssql_driver: str = "ODBC Driver 17 for SQL Server"
    mssql_trust_certificate: bool = True
    pg_host: str = ""
    pg_port: int = 5432
    pg_database: str = ""
    pg_username: str = ""
    pg_password: str = ""
    pg_sslmode: str = "prefer"
    #: Physical connections kept per target. 1 (the default) means "open one per query",
    #: which is the behaviour every published number was measured against, so pooling is
    #: an opt-in change to prove rather than a silent new default.
    db_pool_size: int = 1
    schema_override: Path | None = None
    #: Human-reviewed semantic layer merged over the raw catalog. It lives beside the
    #: synced schema (local, gitignored) because real table names are not portfolio data.
    schema_overlay_path: Path | None = None
    schema_suggestions_path: Path | None = None
    eval_cases_override: Path | None = None
    linking: str = "auto"
    embedding_provider: str = "none"
    embedding_model: str = "text-embedding-v4"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_dimensions: int = 256
    embedding_batch_size: int = 10
    embedding_cache_dir: Path | None = None
    rerank: str = "off"
    rerank_candidates: int = 12
    rrf_k: int = 60
    ast_guard: str = "off"
    value_alignment: str = "on"
    clarify: str = "options"
    value_dictionary_override: Path | None = None
    metric_glossary_override: Path | None = None
    principals_override: Path | None = None
    principal_id: str = ""
    log_path: Path | None = None
    log_level: str = "info"
    log_include_text: bool = False

    def __post_init__(self) -> None:
        self.home = Path(self.home).resolve()
        raw_backend = str(self.db_backend or "sqlite").strip().lower()
        self.db_backend = BACKEND_ALIASES.get(raw_backend, "")
        # An unrecognised value used to fall through to the demo SQLite file: the agent
        # would then answer a real business question from synthetic data and report it
        # as a query result. Config typos must cost a startup, not an answer.
        if not self.db_backend:
            raise ValueError(
                f"未知 NL2SQL_DB_BACKEND={raw_backend!r}，可选: sqlite / mssql / postgres"
            )
        self.db_pool_size = max(1, int(self.db_pool_size))
        self.mode = str(self.mode or "auto").strip().lower()
        if self.mode not in PIPELINE_MODES:
            raise ValueError(
                f"未知 NL2SQL_MODE={self.mode!r}，可选: " + " / ".join(PIPELINE_MODES)
            )
        self.llm_retries = min(5, max(0, int(self.llm_retries)))
        self.llm_retry_backoff_sec = min(30.0, max(0.0, float(self.llm_retry_backoff_sec)))
        self.llm_circuit_failures = max(0, int(self.llm_circuit_failures))
        self.llm_circuit_reset_sec = max(0.0, float(self.llm_circuit_reset_sec))
        self.sql_retries = min(2, max(0, int(self.sql_retries)))
        self.sql_retry_backoff_sec = min(5.0, max(0.0, float(self.sql_retry_backoff_sec)))
        self.result_cache_ttl_sec = max(0.0, float(self.result_cache_ttl_sec))
        self.result_cache_max_entries = max(1, int(self.result_cache_max_entries))
        self.gray_percent = float(self.gray_percent)
        if not 0.0 <= self.gray_percent <= 100.0:
            raise ValueError("NL2SQL_GRAY_PERCENT 必须在 0–100 之间")
        # Half a price table is worse than none: one configured side would silently
        # count the other as free and print a confident, wrong total.
        priced = [self.price_input_per_1m is not None, self.price_output_per_1m is not None]
        if any(priced) and not all(priced):
            raise ValueError(
                "NL2SQL_PRICE_INPUT_PER_1M 与 NL2SQL_PRICE_OUTPUT_PER_1M 必须同时配置"
                "（只配一边会算出一个漏掉一半的假成本）"
            )
        for name in ("price_input_per_1m", "price_output_per_1m", "price_cached_input_per_1m"):
            value = getattr(self, name)
            if value is not None and float(value) < 0:
                raise ValueError(f"{name} 不能为负数")
        self.price_currency = str(self.price_currency or "USD").strip().upper() or "USD"
        self.few_shot_diversity = min(1.0, max(0.0, float(self.few_shot_diversity)))
        self.experience_recall = str(self.experience_recall or "lexical").strip().lower()
        if self.experience_recall not in EXPERIENCE_RECALL_MODES:
            raise ValueError(
                f"未知 NL2SQL_EXPERIENCE_RECALL={self.experience_recall!r}，可选: "
                + " / ".join(EXPERIENCE_RECALL_MODES)
            )
        # An opt-in vector channel with no embedding provider is an inert switch: better
        # to cost a startup than to let someone believe their experience recall is semantic.
        if self.experience_vector and self.embedding_provider in {"", "none", "off"}:
            raise ValueError(
                "NL2SQL_EXPERIENCE_VECTOR=true 需要同时配置 NL2SQL_EMBEDDING=hash（离线测管道）"
                "或 =openai（真实向量），否则经验召回只有词法一路。"
            )
        if self.db_path is None:
            self.db_path = self.home / "var" / "demo_erp.db"
        if self.experience_path is None:
            self.experience_path = self.home / "var" / "experience.db"
        if self.judge_cache_path is None:
            self.judge_cache_path = self.home / "var" / "judge_cache.sqlite"
        if self.embedding_cache_dir is None:
            self.embedding_cache_dir = self.home / "var" / "embeddings"
        if self.schema_overlay_path is None:
            self.schema_overlay_path = self.home / "var" / "schema_overlay.json"
        if self.schema_suggestions_path is None:
            self.schema_suggestions_path = self.home / "var" / "schema_suggestions.json"
        self.db_path = Path(self.db_path)
        self.experience_path = Path(self.experience_path)
        self.judge_cache_path = Path(self.judge_cache_path)
        self.embedding_cache_dir = Path(self.embedding_cache_dir)
        self.schema_overlay_path = Path(self.schema_overlay_path)
        self.schema_suggestions_path = Path(self.schema_suggestions_path)
        if self.log_path is not None:
            # A relative NL2SQL_LOG_PATH belongs to the run directory, not to whatever
            # the process happened to be started from, so --home and the log stay together.
            candidate = Path(self.log_path).expanduser()
            self.log_path = candidate if candidate.is_absolute() else self.home / candidate

    @classmethod
    def load(cls, home: str | Path | None = None, provider: str | None = None) -> "Settings":
        base = Path(home) if home is not None else Path(os.getenv("NL2SQL_HOME", Path.cwd()))
        _load_dotenv(base / ".env")
        package_root = Path(__file__).resolve().parents[2]
        _load_dotenv(package_root / ".env")

        selected_provider = (provider or os.getenv("NL2SQL_PROVIDER") or "").strip().lower()
        api_key = (
            os.getenv("NL2SQL_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or ""
        ).strip()
        if not selected_provider:
            selected_provider = "openai" if api_key else "mock"

        base_url = (
            os.getenv("NL2SQL_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).strip()
        settings = cls(
            provider=selected_provider,
            model=(os.getenv("NL2SQL_MODEL") or os.getenv("OPENAI_MODEL") or "qwen-plus").strip(),
            base_url=base_url,
            api_key=api_key,
            home=base,
            db_path=(os.getenv("NL2SQL_DB_PATH") or None),
            experience_path=(os.getenv("NL2SQL_EXPERIENCE_PATH") or None),
            max_steps=_as_int(os.getenv("NL2SQL_MAX_STEPS"), 12),
            mode=(os.getenv("NL2SQL_MODE") or "auto").strip().lower(),
            max_rows=_as_int(os.getenv("NL2SQL_MAX_ROWS"), 200),
            query_timeout_ms=_as_int(os.getenv("NL2SQL_QUERY_TIMEOUT_MS"), 5000),
            max_tokens=_as_int(os.getenv("NL2SQL_MAX_TOKENS"), 4096),
            disable_thinking=_as_bool(os.getenv("NL2SQL_DISABLE_THINKING"), True),
            allow_sensitive=_as_bool(os.getenv("NL2SQL_ALLOW_SENSITIVE"), False),
            demo_today=os.getenv("NL2SQL_DEMO_TODAY", "2026-07-01").strip(),
            request_timeout_sec=float(os.getenv("NL2SQL_REQUEST_TIMEOUT_SEC", "120")),
            llm_retries=_as_int(os.getenv("NL2SQL_LLM_RETRIES"), 1),
            llm_retry_backoff_sec=_as_float(os.getenv("NL2SQL_LLM_RETRY_BACKOFF_SEC"), 2.0),
            llm_circuit_failures=_as_int(os.getenv("NL2SQL_LLM_CIRCUIT_FAILURES"), 5),
            llm_circuit_reset_sec=_as_float(os.getenv("NL2SQL_LLM_CIRCUIT_RESET_SEC"), 30.0),
            sql_retries=_as_int(os.getenv("NL2SQL_SQL_RETRIES"), 1),
            sql_retry_backoff_sec=_as_float(os.getenv("NL2SQL_SQL_RETRY_BACKOFF_SEC"), 0.05),
            result_cache_ttl_sec=_as_float(os.getenv("NL2SQL_RESULT_CACHE_TTL_SEC"), 0.0),
            result_cache_max_entries=_as_int(os.getenv("NL2SQL_RESULT_CACHE_MAX"), 128),
            gray_percent=_as_float(os.getenv("NL2SQL_GRAY_PERCENT"), 0.0),
            gray_prompt_version=(
                os.getenv("NL2SQL_GRAY_PROMPT_VERSION") or "agent-v5-fewshot-early"
            ).strip(),
            gray_salt=(os.getenv("NL2SQL_GRAY_SALT") or "gray-v1").strip(),
            price_input_per_1m=_as_optional_float(os.getenv("NL2SQL_PRICE_INPUT_PER_1M")),
            price_output_per_1m=_as_optional_float(os.getenv("NL2SQL_PRICE_OUTPUT_PER_1M")),
            price_cached_input_per_1m=_as_optional_float(
                os.getenv("NL2SQL_PRICE_CACHED_INPUT_PER_1M")
            ),
            price_currency=(os.getenv("NL2SQL_PRICE_CURRENCY") or "USD").strip().upper(),
            judge_enabled=_as_bool(os.getenv("NL2SQL_JUDGE_ENABLED"), False),
            judge_provider=(os.getenv("NL2SQL_JUDGE_PROVIDER") or "openai").strip().lower(),
            judge_model=(os.getenv("NL2SQL_JUDGE_MODEL") or "qwen-max").strip(),
            judge_base_url=(os.getenv("NL2SQL_JUDGE_BASE_URL") or base_url).strip(),
            judge_api_key=(os.getenv("NL2SQL_JUDGE_API_KEY") or api_key).strip(),
            judge_max_tokens=_as_int(os.getenv("NL2SQL_JUDGE_MAX_TOKENS"), 800),
            judge_max_rows=_as_int(os.getenv("NL2SQL_JUDGE_MAX_ROWS"), 20),
            judge_cache_path=(os.getenv("NL2SQL_JUDGE_CACHE_PATH") or None),
            judge_prompt_version=(os.getenv("NL2SQL_JUDGE_PROMPT_VERSION") or "judge-v2").strip(),
            judge_max_calls=_as_int(os.getenv("NL2SQL_JUDGE_MAX_CALLS"), 100),
            few_shot_enabled=_as_bool(os.getenv("NL2SQL_FEWSHOT_ENABLED"), True),
            few_shot_max=_as_int(os.getenv("NL2SQL_FEWSHOT_MAX"), 3),
            few_shot_max_chars=_as_int(os.getenv("NL2SQL_FEWSHOT_MAX_CHARS"), 1200),
            few_shot_diversity=_as_float(os.getenv("NL2SQL_FEWSHOT_DIVERSITY"), 0.0),
            experience_recall=(os.getenv("NL2SQL_EXPERIENCE_RECALL") or "rrf").strip().lower(),
            experience_vector=_as_bool(os.getenv("NL2SQL_EXPERIENCE_VECTOR"), False),
            experience_gold_boost=_as_float(os.getenv("NL2SQL_EXPERIENCE_GOLD_BOOST"), 0.05),
            capture_failures=_as_bool(os.getenv("NL2SQL_CAPTURE_FAILURES"), True),
            max_repairs=_as_int(os.getenv("NL2SQL_MAX_REPAIRS"), 2),
            category_boost=_as_float(os.getenv("NL2SQL_CATEGORY_BOOST"), 0.12),
            multi_turn_enabled=_as_bool(os.getenv("NL2SQL_MULTI_TURN"), True),
            rewriter=(os.getenv("NL2SQL_REWRITER") or "auto").strip().lower(),
            history_turns=_as_int(os.getenv("NL2SQL_HISTORY_TURNS"), 3),
            carry_prior_sql=_as_bool(os.getenv("NL2SQL_CARRY_PRIOR_SQL"), True),
            max_value_probes=_as_int(os.getenv("NL2SQL_MAX_VALUE_PROBES"), 4),
            dual_channel=_as_bool(os.getenv("NL2SQL_DUAL_CHANNEL"), False),
            dual_channel_confidence_threshold=_as_float(
                os.getenv("NL2SQL_DUAL_CHANNEL_CONFIDENCE"), 0.75
            ),
            dual_channel_categories=_as_words(
                os.getenv("NL2SQL_DUAL_CHANNEL_CATEGORIES"), ("bom", "kitting")
            ),
            dual_channel_agree_boost=_as_float(os.getenv("NL2SQL_DUAL_CHANNEL_BOOST"), 0.1),
            db_backend=(os.getenv("NL2SQL_DB_BACKEND") or "sqlite").strip().lower(),
            mssql_server=(os.getenv("NL2SQL_MSSQL_SERVER") or "").strip(),
            mssql_port=_as_int(os.getenv("NL2SQL_MSSQL_PORT"), 1433),
            mssql_database=(os.getenv("NL2SQL_MSSQL_DATABASE") or "").strip(),
            mssql_username=(os.getenv("NL2SQL_MSSQL_USERNAME") or "").strip(),
            mssql_password=(os.getenv("NL2SQL_MSSQL_PASSWORD") or "").strip(),
            mssql_driver=(os.getenv("NL2SQL_MSSQL_DRIVER") or "ODBC Driver 17 for SQL Server").strip(),
            mssql_trust_certificate=_as_bool(os.getenv("NL2SQL_MSSQL_TRUST_CERTIFICATE"), True),
            pg_host=(os.getenv("NL2SQL_PG_HOST") or "").strip(),
            pg_port=_as_int(os.getenv("NL2SQL_PG_PORT"), 5432),
            pg_database=(os.getenv("NL2SQL_PG_DATABASE") or "").strip(),
            pg_username=(os.getenv("NL2SQL_PG_USERNAME") or "").strip(),
            pg_password=(os.getenv("NL2SQL_PG_PASSWORD") or "").strip(),
            pg_sslmode=(os.getenv("NL2SQL_PG_SSLMODE") or "prefer").strip().lower(),
            db_pool_size=_as_int(os.getenv("NL2SQL_DB_POOL_SIZE"), 1),
            schema_override=(Path(os.environ["NL2SQL_SCHEMA_PATH"]) if os.getenv("NL2SQL_SCHEMA_PATH") else None),
            schema_overlay_path=(
                Path(os.environ["NL2SQL_SCHEMA_OVERLAY"]) if os.getenv("NL2SQL_SCHEMA_OVERLAY") else None
            ),
            schema_suggestions_path=(
                Path(os.environ["NL2SQL_SCHEMA_SUGGESTIONS"])
                if os.getenv("NL2SQL_SCHEMA_SUGGESTIONS")
                else None
            ),
            eval_cases_override=(
                Path(os.environ["NL2SQL_EVAL_CASES"]) if os.getenv("NL2SQL_EVAL_CASES") else None
            ),
            linking=(os.getenv("NL2SQL_LINKING") or "auto").strip().lower(),
            embedding_provider=(os.getenv("NL2SQL_EMBEDDING") or "none").strip().lower(),
            embedding_model=(os.getenv("NL2SQL_EMBEDDING_MODEL") or "text-embedding-v4").strip(),
            embedding_base_url=(os.getenv("NL2SQL_EMBEDDING_BASE_URL") or "").strip(),
            embedding_api_key=(os.getenv("NL2SQL_EMBEDDING_API_KEY") or "").strip(),
            embedding_dimensions=_as_int(os.getenv("NL2SQL_EMBEDDING_DIMENSIONS"), 256),
            embedding_batch_size=_as_int(os.getenv("NL2SQL_EMBEDDING_BATCH_SIZE"), 10),
            embedding_cache_dir=(os.getenv("NL2SQL_EMBEDDING_CACHE") or None),
            rerank=(os.getenv("NL2SQL_RERANK") or "off").strip().lower(),
            rerank_candidates=_as_int(os.getenv("NL2SQL_RERANK_CANDIDATES"), 12),
            rrf_k=_as_int(os.getenv("NL2SQL_LINKING_RRF_K"), 60),
            ast_guard=(os.getenv("NL2SQL_AST_GUARD") or "off").strip().lower(),
            value_alignment=(os.getenv("NL2SQL_VALUE_ALIGNMENT") or "on").strip().lower(),
            clarify=(os.getenv("NL2SQL_CLARIFY") or "options").strip().lower(),
            value_dictionary_override=(
                Path(os.environ["NL2SQL_VALUE_DICTIONARY"])
                if os.getenv("NL2SQL_VALUE_DICTIONARY")
                else None
            ),
            metric_glossary_override=(
                Path(os.environ["NL2SQL_METRIC_GLOSSARY"])
                if os.getenv("NL2SQL_METRIC_GLOSSARY")
                else None
            ),
            principals_override=(
                Path(os.environ["NL2SQL_PRINCIPALS"]) if os.getenv("NL2SQL_PRINCIPALS") else None
            ),
            principal_id=(os.getenv("NL2SQL_PRINCIPAL") or "").strip(),
            log_path=(Path(os.environ["NL2SQL_LOG_PATH"]) if os.getenv("NL2SQL_LOG_PATH") else None),
            log_level=(os.getenv("NL2SQL_LOG_LEVEL") or "info").strip().lower(),
            log_include_text=_as_bool(os.getenv("NL2SQL_LOG_INCLUDE_TEXT"), False),
        )
        return settings

    @property
    def ast_guard_enabled(self) -> bool:
        return self.ast_guard not in {"", "off", "none"}

    @property
    def price_table(self) -> dict[str, float | str] | None:
        """Configured price table, or None. Never a vendor default."""
        if self.price_input_per_1m is None or self.price_output_per_1m is None:
            return None
        table: dict[str, float | str] = {
            "input_per_1m": float(self.price_input_per_1m),
            "output_per_1m": float(self.price_output_per_1m),
            "currency": self.price_currency,
        }
        if self.price_cached_input_per_1m is not None:
            table["cached_input_per_1m"] = float(self.price_cached_input_per_1m)
        return table

    @property
    def is_mssql(self) -> bool:
        return self.db_backend == "mssql"

    @property
    def is_postgres(self) -> bool:
        return self.db_backend == "postgres"

    @property
    def is_server_backend(self) -> bool:
        return self.db_backend in {"mssql", "postgres"}

    @property
    def database_label(self) -> str:
        """Backend label that is safe to show in a UI or API response."""
        if self.is_mssql:
            return f"mssql:{self.mssql_database or 'unconfigured'}"
        if self.is_postgres:
            return f"postgres:{self.pg_database or 'unconfigured'}"
        return str(self.db_path)

    @property
    def schema_path(self) -> Path:
        if self.schema_override is not None:
            return self.schema_override
        return Path(__file__).resolve().parent / "data" / "demo_schema.json"

    @property
    def eval_cases_path(self) -> Path:
        if self.eval_cases_override is not None:
            return self.eval_cases_override
        return Path(__file__).resolve().parent / "data" / "demo_cases.jsonl"

    @property
    def multi_turn_cases_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "multi_turn_cases.jsonl"

    @property
    def paraphrase_cases_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "paraphrase_cases.jsonl"

    @property
    def adversarial_question_cases_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "adversarial_question_cases.jsonl"

    @property
    def alias_cases_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "alias_cases.jsonl"

    @property
    def value_dictionary_path(self) -> Path:
        """A real deployment points NL2SQL_VALUE_DICTIONARY at its own alias table.

        The packaged default is the synthetic demo dictionary: production customer
        and supplier short forms are business data and do not belong in a repository.
        """
        if self.value_dictionary_override is not None:
            return self.value_dictionary_override
        return Path(__file__).resolve().parent / "data" / "value_dictionary.json"

    @property
    def metric_glossary_path(self) -> Path | None:
        """``None`` means the packaged in-code noun table; a deployment points at its own.

        Which noun is ambiguous is business vocabulary, so the production glossary
        stays outside the repository, same as the alias dictionary.
        """
        return self.metric_glossary_override

    @property
    def clarify_cases_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "clarify_cases.jsonl"

    @property
    def calibration_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "judge_calibration.jsonl"

    @property
    def principals_path(self) -> Path:
        """A real deployment points NL2SQL_PRINCIPALS at its own file.

        Role names and column rules describe the production schema, so the
        packaged default is the synthetic demo set only.
        """
        if self.principals_override is not None:
            return self.principals_override
        return Path(__file__).resolve().parent / "data" / "principals.json"

    @property
    def target(self):
        """Read-only query target for the configured backend."""
        from .tools.db import MssqlTarget, PostgresTarget, SqliteTarget

        if self.is_mssql:
            missing = [
                name
                for name, value in (
                    ("NL2SQL_MSSQL_SERVER", self.mssql_server),
                    ("NL2SQL_MSSQL_DATABASE", self.mssql_database),
                    ("NL2SQL_MSSQL_USERNAME", self.mssql_username),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"NL2SQL_DB_BACKEND=mssql 缺少连接配置: {', '.join(missing)}")
            return MssqlTarget(
                server=self.mssql_server,
                port=self.mssql_port,
                database=self.mssql_database,
                username=self.mssql_username,
                password=self.mssql_password,
                driver=self.mssql_driver,
                trust_server_certificate=self.mssql_trust_certificate,
            )
        if self.is_postgres:
            missing = [
                name
                for name, value in (
                    ("NL2SQL_PG_HOST", self.pg_host),
                    ("NL2SQL_PG_DATABASE", self.pg_database),
                    ("NL2SQL_PG_USERNAME", self.pg_username),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"NL2SQL_DB_BACKEND=postgres 缺少连接配置: {', '.join(missing)}")
            return PostgresTarget(
                host=self.pg_host,
                port=self.pg_port,
                database=self.pg_database,
                username=self.pg_username,
                password=self.pg_password,
                sslmode=self.pg_sslmode,
            )
        return SqliteTarget(Path(self.db_path))

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.experience_path.parent.mkdir(parents=True, exist_ok=True)
