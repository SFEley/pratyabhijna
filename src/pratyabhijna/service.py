"""Graphiti client wrapper and lifecycle management.

Manages the Graphiti client connection, initialization,
and shutdown. Provides the service layer between MCP tools
and graphiti-core.
"""

from __future__ import annotations

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import EdgeNotFoundError, NodeNotFoundError
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.search.search_config import SearchResults
from graphiti_core.search.search_config_recipes import (
    COMBINED_HYBRID_SEARCH_CROSS_ENCODER,
)
from graphiti_core.search.search_filters import SearchFilters

from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES


def _build_llm_client(config: PratyabhijnaConfig):
    """Construct LLM client from config."""
    llm = config.llm
    if llm.provider == "anthropic":
        from graphiti_core.llm_client.config import LLMConfig

        from pratyabhijna.llm_client import CachingAnthropicClient

        return CachingAnthropicClient(
            config=LLMConfig(api_key=llm.api_key or None, model=llm.model),
            shared_tool_models=list(PRATYABHIJNA_ENTITY_TYPES.values()),
        )
    if llm.provider == "openai":
        from graphiti_core.llm_client.openai_client import OpenAIClient
        from graphiti_core.llm_client.config import LLMConfig

        return OpenAIClient(
            config=LLMConfig(api_key=llm.api_key or None, model=llm.model),
        )
    raise ValueError(f"Unsupported LLM provider: {llm.provider}")


def _build_embedder(config: PratyabhijnaConfig):
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


def _build_cross_encoder(config: PratyabhijnaConfig):
    """Construct cross-encoder (reranker) from config.

    Uses the same provider as the embedder — if you're using Voyage
    for embeddings, you get Voyage reranking too.
    """
    emb = config.embedding
    if emb.provider == "voyageai":
        from pratyabhijna.reranker import VoyageRerankerClient

        return VoyageRerankerClient(api_key=emb.api_key or None)
    if emb.provider == "openai":
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

        return OpenAIRerankerClient()
    raise ValueError(f"No cross-encoder available for provider: {emb.provider}")


