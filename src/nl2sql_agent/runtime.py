"""Compose catalog, guard, tools, memory and model into an agent."""

from __future__ import annotations

from dataclasses import dataclass

from .agent import NL2SQLAgent
from .catalog import Catalog
from .config import Settings
from .demo import ensure_demo_db
from .guard import SQLGuard
from .llm import OpenAICompatibleLLM
from .memory import ExperienceStore
from .mock_llm import RuleBasedLLM
from .repair import SQLRepairer
from .tools import (
    ToolRegistry,
    build_experience_tools,
    build_schema_tools,
    build_sql_tools,
)


@dataclass
class Runtime:
    settings: Settings
    catalog: Catalog
    guard: SQLGuard
    store: ExperienceStore
    agent: NL2SQLAgent


def create_runtime(settings: Settings | None = None, provider: str | None = None) -> Runtime:
    settings = settings or Settings.load(provider=provider)
    if provider:
        settings.provider = provider.strip().lower()
    settings.ensure_dirs()
    ensure_demo_db(settings.db_path)

    catalog = Catalog.from_path(settings.schema_path)
    guard = SQLGuard(catalog, allow_sensitive=settings.allow_sensitive)
    repairer = SQLRepairer()
    store = ExperienceStore(settings.experience_path)

    registry = ToolRegistry()
    for tool in build_schema_tools(catalog, settings.db_path):
        registry.register(tool)
    for tool in build_sql_tools(
        guard,
        repairer,
        settings.db_path,
        max_rows=settings.max_rows,
        timeout_ms=settings.query_timeout_ms,
    ):
        registry.register(tool)
    for tool in build_experience_tools(store, catalog.schema_hash(), guard):
        registry.register(tool)

    if settings.provider == "mock":
        llm = RuleBasedLLM(catalog, settings.demo_today)
    elif settings.provider == "openai":
        llm = OpenAICompatibleLLM(settings)
    else:
        raise ValueError(f"未知 provider: {settings.provider}，可选 mock / openai")

    agent = NL2SQLAgent(catalog=catalog, llm=llm, tools=registry, settings=settings)
    return Runtime(settings=settings, catalog=catalog, guard=guard, store=store, agent=agent)


def create_agent(settings: Settings | None = None, provider: str | None = None) -> NL2SQLAgent:
    return create_runtime(settings, provider).agent
