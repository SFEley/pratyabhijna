"""Tests for VesperService — Graphiti client initialization with Kuzu.

Verifies that the service correctly initializes a Graphiti client using the
Kuzu embedded graph database backend.
"""

import pytest
from graphiti_core.driver.kuzu_driver import KuzuDriver


class TestServiceLifecycle:
    """VesperService lifecycle: start, connect, stop."""

    def test_service_not_connected_before_start(self, tmp_data_dir):
        """is_connected is False before start() is called."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService

        config = VesperConfig(db_path=str(tmp_data_dir / "test.kuzu"))
        service = VesperService(config)
        assert not service.is_connected

    @pytest.mark.asyncio
    async def test_service_connected_after_start(self, tmp_data_dir):
        """is_connected is True after start() completes."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService

        config = VesperConfig(db_path=str(tmp_data_dir / "test.kuzu"))
        service = VesperService(config)
        await service.start()
        assert service.is_connected

    @pytest.mark.asyncio
    async def test_service_not_connected_after_stop(self, tmp_data_dir):
        """is_connected is False after stop() is called."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService

        config = VesperConfig(db_path=str(tmp_data_dir / "test.kuzu"))
        service = VesperService(config)
        await service.start()
        await service.stop()
        assert not service.is_connected


class TestGraphitiInitialization:
    """VesperService initializes Graphiti with the correct driver."""

    @pytest.mark.asyncio
    async def test_graphiti_uses_kuzu_driver(self, tmp_data_dir):
        """After start(), the Graphiti client uses a KuzuDriver."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService

        config = VesperConfig(db_path=str(tmp_data_dir / "test.kuzu"))
        service = VesperService(config)
        await service.start()
        assert isinstance(service.graphiti.driver, KuzuDriver)

    @pytest.mark.asyncio
    async def test_kuzu_db_created_at_configured_path(self, tmp_data_dir):
        """Kuzu creates database files at the path from config."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService

        db_path = str(tmp_data_dir / "test.kuzu")
        config = VesperConfig(db_path=db_path)
        service = VesperService(config)
        await service.start()
        # Kuzu creates a directory (not a single file) at the given path
        assert tmp_data_dir.joinpath("test.kuzu").exists()

    @pytest.mark.asyncio
    async def test_service_exposes_entity_types(self, tmp_data_dir):
        """VesperService exposes entity_types for use with add_episode()."""
        from vesper.config import VesperConfig
        from vesper.service import VesperService
        from vesper.entity_types import VESPER_ENTITY_TYPES

        config = VesperConfig(db_path=str(tmp_data_dir / "test.kuzu"))
        service = VesperService(config)
        await service.start()
        assert service.entity_types is VESPER_ENTITY_TYPES
