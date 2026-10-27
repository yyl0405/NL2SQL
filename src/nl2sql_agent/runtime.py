"""Compose catalog, guard, tools, memory and model into an agent."""

from __future__ import annotations

from dataclasses import dataclass

from . import observability
from .agent import NL2SQLAgent
from .ast_guard import AstAnalyzer
from .cache import ResultCache
from .catalog import Catalog
from .config import Settings
from .demo import ensure_demo_db
from .embeddings import build_embedding_provider
from .guard import SQLGuard
from .linking import fusion_mode
from .llm import ChatModel, OpenAICompatibleLLM
from .memory import ExperienceStore
from .mock_llm import RuleBasedLLM
from .principal import Principal, load_principal
from .repair import SQLRepairer
from .rerank import LLMReranker
from .recall import ExperienceRecall, recall_from_settings
from .resilience import CircuitBreaker, ResilientChatModel
from .rewriter import LLMQuestionRewriter, QuestionRewriter, RuleBasedRewriter
from .schema_overlay import apply_overlay, load_overlay
from .tools.db import configure_pool, dialect_of, open_connection
from .tools import (
    ToolRegistry,
    build_experience_tools,
    build_schema_tools,
    build_sql_tools,
)
from .value_align import ValueAligner, ValueDictionary
from .voting import LLMSecondOpinion, RuleBasedSecondOpinion, SecondOpinionGenerator


@dataclass
class Runtime:
    settings: Settings
    catalog: Catalog
    guard: SQLGuard
    store: ExperienceStore
    agent: NL2SQLAgent
    principal: Principal | None = None
    recall: ExperienceRecall | None = None
    #: Final-answer cache; ``None`` when NL2SQL_RESULT_CACHE_TTL_SEC is 0.
    cache: ResultCache | None = None
    #: The model object the agent actually uses. Kept on the runtime so health and
    #: operational readouts can report the breaker state without reaching into the agent.
    llm: ChatModel | None = None


def _probe_backend(settings: Settings) -> None:
    """Fail fast when the configured server database cannot be reached.

    Without it an unreachable host turns a 40-case eval into 40 execution errors
    that still print ``valid_sql_rate 1.0``: a connection failure wearing the
    clothes of a model failure.
    """
    target = settings.target
    dialect = dialect_of(target)
    extra = "mssql" if settings.is_mssql else "postgres"
    prefix = "NL2SQL_MSSQL" if settings.is_mssql else "NL2SQL_PG"
    try:
        conn, _ = open_connection(target, timeout_ms=5000)
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"{dialect.label} 后端缺少驱动：{exc}（pip install -e \".[{extra}]\"）"
        ) from exc
    except Exception as exc:  # driver-specific connect errors; the message must stay readable
        raise SystemExit(
            f"连不上 {target.describe()}：{exc}\n"
            f"请核对 {prefix}_* 连接项、网络与只读账号权限。"
        ) from exc
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchall()
    finally:
        conn.close()


def create_runtime(settings: Settings | None = None, provider: str | None = None) -> Runtime:
    settings = settings or Settings.load(provider=provider)
    if provider:
        settings.provider = provider.strip().lower()
    settings.ensure_dirs()
    # The composition root is the only place that knows both the config and the
    # process lifetime, so the log sink is attached here and nowhere else.
    observability.configure_logging(
        settings.log_path, level=settings.log_level, include_text=settings.log_include_text
    )
    if not settings.is_server_backend:
        ensure_demo_db(settings.db_path)
    if settings.schema_override and not settings.schema_path.exists():
        raise SystemExit(
            f"schema 目录不存在: {settings.schema_path}（先运行 nl2sql-agent schema-sync 生成）"
        )
    if settings.is_server_backend:
        _probe_backend(settings)
    # Pooling is opt-in: with NL2SQL_DB_POOL_SIZE=1 (the default) no pool is registered and
    # every query opens its own connection, which is what the published numbers measured.
    configure_pool(
        settings.target, timeout_ms=settings.query_timeout_ms, max_size=settings.db_pool_size
    )

    embedder = build_embedding_provider(settings)
    if fusion_mode(settings.linking) != "bm25" and embedder is None:
        raise SystemExit(
            f"NL2SQL_LINKING={settings.linking} 需要向量通道：请设置 "
            "NL2SQL_EMBEDDING=hash（离线）或 openai（真实语义向量），"
            "或改用 auto / hybrid 等 BM25 策略。"
        )
    catalog = Catalog.from_path(
        settings.schema_path,
        linking=settings.linking,
        embedder=embedder,
        embedding_cache=settings.embedding_cache_dir,
        rrf_k=settings.rrf_k,
    )
    catalog = apply_overlay(catalog, load_overlay(settings.schema_overlay_path))
    target = settings.target

    # A principal is applied twice, on purpose and differently: the guard keeps
    # the full catalog so it can name the table the caller may not touch, while
    # the agent, its tools and its prompt only ever see the filtered view.
    principal = _load_principal(settings)
    agent_catalog = principal.view(catalog) if principal is not None else catalog
    guard = SQLGuard(
        catalog,
        allow_sensitive=settings.allow_sensitive,
        principal=principal,
        analyzer=_build_analyzer(settings, catalog.dialect),
    )
    schema_hash = agent_catalog.schema_hash()
    repairer = SQLRepairer()
    store = ExperienceStore(settings.experience_path)
    # One embedding provider serves both vector channels; the experience channel stays
    # lexical unless NL2SQL_EXPERIENCE_VECTOR is on, because turning it on means sending
    # stored question text to the provider as well.
    recall = recall_from_settings(store, settings, embedder)

    registry = ToolRegistry()
    for tool in build_schema_tools(agent_catalog, target):
        registry.register(tool)
    for tool in build_sql_tools(
        guard,
        repairer,
        target,
        max_rows=settings.max_rows,
        timeout_ms=settings.query_timeout_ms,
        retries=settings.sql_retries,
        retry_backoff_sec=settings.sql_retry_backoff_sec,
    ):
        registry.register(tool)
    for tool in build_experience_tools(
        store,
        schema_hash,
        guard,
        category_boost=settings.category_boost,
        recall=recall,
    ):
        registry.register(tool)

    if settings.provider == "mock":
        llm: ChatModel = RuleBasedLLM(agent_catalog, settings.demo_today)
    elif settings.provider == "openai":
        llm = _build_llm(settings)
    else:
        raise ValueError(f"未知 provider: {settings.provider}，可选 mock / openai")

    reranker = _build_reranker(settings, llm)
    if reranker is not None:
        agent_catalog.reranker = reranker
        agent_catalog.rerank_candidates = settings.rerank_candidates

    rewriter = _build_rewriter(settings, llm)
    aligner = _build_aligner(settings)
    result_cache = (
        ResultCache(settings.result_cache_ttl_sec, settings.result_cache_max_entries)
        if settings.result_cache_ttl_sec > 0
        else None
    )

    agent = NL2SQLAgent(
        catalog=agent_catalog,
        llm=llm,
        tools=registry,
        settings=settings,
        store=store,
        schema_hash=schema_hash,
        rewriter=rewriter,
        second_opinion=_build_second_opinion(settings, agent_catalog, llm),
        guard=guard,
        aligner=aligner,
        recall=recall,
        cache=result_cache,
    )
    return Runtime(
        settings=settings,
        catalog=catalog,
        guard=guard,
        store=store,
        agent=agent,
        principal=principal,
        recall=recall,
        cache=result_cache,
        llm=llm,
    )


