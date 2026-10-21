from __future__ import annotations

from pathlib import Path

import pytest

from nl2sql_agent.catalog import Catalog
from nl2sql_agent.config import Settings
from nl2sql_agent.guard import SQLGuard
from nl2sql_agent.runtime import Runtime, create_runtime


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
