"""Shared test fixtures for Vesper memory server."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run tests against real services (Neo4j, etc.) instead of mocks.",
    )


@pytest.fixture
def live_mode(request):
    """Whether tests should hit real services."""
    return request.config.getoption("--live") or os.getenv("VESPER_TEST_LIVE") == "1"


@pytest.fixture
def mock_graphiti(live_mode):
    """Patch Neo4jDriver, Graphiti, and client builders unless running in live mode.

    Yields a namespace with .driver_cls, .graphiti_cls, .llm_builder,
    and .embedder_builder (the mocks, or None in live mode) so tests
    can make assertions on calls.
    """
    if live_mode:
        yield MagicNS(driver_cls=None, graphiti_cls=None,
                       llm_builder=None, embedder_builder=None)
    else:
        with patch("vesper.service.Neo4jDriver") as mock_driver, \
             patch("vesper.service.Graphiti") as mock_graphiti_cls, \
             patch("vesper.service._build_llm_client") as mock_llm, \
             patch("vesper.service._build_embedder") as mock_embedder:
            yield MagicNS(driver_cls=mock_driver, graphiti_cls=mock_graphiti_cls,
                           llm_builder=mock_llm, embedder_builder=mock_embedder)


class MagicNS:
    """Simple namespace for fixture return values."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture
def live_config(live_mode):
    """Config tuned for the current test mode.

    Live mode: real Neo4j credentials for the vesper-test database.
    Mock mode: default config (connection details don't matter).
    """
    from vesper.config import VesperConfig, Neo4jConfig

    if live_mode:
        return VesperConfig(
            neo4j=Neo4jConfig(
                uri="neo4j://127.0.0.1:7687",
                user="neo4j",
                password="vesper-test",
                database="vesper-test",
            )
        )
    return VesperConfig()


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Provide a temporary directory for test databases."""
    return tmp_path


@pytest.fixture
def config_env(tmp_data_dir, monkeypatch):
    """Set environment variables pointing to temporary test paths."""
    monkeypatch.setenv("VESPER_ENV", "test")
    monkeypatch.setenv("VESPER_NEO4J__URI", "bolt://localhost:7687")
    monkeypatch.setenv("VESPER_NEO4J__USER", "neo4j")
    monkeypatch.setenv("VESPER_NEO4J__PASSWORD", "")
    monkeypatch.setenv("VESPER_QUEUE__DB_PATH", str(tmp_data_dir / "queue.sqlite"))


@pytest.fixture
def config_yaml(tmp_path):
    """Write a minimal config YAML and return its path."""
    yaml_content = f"""\
neo4j:
  uri: "bolt://localhost:7687"
  user: "neo4j"
  password: ""
  database: "neo4j"

llm:
  provider: "openai"
  model: "gpt-4o-mini"

embedding:
  provider: "openai"
  model: "text-embedding-3-small"

queue:
  db_path: "{tmp_path / 'queue.sqlite'}"
  max_retries: 3

synthesis:
  max_age_hours: 24
  max_delta_changes: 3
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_content)
    return config_file
