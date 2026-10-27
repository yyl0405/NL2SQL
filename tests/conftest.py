from __future__ import annotations

import os
from pathlib import Path

import pytest

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.config import Settings
from nl2sql_agent.guard import SQLGuard
from nl2sql_agent.runtime import Runtime, create_runtime

# Settings.load() folds the developer's own .env into os.environ, and a local
# setup can point the whole suite at a real SQL Server catalog. Tests always run
# against the synthetic demo, whatever is configured on the machine.
DEMO_ENV = {
    "NL2SQL_DB_BACKEND": "sqlite",
    "NL2SQL_SCHEMA_PATH": "",
    "NL2SQL_EVAL_CASES": "",
    "NL2SQL_LINKING": "auto",
    # A developer's own .env may force the deterministic pipeline; the suite measures
    # the packaged route, and pipeline routing is pinned explicitly per test instead.
    "NL2SQL_MODE": "auto",
    "NL2SQL_EMBEDDING": "none",
    "NL2SQL_RERANK": "off",
    "NL2SQL_MSSQL_SERVER": "",
    "NL2SQL_MSSQL_DATABASE": "",
    "NL2SQL_MSSQL_USERNAME": "",
    "NL2SQL_MSSQL_PASSWORD": "",
    "NL2SQL_AST_GUARD": "off",
    "NL2SQL_VALUE_ALIGNMENT": "on",
    "NL2SQL_VALUE_DICTIONARY": "",
    "NL2SQL_CLARIFY": "options",
    "NL2SQL_METRIC_GLOSSARY": "",
    "NL2SQL_PRINCIPAL": "",
    "NL2SQL_PRINCIPALS": "",
    # An empty log path means "no sink at all", so the suite never writes into a
    # developer's own NL2SQL_LOG_PATH or leaves a file handler attached to the
    # module logger for later tests to trip over.
    "NL2SQL_LOG_PATH": "",
    "NL2SQL_LOG_INCLUDE_TEXT": "false",
    # The same reasoning applies to the model: a developer's own .env may carry a
    # live provider and api key for the console, and every published number in the
    # suite is measured against the offline rule-based stand-in. An empty key plus
    # provider=mock means a stray Settings.load() cannot reach the network.
    "NL2SQL_PROVIDER": "mock",
    "NL2SQL_API_KEY": "",
    "OPENAI_API_KEY": "",
    "DASHSCOPE_API_KEY": "",
    "OPENAI_BASE_URL": "",
    "OPENAI_MODEL": "",
    "NL2SQL_MODEL": "",
    "NL2SQL_BASE_URL": "",
    "NL2SQL_JUDGE_ENABLED": "false",
    "NL2SQL_JUDGE_PROVIDER": "mock",
    "NL2SQL_JUDGE_API_KEY": "",
    "NL2SQL_JUDGE_BASE_URL": "",
    "NL2SQL_JUDGE_MODEL": "",
    "NL2SQL_EMBEDDING_API_KEY": "",
    "NL2SQL_EMBEDDING_BASE_URL": "",
    # A developer's curated overlay/semantics file is local data; the suite measures
    # the packaged catalog exactly as it ships.
    "NL2SQL_SCHEMA_OVERLAY": "",
    "NL2SQL_SCHEMA_SUGGESTIONS": "",
    "NL2SQL_MSSQL_PORT": "",
    "NL2SQL_MSSQL_DRIVER": "",
    "NL2SQL_PG_HOST": "",
    "NL2SQL_PG_DATABASE": "",
    "NL2SQL_PG_USERNAME": "",
    "NL2SQL_PG_PASSWORD": "",
    # Gray release must not silently move the suite onto the candidate prompt layout.
    "NL2SQL_GRAY_PERCENT": "0",
    "NL2SQL_DB_PATH": "",
    "NL2SQL_EXPERIENCE_PATH": "",
}


@pytest.fixture(autouse=True)
def demo_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in DEMO_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path, provider="mock")


@pytest.fixture()
def catalog() -> Catalog:
    return Catalog.from_path(Settings().schema_path)


@pytest.fixture()
def guard(catalog: Catalog) -> SQLGuard:
    return SQLGuard(catalog)


@pytest.fixture()
def runtime(settings: Settings) -> Runtime:
    return create_runtime(settings)
