"""Vesper configuration.

Loads from YAML file with environment variable overrides.
Env vars are prefixed VESPER_ and use __ for nesting
(e.g. VESPER_LLM__MODEL overrides llm.model).
"""

from pathlib import Path
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class Neo4jConfig(BaseModel):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = ""
    database: str = "neo4j"


class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o-mini"


class EmbeddingConfig(BaseModel):
    provider: str = "openai"
    model: str = "text-embedding-3-small"


class QueueConfig(BaseModel):
    db_path: str = "./data/queue.sqlite"
    max_retries: int = 3


class SynthesisConfig(BaseModel):
    max_age_hours: int = 24
    max_delta_changes: int = 3


class VesperConfig(BaseSettings):
    model_config = {"env_prefix": "VESPER_", "env_nested_delimiter": "__"}

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
        """Load config from YAML, with env var overrides taking precedence."""
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls(**data)