def _build_llm(settings: Settings) -> ChatModel:
    """Wrap the an online provider so timeouts, retries and the breaker are one policy.

    The mock provider is intentionally left unwrapped: it has no network calls,
    so adding resilience there would only make offline measurements depend on
    code that never fires.
    """
    breaker = (
        CircuitBreaker(settings.llm_circuit_failures, settings.llm_circuit_reset_sec)
        if settings.llm_circuit_failures > 0
        else None
    )
    return ResilientChatModel(
        OpenAICompatibleLLM(settings),
        retries=settings.llm_retries,
        backoff_sec=settings.llm_retry_backoff_sec,
        breaker=breaker,
    )


def _load_principal(settings: Settings) -> Principal | None:
    if not settings.principal_id:
        return None
    return load_principal(settings.principals_path, settings.principal_id)


def _build_analyzer(settings: Settings, dialect: str) -> AstAnalyzer | None:
    """`off` is the default; `on` fails closed when sqlglot is missing."""
    choice = (settings.ast_guard or "off").strip().lower()
    if choice in {"", "off", "none"}:
        return None
    if choice != "on":
        raise ValueError(f"未知 NL2SQL_AST_GUARD: {choice}，可选 off / on")
    return AstAnalyzer(dialect=dialect)


def _build_second_opinion(
    settings: Settings, catalog: Catalog, llm: object
) -> SecondOpinionGenerator | None:
    """Dual-channel voting is opt-in: one extra model call and query per trigger."""
    if not settings.dual_channel:
        return None
    if settings.provider == "openai":
        return LLMSecondOpinion(llm, catalog, settings)
    return RuleBasedSecondOpinion(catalog, settings.demo_today)


def _build_reranker(settings: Settings, llm: object) -> LLMReranker | None:
    choice = (settings.rerank or "off").strip().lower()
    if choice in {"", "off", "none"}:
        return None
    if choice != "llm":
        raise ValueError(f"未知 NL2SQL_RERANK: {choice}，可选 off / llm")
    if settings.provider != "openai":
        # Mock has no model to ask; document the no-op rather than pretend.
        return None
    return LLMReranker(llm, settings.rerank_candidates)


def _build_rewriter(settings: Settings, llm: object) -> QuestionRewriter:
    rule = RuleBasedRewriter()
    choice = settings.rewriter
    if choice == "rule":
        return rule
    if choice == "llm":
        return LLMQuestionRewriter(llm, rule, history_turns=settings.history_turns)
    if choice == "auto":
        # The offline mock model cannot answer a rewrite prompt, so only a real
        # provider gets the LLM channel.
        if settings.provider == "openai" and settings.api_key:
            return LLMQuestionRewriter(llm, rule, history_turns=settings.history_turns)
        return rule
    if choice == "off":
        # Never consulted: NL2SQLAgent skips rewriting for "off".
        return rule
    raise ValueError(f"未知 NL2SQL_REWRITER: {choice}，可选 auto / rule / llm / off")


def _build_aligner(settings: Settings) -> ValueAligner | None:
    """Value alignment is on by default; `off` is the byte-identical escape hatch.

    A malformed dictionary raises instead of degrading: half-loaded aliases would
    inject the wrong value into every question that mentions that entity.
    """
    choice = (settings.value_alignment or "on").strip().lower()
    if choice in {"", "off", "none"}:
        return None
    if choice != "on":
        raise ValueError(f"未知 NL2SQL_VALUE_ALIGNMENT: {choice}，可选 on / off")
    return ValueAligner(ValueDictionary.load(settings.value_dictionary_path))


def create_agent(settings: Settings | None = None, provider: str | None = None) -> NL2SQLAgent:
    return create_runtime(settings, provider).agent
