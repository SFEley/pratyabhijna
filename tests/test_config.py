"""Tests for Pratyabhijna configuration loading.

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
        from pratyabhijna.config import PratyabhijnaConfig

        config = PratyabhijnaConfig.from_yaml(config_yaml)
        assert config.neo4j.uri == "bolt://localhost:7687"
        assert config.neo4j.user == "neo4j"

    def test_has_llm_settings(self, config_yaml):
        """Config includes LLM provider and model."""
        from pratyabhijna.config import PratyabhijnaConfig

        config = PratyabhijnaConfig.from_yaml(config_yaml)
        assert config.llm.provider == "openai"
        assert config.llm.model == "gpt-4o-mini"

    def test_has_embedding_settings(self, config_yaml):
        """Config includes embedding provider and model."""
        from pratyabhijna.config import PratyabhijnaConfig

        config = PratyabhijnaConfig.from_yaml(config_yaml)
        assert config.embedding.provider == "openai"
        assert config.embedding.model == "text-embedding-3-small"

    def test_has_queue_settings(self, config_yaml):
        """Config includes queue persistence path and retry limit."""
        from pratyabhijna.config import PratyabhijnaConfig

        config = PratyabhijnaConfig.from_yaml(config_yaml)
        assert config.queue.db_path is not None
        assert config.queue.max_retries == 3

    def test_has_synthesis_settings(self, config_yaml):
        """Config includes synthesis rebuild thresholds."""
        from pratyabhijna.config import PratyabhijnaConfig

        config = PratyabhijnaConfig.from_yaml(config_yaml)
        assert config.synthesis.max_age_hours == 24
        assert config.synthesis.max_delta_changes == 3

    def test_subject_name_from_yaml(self, tmp_path, monkeypatch):
        """subject_name set in YAML is loaded correctly."""
        from pratyabhijna.config import PratyabhijnaConfig

        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        yaml_content = 'subject_name: "Aria"\nneo4j:\n  uri: "bolt://localhost:7687"\n'
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_content)

        config = PratyabhijnaConfig.from_yaml(config_file)
        assert config.subject_name == "Aria"

    def test_initialization_with_args(self, monkeypatch):
        """Config can be initialized directly with arguments."""
        monkeypatch.delenv("PRATYABHIJNA_ENV", raising=False)
        from pratyabhijna.config import PratyabhijnaConfig

        config = PratyabhijnaConfig(env="prod", log_dir="/var/log/pratyabhijna")
        assert config.env == "prod"


class TestConfigEnvOverrides:
    """Environment variables override YAML values."""

    def test_env_overrides_neo4j_uri(self, config_yaml, monkeypatch):
        """PRATYABHIJNA_NEO4J__URI overrides the YAML neo4j.uri."""
        from pratyabhijna.config import PratyabhijnaConfig

        monkeypatch.setenv("PRATYABHIJNA_NEO4J__URI", "bolt://remotehost:7687")
        config = PratyabhijnaConfig.from_yaml(config_yaml)
        assert config.neo4j.uri == "bolt://remotehost:7687"

    def test_env_overrides_nested_value(self, config_yaml, monkeypatch):
        """PRATYABHIJNA_LLM__MODEL overrides the nested llm.model."""
        from pratyabhijna.config import PratyabhijnaConfig

        monkeypatch.setenv("PRATYABHIJNA_LLM__MODEL", "gpt-4o")
        config = PratyabhijnaConfig.from_yaml(config_yaml)
        assert config.llm.model == "gpt-4o"

    def test_env_overrides_subject_name(self, config_yaml, monkeypatch):
        """PRATYABHIJNA_SUBJECT_NAME overrides the configured subject_name."""
        from pratyabhijna.config import PratyabhijnaConfig

        monkeypatch.setenv("PRATYABHIJNA_SUBJECT_NAME", "Aria")
        config = PratyabhijnaConfig.from_yaml(config_yaml)
        assert config.subject_name == "Aria"


class TestConfigFromEnv:
    """from_env() loads YAML + dotenv for the selected environment."""

    def test_loads_yaml_for_environment(self, tmp_path, monkeypatch):
        """from_env() reads config/{env}.yaml."""
        from pratyabhijna.config import PratyabhijnaConfig

        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "test.yaml").write_text(
            'neo4j:\n  database: "from-yaml"\n'
        )

        config = PratyabhijnaConfig.from_env(env="test", root=tmp_path)
        assert config.env == "test"
        assert config.neo4j.database == "from-yaml"

    def test_loads_dotenv_secrets(self, tmp_path, monkeypatch):
        """from_env() reads .env.{env} for secrets."""
        from pratyabhijna.config import PratyabhijnaConfig

        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        # Pre-register key so monkeypatch cleans up after load_dotenv sets it
        monkeypatch.delenv("PRATYABHIJNA_NEO4J__PASSWORD", raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "dev.yaml").write_text("")
        (tmp_path / ".env.dev").write_text(
            "PRATYABHIJNA_NEO4J__PASSWORD=secret123\n"
        )

        config = PratyabhijnaConfig.from_env(env="dev", root=tmp_path)
        assert config.neo4j.password == "secret123"

    def test_env_vars_override_dotenv(self, tmp_path, monkeypatch):
        """Real env vars take priority over .env.{env} values."""
        from pratyabhijna.config import PratyabhijnaConfig

        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "dev.yaml").write_text("")
        (tmp_path / ".env.dev").write_text(
            "PRATYABHIJNA_NEO4J__PASSWORD=from-dotenv\n"
        )
        # setenv already tracks the key for cleanup
        monkeypatch.setenv("PRATYABHIJNA_NEO4J__PASSWORD", "from-env")

        config = PratyabhijnaConfig.from_env(env="dev", root=tmp_path)
        assert config.neo4j.password == "from-env"

    def test_defaults_to_pratyabhijna_env(self, tmp_path, monkeypatch):
        """from_env() reads PRATYABHIJNA_ENV when no env argument is given."""
        from pratyabhijna.config import PratyabhijnaConfig

        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        monkeypatch.setenv("PRATYABHIJNA_ENV", "prod")

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "prod.yaml").write_text(
            'neo4j:\n  database: "pratyabhijna-prod"\n'
        )

        config = PratyabhijnaConfig.from_env(root=tmp_path)
        assert config.env == "prod"
        assert config.neo4j.database == "pratyabhijna-prod"

    def test_falls_back_to_dev(self, tmp_path, monkeypatch):
        """from_env() defaults to 'dev' when PRATYABHIJNA_ENV is not set."""
        from pratyabhijna.config import PratyabhijnaConfig

        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "dev.yaml").write_text("")

        config = PratyabhijnaConfig.from_env(root=tmp_path)
        assert config.env == "dev"

    def test_works_without_dotenv_file(self, tmp_path, monkeypatch):
        """from_env() works when .env.{env} doesn't exist."""
        from pratyabhijna.config import PratyabhijnaConfig

        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "dev.yaml").write_text("")

        config = PratyabhijnaConfig.from_env(env="dev", root=tmp_path)
        assert config.neo4j.password == ""


class TestConfigDefaults:
    """Config provides defaults when no YAML or env vars are set."""

    def test_default_config_loads(self, monkeypatch):
        """Config can be created with defaults alone (no YAML)."""
        from pratyabhijna.config import PratyabhijnaConfig

        # Clear any env vars that might interfere
        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        config = PratyabhijnaConfig()
        assert config.neo4j.uri == "bolt://localhost:7687"
        assert config.llm.provider == "anthropic"

    def test_default_subject_name(self, monkeypatch):
        """Default subject_name is 'Vesper' for backward compatibility."""
        from pratyabhijna.config import PratyabhijnaConfig

        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        config = PratyabhijnaConfig()
        assert config.subject_name == "Vesper"

    def test_default_synthesis_rebuild_delay(self, monkeypatch):
        """Default synthesis rebuild delay is 2 hours."""
        from pratyabhijna.config import PratyabhijnaConfig

        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        config = PratyabhijnaConfig()
        assert config.synthesis.rebuild_delay_hours == 2.0
