"""Tests for VesperService — Graphiti client initialization with Neo4j.

Verifies that the service correctly initializes a Graphiti client using the
Neo4j graph database backend. Driver and Graphiti are mocked so these tests
do not require a running Neo4j instance.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestServiceLifecycle:
    """VesperService lifecycle: start, connect, stop."""

    def test_service_not_connected_before_start(self):
        """is_connected is False before start() is called."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService

        service = VesperService(VesperConfig())
        assert not service.is_connected

    @pytest.mark.asyncio
    async def test_service_connected_after_start(self):
        """is_connected is True after start() completes."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService

        service = VesperService(VesperConfig())
        with patch("vesper.service.Neo4jDriver"), patch("vesper.service.Graphiti"):
            await service.start()
        assert service.is_connected

    @pytest.mark.asyncio
    async def test_service_not_connected_after_stop(self):
        """is_connected is False after stop() is called."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService

        service = VesperService(VesperConfig())
        with patch("vesper.service.Neo4jDriver"), patch("vesper.service.Graphiti"):
            await service.start()
            await service.stop()
        assert not service.is_connected


class TestGraphitiInitialization:
    """VesperService initializes Graphiti with the correct driver and config."""

    @pytest.mark.asyncio
    async def test_graphiti_initialized_with_neo4j_driver(self):
        """start() creates a Neo4jDriver and passes it to Graphiti."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService

        service = VesperService(VesperConfig())
        with patch("vesper.service.Neo4jDriver") as mock_driver_cls, \
             patch("vesper.service.Graphiti") as mock_graphiti_cls:
            await service.start()

        mock_driver_cls.assert_called_once()
        mock_graphiti_cls.assert_called_once_with(
            graph_driver=mock_driver_cls.return_value
        )

    @pytest.mark.asyncio
    async def test_neo4j_driver_uses_config_credentials(self):
        """Neo4jDriver is initialized with connection params from config."""
        from vesper.config import VesperConfig, Neo4jConfig
        from vesper.service import VesperService

        config = VesperConfig(
            neo4j=Neo4jConfig(
                uri="bolt://testhost:7687",
                user="testuser",
                password="testpass",
                database="testdb",
            )
        )
        service = VesperService(config)
        with patch("vesper.service.Neo4jDriver") as mock_driver_cls, \
             patch("vesper.service.Graphiti"):
            await service.start()

        mock_driver_cls.assert_called_once_with(
            uri="bolt://testhost:7687",
            user="testuser",
            password="testpass",
            database="testdb",
        )

    @pytest.mark.asyncio
    async def test_service_exposes_entity_types(self):
        """VesperService exposes entity_types for use with add_episode()."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService
        from vesper.entity_types import VESPER_ENTITY_TYPES

        service = VesperService(VesperConfig())
        with patch("vesper.service.Neo4jDriver"), patch("vesper.service.Graphiti"):
            await service.start()
        assert service.entity_types is VESPER_ENTITY_TYPES
