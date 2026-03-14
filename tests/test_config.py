"""Tests for Vesper configuration loading.

Verifies that config loads from YAML with env var overrides,
per Phase 1 requirements.
"""

import os
from pathlib import Path
import pytest


class TestConfigLoading:
    """Config loads from YAML file with sensible defaults."""

    def test_loads_from_yaml(self, config_yaml):
        """Config reads values from a YAML file."""
        from vesper.config import VesperConfig

        config = VesperConfig.from_yaml(config_yaml)
        assert config.neo4j.uri == "bolt://localhost:7687"
        assert config.neo4j.user == "neo4j"

    def test_has_llm_settings(self, config_yaml):
        """Config includes LLM provider and model."""
        from vesper.config import VesperConfig

        config = VesperConfig.from_yaml(config_yaml)
        assert config.llm.provider == "openai"
        assert config.llm.model == "gpt-4o-mini"

    def test_has_embedding_settings(self, config_yaml):
        """Config includes embedding provider and model."""
        from vesper.config import VesperConfig

        config = VesperConfig.from_yaml(config_yaml)
        assert config.embedding.provider == "openai"
        assert config.embedding.model == "text-embedding-3-small"

    def test_has_queue_settings(self, config_yaml):
        """Config includes queue persistence path and retry limit."""
        from vesper.config import VesperConfig

        config = VesperConfig.from_yaml(config_yaml)
        assert config.queue.db_path is not None
        assert config.queue.max_retries == 3

    def test_has_synthesis_settings(self, config_yaml):
        """Config includes synthesis rebuild thresholds."""
        from vesper.config import VesperConfig

        config = VesperConfig.from_yaml(config_yaml)
        assert config.synthesis.max_age_hours == 24
        assert config.synthesis.max_delta_changes == 3

    def test_initialization_with_args(self):
        del os.environ["VESPER_ENV"]  # Ensure no env var interference
        """Config can be initialized directly with arguments."""
        from vesper.config import VesperConfig

        config = VesperConfig(env="prod", log_dir="/var/log/vesper")
        assert config.env == "prod"


class TestConfigEnvOverrides:
    """Environment variables override YAML values."""

    def test_env_overrides_neo4j_uri(self, config_yaml, monkeypatch):
        """VESPER_NEO4J__URI overrides the YAML neo4j.uri."""
        from vesper.config import VesperConfig

        monkeypatch.setenv("VESPER_NEO4J__URI", "bolt://remotehost:7687")
        config = VesperConfig.from_yaml(config_yaml)
        assert config.neo4j.uri == "bolt://remotehost:7687"

    def test_env_overrides_nested_value(self, config_yaml, monkeypatch):
        """VESPER_LLM__MODEL overrides the nested llm.model."""
        from vesper.config import VesperConfig

        monkeypatch.setenv("VESPER_LLM__MODEL", "gpt-4o")
        config = VesperConfig.from_yaml(config_yaml)
        assert config.llm.model == "gpt-4o"


class TestConfigDefaults:
    """Config provides defaults when no YAML or env vars are set."""

    def test_default_config_loads(self, monkeypatch):
        """Config can be created with defaults alone (no YAML)."""
        from vesper.config import VesperConfig

        # Clear any env vars that might interfere
        for key in list(os.environ):
            if key.startswith("VESPER_"):
                monkeypatch.delenv(key, raising=False)

        config = VesperConfig()
        assert config.neo4j.uri == "bolt://localhost:7687"
        assert config.llm.provider == "openai"