class PratyabhijnaService:
    """Wraps graphiti-core client with Pratyabhijna-specific lifecycle."""

    def __init__(self, config: PratyabhijnaConfig):
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
        return PRATYABHIJNA_ENTITY_TYPES

    # --- Read operations (Phase 4) ---

    async def recall(
        self,
        query: str,
        search_filter: SearchFilters | None = None,
        num_results: int = 10,
    ) -> SearchResults:
        """Hybrid search across the knowledge graph.

        Uses Graphiti's advanced search with cross-encoder reranking.
        Returns full SearchResults with edges, nodes, and scores.
        """
        return await self._graphiti.search_(
            query=query,
            config=COMBINED_HYBRID_SEARCH_CROSS_ENCODER,
            search_filter=search_filter,
        )

    async def get_entity_by_name(self, name: str) -> EntityNode | None:
        """Find an entity node by name via search.

        Returns the best match or None if nothing relevant found.
        Uses semantic search so fuzzy matching works.

        Note: if no exact name match is found, falls back to the top
        semantic result. This means a query for an entity that doesn't
        exist may return the closest match rather than None. Callers
        needing strict matching should check the returned node's name.
        """
        results = await self._graphiti.search_(
            query=name,
            config=COMBINED_HYBRID_SEARCH_CROSS_ENCODER,
            search_filter=SearchFilters(node_labels=None),
        )
        # Look for an exact or near-exact name match in returned nodes
        for node in results.nodes:
            if node.name.lower() == name.lower():
                return node
        # Fall back to best match if any nodes returned
        return results.nodes[0] if results.nodes else None

    async def get_entity_by_uuid(self, uuid: str) -> EntityNode:
        """Get an entity node by UUID. Raises NodeNotFoundError."""
        return await EntityNode.get_by_uuid(self._graphiti.driver, uuid)

    async def get_edge(self, uuid: str) -> EntityEdge:
        """Get an entity edge by UUID. Raises EdgeNotFoundError."""
        return await EntityEdge.get_by_uuid(self._graphiti.driver, uuid)

    async def get_edges_for_node(self, node_uuid: str) -> list[EntityEdge]:
        """Get all entity edges connected to a node (incoming + outgoing)."""
        driver = self._graphiti.driver
        return await driver.entity_edge_ops.get_by_node_uuid(
            driver, node_uuid
        )

    async def get_episodes_for_node(self, node_uuid: str) -> list[EpisodicNode]:
        """Get all episodes that mention a given entity node."""
        return await EpisodicNode.get_by_entity_node_uuid(
            self._graphiti.driver, node_uuid
        )

    async def get_episodes_by_uuids(self, uuids: list[str]) -> list[EpisodicNode]:
        """Get episodic nodes by their UUIDs."""
        if not uuids:
            return []
        return await EpisodicNode.get_by_uuids(self._graphiti.driver, uuids)

    async def get_latest_episode_by_name(self, name: str) -> EpisodicNode | None:
        """Return the most-recently-created episode whose name matches exactly.

        Used by the synthesizer's ingestion pass to decide whether a repo
        file has already been ingested (and whether it's been revised
        since). Episodes ingested from files use the repo-relative path
        as ``name`` — e.g. ``writing/solo-22-visible-life.md`` — so an
        exact-match lookup is correct.

        Returns None if no episode has that name.
        """
        driver = self._graphiti.driver
        records, _, _ = await driver.execute_query(
            """
            MATCH (e:Episodic {name: $name})
            RETURN e.uuid AS uuid, e.created_at AS created_at
            ORDER BY e.created_at DESC
            LIMIT 1
            """,
            name=name,
            routing_="r",
        )
        if not records:
            return None
        return await EpisodicNode.get_by_uuid(driver, records[0]["uuid"])

    # --- Graph-level counts (used by status) ---------------------------------

    async def count_nodes_total(self) -> int:
        """Total number of nodes across all labels."""
        records, _, _ = await self._graphiti.driver.execute_query(
            "MATCH (n) RETURN count(n) AS n", routing_="r"
        )
        return records[0]["n"] if records else 0

    async def count_nodes_by_label(self) -> dict[str, int]:
        """Node counts per label.

        Graphiti entity nodes carry multiple labels (e.g. ``Entity`` plus
        custom types like ``Person``), so these counts do not sum to
        ``count_nodes_total`` — that's by design.
        """
        records, _, _ = await self._graphiti.driver.execute_query(
            "MATCH (n) UNWIND labels(n) AS label "
            "RETURN label, count(*) AS n",
            routing_="r",
        )
        return {r["label"]: r["n"] for r in records}

    async def count_edges_total(self) -> int:
        """Total number of relationships across all types."""
        records, _, _ = await self._graphiti.driver.execute_query(
            "MATCH ()-[r]->() RETURN count(r) AS n", routing_="r"
        )
        return records[0]["n"] if records else 0

    async def count_edges_by_type(self) -> dict[str, int]:
        """Relationship counts per Neo4j relationship type.

        Uses the structural type (``RELATES_TO``, ``MENTIONS``, etc.), not
        the semantic edge name (``values``, ``works_on``). The type is
        cheap to group on; the name would require reading a property off
        every edge.
        """
        records, _, _ = await self._graphiti.driver.execute_query(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS n",
            routing_="r",
        )
        return {r["t"]: r["n"] for r in records}

    async def count_supersessions(self) -> int:
        """Count edges that have been superseded in the temporal model.

        Graphiti sets ``invalid_at`` on an ``EntityEdge`` when a newer
        episode contradicts it — this is the count of facts the graph
        currently records as no-longer-true.
        """
        records, _, _ = await self._graphiti.driver.execute_query(
            "MATCH ()-[r]->() WHERE r.invalid_at IS NOT NULL "
            "RETURN count(r) AS n",
            routing_="r",
        )
        return records[0]["n"] if records else 0
