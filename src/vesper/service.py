"""Graphiti client wrapper and lifecycle management.

Manages the Graphiti client connection, initialization,
and shutdown. Provides the service layer between MCP tools
and graphiti-core.
"""

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver

from vesper.config import VesperConfig
from vesper.entity_types import VESPER_ENTITY_TYPES


def _build_llm_client(config: VesperConfig):
    """Construct LLM client from config."""
    llm = config.llm
    if llm.provider == "anthropic":
        from graphiti_core.llm_client.anthropic_client import AnthropicClient
        from graphiti_core.llm_client.config import LLMConfig

        return AnthropicClient(
            config=LLMConfig(api_key=llm.api_key or None, model=llm.model),
        )
    if llm.provider == "openai":
        from graphiti_core.llm_client.openai_client import OpenAIClient
        from graphiti_core.llm_client.config import LLMConfig

        return OpenAIClient(
            config=LLMConfig(api_key=llm.api_key or None, model=llm.model),
        )
    raise ValueError(f"Unsupported LLM provider: {llm.provider}")


def _build_embedder(config: VesperConfig):
    """Construct embedding client from config."""
    emb = config.embedding
    if emb.provider == "voyageai":
        from graphiti_core.embedder.voyage import VoyageAIEmbedder, VoyageAIEmbedderConfig

        return VoyageAIEmbedder(
            config=VoyageAIEmbedderConfig(
                api_key=emb.api_key or None,
                embedding_model=emb.model,
            ),
        )
    if emb.provider == "openai":
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

        return OpenAIEmbedder(
            config=OpenAIEmbedderConfig(api_key=emb.api_key or None),
        )
    raise ValueError(f"Unsupported embedding provider: {emb.provider}")


def _build_cross_encoder(config: VesperConfig):
    """Construct cross-encoder (reranker) from config.

    Uses the same provider as the embedder — if you're using Voyage
    for embeddings, you get Voyage reranking too.
    """
    emb = config.embedding
    if emb.provider == "voyageai":
        from vesper.reranker import VoyageRerankerClient

        return VoyageRerankerClient(api_key=emb.api_key or None)
    if emb.provider == "openai":
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

        return OpenAIRerankerClient()
    raise ValueError(f"No cross-encoder available for provider: {emb.provider}")


class VesperService:
    """Wraps graphiti-core client with Vesper-specific lifecycle."""

    def __init__(self, config: VesperConfig):
        self.config = config
        self._graphiti = None

    async def start(self):
        """Initialize the Graphiti client and connect to Neo4j."""
        neo4j = self.config.neo4j
        driver = Neo4jDriver(
            uri=neo4j.uri,
            user=neo4j.user,
            password=neo4j.password,
            database=neo4j.database,
        )
        llm_client = _build_llm_client(self.config)
        embedder = _build_embedder(self.config)
        cross_encoder = _build_cross_encoder(self.config)
        self._graphiti = Graphiti(
            graph_driver=driver,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=cross_encoder,
        )

    async def stop(self):
        """Shut down the Graphiti client."""
        self._graphiti = None

    @property
    def is_connected(self) -> bool:
        """Whether the graph DB connection is active."""
        return self._graphiti is not None

    @property
    def entity_types(self) -> dict:
        """Entity type registry for use with add_episode()."""
        return VESPER_ENTITY_TYPES
