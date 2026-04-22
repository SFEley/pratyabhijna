"""Graphiti client wrapper and lifecycle management.

Manages the Graphiti client connection, initialization,
and shutdown. Provides the service layer between MCP tools
and graphiti-core.
"""

from __future__ import annotations

import asyncio

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import EdgeNotFoundError, NodeNotFoundError
from graphiti_core.nodes import CommunityNode, EntityNode, EpisodicNode
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

    async def build_communities(
        self,
        group_ids: list[str],
        min_community_size: int | None = None,
    ) -> tuple:
        """Rebuild Community nodes from scratch for the given group IDs."""
        from pratyabhijna.communities import (
            DEFAULT_MIN_COMMUNITY_SIZE,
            DEFAULT_SAMPLE_SIZE,
            build_communities,
        )

        return await build_communities(
            self._graphiti.driver,
            self._graphiti.llm_client,
            group_ids,
            sample_size=DEFAULT_SAMPLE_SIZE,
            min_community_size=min_community_size if min_community_size is not None else DEFAULT_MIN_COMMUNITY_SIZE,
        )

    async def remove_episode(self, uuid: str) -> None:
        """Delete an episode and its orphaned edges/nodes from the graph.

        Delegates to graphiti.remove_episode(), which:
        - Deletes entity edges originated by this episode.
        - Deletes entity nodes referenced only by this episode.
        - Deletes the episodic node itself.

        Raises NodeNotFoundError if the episode doesn't exist.
        """
        await self._graphiti.remove_episode(uuid)

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

    # --- Community queries (used by communities tool) -------------------------

    async def get_all_communities(self, group_id: str) -> list[CommunityNode]:
        """Return all Community nodes for the given group."""
        return await CommunityNode.get_by_group_ids(self._graphiti.driver, [group_id])

    async def get_community_member_counts(
        self, community_uuids: list[str]
    ) -> dict[str, int]:
        """Return member counts per community UUID in one query."""
        if not community_uuids:
            return {}
        records, _, _ = await self._graphiti.driver.execute_query(
            "MATCH (c:Community)-[:HAS_MEMBER]->(e) "
            "WHERE c.uuid IN $uuids "
            "RETURN c.uuid AS uuid, count(e) AS member_count",
            uuids=community_uuids,
            routing_="r",
        )
        return {r["uuid"]: r["member_count"] for r in records}

    async def get_community_by_name(
        self, name: str, group_id: str
    ) -> CommunityNode | None:
        """Find a community by case-insensitive name. Returns None if not found."""
        driver = self._graphiti.driver
        records, _, _ = await driver.execute_query(
            "MATCH (c:Community {group_id: $group_id}) "
            "WHERE toLower(c.name) = toLower($name) "
            "RETURN c.uuid AS uuid "
            "LIMIT 1",
            group_id=group_id,
            name=name,
            routing_="r",
        )
        if not records:
            return None
        return await CommunityNode.get_by_uuid(driver, records[0]["uuid"])

    async def get_community_members(self, community_uuid: str) -> list[dict]:
        """Return member entity stubs for a community, ordered by name."""
        records, _, _ = await self._graphiti.driver.execute_query(
            "MATCH (c:Community {uuid: $uuid})-[:HAS_MEMBER]->(e:Entity) "
            "RETURN e.uuid AS uuid, e.name AS name, labels(e) AS labels, "
            "e.summary AS summary "
            "ORDER BY e.name",
            uuid=community_uuid,
            routing_="r",
        )
        return [
            {
                "uuid": r["uuid"],
                "name": r["name"],
                "labels": r["labels"],
                "summary": r["summary"],
            }
            for r in records
        ]

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

    # --- General query execution (used by the query tool) --------------------

    async def execute_read_query(
        self,
        cypher: str,
        params: dict | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Execute a read-only Cypher query and return serialized rows.

        Rows are truncated to ``limit`` regardless of what the query asks
        for — this is a defence-in-depth cap on context size. Values in
        rows are normalized to JSON-safe types (Nodes → dicts, datetimes
        → ISO strings) via ``_serialize_cypher_value``.

        Does not enforce read-only semantics itself; the caller (the
        query tool's read handler) applies a regex gate before calling
        this method.
        """
        records, _, _ = await self._graphiti.driver.execute_query(
            cypher,
            **(params or {}),
            routing_="r",
        )
        rows: list[dict] = []
        for record in records[:limit]:
            rows.append({k: _serialize_cypher_value(v) for k, v in record.items()})
        return rows

    async def execute_write_query(
        self,
        cypher: str,
        params: dict | None = None,
    ) -> dict:
        """Execute a Cypher write query and return a counters summary.

        Returns the keys available on the Neo4j ``SummaryCounters`` object:
        created / deleted nodes and relationships, properties set, labels
        added / removed. Any counter the driver doesn't expose is omitted
        rather than defaulted to 0.
        """
        _, summary, _ = await self._graphiti.driver.execute_query(
            cypher,
            **(params or {}),
        )
        counters = getattr(summary, "counters", None)
        if counters is None:
            return {}
        keys = [
            "nodes_created",
            "nodes_deleted",
            "relationships_created",
            "relationships_deleted",
            "properties_set",
            "labels_added",
            "labels_removed",
            "indexes_added",
            "indexes_removed",
            "constraints_added",
            "constraints_removed",
        ]
        out: dict = {}
        for key in keys:
            value = getattr(counters, key, None)
            if value is not None:
                out[key] = value
        return out

    async def introspect_schema(self) -> str:
        """Return a Markdown snapshot of labels, relationship types, and property keys.

        Runs three introspection queries in parallel. The output is
        intended for embedding in an LLM system prompt — it is a single
        text block, not structured data. If the agent needs property
        detail per label, it can issue targeted reads itself.
        """
        driver = self._graphiti.driver

        async def _labels() -> list[str]:
            records, _, _ = await driver.execute_query(
                "CALL db.labels() YIELD label RETURN label", routing_="r"
            )
            return sorted(r["label"] for r in records)

        async def _rel_types() -> list[str]:
            records, _, _ = await driver.execute_query(
                "CALL db.relationshipTypes() YIELD relationshipType "
                "RETURN relationshipType",
                routing_="r",
            )
            return sorted(r["relationshipType"] for r in records)

        async def _prop_keys() -> list[str]:
            records, _, _ = await driver.execute_query(
                "CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey",
                routing_="r",
            )
            return sorted(r["propertyKey"] for r in records)

        labels, rel_types, prop_keys = await asyncio.gather(
            _labels(), _rel_types(), _prop_keys()
        )

        def _fmt(items: list[str]) -> str:
            return ", ".join(items) if items else "(none)"

        return "\n".join(
            [
                "### Node labels",
                _fmt(labels),
                "",
                "### Relationship types",
                _fmt(rel_types),
                "",
                "### Property keys (union across nodes and relationships)",
                _fmt(prop_keys),
                "",
                "Property sets vary by label. For schema detail on a specific "
                "label, query `MATCH (n:<Label>) WITH n LIMIT 1 RETURN keys(n)`.",
            ]
        )


def _serialize_cypher_value(v):
    """Recursively convert a Neo4j value to a JSON-safe structure.

    Nodes become {_type: node, element_id, labels, properties}; relationships
    become {_type: relationship, element_id, type, start, end, properties};
    datetimes become ISO-8601 strings. Primitives pass through.
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_serialize_cypher_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _serialize_cypher_value(x) for k, x in v.items()}
    if hasattr(v, "labels") and hasattr(v, "element_id"):
        return {
            "_type": "node",
            "element_id": v.element_id,
            "labels": list(v.labels),
            "properties": {
                k: _serialize_cypher_value(x) for k, x in dict(v).items()
            },
        }
    if hasattr(v, "type") and hasattr(v, "start_node"):
        start = getattr(v, "start_node", None)
        end = getattr(v, "end_node", None)
        return {
            "_type": "relationship",
            "element_id": getattr(v, "element_id", None),
            "type": v.type,
            "start": getattr(start, "element_id", None) if start else None,
            "end": getattr(end, "element_id", None) if end else None,
            "properties": {
                k: _serialize_cypher_value(x) for k, x in dict(v).items()
            },
        }
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)
