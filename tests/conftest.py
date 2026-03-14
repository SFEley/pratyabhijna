"""Shared test fixtures for Vesper memory server."""

import os
from pathlib import Path

import pytest


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
