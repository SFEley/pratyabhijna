"""Vesper configuration.

Loads from per-environment YAML + dotenv files, with env var overrides.

Resolution order (highest priority first):
  1. Environment variables (VESPER_ prefix, __ for nesting)
  2. .env.{env} file (secrets — gitignored)
  3. config/{env}.yaml (structural config — version-controlled)
  4. Field defaults in this module

Select the environment with VESPER_ENV (default: "dev").
"""

import os
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Root of the server package (server/)
_SERVER_ROOT = Path(__file__).resolve().parent.parent.parent


class Neo4jConfig(BaseModel):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = ""
    database: str = "neo4j"


class LLMConfig(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""


class EmbeddingConfig(BaseModel):
    provider: str = "voyageai"
    model: str = "voyage-3"
    api_key: str = ""


class QueueConfig(BaseModel):
    db_path: str = "./data/queue.sqlite"
    max_retries: int = 3


class SynthesisConfig(BaseModel):
    max_age_hours: int = 24
    max_delta_changes: int = 3


class VesperConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VESPER_", env_nested_delimiter="__")

    env: str = "dev"
    log_dir: str = "./logs"
    log_level: str = "INFO"

    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    synthesis: SynthesisConfig = Field(default_factory=SynthesisConfig)

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        """Env vars take priority over init kwargs (which come from YAML)."""
        return (
            kwargs["env_settings"],
            kwargs["init_settings"],
        )

    @classmethod
    def from_yaml(cls, path: Path) -> "VesperConfig":
        """Load config from a specific YAML file, with env var overrides."""
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls(**data)

    @classmethod
    def from_env(cls, env: str | None = None, root: Path | None = None) -> "VesperConfig":
        """Load config for the given environment.

        Reads config/{env}.yaml for structural settings, then loads
        .env.{env} for secrets. Environment variables always win.

        Args:
            env: Environment name (dev/test/prod). Falls back to
                 VESPER_ENV, then "dev".
            root: Server root directory. Defaults to the server/
                  directory containing this package.
        """
        import yaml
        from dotenv import load_dotenv

        env = env or os.getenv("VESPER_ENV", "dev")
        root = root or _SERVER_ROOT

        # Load secrets from .env.{env} into the process environment.
        # Existing env vars are NOT overwritten (they take priority).
        dotenv_path = root / f".env.{env}"
        if dotenv_path.exists():
            load_dotenv(dotenv_path, override=False)

        # Load structural config from YAML.
        yaml_path = root / "config" / f"{env}.yaml"
        if yaml_path.exists():
            with open(yaml_path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        data["env"] = env
        return cls(**data)
