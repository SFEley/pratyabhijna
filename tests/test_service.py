"""Tests for PratyabhijnaService — Graphiti client initialization with Neo4j.

Verifies that the service correctly initializes a Graphiti client using the
Neo4j graph database backend. In mock mode (default), driver and Graphiti are
patched. In live mode (--live or PRATYABHIJNA_TEST_LIVE=1), tests hit a real Neo4j
instance and verify the actual connection.
"""

import pytest


class TestServiceLifecycle:
    """PratyabhijnaService lifecycle: start, connect, stop."""

    def test_service_not_connected_before_start(self):
        """is_connected is False before start() is called."""
        from pratyabhijna.config import PratyabhijnaConfig
        from pratyabhijna.service import PratyabhijnaService

        service = PratyabhijnaService(PratyabhijnaConfig())
        assert not service.is_connected

    @pytest.mark.asyncio
    async def test_service_connected_after_start(self, mock_graphiti, live_config):
        """is_connected is True after start() completes."""
        from pratyabhijna.service import PratyabhijnaService

        service = PratyabhijnaService(live_config)
        await service.start()
        assert service.is_connected
        await service.stop()

    @pytest.mark.asyncio
    async def test_service_not_connected_after_stop(self, mock_graphiti, live_config):
        """is_connected is False after stop() is called."""
        from pratyabhijna.service import PratyabhijnaService

        service = PratyabhijnaService(live_config)
        await service.start()
        await service.stop()
        assert not service.is_connected


class TestGraphitiInitialization:
    """PratyabhijnaService initializes Graphiti with the correct driver and config."""

    @pytest.mark.asyncio
    async def test_graphiti_initialized_with_neo4j_driver(self, mock_graphiti, live_config, live_mode):
        """start() creates a Neo4jDriver and passes it to Graphiti."""
        from pratyabhijna.service import PratyabhijnaService

        service = PratyabhijnaService(live_config)
        await service.start()

        if not live_mode:
            mock_graphiti.driver_cls.assert_called_once()
            mock_graphiti.graphiti_cls.assert_called_once_with(
                graph_driver=mock_graphiti.driver_cls.return_value,
                llm_client=mock_graphiti.llm_builder.return_value,
                embedder=mock_graphiti.embedder_builder.return_value,
                cross_encoder=mock_graphiti.cross_encoder_builder.return_value,
            )
        else:
            assert service.is_connected

        await service.stop()

    @pytest.mark.asyncio
    async def test_neo4j_driver_uses_config_credentials(self, mock_graphiti, live_mode, monkeypatch):
        """Neo4jDriver is initialized with connection params from config."""
        import os
        from pratyabhijna.config import PratyabhijnaConfig, Neo4jConfig
        from pratyabhijna.service import PratyabhijnaService

        # Clear any PRATYABHIJNA_ env vars so pydantic-settings doesn't override init kwargs
        for key in list(os.environ):
            if key.startswith("PRATYABHIJNA_"):
                monkeypatch.delenv(key, raising=False)

        config = PratyabhijnaConfig(
            neo4j=Neo4jConfig(
                uri="bolt://testhost:7687",
                user="testuser",
                password="testpass",
                database="testdb",
            )
        )

        if live_mode:
            pytest.skip("Credential wiring test only meaningful with mocks")

        service = PratyabhijnaService(config)
        await service.start()

        mock_graphiti.driver_cls.assert_called_once_with(
            uri="bolt://testhost:7687",
            user="testuser",
            password="testpass",
            database="testdb",
        )
        await service.stop()

    @pytest.mark.asyncio
    async def test_service_exposes_entity_types(self, mock_graphiti, live_config):
        """PratyabhijnaService exposes entity_types for use with add_episode()."""
        from pratyabhijna.service import PratyabhijnaService
        from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES

        service = PratyabhijnaService(live_config)
        await service.start()
        assert service.entity_types is PRATYABHIJNA_ENTITY_TYPES
        await service.stop()


class TestEpisodeHashIndex:
    """The Stage 0 idempotency index is created on service start."""

    @pytest.mark.asyncio
    async def test_index_creation_call_made(self, mock_graphiti, live_config, live_mode):
        """start() issues a CREATE INDEX ... IF NOT EXISTS call.

        In mock mode, asserts on the call. In live mode, asserts the index
        is actually present in Neo4j.
        """
        from pratyabhijna.service import PratyabhijnaService

        service = PratyabhijnaService(live_config)
        await service.start()
        try:
            if not live_mode:
                # Mock mode: the Graphiti instance is a MagicMock, so its driver
                # captured every execute_query call. Search for the index DDL.
                ctor = mock_graphiti.graphiti_cls
                graphiti_instance = ctor.return_value
                calls = graphiti_instance.driver.execute_query.await_args_list
                index_calls = [
                    c for c in calls
                    if "idx_episodic_group_hash" in (c.args[0] if c.args else "")
                ]
                assert index_calls, "expected CREATE INDEX call with idx_episodic_group_hash"
            else:
                records, _, _ = await service._graphiti.driver.execute_query(
                    "SHOW INDEXES YIELD name WHERE name = 'idx_episodic_group_hash' "
                    "RETURN name"
                )
                assert len(records) == 1
        finally:
            await service.stop()
