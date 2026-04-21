"""Pratyabhijna configuration.

Loads from per-environment YAML + dotenv files, with env var overrides.

Resolution order (highest priority first):
  1. Environment variables (PRATYABHIJNA_ prefix, __ for nesting)
  2. .env.{env} file (secrets — gitignored)
  3. config/{env}.yaml (structural config — version-controlled)
  4. Field defaults in this module

Select the environment with PRATYABHIJNA_ENV (default: "dev").
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
    model: str = "voyage-4"
    api_key: str = ""


class QueueConfig(BaseModel):
    db_path: str = "./data/queue.sqlite"
    max_retries: int = 10
    poll_interval: float = 0.5
    backoff_base_seconds: float = 60.0


class SynthesisThinkingConfig(BaseModel):
    # Adaptive thinking: the model decides when and how much to think.
    # Supported on Claude Opus 4.6 and Sonnet 4.6. The effort parameter
    # is soft guidance (low/medium/high/max); "high" is the default and
    # means Claude almost always thinks. Adaptive mode also enables
    # interleaved thinking between tool calls on Opus 4.6 — important
    # for the synthesizer's tool-use loop.
    enabled: bool = True
    effort: str = "high"


class SynthesisConfig(BaseModel):
    max_age_hours: int = 24
    max_delta_changes: int = 3
    rebuild_delay_hours: float = 2.0
    model: str = "claude-opus-4-6"
    thinking: SynthesisThinkingConfig = Field(default_factory=SynthesisThinkingConfig)
    # Singleton branch for protected-layer proposals awaiting solo review.
    draft_branch: str = "synth/draft"
    # Periodic from-scratch rebuild lands on draft_branch regardless
    # of which layer it touched.
    full_rebuild_cadence_days: int = 30
    # Minimum cluster size after label propagation. Sub-threshold clusters are
    # merged into their best-connected large cluster before community nodes are built.
    min_community_size: int = 4
    # Upper bound on tool-use loop iterations, as a circuit breaker.
    max_iterations: int = 40
    # Bound on how far back the ingestion scan looks.
    ingestion_lookback_days: int | None = 14


class ServerConfig(BaseModel):
    url: str = ""    # Public-facing HTTPS URL (e.g. https://vesper.example.com). Required for HTTP transport + auth.
    port: int = 3000  # Local bind port for streamable-http transport (Caddy proxies externally).


class OAuthConfig(BaseModel):
    """OAuth 2.1 authorization-server settings.

    Enabled automatically whenever ``server.url`` and ``api_key`` are
    both set — OAuth is the only auth model in HTTP mode. The shared
    secret used to log in at ``/login`` is ``api_key``; there is no
    separate password.
    """

    db_path: str = "./data/oauth.sqlite"
    access_token_ttl_seconds: int = 3600              # 1 hour
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days
    auth_code_ttl_seconds: int = 600                  # 10 minutes
    pending_session_ttl_seconds: int = 600            # 10 minutes


class SeedConfig(BaseModel):
    # Reserved for future seed-command options. Currently empty: the
    # seed command creates the subject Person node using config.subject_name
    # and does not read tier prose from disk (files in the subject's repo
    # are canonical, read by bootstrap at request time).
    pass


class ResourcesConfig(BaseModel):
    repo_path: str = ""
    directories: list[str] = []


class PratyabhijnaConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRATYABHIJNA_", env_nested_delimiter="__")

    env: str = "dev"
    subject_name: str = ""
    log_dir: str = "./logs"
    log_level: str = "INFO"
    api_key: str = ""  # Shared secret for OAuth /login. Required when server.url is set.

    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    synthesis: SynthesisConfig = Field(default_factory=SynthesisConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    oauth: OAuthConfig = Field(default_factory=OAuthConfig)
    seed: SeedConfig = Field(default_factory=SeedConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        """Env vars take priority over init kwargs (which come from YAML)."""
        return (
            kwargs["env_settings"],
            kwargs["init_settings"],
        )

    @classmethod
    def from_yaml(cls, path: Path) -> "PratyabhijnaConfig":
        """Load config from a specific YAML file, with env var overrides."""
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls(**data)

    @classmethod
    def from_env(cls, env: str | None = None, root: Path | None = None) -> "PratyabhijnaConfig":
        """Load config for the given environment.

        Reads config/{env}.yaml for structural settings, then loads
        .env.{env} for secrets. Environment variables always win.

        Args:
            env: Environment name (dev/test/prod). Falls back to
                 PRATYABHIJNA_ENV, then "dev".
            root: Server root directory. Defaults to the server/
                  directory containing this package.
        """
        import yaml
        from dotenv import load_dotenv

        env = env or os.getenv("PRATYABHIJNA_ENV", "dev")
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
