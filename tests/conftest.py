"""Shared pytest fixtures for the QueryBridge test suite."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from backend.database import DatabaseManager
from backend.models import SchemaModel
from backend.schema_inspector import SchemaInspector, bundled_data_dictionary
from config import Settings, get_settings
from database.init_db import build_database
from database.sample_data import DEFAULT_SEED

# Variables that could leak the developer's real environment into a test.
_ISOLATED_PREFIXES = ("QB_",)
_ISOLATED_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "RENDER_EXTERNAL_HOSTNAME")


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every QueryBridge/provider variable so tests see pure defaults."""
    for name in list(os.environ):
        if name.startswith(_ISOLATED_PREFIXES) or name in _ISOLATED_NAMES:
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def make_settings(isolated_env: pytest.MonkeyPatch) -> Callable[..., Settings]:
    """Build Settings from keyword overrides, ignoring any real .env file."""

    def _factory(**overrides: object) -> Settings:
        return Settings(_env_file=None, **overrides)

    return _factory


@pytest.fixture(scope="session")
def demo_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A freshly built demo database, shared (read-only) by the whole session."""
    path = tmp_path_factory.mktemp("demo") / "querybridge_demo.db"
    build_database(path, seed=DEFAULT_SEED)
    return path


@pytest.fixture(scope="session")
def demo_db(demo_db_path: Path) -> Iterator[DatabaseManager]:
    manager = DatabaseManager.for_sqlite_file(demo_db_path)
    yield manager
    manager.dispose()


@pytest.fixture(scope="session")
def demo_schema(demo_db: DatabaseManager) -> SchemaModel:
    return SchemaInspector(demo_db, data_dictionary=bundled_data_dictionary()).inspect()


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Make sure no test reuses settings cached by another test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
