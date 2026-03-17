"""Tests for VesperService — Graphiti client initialization with Neo4j.

Verifies that the service correctly initializes a Graphiti client using the
Neo4j graph database backend. In mock mode (default), driver and Graphiti are
patched. In live mode (--live or VESPER_TEST_LIVE=1), tests hit a real Neo4j
instance and verify the actual connection.
"""

import pytest


class TestServiceLifecycle:
    """VesperService lifecycle: start, connect, stop."""

    def test_service_not_connected_before_start(self):
        """is_connected is False before start() is called."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService

        service = VesperService(VesperConfig())
        assert not service.is_connected

    @pytest.mark.asyncio
    async def test_service_connected_after_start(self, mock_graphiti, live_config):
        """is_connected is True after start() completes."""
        from vesper.service import VesperService

        service = VesperService(live_config)
        await service.start()
        assert service.is_connected
        await service.stop()

    @pytest.mark.asyncio
    async def test_service_not_connected_after_stop(self, mock_graphiti, live_config):
        """is_connected is False after stop() is called."""
        from vesper.service import VesperService

        service = VesperService(live_config)
        await service.start()
        await service.stop()
        assert not service.is_connected


class TestGraphitiInitialization:
    """VesperService initializes Graphiti with the correct driver and config."""

    @pytest.mark.asyncio
    async def test_graphiti_initialized_with_neo4j_driver(self, mock_graphiti, live_config, live_mode):
        """start() creates a Neo4jDriver and passes it to Graphiti."""
        from vesper.service import VesperService

        service = VesperService(live_config)
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
        from vesper.config import VesperConfig, Neo4jConfig
        from vesper.service import VesperService

        # Clear any VESPER_ env vars so pydantic-settings doesn't override init kwargs
        for key in list(os.environ):
            if key.startswith("VESPER_"):
                monkeypatch.delenv(key, raising=False)

        config = VesperConfig(
            neo4j=Neo4jConfig(
                uri="bolt://testhost:7687",
                user="testuser",
                password="testpass",
                database="testdb",
            )
        )

        if live_mode:
            pytest.skip("Credential wiring test only meaningful with mocks")

        service = VesperService(config)
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
        """VesperService exposes entity_types for use with add_episode()."""
        from vesper.service import VesperService
        from vesper.entity_types import VESPER_ENTITY_TYPES

        service = VesperService(live_config)
        await service.start()
        assert service.entity_types is VESPER_ENTITY_TYPES
        await service.stop()
