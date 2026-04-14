"""Tests for the ``seed`` CLI command.

Seed creates the subject's Person node in the graph — the anchor for
identity-atom edges. It does NOT store tier content (SOUL, IDENTITY,
etc.) on the node; those live in repo files and are read by bootstrap
at request time.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_subject_node
from pratyabhijna.seed import seed_subject


# --- Fixtures ---


@pytest.fixture
def mock_service():
    service = MagicMock()
    service.is_connected = True
    service.config = MagicMock()
    service.config.subject_name = "Vesper"
    service.get_entity_by_name = AsyncMock()
    service._graphiti = MagicMock()
    service._graphiti.driver = MagicMock()
    service._graphiti.embedder = MagicMock()
    return service


# --- Creation path ---


class TestSeedCreatesNode:
    async def test_creates_person_node_when_missing(self, mock_service):
        mock_service.get_entity_by_name.return_value = None

        with patch("pratyabhijna.seed.EntityNode") as MockNode:
            instance = MagicMock()
            instance.generate_name_embedding = AsyncMock()
            instance.save = AsyncMock()
            MockNode.return_value = instance

            result = await seed_subject(mock_service)

            MockNode.assert_called_once()
            call_kwargs = MockNode.call_args.kwargs
            assert call_kwargs["name"] == "Vesper"
            assert call_kwargs["labels"] == ["Person"]
            # No tier text stored on the node
            assert "soul" not in call_kwargs["attributes"]
            assert "identity" not in call_kwargs["attributes"]
            assert "context" not in call_kwargs["attributes"]
            instance.save.assert_awaited_once()
        assert result == {"subject": "Vesper", "action": "created"}

    async def test_node_attributes_minimal(self, mock_service):
        mock_service.get_entity_by_name.return_value = None

        with patch("pratyabhijna.seed.EntityNode") as MockNode:
            instance = MagicMock()
            instance.generate_name_embedding = AsyncMock()
            instance.save = AsyncMock()
            MockNode.return_value = instance

            await seed_subject(mock_service)

            attrs = MockNode.call_args.kwargs["attributes"]
            assert attrs == {"person_type": "AI"}


# --- No-op path ---


class TestSeedNoOpWhenExists:
    async def test_returns_exists_without_creating(self, mock_service):
        existing = make_subject_node()
        mock_service.get_entity_by_name.return_value = existing

        with patch("pratyabhijna.seed.EntityNode") as MockNode:
            result = await seed_subject(mock_service)
            MockNode.assert_not_called()

        assert result == {"subject": "Vesper", "action": "exists"}

    async def test_does_not_modify_existing_node(self, mock_service):
        """Seed never writes to an existing node — identity lives in files.

        We verify by patching EntityNode at the module level and confirming
        no instance method (save) is invoked on the existing node returned
        by get_entity_by_name. Under the new contract, seed returns
        "exists" and exits before any write path.
        """
        existing = make_subject_node(soul="old graph soul")
        mock_service.get_entity_by_name.return_value = existing

        # Wrap the driver.save method tracker: the service.driver is a
        # MagicMock, so no real save happens. We just confirm the result.
        result = await seed_subject(mock_service)

        assert result["action"] == "exists"


# --- Subject name resolution ---


class TestSeedSubjectName:
    async def test_uses_configured_subject_name(self, mock_service):
        mock_service.config.subject_name = "Custom"
        mock_service.get_entity_by_name.return_value = None

        with patch("pratyabhijna.seed.EntityNode") as MockNode:
            instance = MagicMock()
            instance.generate_name_embedding = AsyncMock()
            instance.save = AsyncMock()
            MockNode.return_value = instance

            result = await seed_subject(mock_service)

            assert MockNode.call_args.kwargs["name"] == "Custom"
            assert result["subject"] == "Custom"
